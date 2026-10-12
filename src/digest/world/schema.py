"""World file shape: channels, personas, entities, dated events and their claims."""

from __future__ import annotations

import datetime as dt
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from digest.models import (
    Attribute,
    Authority,
    Frozen,
    Glossary,
    GlossaryEntity,
    GlossaryPerson,
    Role,
)
from digest.project import ProjectConfig, require_unique

EntityType = Literal[
    "part",
    "assembly",
    "pcba",
    "harness",
    "molded",
    "battery",
    "purchased",
    "component",
    "subsystem",
    "document",
    "eco",
    "ecr",
    "deviation",
    "fa",
    "po",
    "gate",
    "build",
    "test",
    "release",
    "vendor",
]


class Entity(Frozen):
    id: str
    type: EntityType
    description: str
    owner: Role | None = None
    vendor: str | None = None
    subsystem: str | None = None  # id of an entity of type "subsystem"
    parent: str | None = None  # id of the containing entity (BOM or document set)
    aliases: tuple[str, ...] = ()  # names people use in chat; the extractor's glossary
    state: dict[Attribute, str] = Field(default_factory=dict)  # initial values, e.g. {"rev": "B"}


class Persona(Frozen):
    user_id: str  # Slack-style, e.g. "U0ME01" or "B0PLM01" for a bot
    name: str
    kind: Literal["human", "bot"] = "human"
    role: Role | None = None  # required for a human, None for a bot
    team: str | None = None
    manager: str | None = None  # persona user_id
    channels: tuple[str, ...]
    owned_entities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    authority: Authority = "owner_stated"  # "plm_bot" for a system of record


class Channel(Frozen):
    id: str
    name: str
    purpose: str
    private: bool = False  # visible to members only; the M3 visibility filter reads it


class WorldClaim(Frozen):
    entity: str
    attribute: Attribute
    value: str
    authority: Authority | None = None  # None means the poster's authority
    supersedes: str | None = None  # event id whose claim on the same key this replaces
    contradicts: tuple[str, ...] = ()  # event ids whose claims on the same key this disputes


class TaskTouch(Frozen):
    task: str  # ChecklistTask.id
    effect: Literal["evidence", "done"]


class Ask(Frozen):
    of: str  # persona user_id the question is addressed to
    answered_day: int | None  # working day of the reply, None when never answered


class WorldEvent(Frozen):
    id: str
    day: int  # 1-based working-day index from start_date
    channel: str
    poster: str  # persona user_id
    kind: Literal[
        "decision",
        "blocker",
        "risk",
        "schedule_change",
        "spec_change",
        "bom_change",
        "ask",
        "fyi",
        "distractor",
    ]
    summary: str  # ground truth, one sentence
    scenario: str | None = None  # "S1".."S15", "N1".."N5"
    claims: tuple[WorldClaim, ...] = ()
    tasks: tuple[TaskTouch, ...] = ()
    affected_roles: tuple[Role, ...] = ()
    must_see_for: tuple[str, ...] = ()  # persona user_ids
    known_by: dict[str, int] = Field(default_factory=dict)  # persona user_id -> working day
    noise: dict[str, bool] = Field(default_factory=dict)  # renderer knobs
    ask: Ask | None = None  # present exactly when kind == "ask"


class World(Frozen):
    name: str
    program: str
    description: str = ""  # one line about the product, for the renderer prompt
    workspace_url: str
    timezone: str  # IANA name
    start_date: dt.date  # a Monday
    days: int  # working days
    project: ProjectConfig
    channels: tuple[Channel, ...]
    personas: tuple[Persona, ...]
    entities: tuple[Entity, ...]
    events: tuple[WorldEvent, ...]

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {value}") from exc
        return value

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        require_unique("channel", [channel.id for channel in self.channels])
        require_unique("event", [event.id for event in self.events])
        require_unique("entity", [entity.id for entity in self.entities])
        require_unique("persona", [persona.user_id for persona in self.personas])
        self._check_aliases()
        self._check_entities()
        self._check_personas()
        self._check_events()
        self._check_asks()
        self._check_calendar()
        return self

    def _check_aliases(self) -> None:
        holders: dict[str, list[str]] = {}
        for item_id, aliases in [(e.id, e.aliases) for e in self.entities] + [
            (p.user_id, p.aliases) for p in self.personas
        ]:
            for alias in aliases:
                holders.setdefault(alias, []).append(item_id)
        for alias, ids in holders.items():
            if len(ids) > 1:
                raise ValueError(f"alias {alias!r} is used by {', '.join(ids)}")

    def _check_entities(self) -> None:
        types = {entity.id: entity.type for entity in self.entities}
        for entity in self.entities:
            if entity.subsystem is not None and types.get(entity.subsystem) != "subsystem":
                raise ValueError(
                    f"entity {entity.id}: subsystem {entity.subsystem} is not a subsystem entity"
                )
            if entity.parent is not None and entity.parent not in types:
                raise ValueError(f"entity {entity.id}: unknown parent {entity.parent}")

    def _check_personas(self) -> None:
        channels = {channel.id for channel in self.channels}
        entities = {entity.id for entity in self.entities}
        personas = {persona.user_id for persona in self.personas}
        for persona in self.personas:
            pid = persona.user_id
            if (persona.kind == "human") != (persona.role is not None):
                raise ValueError(f"persona {pid}: a human needs a role and a bot has none")
            for channel in persona.channels:
                if channel not in channels:
                    raise ValueError(f"persona {pid}: unknown channel {channel}")
            for entity in persona.owned_entities:
                if entity not in entities:
                    raise ValueError(f"persona {pid}: unknown owned entity {entity}")
            if persona.manager is not None and (
                persona.manager == pid or persona.manager not in personas
            ):
                raise ValueError(f"persona {pid}: manager {persona.manager} is not another persona")

    def _check_events(self) -> None:
        channels = {channel.id for channel in self.channels}
        entities = {entity.id for entity in self.entities}
        personas = {persona.user_id: persona for persona in self.personas}
        humans = {pid for pid, persona in personas.items() if persona.kind == "human"}
        tasks = {task.id for gate in self.project.gates for task in gate.checklist}
        events = {event.id: event for event in self.events}
        for event in self.events:
            eid = event.id
            if event.channel not in channels:
                raise ValueError(f"event {eid}: unknown channel {event.channel}")
            poster = personas.get(event.poster)
            if poster is None:
                raise ValueError(f"event {eid}: unknown poster {event.poster}")
            if event.channel not in poster.channels:
                raise ValueError(f"event {eid}: poster {poster.user_id} is not in {event.channel}")
            for day in [event.day, *event.known_by.values()]:
                if not 1 <= day <= self.days:
                    raise ValueError(f"event {eid}: day {day} is outside 1..{self.days}")
            for pid in [*event.must_see_for, *event.known_by]:
                if pid not in humans:
                    raise ValueError(f"event {eid}: {pid} is not a human persona")
            for touch in event.tasks:
                if touch.task not in tasks:
                    raise ValueError(f"event {eid}: unknown checklist task {touch.task}")
            for claim in event.claims:
                if claim.entity not in entities:
                    raise ValueError(f"event {eid}: unknown claim entity {claim.entity}")
                links = [claim.supersedes] if claim.supersedes is not None else []
                for target_id in [*links, *claim.contradicts]:
                    target = events.get(target_id)
                    if (
                        target is None
                        or target is event
                        or target.day > event.day
                        or not any(
                            (c.entity, c.attribute) == (claim.entity, claim.attribute)
                            for c in target.claims
                        )
                    ):
                        raise ValueError(
                            f"event {eid}: {target_id} is not another earlier or same-day event "
                            f"with a claim on {claim.entity} {claim.attribute}"
                        )

    def _check_asks(self) -> None:
        personas = {persona.user_id: persona for persona in self.personas}
        for event in self.events:
            eid, ask = event.id, event.ask
            if (event.kind == "ask") != (ask is not None):
                raise ValueError(f"event {eid}: ask is set exactly when kind is ask")
            if ask is None:
                continue
            asked = personas.get(ask.of)
            if asked is None or asked.kind != "human":
                raise ValueError(f"event {eid}: ask.of {ask.of} is not a human persona")
            if event.channel not in asked.channels:
                raise ValueError(f"event {eid}: ask.of {ask.of} is not in {event.channel}")
            if ask.of == event.poster:
                raise ValueError(f"event {eid}: ask.of {ask.of} is the poster")
            if ask.answered_day is not None and not event.day <= ask.answered_day <= self.days:
                raise ValueError(
                    f"event {eid}: ask.answered_day {ask.answered_day} is outside "
                    f"{event.day}..{self.days}"
                )

    def _check_calendar(self) -> None:
        if self.start_date.weekday() != 0:
            raise ValueError(f"start_date {self.start_date} is not a Monday")
        first = self.project.phases[0]
        if first.from_date > self.start_date:
            raise ValueError(f"phase {first.phase} starts after start_date {self.start_date}")
        working_days = {self.date_of(day) for day in range(1, self.days + 1)}
        for gate in self.project.gates:
            if gate.date not in working_days:
                raise ValueError(f"gate {gate.id}: {gate.date} is not a working day of the world")

    def date_of(self, day: int) -> dt.date:
        weeks, weekday = divmod(day - 1, 5)
        return self.start_date + dt.timedelta(weeks=weeks, days=weekday)

    def glossary(self) -> Glossary:
        """Every entity and persona as the extractor's glossary (plan B0.3)."""
        return Glossary(
            entities=tuple(
                GlossaryEntity(id=e.id, type=e.type, description=e.description, aliases=e.aliases)
                for e in self.entities
            ),
            people=tuple(
                GlossaryPerson(
                    user_id=p.user_id,
                    name=p.name,
                    role=p.role,
                    aliases=p.aliases,
                    authority=p.authority,
                )
                for p in self.personas
            ),
        )

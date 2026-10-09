"""World file shape: channels, personas, entities, dated events and their claims."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from digest.models import Attribute, Authority, Frozen, Role
from digest.project import ProjectConfig

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


class World(Frozen):
    name: str
    program: str
    workspace_url: str
    timezone: str  # IANA name
    start_date: dt.date  # a Monday
    days: int  # working days
    project: ProjectConfig
    channels: tuple[Channel, ...]
    personas: tuple[Persona, ...]
    entities: tuple[Entity, ...]
    events: tuple[WorldEvent, ...]

    def date_of(self, day: int) -> dt.date:
        raise NotImplementedError

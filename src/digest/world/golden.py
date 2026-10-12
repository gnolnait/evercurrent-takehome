"""Golden labels derived from the world script and the render manifest (SPEC section 6)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Literal, cast

from digest.models import Attribute, Authority, Frozen, ItemType, Role
from digest.priors import Priors
from digest.world.manifest import Manifest
from digest.world.schema import TaskTouch, World, WorldEvent


class GoldenClaim(Frozen):
    entity: str
    attribute: Attribute
    value: str
    authority: Authority
    supersedes: str | None  # event id
    contradicts: tuple[str, ...]


class GoldenItem(Frozen):
    event_id: str
    scenario: str | None
    day: int
    date: dt.date
    channel_id: str
    thread_ts: str
    type: ItemType  # the event kind; distractors are not items
    summary: str
    entities: tuple[str, ...]  # claim entities, deduplicated
    claims: tuple[GoldenClaim, ...]
    tasks: tuple[TaskTouch, ...]
    owner: str | None  # ask: the persona asked
    affected_roles: tuple[Role, ...]
    must_see_for: tuple[str, ...]
    known_by: dict[str, int]


class GoldenAbsence(Frozen):
    day: int
    date: dt.date
    source: Literal["checklist", "ask"]
    gate: str | None
    task: str | None
    cites: str  # event id of the last evidence or the ask
    audience: tuple[str, ...]  # roles for a checklist absence; user ids for an ask
    days_silent: int


class GoldenDistractor(Frozen):
    event_id: str
    day: int
    channel_id: str
    thread_ts: str


class Golden(Frozen):
    ask_reply_cadence_days: int  # the prior the absences were derived with
    items: tuple[GoldenItem, ...]
    absences: tuple[GoldenAbsence, ...]
    distractors: tuple[GoldenDistractor, ...]


def derive_golden(world: World, manifest: Manifest, priors: Priors) -> Golden:
    """The labels of the script, in the order `write_golden` writes them (plan B2.5, B2.6)."""
    items = [
        _golden_item(world, manifest, event) for event in world.events if event.kind != "distractor"
    ]
    distractors = [
        GoldenDistractor(
            event_id=event.id,
            day=event.day,
            channel_id=manifest.threads[event.id].channel_id,
            thread_ts=manifest.threads[event.id].thread_ts,
        )
        for event in world.events
        if event.kind == "distractor" and event.id in manifest.threads
    ]
    cadence = priors.absence.ask_reply_cadence_days
    absences = [*_checklist_absences(world), *_ask_absences(world, cadence)]
    items.sort(key=lambda item: (item.day, item.event_id))
    absences.sort(key=lambda row: (row.day, row.source, row.gate or "", row.task or "", row.cites))
    distractors.sort(key=lambda row: (row.day, row.event_id))
    return Golden(
        ask_reply_cadence_days=cadence,
        items=tuple(items),
        absences=tuple(absences),
        distractors=tuple(distractors),
    )


def _golden_item(world: World, manifest: Manifest, event: WorldEvent) -> GoldenItem:
    thread = manifest.threads[event.id]  # the KeyError names the event the manifest lacks
    poster = next(persona for persona in world.personas if persona.user_id == event.poster)
    return GoldenItem(
        event_id=event.id,
        scenario=event.scenario,
        day=event.day,
        date=world.date_of(event.day),
        channel_id=thread.channel_id,
        thread_ts=thread.thread_ts,
        type=cast(ItemType, event.kind),  # the caller passes no distractor
        summary=event.summary,
        entities=tuple(dict.fromkeys(claim.entity for claim in event.claims)),
        claims=tuple(
            GoldenClaim(
                entity=claim.entity,
                attribute=claim.attribute,
                value=claim.value,
                authority=claim.authority or poster.authority,
                supersedes=claim.supersedes,
                contradicts=claim.contradicts,
            )
            for claim in event.claims
        ),
        tasks=event.tasks,
        owner=event.ask.of if event.ask is not None else None,
        affected_roles=event.affected_roles,
        must_see_for=event.must_see_for,
        known_by=event.known_by,
    )


def _checklist_absences(world: World) -> list[GoldenAbsence]:
    """B2.2 on the script, day by day: the latest touch, else the first claim on the gate."""
    gate_days = {world.date_of(day): day for day in range(1, world.days + 1)}
    # The renderer writes a day's events in file order, so file order stands for ts order.
    order = {event.id: position for position, event in enumerate(world.events)}
    absences: list[GoldenAbsence] = []
    for gate in world.project.gates:
        gate_day = gate_days[gate.date]
        anchors = [e for e in world.events if any(c.entity == gate.id for c in e.claims)]
        for task in gate.checklist:
            touches = [
                (e, touch) for e in world.events for touch in e.tasks if touch.task == task.id
            ]
            for day in range(max(1, gate_day - gate.buffer_days), gate_day + 1):
                known = [(e, touch) for e, touch in touches if e.day <= day]
                if any(touch.effect == "done" for _, touch in known):
                    continue
                cited = max((e for e, _ in known), key=lambda e: (e.day, order[e.id]), default=None)
                if cited is None:  # no touch yet: the first claim on the gate anchors the task
                    cited = min(
                        (e for e in anchors if e.day <= day),
                        key=lambda e: (e.day, order[e.id]),
                        default=None,
                    )
                if cited is None or day - cited.day <= task.expected_cadence_days:
                    continue
                absences.append(
                    GoldenAbsence(
                        day=day,
                        date=world.date_of(day),
                        source="checklist",
                        gate=gate.id,
                        task=task.id,
                        cites=cited.id,
                        audience=(task.owner,),
                        days_silent=day - cited.day,
                    )
                )
    return absences


def _ask_absences(world: World, cadence_days: int) -> list[GoldenAbsence]:
    """B2.3 on the script: an ask with no reply by day `d` and older than the cadence."""
    absences: list[GoldenAbsence] = []
    for event in world.events:
        ask = event.ask
        if ask is None:
            continue
        for day in range(event.day, world.days + 1):
            if ask.answered_day is not None and ask.answered_day <= day:
                continue
            if day - event.day <= cadence_days:
                continue
            absences.append(
                GoldenAbsence(
                    day=day,
                    date=world.date_of(day),
                    source="ask",
                    gate=None,
                    task=None,
                    cites=event.id,
                    audience=(event.poster, ask.of),
                    days_silent=day - event.day,
                )
            )
    return absences


def write_golden(golden: Golden, path: Path) -> None:
    """One JSON object per line: the meta line, then items, absences and distractors."""
    rows = [
        {"kind": "meta", "ask_reply_cadence_days": golden.ask_reply_cadence_days},
        *({"kind": "item", **row.model_dump(mode="json")} for row in golden.items),
        *({"kind": "absence", **row.model_dump(mode="json")} for row in golden.absences),
        *({"kind": "distractor", **row.model_dump(mode="json")} for row in golden.distractors),
    ]
    text = "".join(json.dumps(row) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_golden(path: Path) -> Golden:
    """Read a file `write_golden` wrote; a malformed line raises naming the file and the line."""
    meta: dict[str, int] = {}
    items: list[GoldenItem] = []
    absences: list[GoldenAbsence] = []
    distractors: list[GoldenDistractor] = []
    with path.open(encoding="utf-8") as lines:
        for number, line in enumerate(lines, start=1):
            try:
                row = json.loads(line)
                kind = row.pop("kind")
                if kind == "meta":
                    meta = row
                elif kind == "item":
                    items.append(GoldenItem.model_validate(row))
                elif kind == "absence":
                    absences.append(GoldenAbsence.model_validate(row))
                elif kind == "distractor":
                    distractors.append(GoldenDistractor.model_validate(row))
                else:
                    raise ValueError(f"unknown kind {kind!r}")
            except (ValueError, KeyError) as error:  # JSONDecodeError and ValidationError too
                raise ValueError(f"{path}:{number}: {error}") from error
    return Golden(
        ask_reply_cadence_days=meta["ask_reply_cadence_days"],
        items=tuple(items),
        absences=tuple(absences),
        distractors=tuple(distractors),
    )

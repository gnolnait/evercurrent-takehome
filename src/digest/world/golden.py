"""Golden labels derived from the world script and the render manifest (SPEC section 6)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

from digest.models import Attribute, Authority, Frozen, ItemType, Role
from digest.priors import Priors
from digest.world.manifest import Manifest
from digest.world.schema import TaskTouch, World


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
    raise NotImplementedError


def write_golden(golden: Golden, path: Path) -> None:
    raise NotImplementedError


def read_golden(path: Path) -> Golden:
    raise NotImplementedError

"""The feedback log: done, snooze and undo events from the Slack digest (docs/plans/m5-bot.md).

The log is append-only. No function here writes the items table.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from digest.models import Frozen
from digest.store import Store

EventType = Literal["done", "snooze", "undo"]
SubjectKind = Literal["item", "checklist_absence", "ask_absence"]


class Subject(Frozen):
    """What a digest line is about."""

    key: str
    kind: SubjectKind
    owner: str | None


class FeedbackEvent(Frozen):
    """One row of the feedback log."""

    id: int
    event: EventType
    item_id: str
    reader: str
    slack_user: str
    run_id: str
    day: date
    payload: dict[str, Any]
    ts: datetime
    idempotency_key: str


@dataclass(frozen=True)
class FeedbackState:
    """The feedback in effect for one reader on one day."""

    resolved: frozenset[str]
    snoozed: Mapping[str, date]
    suppressed: frozenset[tuple[str, str, str]]


class UndoMismatch(ValueError):
    """The undo target is an event of another reader, day or item."""


def record(
    store: Store,
    event: Literal["done", "snooze"],
    subject: Subject,
    *,
    reader: str,
    slack_user: str,
    run_id: str,
    day: date,
    idempotency_key: str,
    at: datetime,
) -> int | None:
    """Write one done or snooze event. Return its id, or None for a key already seen."""
    raise NotImplementedError


def undo(
    store: Store,
    event_id: int,
    *,
    reader: str,
    day: date,
    item_id: str,
    slack_user: str,
    idempotency_key: str,
    at: datetime,
) -> int | None:
    """Write an undo of a live event. Return its id, or None for a key already seen."""
    raise NotImplementedError


def active(store: Store, reader: str, day: date) -> FeedbackState:
    """Return the feedback in effect for the reader on the day."""
    raise NotImplementedError


def shown_marks(store: Store, reader: str, day: date) -> dict[str, FeedbackEvent]:
    """Return the newest live event per item for the reader and day."""
    raise NotImplementedError

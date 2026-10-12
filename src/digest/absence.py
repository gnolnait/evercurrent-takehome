"""The absence detector: checklist tasks inside a gate buffer and unanswered asks (SPEC 5.5)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from digest.models import Event, Item
from digest.priors import Priors
from digest.project import ProjectConfig
from digest.store import Store


def checklist_absences(
    project: ProjectConfig, items: Sequence[Item], as_of: date, zone: ZoneInfo
) -> list[Item]:
    raise NotImplementedError


def ask_absences(
    items: Sequence[Item],
    thread_events: Callable[[Item], Sequence[Event]],
    as_of: date,
    cadence_days: int,
    zone: ZoneInfo,
) -> list[Item]:
    raise NotImplementedError


def detect_absences(
    store: Store, project: ProjectConfig, priors: Priors, as_of: datetime, zone: ZoneInfo
) -> list[Item]:
    raise NotImplementedError

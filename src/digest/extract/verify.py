"""Verification in code: quotes, ids and links the model returned (plan B1.5, D5)."""

from __future__ import annotations

from collections.abc import Mapping

from digest.extract.schema import Extraction
from digest.models import Event, Frozen, Glossary, Item
from digest.project import ChecklistTask
from digest.store import ThreadDelta

DROP_FIELDS = (
    "evidence.unknown_message",
    "evidence.quote",
    "item_id.unknown",
    "entities.unresolved",
    "claims.entity",
    "claims.message",
    "owner.unresolved",
    "due.format",
    "tasks.unknown",
    "tasks.message",
    "evidence.stale",
    "claims.stale",
    "tasks.stale",
)


class Verified(Frozen):
    items: tuple[Item, ...]
    drops: dict[str, int]  # every DROP_FIELDS key present, zero when nothing dropped


def verify(
    extraction: Extraction,
    delta: ThreadDelta,
    citable: Mapping[str, Event],
    glossary: Glossary,
    tasks: Mapping[str, ChecklistTask],
    existing: Mapping[str, Item],
) -> Verified:
    raise NotImplementedError

"""Replay ingest: the JSONL corpus into the store, bounded by a day (SPEC section 6)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from digest.models import Event, Frozen
from digest.store import Store


class IngestReport(Frozen):
    read: int
    written: int
    after_until: int


class ReplayFormatError(ValueError):
    """A corpus line that does not parse. The message names the file and the 1-based line."""


def read_events(path: Path, workspace_url: str) -> Iterator[Event]:
    raise NotImplementedError


def ingest(store: Store, path: Path, until: datetime, workspace_url: str) -> IngestReport:
    raise NotImplementedError

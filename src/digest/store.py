"""SQLite store: one version per message, full-text search and stage runs (SPEC section 5)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from digest.models import Event, Frozen


class StageRun(Frozen):
    stage: str
    thread_id: str
    last_ts: str
    prompt_version: str
    model: str
    run_at: datetime
    status: Literal["ok", "failed"]


class ThreadDelta(Frozen):
    thread_id: str
    channel_id: str
    thread_ts: str
    last_ts: str
    since_ts: str | None
    root: Event | None
    changed: tuple[Event, ...]


class Store:
    @classmethod
    def open(cls, path: Path | Literal[":memory:"]) -> Self:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        raise NotImplementedError

    def __exit__(self, *exc: object) -> None:
        raise NotImplementedError

    def set_meta(self, key: str, value: str) -> None:
        raise NotImplementedError

    def get_meta(self, key: str) -> str | None:
        raise NotImplementedError

    def upsert_events(self, events: Iterable[Event]) -> int:
        raise NotImplementedError

    def events_in_thread(self, thread_id: str, as_of: datetime) -> list[Event]:
        raise NotImplementedError

    def search_events(self, query: str, as_of: datetime, limit: int = 50) -> list[Event]:
        raise NotImplementedError

    def record_stage_run(self, run: StageRun) -> None:
        raise NotImplementedError

    def thread_deltas(
        self, stage: str, prompt_version: str, model: str, as_of: datetime
    ) -> list[ThreadDelta]:
        raise NotImplementedError

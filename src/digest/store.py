"""SQLite store: one version per message, full-text search and stage runs (SPEC section 5)."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, Self

from digest.models import Event, Frozen, micros_since_epoch

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
_EVENT_COLUMNS = (
    "source",
    "channel_id",
    "channel_name",
    "thread_ts",
    "ts",
    "event_ts",
    "user_id",
    "text",
    "mentions",
    "reactions",
    "files",
    "permalink",
    "edited",
    "deleted",
)
_JSON_COLUMNS = ("mentions", "reactions", "files")
_SELECT = f"SELECT {', '.join(f'events.{c}' for c in _EVENT_COLUMNS)} FROM events"
_WRITTEN = (*_EVENT_COLUMNS, "ts_us", "event_ts_us")
# One statement, not INSERT OR REPLACE: only an update fires the FTS update trigger.
_UPSERT = (
    f"INSERT INTO events ({', '.join(_WRITTEN)}) VALUES ({', '.join(f':{c}' for c in _WRITTEN)})"
    " ON CONFLICT (channel_id, ts) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _WRITTEN if c not in {"channel_id", "ts"})
)


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
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    def open(cls, path: Path | Literal[":memory:"]) -> Self:
        if path != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(Path(__file__).with_name("schema.sql").read_text("utf-8"))
        store = cls(connection)
        with connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT (key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
        version = store.get_meta("schema_version")
        if version != SCHEMA_VERSION:
            connection.close()
            raise ValueError(
                f"store {path} has schema version {version}, expected {SCHEMA_VERSION}"
            )
        log.debug("store_opened", extra={"path": str(path)})
        return store

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def upsert_events(self, events: Iterable[Event]) -> int:
        rows = [_event_row(event) for event in events]
        with self._db:
            self._db.executemany(_UPSERT, rows)
        return len(rows)

    def events_in_thread(self, thread_id: str, as_of: datetime) -> list[Event]:
        channel_id, _, thread_ts = thread_id.rpartition(":")
        rows = self._db.execute(
            f"{_SELECT} WHERE channel_id = ? AND thread_ts = ? AND ts_us <= ? ORDER BY ts_us",
            (channel_id, thread_ts, micros_since_epoch(as_of)),
        )
        return [_event(row) for row in rows]

    def search_events(self, query: str, as_of: datetime, limit: int = 50) -> list[Event]:
        terms = query.split()
        if not terms:
            return []
        match = " ".join('"' + term.replace('"', '""') + '"' for term in terms)
        rows = self._db.execute(
            f"{_SELECT} JOIN events_fts ON events_fts.rowid = events.rowid"
            " WHERE events_fts MATCH ? AND events.ts_us <= ?"
            " ORDER BY events.ts_us, events.channel_id LIMIT ?",
            (match, micros_since_epoch(as_of), limit),
        )
        return [_event(row) for row in rows]

    def record_stage_run(self, run: StageRun) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO stage_runs"
                " (stage, thread_id, last_ts, prompt_version, model, run_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (stage, thread_id, last_ts, prompt_version, model)"
                " DO UPDATE SET run_at = excluded.run_at, status = excluded.status",
                (
                    run.stage,
                    run.thread_id,
                    run.last_ts,
                    run.prompt_version,
                    run.model,
                    run.run_at.isoformat(),
                    run.status,
                ),
            )

    def thread_deltas(
        self, stage: str, prompt_version: str, model: str, as_of: datetime
    ) -> list[ThreadDelta]:
        done: dict[str, list[str]] = {}
        for row in self._db.execute(
            "SELECT thread_id, last_ts FROM stage_runs"
            " WHERE stage = ? AND prompt_version = ? AND model = ? AND status = 'ok'",
            (stage, prompt_version, model),
        ):
            done.setdefault(row["thread_id"], []).append(row["last_ts"])
        rows = self._db.execute(
            f"{_SELECT} WHERE ts_us <= ? ORDER BY channel_id, thread_ts, ts_us",
            (micros_since_epoch(as_of),),
        )
        deltas = []
        for thread_id, group in groupby(map(_event, rows), key=lambda event: event.thread_id):
            events = list(group)
            last_ts = max(event.event_ts for event in events)
            runs = done.get(thread_id, [])
            if last_ts in runs:
                continue
            since_ts = max((ts for ts in runs if ts < last_ts), default=None)
            root = next((e for e in events if e.ts == e.thread_ts), None)
            deltas.append(
                ThreadDelta(
                    thread_id=thread_id,
                    channel_id=events[0].channel_id,
                    thread_ts=events[0].thread_ts,
                    last_ts=last_ts,
                    since_ts=since_ts,
                    root=root,
                    changed=tuple(e for e in events if since_ts is None or e.event_ts > since_ts),
                )
            )
        return deltas


def _event_row(event: Event) -> dict[str, Any]:
    row = event.model_dump(mode="json")
    for column in _JSON_COLUMNS:
        row[column] = json.dumps(row[column])
    return row | {"ts_us": event.ts_us, "event_ts_us": micros_since_epoch(event.observed_at)}


def _event(row: sqlite3.Row) -> Event:
    values = dict(row)
    for column in _JSON_COLUMNS:
        values[column] = json.loads(values[column])
    return Event.model_validate(values)

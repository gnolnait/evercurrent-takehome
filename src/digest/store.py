"""SQLite store: one version per message, full-text search and stage runs (SPEC section 5)."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, Self

from digest.models import Event, Frozen, Item, TriageDecision, micros_since_epoch, ts_micros

log = logging.getLogger(__name__)

SCHEMA_VERSION = "2"
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
_SET_META = (
    "INSERT INTO meta (key, value) VALUES (?, ?)"
    " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
)
_ITEM_FIELDS = tuple(field for field in Item.model_fields if field != "absence")
_ITEM_JSON_COLUMNS = ("entities", "affected_roles", "evidence", "claims", "tasks")
_ITEM_WRITTEN = (*_ITEM_FIELDS, "first_seen_us", "last_updated_us", "prompt_version", "model")
_SELECT_ITEMS = f"SELECT {', '.join(_ITEM_FIELDS)} FROM items"
_UPSERT_ITEM = (
    f"INSERT INTO items ({', '.join(_ITEM_WRITTEN)})"
    f" VALUES ({', '.join(f':{c}' for c in _ITEM_WRITTEN)})"
    " ON CONFLICT (item_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _ITEM_WRITTEN if c != "item_id")
)
_TRIAGE_FIELDS = tuple(TriageDecision.model_fields)
_TRIAGE_KEY = ("thread_id", "last_ts", "prompt_version", "model")
_SELECT_TRIAGE = f"SELECT {', '.join(_TRIAGE_FIELDS)} FROM triage"
_UPSERT_TRIAGE = (
    f"INSERT INTO triage ({', '.join(_TRIAGE_FIELDS)})"
    f" VALUES ({', '.join(f':{c}' for c in _TRIAGE_FIELDS)})"
    f" ON CONFLICT ({', '.join(_TRIAGE_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _TRIAGE_FIELDS if c not in _TRIAGE_KEY)
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
    def __init__(self, connection: sqlite3.Connection, path: Path | Literal[":memory:"]) -> None:
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: Path | Literal[":memory:"]) -> Self:
        """Open or create a store. Refuse a file of another schema version before any write."""
        if path != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.row_factory = sqlite3.Row
            _check_version(connection, path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(Path(__file__).with_name("schema.sql").read_text("utf-8"))
            with connection:
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
                    " ON CONFLICT (key) DO NOTHING",
                    (SCHEMA_VERSION,),
                )
            log.debug("store_opened", extra={"path": str(path)})
            return cls(connection, path)
        except BaseException:  # close, so a cleanup can delete the file on Windows
            connection.close()
            raise

    @classmethod
    def open_readonly(cls, path: Path) -> Self:
        """Open an existing store read-only. Never write the file or its write-ahead log.

        The connection uses `mode=ro`. The function runs no schema script and no write.
        Raise ValueError for a file that is not a store of this schema version, an empty
        file included, and sqlite3.DatabaseError for a file that is not SQLite.
        """
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            _check_version(connection, path, allow_empty=False)
        except Exception:
            connection.close()
            raise
        log.debug("store_opened_readonly", extra={"path": str(path)})
        return cls(connection, path)

    def copy_to(self, path: Path) -> Store:
        """Copy this store to a new file at `path` and open the copy.

        The copy uses the SQLite online backup API from this store's own connection, so
        rows still in the write-ahead log are copied too. A backup only reads its source,
        so this store is not changed. Raise FileExistsError when `path` exists, and
        ValueError for an in-memory store. When the backup or the open of the copy fails,
        remove the file at `path` (with its `-wal` and `-shm` files) and raise the error.
        """
        if self.path == ":memory:":
            raise ValueError("cannot copy an in-memory store")
        if path.exists():
            raise FileExistsError(f"cannot copy store {self.path} to {path}: {path} exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            target = sqlite3.connect(path)
            try:
                self._db.backup(target)
            finally:
                target.close()
            copy = type(self).open(path)
        except BaseException:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            raise
        log.info("store_copied", extra={"source": str(self.path), "path": str(path)})
        return copy

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(_SET_META, (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def upsert_events(
        self, events: Iterable[Event], *, meta: Mapping[str, str] | None = None
    ) -> int:
        """Write the events and the `meta` pairs in one transaction; return the events written."""
        rows = [_event_row(event) for event in events]
        with self._db:
            self._db.executemany(_UPSERT, rows)
            self._db.executemany(_SET_META, (meta or {}).items())
        return len(rows)

    def latest_event_ts(self) -> str | None:
        """The `event_ts` of the latest event version in the store; None for no event."""
        row = self._db.execute(
            "SELECT event_ts FROM events ORDER BY event_ts_us DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row["event_ts"]

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
            " ORDER BY events.ts_us DESC, events.channel_id LIMIT ?",
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

    def upsert_items(self, items: Iterable[Item], *, prompt_version: str, model: str) -> int:
        """Write the items with their provenance; return the count. Absence items are refused."""
        items = list(items)
        absent = next((item.item_id for item in items if item.type == "absence"), None)
        if absent is not None:
            raise ValueError(f"item {absent} is an absence; absence items are not stored")
        provenance = {"prompt_version": prompt_version, "model": model}
        rows = [_item_row(item) | provenance for item in items]
        with self._db:
            self._db.executemany(_UPSERT_ITEM, rows)
        return len(rows)

    def items_for_thread(self, thread_id: str) -> list[Item]:
        rows = self._db.execute(
            f"{_SELECT_ITEMS} WHERE thread_id = ? ORDER BY first_seen_us, item_id", (thread_id,)
        )
        return [_item(row) for row in rows]

    def items(self, as_of: datetime) -> list[Item]:
        """Items whose `last_updated` is at or before `as_of`, in `first_seen` order."""
        rows = self._db.execute(
            f"{_SELECT_ITEMS} WHERE last_updated_us <= ? ORDER BY first_seen_us, item_id",
            (micros_since_epoch(as_of),),
        )
        return [_item(row) for row in rows]

    def record_triage(self, decision: TriageDecision) -> None:
        row = decision.model_dump() | {"decided_at": decision.decided_at.isoformat()}
        with self._db:
            self._db.execute(_UPSERT_TRIAGE, row)

    def triage_decision(
        self, thread_id: str, last_ts: str, prompt_version: str, model: str
    ) -> TriageDecision | None:
        row = self._db.execute(
            f"{_SELECT_TRIAGE} WHERE {' AND '.join(f'{c} = ?' for c in _TRIAGE_KEY)}",
            (thread_id, last_ts, prompt_version, model),
        ).fetchone()
        return None if row is None else TriageDecision.model_validate(dict(row))

    def triage_for_thread(self, thread_id: str) -> list[TriageDecision]:
        """Every revision's decision, in `last_ts` order."""
        rows = self._db.execute(
            f"{_SELECT_TRIAGE} WHERE thread_id = ? ORDER BY last_ts, prompt_version, model",
            (thread_id,),
        )
        return [TriageDecision.model_validate(dict(row)) for row in rows]


def _check_version(
    connection: sqlite3.Connection, path: Path | str, *, allow_empty: bool = True
) -> None:
    """Accept a store of this schema version, and an empty database when `allow_empty`.

    Raise ValueError for anything else.
    """
    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}
    if not tables:
        if allow_empty:
            return
        raise ValueError(f"store {path} is empty: it has no schema, so it is not a store")
    row = None
    if "meta" in tables:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    version = None if row is None else row["value"]
    if version != SCHEMA_VERSION:
        raise ValueError(f"store {path} has schema version {version}, expected {SCHEMA_VERSION}")


def _event_row(event: Event) -> dict[str, Any]:
    row = event.model_dump(mode="json")
    for column in _JSON_COLUMNS:
        row[column] = json.dumps(row[column])
    return row | {"ts_us": event.ts_us, "event_ts_us": ts_micros(event.event_ts)}


def _event(row: sqlite3.Row) -> Event:
    values = dict(row)
    for column in _JSON_COLUMNS:
        values[column] = json.loads(values[column])
    return Event.model_validate(values)


def _item_row(item: Item) -> dict[str, Any]:
    row = item.model_dump(mode="json", exclude={"absence"})
    for column in _ITEM_JSON_COLUMNS:
        row[column] = json.dumps(row[column])
    return row | {
        "first_seen_us": ts_micros(item.first_seen),
        "last_updated_us": ts_micros(item.last_updated),
    }


def _item(row: sqlite3.Row) -> Item:
    values = dict(row)
    for column in _ITEM_JSON_COLUMNS:
        values[column] = json.loads(values[column])
    return Item.model_validate(values)

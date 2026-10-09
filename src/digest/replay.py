"""Replay ingest: the JSONL corpus into the store, bounded by a day (SPEC section 6)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from digest.models import Event, Frozen, micros_since_epoch, permalink_for, ts_micros
from digest.store import Store
from digest.world.load import load_world_header

log = logging.getLogger(__name__)


class IngestReport(Frozen):
    read: int
    written: int
    after_until: int


class ReplayFormatError(ValueError):
    """A corpus line that does not parse, or whose `event_ts` is lower than the line before.

    The message names the file and the 1-based line.
    """


class ReplayRefused(ValueError):
    """The world or the store does not allow this replay. The message gives the reason."""


def replay_world(store: Store, world_dir: Path, until: date) -> IngestReport:
    """Ingest a world's corpus up to the end of `until` in the world's timezone.

    Replay only moves forward, and a store holds one world. Meta is written after the ingest.
    """
    name = world_dir.name
    world_file, corpus = world_dir / "world.yaml", world_dir / "events.jsonl"
    if not world_file.is_file():
        raise ReplayRefused(f"no such world: {world_file}")
    if not corpus.is_file():
        raise ReplayRefused(f"no corpus for world {name!r}: {corpus}")
    try:
        header = load_world_header(world_file)
        zone = ZoneInfo(header.timezone)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as error:
        # ZoneInfoNotFoundError is a KeyError.
        raise ReplayRefused(f"cannot read {world_file}: {type(error).__name__}: {error}") from error
    end = datetime.combine(until, time.max, zone).astimezone(UTC)
    stored_world = store.get_meta("world")
    if stored_world not in (None, name):
        raise ReplayRefused(f"the store holds world {stored_world!r}, not {name!r}")
    stored_until = store.get_meta("until")
    if stored_until is not None and datetime.fromisoformat(stored_until) > end:
        raise ReplayRefused(
            f"--until {until} ends at {end.isoformat()}, earlier than the store's until"
            f" {stored_until}; replay only moves forward, so delete the store to start over"
        )
    try:
        report = ingest(store, corpus, end, header.workspace_url)
    except OSError as error:
        raise ReplayRefused(f"cannot read {corpus}: {error}") from error
    store.set_meta("world", name)
    store.set_meta("timezone", header.timezone)
    store.set_meta("workspace_url", header.workspace_url)
    store.set_meta("until", end.isoformat())
    return report


def read_events(path: Path, workspace_url: str) -> Iterator[Event]:
    """Yield one event per line. Lines must be sorted by `event_ts`."""
    previous: Event | None = None
    with path.open("rb") as lines:
        for number, line in enumerate(lines, 1):
            try:
                raw = json.loads(line.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("a line must be a JSON object")
                event = Event.model_validate(_defaults(raw, workspace_url) | raw)
            except ValueError as error:  # Unicode, JSON and validation errors are ValueErrors.
                raise ReplayFormatError(f"{path}:{number}: {error}") from error
            if previous is not None and event.event_ts < previous.event_ts:
                raise ReplayFormatError(
                    f"{path}:{number}: event_ts {event.event_ts} is before the previous line's"
                    f" {previous.event_ts}"
                )
            previous = event
            yield event


def _defaults(raw: dict[str, Any], workspace_url: str) -> dict[str, Any]:
    defaults: dict[str, Any] = {"source": "replay"}
    ts = raw.get("ts")
    # permalink_for needs a string ts; validation reports a ts of any other type.
    if isinstance(ts, str):
        defaults |= {"thread_ts": ts, "event_ts": ts}
        if "channel_id" in raw:
            thread_ts = raw.get("thread_ts", ts)
            defaults["permalink"] = permalink_for(workspace_url, raw["channel_id"], ts, thread_ts)
    return defaults


def ingest(store: Store, path: Path, until: datetime, workspace_url: str) -> IngestReport:
    """Write every line observed by `until`; drop reactions made after it.

    The whole file is read before the first write, so a bad line writes nothing.
    """
    limit = micros_since_epoch(until)
    read, kept = 0, []
    for event in read_events(path, workspace_url):
        read += 1
        if ts_micros(event.event_ts) <= limit:
            reactions = tuple(r for r in event.reactions if ts_micros(r.ts) <= limit)
            kept.append(event.model_copy(update={"reactions": reactions}))
    written = store.upsert_events(kept)
    log.info(
        "replay_ingested",
        extra={"path": str(path), "read": read, "written": written, "until": until.isoformat()},
    )
    return IngestReport(read=read, written=written, after_until=read - written)

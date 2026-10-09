"""Replay ingest: the JSONL corpus into the store, bounded by a day (SPEC section 6)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from digest.models import Event, Frozen, micros_since_epoch, permalink_for
from digest.store import Store

log = logging.getLogger(__name__)


class IngestReport(Frozen):
    read: int
    written: int
    after_until: int


class ReplayFormatError(ValueError):
    """A corpus line that does not parse. The message names the file and the 1-based line."""


def read_events(path: Path, workspace_url: str) -> Iterator[Event]:
    """Yield one event per line. Lines must be sorted by `event_ts`."""
    previous: Event | None = None
    with path.open(encoding="utf-8") as lines:
        for number, line in enumerate(lines, 1):
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("a line must be a JSON object")
                event = Event.model_validate(_defaults(raw, workspace_url) | raw)
            except ValueError as error:  # JSONDecodeError and ValidationError included
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
    ts, channel_id = raw.get("ts"), raw.get("channel_id")
    if isinstance(ts, str):
        thread_ts = raw.get("thread_ts", ts)
        defaults |= {"thread_ts": ts, "event_ts": ts}
        if isinstance(channel_id, str) and isinstance(thread_ts, str):
            defaults["permalink"] = permalink_for(workspace_url, channel_id, ts, thread_ts)
    return defaults


def ingest(store: Store, path: Path, until: datetime, workspace_url: str) -> IngestReport:
    """Write every line observed by `until`; drop reactions made after it.

    The whole file is read before the first write, so a bad line writes nothing.
    """
    micros = micros_since_epoch(until)
    bound = f"{micros // 1_000_000:010d}.{micros % 1_000_000:06d}"
    read, kept = 0, []
    for event in read_events(path, workspace_url):
        read += 1
        if event.event_ts <= bound:
            reactions = tuple(r for r in event.reactions if r.ts <= bound)
            kept.append(event.model_copy(update={"reactions": reactions}))
    written = store.upsert_events(kept)
    log.info(
        "replay_ingested",
        extra={"path": str(path), "read": read, "written": written, "until": until.isoformat()},
    )
    return IngestReport(read=read, written=written, after_until=read - written)

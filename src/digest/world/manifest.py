"""The render manifest: which thread of `events.jsonl` carries each world event."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from digest.models import TS_PATTERN, Frozen


class ThreadRef(Frozen):
    channel_id: str
    thread_ts: str = Field(pattern=TS_PATTERN)
    authored: Literal["human", "render"]


class Manifest(Frozen):
    threads: dict[str, ThreadRef]  # world event id -> thread


def load_manifest(path: Path) -> Manifest:
    raise NotImplementedError


def write_manifest(manifest: Manifest, path: Path) -> None:
    raise NotImplementedError

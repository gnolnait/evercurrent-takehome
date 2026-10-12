"""The render manifest: which thread of `events.jsonl` carries each world event."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from digest.models import TS_PATTERN, Frozen


class ThreadRef(Frozen):
    channel_id: str
    thread_ts: str = Field(pattern=TS_PATTERN)
    authored: Literal["human", "render"]


class Manifest(Frozen):
    threads: dict[str, ThreadRef]  # world event id -> thread


def load_manifest(path: Path) -> Manifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}:{error.lineno}: {error.msg}") from error
    try:
        return Manifest.model_validate(data)
    except ValidationError as error:
        error.add_note(f"file: {path}")
        raise


def write_manifest(manifest: Manifest, path: Path) -> None:
    text = json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")

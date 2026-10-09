"""Read world files from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from digest.models import Frozen
from digest.world.schema import World


class WorldHeader(Frozen):
    name: str
    workspace_url: str
    timezone: str


def load_world_header(path: Path) -> WorldHeader:
    """Read the three keys replay needs and ignore the rest of the file."""
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return WorldHeader(
        name=raw["name"], workspace_url=raw["workspace_url"], timezone=raw["timezone"]
    )


def load_world(path: Path) -> World:
    raise NotImplementedError

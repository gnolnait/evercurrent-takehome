"""The renderer prompt and its request (plan B3.1)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from digest.corpus.avoid import AvoidNames
from digest.llm import LLMRequest
from digest.world.schema import World, WorldEvent

RENDER_PROMPT_VERSION = "m2.1"
RENDER_SYSTEM = ""


def render_request(
    world: World,
    event: WorldEvent,
    earlier: Sequence[WorldEvent],
    avoid: AvoidNames,
    output_schema: dict[str, Any],
) -> LLMRequest:
    raise NotImplementedError

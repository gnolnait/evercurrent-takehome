"""Render the world's events into `events.jsonl` threads through the `render` model (plan unit 3)."""

from __future__ import annotations

from pathlib import Path

from digest.corpus.avoid import AvoidNames
from digest.llm.client import LLM
from digest.models import Frozen
from digest.world.manifest import Manifest
from digest.world.schema import World, WorldEvent


class RenderedReaction(Frozen):
    name: str
    user_id: str  # a Literal of the channel's human members in the per-event model


class RenderedMessage(Frozen):
    poster: str  # a Literal of the personas whose channels include the event's channel
    day_offset: int  # 0 is the event's day; an answered ask puts the reply on answered_day
    minutes_after_start: int
    text: str
    files: list[str]
    reactions: list[RenderedReaction]


class RenderedThread(Frozen):
    messages: list[RenderedMessage]  # the first message is the thread root


def thread_model(world: World, event: WorldEvent) -> type[RenderedThread]:
    """A RenderedThread subclass whose poster and reactor fields are Literals for this event."""
    raise NotImplementedError


class RenderReport(Frozen):
    rendered: int
    reused: int  # anchor threads
    messages: int
    messages_per_thread: dict[str, int]
    chars_per_message: tuple[int, ...]


class RenderError(ValueError):
    """A rendered thread broke a rule. The message names the event, the rule and the cassette key."""


def render_world(
    world: World,
    llm: LLM,
    anchor: Path,
    manifest_in: Manifest,
    avoid: AvoidNames,
    events_out: Path,
    manifest_out: Path,
) -> RenderReport:
    raise NotImplementedError

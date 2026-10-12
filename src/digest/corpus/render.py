"""Render the world's events into `events.jsonl` threads through the `render` model (plan unit 3)."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, time, timedelta
from itertools import count
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import create_model

from digest.corpus.avoid import AvoidNames, find_avoided
from digest.corpus.prompts import render_request
from digest.llm.cassette import request_key
from digest.llm.client import LLM
from digest.models import Event, Frozen, Reaction, permalink_for, ts_micros
from digest.replay import read_events
from digest.world.manifest import Manifest, ThreadRef, write_manifest
from digest.world.schema import World, WorldEvent

log = logging.getLogger(__name__)

_DAY_START = time(8, 30)
_MINUTES_PER_EVENT = 25
_FIRST_FRACTION = 10_000  # anchor lines use fractions below this
_MENTION = re.compile(r"<@(U[A-Z0-9]+)>")


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
    members = [persona for persona in world.personas if event.channel in persona.channels]
    posters = tuple(persona.user_id for persona in members)
    reactors = tuple(persona.user_id for persona in members if persona.kind == "human")
    # The types are built from runtime values, so the subscripts are explicit calls.
    reaction = create_model(
        "RenderedReaction", __base__=RenderedReaction, user_id=(Literal.__getitem__(reactors), ...)
    )
    message = create_model(
        "RenderedMessage",
        __base__=RenderedMessage,
        poster=(Literal.__getitem__(posters), ...),
        reactions=(list.__class_getitem__(reaction), ...),
    )
    return create_model(
        "RenderedThread", __base__=RenderedThread, messages=(list.__class_getitem__(message), ...)
    )


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
    """Render every event that has no human-authored thread, then write both files.

    Every thread is checked before the first write, so a broken thread writes nothing.
    """
    human = {
        event_id: ref for event_id, ref in manifest_in.threads.items() if ref.authored == "human"
    }
    # A world with no human-authored slice has no anchor file.
    rows = list(read_events(anchor, world.workspace_url)) if anchor.exists() else []
    threads = dict(human)
    per_thread: dict[str, int] = {}
    chars: list[int] = []
    fractions = count(_FIRST_FRACTION)
    for event, slot, earlier in _render_order(world):
        if event.id in human:
            continue
        model_cls = thread_model(world, event)
        request = render_request(world, event, earlier, avoid, model_cls.model_json_schema())
        thread = llm.extract(request, model_cls)
        moments = _moments(world, event, slot, thread)
        rule = _broken_rule(world, event, thread, moments, avoid)
        if rule is not None:
            raise RenderError(
                f"event {event.id} breaks a render rule: {rule}; cassette key {request_key(request)}"
            )
        thread_rows = _thread_rows(world, event, thread, moments, fractions)
        rows.extend(thread_rows)
        threads[event.id] = ThreadRef(
            channel_id=event.channel, thread_ts=thread_rows[0].ts, authored="render"
        )
        per_thread[event.id] = len(thread_rows)
        chars.extend(len(message.text) for message in thread.messages)
    rows.sort(key=lambda row: ts_micros(row.event_ts))
    events_out.write_text(
        "".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8", newline="\n"
    )
    write_manifest(Manifest(threads=threads), manifest_out)
    report = RenderReport(
        rendered=len(per_thread),
        reused=sum(event.id in human for event in world.events),
        messages=len(chars),
        messages_per_thread=per_thread,
        chars_per_message=tuple(chars),
    )
    log.info(
        "world_rendered",
        extra={"rendered": report.rendered, "reused": report.reused, "messages": report.messages},
    )
    return report


def _render_order(world: World) -> Iterator[tuple[WorldEvent, int, tuple[WorldEvent, ...]]]:
    """Each event in (day, file position) order, with its slot on its day and its earlier events.

    The earlier events are those of the same scenario, human-authored or not.
    """
    slots: Counter[int] = Counter()
    seen: dict[str, list[WorldEvent]] = {}
    for _, event in sorted(enumerate(world.events), key=lambda pair: (pair[1].day, pair[0])):
        earlier: tuple[WorldEvent, ...] = ()
        if event.scenario is not None:
            earlier = tuple(seen.setdefault(event.scenario, []))
            seen[event.scenario].append(event)
        yield event, slots[event.day], earlier
        slots[event.day] += 1


def _moments(world: World, event: WorldEvent, slot: int, thread: RenderedThread) -> list[datetime]:
    zone = ZoneInfo(world.timezone)
    return [
        datetime.combine(world.date_of(event.day + message.day_offset), _DAY_START, zone)
        + timedelta(minutes=_MINUTES_PER_EVENT * slot + message.minutes_after_start)
        for message in thread.messages
    ]


def _broken_rule(
    world: World,
    event: WorldEvent,
    thread: RenderedThread,
    moments: list[datetime],
    avoid: AvoidNames,
) -> str | None:
    """The first rule of plan B3.2 and B3.3 that the thread breaks, or None."""
    messages = thread.messages
    if not messages:
        return "the thread has no messages"
    root = messages[0]
    if root.poster != event.poster:
        return f"the root poster {root.poster} is not the event poster {event.poster}"
    if root.day_offset != 0:
        return f"the root has day_offset {root.day_offset}; the root must be on the event's day"
    ask = event.ask
    if ask is not None:
        for number, message in enumerate(messages[:-1], 1):
            if message.poster == ask.of:
                return (
                    f"message {number} is by {ask.of}; "
                    "only the last message may be by the person asked"
                )
        if ask.answered_day is None and messages[-1].poster == ask.of:
            return f"the ask is never answered, but {ask.of} posts message {len(messages)}"
        if ask.answered_day is not None and (len(messages) < 2 or messages[-1].poster != ask.of):
            return f"an answered ask needs at least two messages, the last a reply by {ask.of}"
    for number, message in enumerate(messages, 1):
        expected = 0
        if ask is not None and ask.answered_day is not None and number == len(messages):
            expected = ask.answered_day - event.day
        if message.day_offset != expected:
            return f"message {number} has day_offset {message.day_offset}, not {expected}"
    zone = ZoneInfo(world.timezone)
    for number, (message, moment) in enumerate(zip(messages, moments, strict=True), 1):
        day = world.date_of(event.day + message.day_offset)
        if moment.astimezone(zone).date() != day:
            return f"the timestamp of message {number} is not on the date {day}"
    for number, moment in enumerate(moments[1:], 2):
        if moment < moments[0]:
            return f"message {number} precedes the root"
    text = "\n".join(message.text for message in messages)
    if not event.noise.get("indirection", False):
        entities = {entity.id: entity for entity in world.entities}
        folded = text.casefold()
        for claim in event.claims:
            names = (claim.entity, *entities[claim.entity].aliases)
            if not any(name.casefold() in folded for name in names):
                return f"claim entity {claim.entity} is not named by its id or an alias"
    files = "\n".join(name for message in messages for name in message.files)
    found = find_avoided(f"{text}\n{files}", avoid)
    if found:
        return f"the thread uses entries of the avoid list: {', '.join(found)}"
    return None


def _thread_rows(
    world: World,
    event: WorldEvent,
    thread: RenderedThread,
    moments: list[datetime],
    fractions: Iterator[int],
) -> list[Event]:
    channel = next(channel for channel in world.channels if channel.id == event.channel)
    rows: list[Event] = []
    for message, moment in zip(thread.messages, moments, strict=True):
        seconds = int(moment.timestamp())
        fraction = next(fractions)
        ts = f"{seconds}.{fraction:06d}"
        thread_ts = rows[0].ts if rows else ts
        rows.append(
            Event(
                source="replay",
                channel_id=channel.id,
                channel_name=channel.name,
                thread_ts=thread_ts,
                ts=ts,
                event_ts=ts,
                user_id=message.poster,
                text=message.text,
                mentions=tuple(dict.fromkeys(_MENTION.findall(message.text))),
                reactions=tuple(
                    Reaction(
                        name=reaction.name,
                        user_id=reaction.user_id,
                        ts=f"{seconds + 1}.{fraction:06d}",
                    )
                    for reaction in message.reactions
                ),
                files=tuple(message.files),
                permalink=permalink_for(world.workspace_url, channel.id, ts, thread_ts),
            )
        )
    return rows

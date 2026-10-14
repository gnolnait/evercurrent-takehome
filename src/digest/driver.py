"""The what-if driver (docs/plans/m3-driver.md).

It builds a message as a persona (`make_event`), holds the script line of `digest say`,
and compares two selections of one reader (`diff_selections`). The module makes no model
calls, reads no wall-clock time and writes nothing to a store.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field

from digest.daily.select import SECTION_CAPS, Section, Selection
from digest.models import TS_PATTERN, Event, Frozen, micros_since_epoch, permalink_for, ts_micros
from digest.world.schema import World

DiffField = Literal["section", "lane", "rank", "title", "reason"]


class LineView(Frozen):
    """One line of a selection as the diff compares it."""

    item_id: str
    title: str
    section: Section
    lane: Literal["must_see", "ranked"]
    # 1-based position among the ranked lines of its section; None in the must-see lane
    rank: int | None
    reason: str


class Change(Frozen):
    """An item in both selections whose view differs."""

    item_id: str
    before: LineView
    after: LineView
    fields: tuple[DiffField, ...]  # every differing field, in the order of `DiffField`


class ReaderDiff(Frozen):
    """What changed for one reader between two selections."""

    user_id: str
    added: tuple[LineView, ...]
    removed: tuple[LineView, ...]
    changed: tuple[Change, ...]


def diff_selections(before: Selection, after: Selection) -> ReaderDiff:
    """Compare two selections of one reader by `item_id` (plan m3-driver.md, `digest diff`).

    The caller passes two selections of the same reader; `user_id` is `after.profile.user_id`.

    Each tuple is ordered by section (as `Selection.lines`), then lane (must-see first),
    then rank, then `item_id`; a `Change` sorts by its `after` view.
    """
    user_id = after.profile.user_id
    if before.profile.user_id != user_id:
        raise ValueError(f"cannot diff readers {before.profile.user_id} and {user_id}")
    before_views = _views(before)
    after_views = _views(after)
    added = [view for item_id, view in after_views.items() if item_id not in before_views]
    removed = [view for item_id, view in before_views.items() if item_id not in after_views]
    changed: list[Change] = []
    for item_id, after_view in after_views.items():
        before_view = before_views.get(item_id)
        if before_view is None or before_view == after_view:
            continue
        fields = tuple(
            field
            for field in get_args(DiffField)
            if getattr(before_view, field) != getattr(after_view, field)
        )
        changed.append(Change(item_id=item_id, before=before_view, after=after_view, fields=fields))
    return ReaderDiff(
        user_id=user_id,
        added=tuple(sorted(added, key=_order)),
        removed=tuple(sorted(removed, key=_order)),
        changed=tuple(sorted(changed, key=_change_order)),
    )


_SECTION_ORDER: dict[Section, int] = {section: index for index, section in enumerate(SECTION_CAPS)}


def _views(selection: Selection) -> dict[str, LineView]:
    """Map each `item_id` of the selection to its view; refuse a duplicate `item_id`."""
    views: dict[str, LineView] = {}
    ranks: dict[Section, int] = {}
    for line in selection.lines:
        item_id = line.item.item_id
        if item_id in views:
            raise ValueError(f"item_id {item_id} occurs twice in one selection")
        rank = None
        if not line.must_see:
            rank = ranks.get(line.section, 0) + 1
            ranks[line.section] = rank
        views[item_id] = LineView(
            item_id=item_id,
            title=line.item.title,
            section=line.section,
            lane="must_see" if line.must_see else "ranked",
            rank=rank,
            reason=line.reason,
        )
    return views


def _change_order(change: Change) -> tuple[int, int, int, str]:
    """Sort key of a `Change`: the key of its `after` view."""
    return _order(change.after)


def _order(view: LineView) -> tuple[int, int, int, str]:
    """Sort key: section, then lane (must-see first), then rank, then `item_id`."""
    return (
        _SECTION_ORDER[view.section],
        0 if view.lane == "must_see" else 1,
        view.rank or 0,
        view.item_id,
    )


class StoreView(Frozen):
    """The store facts that `make_event` reads, and no others."""

    day: date  # the world-zone date of meta `until`
    latest_event_ts: str | None = Field(pattern=TS_PATTERN)  # None for a store with no event


def make_event(
    world: World,
    store_view: StoreView,
    persona_id: str,
    channel_name: str,
    at: datetime,
    text: str,
    thread: str | None,
) -> Event:
    """Build the event of one `say` line. Refuse a message that breaks rule S1 or S2.

    `at` is a naive datetime, read in the world's time zone. `thread` is the `ts` of the
    top-level message to reply to, or None for a new top-level message. The function does
    not check that `thread` exists (rule S3); the command does.
    Raise ValueError with a one-line reason for a refused message.
    """
    # S1: a human persona that is a member of a known channel.
    persona = next((p for p in world.personas if p.user_id == persona_id), None)
    if persona is None:
        raise ValueError(f"unknown persona {persona_id}: no persona with that id in the world")
    if persona.kind == "bot":
        raise ValueError(f"persona {persona_id} is a bot; only a human persona can say a message")
    channel = next((c for c in world.channels if c.name == channel_name), None)
    if channel is None:
        raise ValueError(f"unknown channel {channel_name}: no channel with that name in the world")
    if channel.id not in persona.channels:
        raise ValueError(
            f"persona {persona_id} is not a member of channel {channel_name} "
            "(not in the persona's channel list)"
        )
    # S2: the store's day is a working day, `at` falls on it and is after every event.
    working_days = {world.date_of(day) for day in range(1, world.days + 1)}
    if store_view.day not in working_days:
        raise ValueError(f"the store's day {store_view.day} is not a working day of the world")
    if at.tzinfo is not None:
        raise ValueError(f"--at {at.isoformat()} has a UTC offset; give the world's local time")
    if at.date() != store_view.day:
        raise ValueError(f"--at {at.isoformat()} is not on the store's day {store_view.day}")
    micros = micros_since_epoch(at.replace(tzinfo=ZoneInfo(world.timezone)))
    ts = f"{micros // 1_000_000:010d}.{micros % 1_000_000:06d}"
    latest = store_view.latest_event_ts
    if latest is not None and ts_micros(ts) <= ts_micros(latest):
        raise ValueError(
            f"--at {at.isoformat()} (ts {ts}) must be strictly later than the latest event "
            f"in the store (ts {latest})"
        )
    thread_ts = thread or ts
    return Event(
        source="replay",
        channel_id=channel.id,
        channel_name=channel.name,
        thread_ts=thread_ts,
        ts=ts,
        event_ts=ts,
        user_id=persona.user_id,
        text=text,
        permalink=permalink_for(world.workspace_url, channel.id, ts, thread_ts),
    )


class ScriptLine(Frozen):
    """The arguments of one `say`, as one JSON line of a script file.

    The JSON keys are the option names, so the persona id has the key "as".
    """

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    as_: str = Field(alias="as")  # persona id
    channel: str  # channel name
    at: str  # the `--at` text, YYYY-MM-DDTHH:MM[:SS] in the world's time zone
    text: str
    thread: str | None = Field(default=None, pattern=TS_PATTERN)

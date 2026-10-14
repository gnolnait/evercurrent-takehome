"""Render a reader's selection as plain text, and select and build a digest from a store.

See plan B6.1 and B6.2.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from digest.absence import detect_absences
from digest.daily.profile import profile_for
from digest.daily.select import Line, Section, Selection, select
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.models import Item, ts_micros
from digest.priors import Priors
from digest.project import ChecklistTask, add_working_days
from digest.store import Store
from digest.world.schema import World

SECTION_TITLES: Mapping[Section, str] = {
    "needs_you": "Needs you",
    "not_happened": "Not happened",
    "decisions": "Decisions",
    "risks": "Risks",
    "fyi": "FYI",
}


def render_digest(world: World, selection: Selection) -> str:
    """The digest as plain text: a header, the window and phase lines, the sections, the footer."""
    names = {persona.user_id: persona.name for persona in world.personas}
    tasks = {(gate.id, task.id): task for gate in world.project.gates for task in gate.checklist}
    profile = selection.profile
    header = f"Daily digest for {profile.name} ({profile.role}) - {selection.day.isoformat()}"
    blocks = ["\n".join([header, *context_lines(world, selection)])]
    for section, title in SECTION_TITLES.items():
        lines = [line for line in selection.lines if line.section == section]
        if lines:
            blocks.append("\n".join([title, *(_line(line, names, tasks) for line in lines)]))
    if not selection.lines:
        blocks.append(f"Nothing new for {profile.name}.")
    if selection.hidden > 0:
        noun = "item" if selection.hidden == 1 else "items"
        blocks.append(f"{selection.hidden} {noun} hidden by the visibility filter.")
    return "\n\n".join(blocks)


def context_lines(world: World, selection: Selection) -> list[str]:
    """The lines under the header: the window line, then the phase line when there is one."""
    phase = phase_line(world, selection.day)
    return [window_line(selection)] if phase is None else [window_line(selection), phase]


def phase_line(world: World, day: date) -> str | None:
    """The project phase on `day` and the next gate, or None before the first phase window.

    The next gate is the gate with the earliest date at or after `day`, ties to the lowest id.
    """
    started = [window for window in world.project.phases if window.from_date <= day]
    if not started:
        return None
    current = max(started, key=lambda window: window.from_date)
    text = f"Phase: {current.phase} since {current.from_date.isoformat()}."
    ahead = [gate for gate in world.project.gates if gate.date >= day]
    if not ahead:
        return text
    gate = min(ahead, key=lambda gate: (gate.date, gate.id))
    count, reached = 0, day
    while reached < gate.date:
        count += 1
        reached = add_working_days(day, count)
    noun = "working day" if count == 1 else "working days"
    when = "today" if count == 0 else f"in {count} {noun}"
    name = " ".join(gate.name.split())
    return f"{text} Next gate: {gate.id} {name}, {when}."


def window_line(selection: Selection) -> str:
    """The line under the header that names the digest window."""
    if selection.previous_digest is None:
        return f"First digest: the last {selection.first_working_days} working days"
    return f"Since your digest of {selection.previous_digest.isoformat()}"


def build_digest(store: Store, world: World, priors: Priors, user_id: str, day: date) -> str:
    """Select and render the reader's digest for `day`; see `select_digest` for the refusals."""
    return render_digest(world, select_digest(store, world, priors, user_id, day))


def select_digest(
    store: Store,
    world: World,
    priors: Priors,
    user_id: str,
    day: date,
    *,
    previous_digest: date | None = None,
) -> Selection:
    """Select the reader's lines for `day` from a replayed and extracted store.

    The window starts at the end of `previous_digest`, at most `max_working_days` back;
    without one, the digest is a first digest. An item's newest evidence is the greatest `event_ts` among the stored messages
    its evidence cites, so an edit counts as new evidence.

    Raise ValueError for a store that `check_store_day` refuses. Raise KeyError for an
    unknown user and ValueError for a bot.
    """
    check_store_day(store, world, day)
    try:
        profile = profile_for(world, user_id)
    except ValueError as error:
        raise ValueError(f"{user_id} is a bot, not a human reader: {error}") from error
    zone = ZoneInfo(world.timezone)
    as_of = datetime.combine(day, time.max, zone).astimezone(UTC)
    items = store.items(as_of)
    absences = detect_absences(store, world.project, priors, as_of, zone)
    # Extract cites only messages of the item's own thread. A cited message the store does not
    # hold counts at its `message_ts`.
    event_times = {
        (event.channel_id, event.ts): ts_micros(event.event_ts)
        for thread_id in {item.thread_id for item in items}
        for event in store.events_in_thread(thread_id, as_of)
    }
    newest_evidence = {
        item.item_id: max(
            event_times.get((entry.channel_id, entry.message_ts), ts_micros(entry.message_ts))
            for entry in item.evidence
        )
        for item in items
    }
    recent_start = datetime.combine(
        add_working_days(day, -priors.daily.recent_activity.working_days), time.max, zone
    )
    recent_messages = store.authored_messages(profile.user_id, recent_start, as_of)
    return select(
        world,
        profile,
        items,
        absences,
        priors,
        day,
        previous_digest,
        newest_evidence,
        recent_messages,
    )


def replayed_until(store: Store) -> datetime:
    """The instant in the store's `meta.until`.

    Raise ValueError for a store without `meta.until`, or for a value that is not an ISO
    timestamp with a UTC offset.
    """
    until = store.get_meta("until")
    if until is None:
        raise ValueError(f"store {store.path} has no meta key 'until'; run digest replay first")
    try:
        replayed = datetime.fromisoformat(until)
    except ValueError:
        replayed = None
    if replayed is None or replayed.tzinfo is None:
        raise ValueError(
            f"store {store.path} meta key 'until' is {until!r}, "
            "not an ISO timestamp with a UTC offset"
        )
    return replayed


def check_store_day(store: Store, world: World, day: date) -> None:
    """Refuse a store that cannot show `day`.

    Raise ValueError for a store that `replayed_until` refuses, a store whose `meta.until`
    is not exactly the end of `day` in the world's zone, or a store with extract deltas
    pending. The store keeps only the latest version of each item, so a store replayed
    past `day` cannot show `day`: its refusal names a fresh store as the remedy. The
    caller checks that the store holds the world: replay stores the world directory name,
    and `world` does not carry it.
    """
    as_of = datetime.combine(day, time.max, ZoneInfo(world.timezone)).astimezone(UTC)
    replayed = replayed_until(store)
    until = store.get_meta("until")  # the stored text, so a refusal quotes it as it is
    if replayed > as_of:
        raise ValueError(
            f"store {store.path} is replayed until {until}, past the end of day {day} "
            f"({as_of.isoformat()}), and keeps no earlier item versions; use a fresh --db, "
            f"replay it to day {day}, then run digest extract"
        )
    if replayed != as_of:
        raise ValueError(
            f"store {store.path} is replayed until {until}, before the end of day {day} "
            f"({as_of.isoformat()}); run digest replay --until {day}, then digest extract"
        )
    if store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", as_of):
        raise ValueError(
            f"store {store.path} has extract deltas pending as of day {day}; "
            "run digest extract first"
        )


def item_parts(
    item: Item, names: Mapping[str, str], tasks: Mapping[tuple[str, str], ChecklistTask]
) -> tuple[str, str]:
    """The item's heading and its one-line summary, without "- ", "[must see]" or sources.

    A checklist absence takes the task text and owner role from the world's checklist.
    """
    detail = item.absence
    if detail is None:
        entities = f" ({', '.join(item.entities)})" if item.entities else ""
        owner = f" - owner {names.get(item.owner, item.owner)}" if item.owner else ""
        head = f"{_plain(item.title)}{entities}{owner}"
    elif detail.source == "checklist":
        task = tasks.get((detail.gate or "", detail.task or ""))
        what = f"{task.task} ({task.owner})" if task else detail.task
        head = (
            f"Not happened: {what} for {detail.gate} - "
            f"{_days(detail.days_silent)} silent, "
            f"{detail.buffer_consumed}/{detail.buffer_days} buffer days used"
        )
    else:
        asker = names.get(detail.asker, detail.asker) if detail.asker else "someone"
        owner = names.get(item.owner, item.owner) if item.owner else "someone"
        head = f"Not happened: {asker} asked {owner}, no reply for {_days(detail.days_silent)}"
    return head, _plain(item.summary)


def line_parts(
    line: Line, names: Mapping[str, str], tasks: Mapping[tuple[str, str], ChecklistTask]
) -> tuple[str, str]:
    """The line's heading and its one-line summary, as `item_parts` gives them."""
    return item_parts(line.item, names, tasks)


def _line(
    line: Line, names: Mapping[str, str], tasks: Mapping[tuple[str, str], ChecklistTask]
) -> str:
    """One line, then its indented summary and source links."""
    heading, summary = line_parts(line, names, tasks)
    head = f"- {heading}"
    if line.must_see:
        head += " [must see]"
    sources = [f"  source: {entry.permalink}" for entry in line.item.evidence]
    return "\n".join([head, f"  {summary}", *sources])


def _days(count: int) -> str:
    return f"{count} working {'day' if count == 1 else 'days'}"


def _plain(text: str) -> str:
    """The text on one line; a line break in model or Slack text would break the layout."""
    return " ".join(text.split())

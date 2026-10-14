"""Render a reader's selection as a Slack top message and one thread reply.

The layout is the contract of docs/plans/m5-layout.md; the section numbers below refer to it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from digest.daily.render import SECTION_TITLES, context_lines
from digest.daily.select import Line, Selection
from digest.models import Evidence, Item, micros_since_epoch, ts_datetime, ts_micros
from digest.project import ChecklistTask, Gate
from digest.seed import SeedMap, channel_of, message_permalink
from digest.world.schema import Persona, World

MAX_BLOCKS = 50  # Slack refuses a message with more blocks
MAX_SECTION_TEXT = 3000  # Slack refuses a longer section text
MAX_HEADER_TEXT = 150  # Slack refuses a longer header text
MAX_NOTIFICATION = 150  # the phone shows about this much of `text`
TITLE_LIMIT = 90
BODY_LIMIT = 160
QUOTE_LIMIT = 100
TOP_MUST_SEE = 5
TOP_RANKED = 3
SHOWN_CODES = 2
SHOWN_FILES = 2
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
SEPARATOR = " \N{MIDDLE DOT} "
OPEN_QUOTE = "\N{LEFT DOUBLE QUOTATION MARK}"
CLOSE_QUOTE = "\N{RIGHT DOUBLE QUOTATION MARK}"
# A cut can end inside a link or an entity that this module wrote. Escaped text holds no
# other `<` and no other entity start, so the cut drops only a partial token of its own.
_PARTIAL_TOKEN = re.compile(r"<[^>]*$|&[a-z]*$")
_SENTENCE_END = re.compile(r"(?<=[.?!])(?=\s|$)")
_CODE = re.compile(r"[A-Z][A-Z0-9]*(-[A-Z0-9.]+)+")
_USER_TOKEN = re.compile(r"<@([A-Z0-9]+)(?:\|[^<>]*)?>")
_CHANNEL_TOKEN = re.compile(r"<#C[A-Z0-9]+\|([^<>]*)>")
_LINK_TOKEN = re.compile(r"<[^<>|@#!][^<>|]*\|([^<>]*)>")
_TRAILING_PARENS = re.compile(r" \([^()]*\)$")
_NOT_CODE_TYPES = frozenset({"vendor", "subsystem", "build"})
_SECTION_ORDER = tuple(SECTION_TITLES)
_FALLBACK_LABELS = {
    "needs_you": "Needs you",
    "not_happened": "Not happened",
    "decisions": "Decision",
    "risks": "Risk",
    "fyi": "FYI",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class _Inputs:
    """The renderer's inputs and the world lookups every item needs."""

    world: World
    selection: Selection
    links: SeedMap | None
    authors: Mapping[str, str]
    zone: ZoneInfo
    previous_end: datetime | None  # P of plan section 2; None for a first digest
    personas: Mapping[str, Persona]
    gates: Mapping[str, Gate]
    gate_codes: frozenset[str]
    entity_types: Mapping[str, str]
    channel_names: Mapping[str, str]

    @property
    def reader(self) -> str:
        return self.selection.profile.user_id

    def task(self, item: Item) -> ChecklistTask | None:
        """The world checklist task of a checklist absence, or None."""
        detail = item.absence
        gate = self.gates.get(detail.gate or "") if detail is not None else None
        if detail is None or gate is None:
            return None
        return next((task for task in gate.checklist if task.id == detail.task), None)


@dataclass(frozen=True)
class _Kept:
    """A line after the fold (plan 4.3): the absorber, its members, its selection order."""

    line: Line
    members: tuple[Line, ...]
    index: int
    must_see: bool

    @property
    def item(self) -> Item:
        return self.line.item


def slack_digest(
    world: World,
    selection: Selection,
    *,
    links: SeedMap | None,
    authors: Mapping[str, str],
    threaded: bool = True,
    reader_in_header: bool = False,
) -> list[dict[str, Any]]:
    """The digest as a top message and, when the thread holds items, one thread reply.

    Each message is {"text": str, "blocks": list}. `authors` maps a world message key
    (`<channel>:<ts>`) to the user id who posted it. `threaded=False` gives the webhook
    wording, where the reply is a second plain message. `reader_in_header=True` adds the
    reader's first name and role to the header, for a destination that gets the digests
    of several readers.
    """
    inputs = _inputs(world, selection, links, authors)
    kept = _fold(inputs)
    gate = _gate(inputs, kept)
    must = sorted((one for one in kept if one.must_see), key=lambda one: _top_key(inputs, one))
    if must:
        top, overflow = must[:TOP_MUST_SEE], must[TOP_MUST_SEE:]
    else:
        top = sorted(kept, key=lambda one: (-one.line.score, one.index))[:TOP_RANKED]
        overflow = []
    on_top = {one.index for one in top} | {one.index for one in overflow}
    groups = [
        (title, sorted(rest, key=lambda one: (-one.line.score, one.index)))
        for section, title in SECTION_TITLES.items()
        if (
            rest := [one for one in kept if one.index not in on_top and one.line.section == section]
        )
    ]
    listing = ([f"Read first {len(overflow)}"] if overflow else []) + [
        f"{title} {len(items)}" for title, items in groups
    ]
    new = sum("New" in _age_tags(inputs, one) for one in kept)
    count = _count_line(
        len(must), len(kept), len(top), len(kept) - len(top) - len(overflow), new, inputs, threaded
    )
    summary = count if gate is None else f"{_gate_line(inputs, gate)}\n{count}"
    blocks: list[dict[str, Any]] = [
        _header(world, selection, reader_in_header),
        _context("\n".join(_escape(line) for line in context_lines(world, selection))),
        _section(summary),
    ]
    if top:
        blocks.append({"type": "divider"})
        blocks.extend(block for one in top for block in _item_blocks(inputs, one, gate, True))
    foot = _foot(listing, selection.hidden, threaded)
    if foot:
        blocks += [{"type": "divider"}, _context(foot)]
    messages = [{"text": _notification(inputs, gate, top, len(must)), "blocks": blocks}]
    if listing:
        reply_groups = ([("Read first", overflow, True)] if overflow else []) + [
            (title, items, False) for title, items in groups
        ]
        messages.append(
            {
                "text": f"More from your digest: {', '.join(listing)}",
                "blocks": _reply_blocks(inputs, gate, reply_groups),
            }
        )
    return messages


def _inputs(
    world: World, selection: Selection, links: SeedMap | None, authors: Mapping[str, str]
) -> _Inputs:
    zone = ZoneInfo(world.timezone)
    previous = selection.previous_digest
    gates = {gate.id: gate for gate in world.project.gates}
    types = {entity.id: entity.type for entity in world.entities}
    return _Inputs(
        world=world,
        selection=selection,
        links=links,
        authors=authors,
        zone=zone,
        previous_end=None if previous is None else datetime.combine(previous, time.max, zone),
        personas={persona.user_id: persona for persona in world.personas},
        gates=gates,
        gate_codes=frozenset(gates) | {key for key, kind in types.items() if kind == "gate"},
        entity_types=types,
        channel_names={channel.id: channel.name for channel in world.channels},
    )


# --- fold and order (plan 4) ---------------------------------------------------------------


def _fold(inputs: _Inputs) -> list[_Kept]:
    """The kept items in selection order: each absorbable risk joins its first absorber.

    Only a line before the risk can absorb it, so a fold never moves a risk to a later section.
    """
    lines = inputs.selection.lines
    members: dict[int, list[Line]] = {}
    absorbed: set[int] = set()
    for index, line in enumerate(lines):
        if not _absorbable(line):
            continue
        for host, candidate in enumerate(lines[:index]):
            if not _absorbable(candidate) and _absorbs(inputs, candidate.item, line.item):
                members.setdefault(host, []).append(line)
                absorbed.add(index)
                break
    return [
        _Kept(
            line=line,
            members=tuple(members.get(index, ())),
            index=index,
            must_see=line.must_see or any(member.must_see for member in members.get(index, ())),
        )
        for index, line in enumerate(lines)
        if index not in absorbed
    ]


def _absorbable(line: Line) -> bool:
    return line.section == "risks" and line.item.absence is None


def _absorbs(inputs: _Inputs, host: Item, risk: Item) -> bool:
    """Whether `host` absorbs `risk` by rule (a) or rule (b) of plan 4.3."""
    if _is_checklist(host):
        task = inputs.task(host)
        return task is not None and any(
            re.search(rf"(?<![A-Za-z0-9-]){re.escape(entity)}(?![A-Za-z0-9-])", task.task)
            for entity in risk.entities
        )
    if _is_ask(host) and host.thread_id == risk.thread_id:
        return bool((set(host.entities) & set(risk.entities)) - inputs.gate_codes)
    return False


def _top_key(inputs: _Inputs, kept: _Kept) -> tuple[int, date, int, int]:
    """Section, nearest upcoming gate date (none last), days silent (highest first), selection."""
    item = kept.item
    detail = item.absence
    today = inputs.selection.day
    if _is_checklist(item):
        gate = inputs.gates.get(detail.gate or "") if detail is not None else None
        dates = [gate.date] if gate is not None else []
    else:
        dates = [
            inputs.gates[entity].date
            for entity in item.entities
            if entity in inputs.gates and inputs.gates[entity].date >= today
        ]
    silent = detail.days_silent if detail is not None else 0
    section = _SECTION_ORDER.index(kept.line.section)
    return (section, min(dates, default=date.max), -silent, kept.index)


# --- top message (plan 3) ------------------------------------------------------------------


def _gate(inputs: _Inputs, kept: list[_Kept]) -> Gate | None:
    """The gate of the gate line (plan 3.1): the first rule that gives a gate wins."""
    profile = inputs.selection.profile
    ahead = [gate for gate in inputs.world.project.gates if gate.date >= inputs.selection.day]
    rules: tuple[Callable[[Gate], bool], ...] = (
        lambda gate: gate.id in profile.owned_entities,
        lambda gate: any(task.owner == profile.role for task in gate.checklist),
        lambda gate: any(gate.id in one.item.entities for one in kept),
    )
    for rule in rules:
        hits = [gate for gate in ahead if rule(gate)]
        if hits:
            return min(hits, key=lambda gate: (gate.date, gate.id))
    return None


def _gate_line(inputs: _Inputs, gate: Gate) -> str:
    name = _TRAILING_PARENS.sub("", _flatten(gate.name))
    when = _when(inputs, gate)
    text = f"*{_escape(name)} ({_escape(gate.id)}) is {when}, {_date_text(gate.date)}.*"
    if gate.buffer_days == 0:
        return text + " No buffer days."
    used = [
        line.item.absence.buffer_consumed
        for line in inputs.selection.lines
        if line.item.absence is not None
        and line.item.absence.source == "checklist"
        and line.item.absence.gate == gate.id
        and line.item.absence.buffer_consumed is not None
    ]
    if not used:
        return text
    return text + f" {max(used)} of {gate.buffer_days} buffer days are used."


def _when(inputs: _Inputs, gate: Gate) -> str:
    days = (gate.date - inputs.selection.day).days
    return {0: "today", 1: "tomorrow"}.get(days, f"in {days} days")


def _count_line(
    must: int, total: int, top: int, rest: int, new: int, inputs: _Inputs, threaded: bool
) -> str:
    """The count line of plan 3.2. `rest` counts the thread items that are not must-see."""
    if total == 0:
        return "Nothing new for you."
    more = f", {rest} more {'in the thread' if threaded else 'below'}" if rest else ""
    tail = "" if inputs.previous_end is None else f" {new} of {total} are new."
    if must > TOP_MUST_SEE:
        place = "the thread" if threaded else "the next message"
        first = f"*{must} to read first*: {TOP_MUST_SEE} below, {must - TOP_MUST_SEE} at the top"
        return f"{first} of {place}{more}.{tail}"
    if must:
        return f"*{must} to read first*{more}.{tail}"
    return f"Nothing needs you today. *{top} worth a look*{more}.{tail}"


def _foot(listing: list[str], hidden: int, threaded: bool) -> str:
    parts = []
    if listing:
        parts.append(f"{'In the thread' if threaded else 'Next message'}: {', '.join(listing)}")
    if hidden:
        noun = "item" if hidden == 1 else "items"
        parts.append(f"{hidden} {noun} left out: from channels you cannot see.")
    return ". ".join(parts)


def _notification(inputs: _Inputs, gate: Gate | None, top: list[_Kept], must: int) -> str:
    """The `text` of the top message (plan 3.4), cut so that its escaped form fits."""
    text = "" if gate is None else f"{gate.id} {_when(inputs, gate)}. "
    if not top:
        text += "Nothing new for you."
    else:
        title = _title(inputs, top[0])
        text += f"{must} to read first, starting with {title}" if must else f"Worth a look: {title}"
        if not title.endswith((".", "?", "!")):
            text += "."
    return _escape(_shorten(text, MAX_NOTIFICATION, lambda part: len(_escape(part))))


def _header(world: World, selection: Selection, reader_in_header: bool) -> dict[str, Any]:
    # plain_text shows `<` and `&` as typed, so the header needs no escape and no token cut.
    header = f"{world.program}{SEPARATOR}{_date_text(selection.day)}"
    if reader_in_header:
        profile = selection.profile
        header += f"{SEPARATOR}{profile.name.split()[0]} ({_role_text(profile.role)})"
    header = _flatten(header)
    if len(header) > MAX_HEADER_TEXT:
        header = header[: MAX_HEADER_TEXT - 1] + ELLIPSIS
    return {"type": "header", "text": {"type": "plain_text", "text": header}}


# --- thread reply (plan 8) -----------------------------------------------------------------


def _reply_blocks(
    inputs: _Inputs, gate: Gate | None, groups: list[tuple[str, list[_Kept], bool]]
) -> list[dict[str, Any]]:
    """The groups, each item with its group's divider and heading when it is the first.

    Past 50 blocks, whole items are packed in order and one block says how many are left out.
    """
    entries: list[list[dict[str, Any]]] = []
    for number, (title, items, labeled) in enumerate(groups):
        for position, kept in enumerate(items):
            lead = []
            if position == 0:
                lead = [{"type": "divider"}] if number else []
                lead.append(_section(f"*{title}*{SEPARATOR}{len(items)}"))
            entries.append(lead + _item_blocks(inputs, kept, gate, labeled))
    blocks = [block for entry in entries for block in entry]
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    blocks, shown = [], 0
    for entry in entries:
        if len(blocks) + len(entry) + 1 > MAX_BLOCKS:
            break
        blocks += entry
        shown += 1
    omitted = len(entries) - shown
    noun = "item" if omitted == 1 else "items"
    return [*blocks, _context(f"{omitted} more {noun} not shown")]


# --- one item (plan 5 and 6) ---------------------------------------------------------------


def _item_blocks(
    inputs: _Inputs, kept: _Kept, gate: Gate | None, labeled: bool
) -> list[dict[str, Any]]:
    """The section with the title and body, then the context with the meta line."""
    title = _title(inputs, kept)
    body = _body(inputs, kept)
    label = _label(inputs, kept, title) if labeled else ""
    owner = _owner_part(inputs, kept)
    if label and owner == "Owner: you":
        owner = ""
    parts = [
        f"*{_escape(label)}*" if label else "",
        _escape(owner),
        *_age_tags(inputs, kept),
        _escape(", ".join(_codes(inputs, kept, (title, body, label)))),
        _escape(_buffer_part(inputs, kept, gate)),
        _source_part(inputs, kept),
        _checked_part(inputs, kept),
        _files_part(inputs, kept),
    ]
    text = f"*{_escape(title)}*" + (f"\n{_escape(body)}" if body else "")
    return [_section(text), _context(SEPARATOR.join(part for part in parts if part))]


def _title(inputs: _Inputs, kept: _Kept) -> str:
    """The displayed title (plan 5.2), raw and cut."""
    item = kept.item
    detail = item.absence
    title = item.title
    if detail is not None and detail.source == "checklist":
        task = inputs.task(item)
        noun = _task_noun(task.task) if task is not None else detail.task or ""
        title = f"{noun}: no progress evidence for {_days(detail.days_silent)}"
    elif detail is not None:
        asker = _asker(inputs, item)
        days = _days(detail.days_silent)
        if asker == inputs.reader:
            title = f"No reply from {_first_name(inputs, item.owner)} in this thread for {days}"
        elif item.owner == inputs.reader:
            asker_name = _first_name(inputs, asker, start=True)
            title = f"{asker_name} has had no reply from you in this thread for {days}"
    elif item.type == "ask" and kept.members:
        title = kept.members[0].item.title
    return _shorten(_flatten(title), TITLE_LIMIT)


def _task_noun(task: str) -> str:
    """The task text without its first word and without a `the` that follows it."""
    words = task.split()[1:]
    if words and words[0].lower() == "the":
        words = words[1:]
    return " ".join(words)


def _body(inputs: _Inputs, kept: _Kept) -> str:
    """The body (plan 5.3), raw and cut."""
    item = kept.item
    if _is_ask(item):
        message = _ask_message(inputs, item)
        if message is None:
            return _first_sentence(item.summary)
        asker = inputs.authors[_key(message)]
        quote = _shorten(_clean_quote(inputs, message.quote), QUOTE_LIMIT)
        if item.owner == inputs.reader:
            prefix = f"{_first_name(inputs, asker, start=True)} asked you: "
        elif item.absence is not None and asker == inputs.reader:
            prefix = "You asked: "
        else:
            prefix = f"{_first_name(inputs, asker, start=True)} asked: "
        return f"{prefix}{OPEN_QUOTE}{quote}{CLOSE_QUOTE}"
    if _is_checklist(item):
        if kept.members:
            best = min(kept.members, key=lambda member: (not member.must_see, -member.score))
            return _first_sentence(best.item.summary)
        task = inputs.task(item)
        if task is None:
            return ""
        return f"Progress is expected every {_days(task.expected_cadence_days)}."
    return _first_sentence(item.summary)


def _first_sentence(summary: str) -> str:
    parts = (part.strip() for part in _SENTENCE_END.split(_flatten(summary)))
    return _shorten(next((part for part in parts if part), ""), BODY_LIMIT)


def _clean_quote(inputs: _Inputs, quote: str) -> str:
    """The quote with Slack tokens shown as plain words (plan 5.3, quote cleanup)."""
    reader = re.escape(inputs.reader)
    text = re.sub(rf"^<@{reader}(?:\|[^<>]*)?>[,:;.!?-]*\s*", "", _flatten(quote))
    text = _USER_TOKEN.sub(lambda match: "@" + _first_name(inputs, match.group(1)), text)
    text = _CHANNEL_TOKEN.sub(lambda match: "#" + match.group(1), text)
    return _LINK_TOKEN.sub(lambda match: match.group(1), text).strip()


def _label(inputs: _Inputs, kept: _Kept, title: str) -> str:
    """The label of a top or overflow item (plan 5.4): the first row that matches."""
    item = kept.item
    profile = inputs.selection.profile
    if _is_ask(item) and item.owner == inputs.reader:
        return "Asked of you"
    if _is_ask(item) and item.absence is not None and _asker(inputs, item) == inputs.reader:
        return "Your question"
    if _is_checklist(item):
        task = inputs.task(item)
        return "Your gate task" if task is not None and task.owner == profile.role else "Gate task"
    owned = [entity for entity in item.entities if entity in profile.owned_entities]
    if kept.must_see and owned:
        code = next((entity for entity in owned if entity not in inputs.gate_codes), owned[0])
        if code in inputs.gate_codes:
            return "On your gate" if code in title else f"On your gate {code}"
        return "You own this" if title.startswith(code) else f"On your {code}"
    return _FALLBACK_LABELS[kept.line.section]


def _owner_part(inputs: _Inputs, kept: _Kept) -> str:
    """`Owner: ...` (plan 5.4), or an empty text."""
    item = kept.item
    if kept.line.section == "fyi":
        return ""
    if _is_checklist(item):
        task = inputs.task(item)
        if task is None:
            return ""
        holders = [
            persona
            for persona in inputs.personas.values()
            if persona.kind == "human" and persona.role == task.owner
        ]
        # The checklist names a role, not a person: show the person only when one has it.
        if len(holders) == 1:
            return _owner_of(inputs, holders[0])
        return f"Owner: {_role_text(task.owner)}"
    if item.owner == inputs.reader:
        return "Owner: you"
    persona = inputs.personas.get(item.owner or "")
    if persona is None or persona.kind != "human":
        return ""
    return _owner_of(inputs, persona)


def _owner_of(inputs: _Inputs, persona: Persona) -> str:
    if persona.user_id == inputs.reader:
        return "Owner: you"
    role = f" ({_role_text(persona.role)})" if persona.role is not None else ""
    return f"Owner: {persona.name.split()[0]}{role}"


def _age_tags(inputs: _Inputs, kept: _Kept) -> list[str]:
    """The age tags of plan 5.6. An absence has none: its title carries the days."""
    item = kept.item
    if item.absence is not None:
        return []
    asked = None
    if item.type == "ask" and item.owner == inputs.reader:
        asked = _ask_message(inputs, item)
    tags = []
    seen = ts_micros(item.first_seen)
    if inputs.previous_end is not None and seen > micros_since_epoch(inputs.previous_end):
        tags.append("New")
    elif asked is not None:
        tags.append(f"Asked {_date_text(_local_date(inputs, asked.message_ts))}")
    elif _local_date(inputs, item.first_seen) < inputs.selection.day:
        tags.append(f"Open since {_date_text(_local_date(inputs, item.first_seen))}")
    if asked is not None and any(
        ts_micros(entry.message_ts) > ts_micros(asked.message_ts)
        and inputs.authors.get(_key(entry)) == inputs.reader
        for entry in item.evidence
    ):
        tags.append("You replied")
    return tags


def _codes(inputs: _Inputs, kept: _Kept, shown: tuple[str, ...]) -> list[str]:
    """At most two entity codes (plan 5.5): owned codes first, none already in `shown`."""
    owned = inputs.selection.profile.owned_entities
    candidates = [kept.item.entities, *(member.item.entities for member in kept.members)]
    codes = [
        entity
        for entity in dict.fromkeys(entity for entities in candidates for entity in entities)
        if _CODE.fullmatch(entity)
        and entity not in inputs.gate_codes
        and inputs.entity_types.get(entity) not in _NOT_CODE_TYPES
        and not any(entity in text for text in shown)
    ]
    ordered = [code for code in codes if code in owned] + [
        code for code in codes if code not in owned
    ]
    return ordered[:SHOWN_CODES]


def _buffer_part(inputs: _Inputs, kept: _Kept, gate: Gate | None) -> str:
    """The buffer of a checklist absence whose gate is not the gate line's gate."""
    detail = kept.item.absence
    if detail is None or detail.source != "checklist" or detail.gate is None:
        return ""
    if gate is not None and detail.gate == gate.id:
        return ""
    world_gate = inputs.gates.get(detail.gate)
    days = world_gate.buffer_days if world_gate is not None else detail.buffer_days
    if days == 0:
        return f"{detail.gate}: no buffer days"
    if days is None or detail.buffer_consumed is None:
        return ""
    return f"{detail.gate}: {detail.buffer_consumed} of {days} buffer days used"


def _source_part(inputs: _Inputs, kept: _Kept) -> str:
    """The one source link (plan 6.1). An unmapped source has no link, only its channel."""
    item = kept.item
    message = _ask_message(inputs, item)
    target = item.thread_id if message is None else _key(message)
    links = inputs.links
    permalink = None if links is None else message_permalink(links, target)
    if links is not None and permalink is not None:
        word = "Thread" if target == item.thread_id else "Message"
        return f"<{permalink}|{word}> in <#{channel_of(links, target)}>"
    channel = _channel(target)
    slack_id = None if links is None else links.channels.get(channel)
    if slack_id is not None:
        return f"In <#{slack_id}>"
    return f"In #{_escape(inputs.channel_names.get(channel, channel))}"


def _checked_part(inputs: _Inputs, kept: _Kept) -> str:
    """Where the absence detector looked for a checklist task."""
    if not _is_checklist(kept.item):
        return ""
    channel = _channel(kept.item.thread_id)
    return f"checked: #{_escape(inputs.channel_names.get(channel, channel))} threads"


def _files_part(inputs: _Inputs, kept: _Kept) -> str:
    """The seed-map files of the item and its members, in map order, each name once."""
    links = inputs.links
    if links is None:
        return ""
    keys = {_key(entry) for line in (kept.line, *kept.members) for entry in line.item.evidence}
    files: dict[str, str] = {}
    for path, mapped in links.files.items():
        key, _, name = path.partition("/")
        if key in keys and name not in files:
            files[name] = mapped.permalink
    if not files:
        return ""
    shown = list(files.items())[:SHOWN_FILES]
    part = ":paperclip: " + " ".join(f"<{url}|{_escape(name)}>" for name, url in shown)
    extra = len(files) - len(shown)
    if extra:
        part += f" +{extra} {'file' if extra == 1 else 'files'}"
    return part


# --- shared lookups ------------------------------------------------------------------------


def _ask_message(inputs: _Inputs, item: Item) -> Evidence | None:
    """The ask message (plan 5.1): the first entry by a known author who is not the owner."""
    if not _is_ask(item):
        return None
    for entry in item.evidence:
        if inputs.authors.get(_key(entry), item.owner) != item.owner:
            return entry
    return None


def _asker(inputs: _Inputs, item: Item) -> str | None:
    message = _ask_message(inputs, item)
    if message is not None:
        return inputs.authors[_key(message)]
    return item.absence.asker if item.absence is not None else None


def _is_ask(item: Item) -> bool:
    return item.type == "ask" or (item.absence is not None and item.absence.source == "ask")


def _is_checklist(item: Item) -> bool:
    return item.absence is not None and item.absence.source == "checklist"


def _key(entry: Evidence) -> str:
    return f"{entry.channel_id}:{entry.message_ts}"


def _channel(key: str) -> str:
    return key.partition(":")[0]


def _first_name(inputs: _Inputs, user_id: str | None, *, start: bool = False) -> str:
    persona = inputs.personas.get(user_id or "")
    if persona is None:
        return "Someone" if start else "someone"
    return persona.name.split()[0]


def _role_text(role: str) -> str:
    return role.replace("_", " ")


def _local_date(inputs: _Inputs, ts: str) -> date:
    return ts_datetime(ts).astimezone(inputs.zone).date()


def _date_text(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _days(count: int) -> str:
    return f"{count} working {'day' if count == 1 else 'days'}"


# --- text pipeline (plan 9) ----------------------------------------------------------------


def _section(text: str) -> dict[str, Any]:
    """A section block with one mrkdwn text object."""
    return {"type": "section", "text": _mrkdwn(text)}


def _context(text: str) -> dict[str, Any]:
    """A context block with one mrkdwn element."""
    return {"type": "context", "elements": [_mrkdwn(text)]}


def _mrkdwn(text: str) -> dict[str, Any]:
    """A mrkdwn text object, cut to 3000 characters.

    `verbatim` stops Slack from turning a bare URL, `@here` or `#channel` into a link or a
    mention. The explicit `<url|text>` and `<#C...>` links still work.
    """
    return {"type": "mrkdwn", "text": _cut(text, MAX_SECTION_TEXT), "verbatim": True}


def _flatten(text: str) -> str:
    return " ".join(text.split())


def _shorten(text: str, limit: int, width: Callable[[str], int] = len) -> str:
    """The text, or its longest start that ends at a word end, plus `…`, within `limit`.

    `width` measures a text. A text with no such start (one long word) is cut hard.
    """
    if width(text) <= limit:
        return text
    # width(head) >= len(head), so no start longer than `limit` can fit.
    for end in range(min(len(text) - 1, limit), 0, -1):
        head = text[:end].rstrip()
        if text[end] == " " and head and width(head) + 1 <= limit:
            return head + ELLIPSIS
    end = limit - 1
    while end > 0 and width(text[:end]) + 1 > limit:
        end -= 1
    return text[:end] + ELLIPSIS


def _escape(text: str) -> str:
    """The text on one line, with the three characters that Slack mrkdwn parses escaped."""
    return _flatten(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, limit: int) -> str:
    """The text, or its start and an ellipsis when it is longer than `limit` characters."""
    if len(text) <= limit:
        return text
    return _PARTIAL_TOKEN.sub("", text[: limit - 1]) + ELLIPSIS

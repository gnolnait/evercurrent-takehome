"""Render a reader's selection as a Slack Block Kit message; see plan B6.4."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from digest.daily.render import SECTION_TITLES, window_line
from digest.daily.select import Line, Selection
from digest.models import Item
from digest.project import ChecklistTask
from digest.seed import SeedMap, channel_of, files_of, message_permalink
from digest.world.schema import World

MAX_BLOCKS = 50  # Slack refuses a message with more blocks
MAX_SECTION_TEXT = 3000  # Slack refuses a longer section text
MAX_HEADER_TEXT = 150  # Slack refuses a longer header text
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
# A cut can end inside a link or an entity that this module wrote. Escaped text holds no
# other `<` and no other entity start, so the cut drops only a partial token of its own.
_PARTIAL_TOKEN = re.compile(r"<[^>]*$|&[a-z]*$")


def slack_message(
    world: World, selection: Selection, links: SeedMap | None = None
) -> dict[str, Any]:
    """The digest as Slack `text` and Block Kit `blocks`: a header, the window line, the sections, the footers.

    The message holds at most 50 blocks. Lines past the limit are left out, and the last
    block says how many. With a seed map in `links`, the source links of mapped messages
    point at the seeded Slack messages (docs/plans/m5-links.md).
    """
    names = {persona.user_id: persona.name for persona in world.personas}
    tasks = {(gate.id, task.id): task for gate in world.project.gates for task in gate.checklist}
    profile = selection.profile
    header = f"Daily digest for {profile.name} ({profile.role}) - {selection.day.isoformat()}"
    body: list[tuple[dict[str, Any], bool]] = []  # each block, and whether it holds a line
    for section, title in SECTION_TITLES.items():
        lines = [line for line in selection.lines if line.section == section]
        if lines:
            body.append((_section(f"*{title}*"), False))
            body.extend((_section(_line(line, names, tasks, links)), True) for line in lines)
    if not selection.lines:
        body.append((_section(f"Nothing new for {_escape(profile.name)}."), False))
    footer = []
    if selection.hidden > 0:
        noun = "item" if selection.hidden == 1 else "items"
        footer.append(_section(f"{selection.hidden} {noun} hidden by the visibility filter."))
    # The header and the window line are fixed blocks; one more is kept for the left-out count.
    if 2 + len(body) + len(footer) > MAX_BLOCKS:
        kept = body[: MAX_BLOCKS - 3 - len(footer)]
        if not kept[-1][1]:  # a section title with no line under it
            kept.pop()
        omitted = sum(is_line for _, is_line in body) - sum(is_line for _, is_line in kept)
        noun = "line" if omitted == 1 else "lines"
        body = kept
        footer.append(_section(f"{omitted} {noun} left out."))
    # plain_text shows `<` and `&` as typed, so the header needs no escape and no token cut.
    if len(header) > MAX_HEADER_TEXT:
        header = header[: MAX_HEADER_TEXT - 1] + ELLIPSIS
    header_block = {"type": "header", "text": {"type": "plain_text", "text": header}}
    window = window_line(selection)
    return {
        "text": f"{_escape(header)}\n{_escape(window)}",
        "blocks": [header_block, _section(_escape(window)), *(block for block, _ in body), *footer],
    }


def _line(
    line: Line,
    names: Mapping[str, str],
    tasks: Mapping[tuple[str, str], ChecklistTask],
    links: SeedMap | None,
) -> str:
    """One line in mrkdwn: the bold head, the summary, the source links.

    The text is the terminal line of `render_digest`. Text from the model and from Slack is
    escaped, so it cannot form a link or a mention.
    """
    item = line.item
    detail = item.absence
    if detail is None:
        entities = f" ({_escape(', '.join(item.entities))})" if item.entities else ""
        owner = f" - owner {_escape(names.get(item.owner, item.owner))}" if item.owner else ""
        head = f"*{_escape(item.title)}*{entities}{owner}"
    elif detail.source == "checklist":
        task = tasks.get((detail.gate or "", detail.task or ""))
        what = f"{task.task} ({task.owner})" if task else (detail.task or "")
        head = (
            f"*Not happened: {_escape(what)}* for {_escape(detail.gate or '')} - "
            f"{_days(detail.days_silent)} silent, "
            f"{detail.buffer_consumed}/{detail.buffer_days} buffer days used"
        )
    else:
        asker = names.get(detail.asker, detail.asker) if detail.asker else "someone"
        owner = names.get(item.owner, item.owner) if item.owner else "someone"
        head = (
            f"*Not happened: {_escape(asker)} asked {_escape(owner)}*, "
            f"no reply for {_days(detail.days_silent)}"
        )
    if line.must_see:
        head += " *[must see]*"
    sources = _sources(item, links)
    return "\n".join(part for part in (head, _escape(item.summary), sources) if part)


def _sources(item: Item, links: SeedMap | None) -> str:
    """The source links of an item, each mapped one with its file links, then its channels.

    A source that is not in `links` keeps its world permalink (plan L1 to L4). Only URLs
    and channel ids go inside `<...>` unescaped. A file name is escaped.
    """
    if links is None:
        return " ".join(f"<{entry.permalink}|source>" for entry in item.evidence)
    parts: list[str] = []
    channels: list[str] = []
    for entry in item.evidence:
        key = f"{entry.channel_id}:{entry.message_ts}"
        permalink = message_permalink(links, key)
        channel = channel_of(links, key)
        if permalink is None or channel is None:
            parts.append(f"<{entry.permalink}|source>")
            continue
        parts.append(f"<{permalink}|source>")
        parts.extend(f"<{url}|{_escape(name)}>" for name, url in files_of(links, key))
        if channel not in channels:
            channels.append(channel)
    if channels:
        parts.append("in " + " ".join(f"<#{channel}>" for channel in channels))
    return " ".join(parts)


def _section(text: str) -> dict[str, Any]:
    """A section block with one mrkdwn text object.

    `verbatim` stops Slack from turning a bare URL, `@here` or `#channel` into a link or a
    mention. The explicit `<url|text>` and `<#C...>` links still work.
    """
    text_object = {"type": "mrkdwn", "text": _cut(text, MAX_SECTION_TEXT), "verbatim": True}
    return {"type": "section", "text": text_object}


def _escape(text: str) -> str:
    """The text on one line, with the three characters that Slack mrkdwn parses escaped."""
    flat = " ".join(text.split())
    return flat.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, limit: int) -> str:
    """The text, or its start and an ellipsis when it is longer than `limit` characters."""
    if len(text) <= limit:
        return text
    return _PARTIAL_TOKEN.sub("", text[: limit - 1]) + ELLIPSIS


def _days(count: int) -> str:
    return f"{count} working {'day' if count == 1 else 'days'}"

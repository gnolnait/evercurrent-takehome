"""The triage and extraction prompts and their requests (plan B1.2, B1.3, Q1 to Q3, Q7)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from digest.llm import LLMRequest, Message
from digest.models import Event, Glossary, Item
from digest.project import ProjectConfig
from digest.store import ThreadDelta

EXTRACT_PROMPT_VERSION = "m2.3"  # triage and extraction prompts; m2.3 adds plan Q7

# One rule per line: a reader can find each rule of plan B1.3 on its own line.
EXTRACT_SYSTEM = "\n".join(
    (
        "You read one Slack thread of a hardware engineering program and extract project items.",
        "The user message lists, in this order: the glossary of entities and people, the checklist"
        " tasks of gates on or after today, the existing items of this thread, the thread root"
        " and the changed messages.",
        "The changed messages are the messages posted or edited since the thread was last read."
        " This delta is the set of changed messages.",
        "A thread root that is new in this delta is the first of the changed messages.",
        "Return items for new information only: a decision, a blocker, a risk, a schedule change,"
        " a spec change, a BOM change, an ask, or an fyi that a program member needs to know.",
        "Do not return an item again when a changed message adds nothing to an existing item.",
        "To update or close an existing item, set item_id to its item_id from the list."
        " For a new item, set item_id to null.",
        "Set status to resolved when the messages close the item, else open.",
        "Each evidence entry names one listed message by its ts and quotes a span of that"
        " message verbatim. Do not join text from two messages in one quote.",
        "Use null, never a guess, for owner, due and item_id when the messages do not state them.",
        "Write owner as a user id from the glossary. Write due as YYYY-MM-DD.",
        "For an ask, the owner is the person asked, not the person who asks."
        " The first evidence entry of an ask is the message that asks.",
        "Write each entity as a glossary entity id. Write a revision as a claim with attribute"
        " rev, not as part of the entity.",
        "Each claim states one attribute value of one entity and names the evidence message"
        " that states it.",
        "In tasks, name only checklist tasks from the list, and only when a message reports"
        " progress on the task: a run, a result, a review held, a sign-off, a part received.",
        "Set effect to done only when a message states that the task is complete; else set"
        " effect to evidence. Each task link names the evidence message that shows the progress.",
        "A task link needs a progress report: a question about a task, or a status roll-up that"
        " lists a task as open, is not a task link. A question can still be an ask item.",
        "Write the rationale first, before every other field, for the output and for each item.",
        "When the changed messages carry no new project information, return an empty items list.",
    )
)

TRIAGE_SYSTEM = "\n".join(
    (
        "You read one Slack thread of a hardware engineering program.",
        "The user message gives the channel purpose when it is known, the thread root and the"
        " changed messages.",
        "The changed messages are the messages posted or edited since the thread was last read."
        " This delta is the set of changed messages.",
        "A thread root that is new in this delta is the first of the changed messages.",
        "Give the probability, from 0 to 1, that the changed messages carry project information:"
        " a decision, a blocker, a risk, a schedule change, a spec change, a BOM change, an ask"
        " about the program, or an fyi that a program member needs to know.",
        "Small talk, thanks and social messages are not project information.",
        "Write the rationale first, then the probability.",
    )
)


def extract_request(
    delta: ThreadDelta,
    glossary: Glossary,
    project: ProjectConfig,
    as_of: date,
    existing: Sequence[Item],
    citable: Sequence[Event],
) -> LLMRequest:
    sections = (
        _glossary_section(glossary),
        _tasks_section(project, as_of),
        _existing_section(existing),
        _thread_section(delta, citable, with_files=True),
    )
    return LLMRequest(
        model="extract",
        system=EXTRACT_SYSTEM,
        messages=(Message(role="user", content="\n\n".join(sections)),),
    )


def triage_request(
    delta: ThreadDelta, channel_purpose: str | None, citable: Sequence[Event]
) -> LLMRequest:
    sections = (
        *(() if channel_purpose is None else (f"Channel purpose: {channel_purpose}",)),
        _thread_section(delta, citable, with_files=False),
    )
    return LLMRequest(
        model="classify",
        system=TRIAGE_SYSTEM,
        messages=(Message(role="user", content="\n\n".join(sections)),),
    )


def _glossary_section(glossary: Glossary) -> str:
    lines = ["Glossary entities (id | type | description | aliases):"]
    lines += [
        f"- {e.id} | {e.type} | {e.description} | {', '.join(e.aliases) or 'none'}"
        for e in glossary.entities
    ]
    lines.append("Glossary people (user id | name | role | aliases):")
    lines += [
        f"- {p.user_id} | {p.name} | {p.role or 'none'} | {', '.join(p.aliases) or 'none'}"
        for p in glossary.people
    ]
    return "\n".join(lines)


def _tasks_section(project: ProjectConfig, as_of: date) -> str:
    lines = [f"Checklist tasks of gates on or after {as_of} (task id | task | owner role | gate):"]
    lines += [
        f"- {task.id} | {task.task} | {task.owner} | {gate.id} on {gate.date}"
        for gate in project.gates
        if gate.date >= as_of
        for task in gate.checklist
    ]
    return "\n".join(lines)


def _existing_section(existing: Sequence[Item]) -> str:
    lines = ["Existing items of this thread:"]
    for item in existing:
        claims = "; ".join(
            f"{c.entity} {c.attribute}={c.value} (ts {c.message_ts})" for c in item.claims
        )
        tasks = "; ".join(f"{t.task} {t.effect} (ts {t.message_ts})" for t in item.tasks)
        lines += [
            f"- item_id: {item.item_id}",
            f"  type: {item.type}",
            f"  title: {item.title}",
            f"  status: {item.status}",
            f"  owner: {item.owner or 'null'}",
            f"  due: {item.due or 'null'}",
            f"  entities: {', '.join(item.entities) or 'none'}",
            f"  claims: {claims or 'none'}",
            f"  tasks: {tasks or 'none'}",
        ]
    if not existing:
        lines.append("none")
    return "\n".join(lines)


def _thread_section(delta: ThreadDelta, citable: Sequence[Event], *, with_files: bool) -> str:
    """The root section, then the changed messages in ts order (plan Q1).

    A root among the delta's changed rows is listed once, as the first changed message, and the
    root section says that it is new in this delta. Any other root is context in the root
    section. A deleted row never enters a prompt.
    """
    rows = sorted((row for row in citable if not row.deleted), key=lambda row: row.ts_us)
    changed_ts = {row.ts for row in delta.changed}
    root = [row for row in rows if row.ts == delta.thread_ts]
    new_root = bool(root) and delta.thread_ts in changed_ts
    changed = [row for row in rows if new_root or row.ts != delta.thread_ts]
    lines = ["Thread root:"]
    if new_root:
        lines.append("new in this delta: it is the first changed message below")
    else:
        lines += [_message(row, with_files=with_files) for row in root] or ["not available"]
    lines.append("Changed messages, oldest first:")
    lines += [_message(row, with_files=with_files) for row in changed] or ["none"]
    return "\n".join(lines)


def _message(row: Event, *, with_files: bool) -> str:
    text = f"[ts {row.ts}] author {row.user_id}: {row.text}"
    if with_files and row.files:
        text += f"\n  files: {', '.join(row.files)}"
    return text

"""The absence detector: checklist tasks inside a gate buffer and unanswered asks (SPEC 5.5)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from digest.models import (
    AbsenceDetail,
    Event,
    Evidence,
    Item,
    Severity,
    ts_datetime,
    ts_micros,
)
from digest.priors import Priors
from digest.project import (
    ChecklistTask,
    Gate,
    ProjectConfig,
    add_working_days,
    working_days_between,
)
from digest.store import Store

log = logging.getLogger(__name__)


def checklist_absences(
    project: ProjectConfig, items: Sequence[Item], as_of: date, zone: ZoneInfo
) -> list[Item]:
    """One absence per open checklist task, inside its gate buffer, silent past its cadence."""
    absences: list[Item] = []
    for gate in sorted(project.gates, key=lambda gate: gate.id):
        buffer_start = add_working_days(gate.date, -gate.buffer_days)
        if not buffer_start <= as_of <= gate.date:
            continue
        for task in sorted(gate.checklist, key=lambda task: task.id):
            cited = _task_evidence(gate, task, items, as_of, zone)
            if cited is None:
                continue
            item, evidence = cited
            days_silent = working_days_between(_local_date(evidence.message_ts, zone), as_of)
            if days_silent <= task.expected_cadence_days:
                continue
            absences.append(
                _checklist_item(gate, task, item, evidence, days_silent, buffer_start, as_of)
            )
    return absences


def _task_evidence(
    gate: Gate, task: ChecklistTask, items: Sequence[Item], as_of: date, zone: ZoneInfo
) -> tuple[Item, Evidence] | None:
    """The item and evidence entry the task's silence counts from; None for a done task."""
    links = [
        (item, link)
        for item in items
        for link in item.tasks
        if link.task == task.id and _local_date(link.message_ts, zone) <= as_of
    ]
    if any(link.effect == "done" for _, link in links):
        return None
    if links:
        item, link = max(links, key=lambda pair: (ts_micros(pair[1].message_ts), pair[0].item_id))
        return item, _entry(item, link.message_ts)
    claims = [
        (item, claim)
        for item in items
        for claim in item.claims
        if claim.entity == gate.id and _local_date(claim.message_ts, zone) <= as_of
    ]
    if not claims:
        return None
    item, claim = min(claims, key=lambda pair: (ts_micros(pair[1].message_ts), pair[0].item_id))
    return item, _entry(item, claim.message_ts)


def _checklist_item(
    gate: Gate,
    task: ChecklistTask,
    item: Item,
    evidence: Evidence,
    days_silent: int,
    buffer_start: date,
    as_of: date,
) -> Item:
    buffer_consumed = working_days_between(buffer_start, as_of)
    severity: Severity = "high" if task.priority in ("critical", "high") else "medium"
    return Item(
        item_id=f"absence:{task.id}",
        thread_id=item.thread_id,
        type="absence",
        title=f"{gate.name}: no progress on {task.task}",
        summary=(
            f"No evidence for {days_silent} working days (expected every "
            f"{task.expected_cadence_days}); {buffer_consumed} of {gate.buffer_days} "
            f"buffer days used before {gate.id} on {gate.date}."
        ),
        entities=tuple(dict.fromkeys((gate.id, *item.entities))),
        owner=None,
        due=gate.date,
        affected_roles=(task.owner,),
        severity=severity,
        confidence="high",
        evidence=(evidence,),
        first_seen=evidence.message_ts,
        last_updated=evidence.message_ts,
        absence=AbsenceDetail(
            source="checklist",
            gate=gate.id,
            task=task.id,
            asker=None,
            days_silent=days_silent,
            buffer_consumed=buffer_consumed,
            buffer_days=gate.buffer_days,
        ),
    )


def ask_absences(
    items: Sequence[Item],
    thread_events: Callable[[Item], Sequence[Event]],
    as_of: date,
    cadence_days: int,
    zone: ZoneInfo,
) -> list[Item]:
    """One absence per open ask whose person asked has not replied within the cadence."""
    absences: list[Item] = []
    items = sorted(items, key=lambda item: item.item_id)
    for ask in items:
        if ask.type != "ask" or ask.status != "open":
            continue
        owner = ask.owner
        if owner is None:
            continue
        events = thread_events(ask)
        authors = {event.ts: event.user_id for event in events}
        evidence = next(
            (entry for entry in ask.evidence if authors.get(entry.message_ts, owner) != owner),
            None,
        )
        if evidence is None:
            log.warning("absence_ask_without_message", extra={"item_id": ask.item_id})
            continue
        asked_at = ts_micros(evidence.message_ts)
        replies = [
            event
            for event in events
            if event.user_id == owner and not event.deleted and event.ts_us > asked_at
        ]
        if replies:
            continue
        days_silent = working_days_between(_local_date(evidence.message_ts, zone), as_of)
        if days_silent <= cadence_days:
            continue
        absences.append(_ask_item(ask, owner, authors[evidence.message_ts], evidence, days_silent))
    return absences


def _ask_item(ask: Item, owner: str, asker: str, evidence: Evidence, days_silent: int) -> Item:
    return Item(
        item_id=f"absence:ask:{ask.item_id}",
        thread_id=ask.thread_id,
        type="absence",
        title=f"{asker} asked {owner}; no reply for {days_silent} working days",
        summary=ask.summary,
        entities=ask.entities,
        owner=owner,
        due=ask.due,
        affected_roles=ask.affected_roles,
        severity="medium",
        confidence="high",
        evidence=(evidence,),
        first_seen=evidence.message_ts,
        last_updated=evidence.message_ts,
        absence=AbsenceDetail(
            source="ask",
            gate=None,
            task=None,
            asker=asker,
            days_silent=days_silent,
            buffer_consumed=None,
            buffer_days=None,
        ),
    )


def detect_absences(
    store: Store, project: ProjectConfig, priors: Priors, as_of: datetime, zone: ZoneInfo
) -> list[Item]:
    """Both sources over the stored items as of an instant: checklist first, then asks."""
    items = store.items(as_of)
    day = as_of.astimezone(zone).date()
    return [
        *checklist_absences(project, items, day, zone),
        *ask_absences(
            items,
            lambda item: store.events_in_thread(item.thread_id, as_of),
            day,
            priors.absence.ask_reply_cadence_days,
            zone,
        ),
    ]


def _entry(item: Item, message_ts: str) -> Evidence:
    """The item's evidence entry for a message; extraction keeps one per cited message."""
    return next(entry for entry in item.evidence if entry.message_ts == message_ts)


def _local_date(message_ts: str, zone: ZoneInfo) -> date:
    return ts_datetime(message_ts).astimezone(zone).date()

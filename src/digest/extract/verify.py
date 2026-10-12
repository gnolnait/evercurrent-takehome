"""Verification in code: quotes, ids, links and claim value forms (plan B1.5, Q5, D5)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date

from digest.extract.normalize import normalize_space, resolve_entity, resolve_person
from digest.extract.schema import (
    ExtractedClaim,
    ExtractedEvidence,
    ExtractedItem,
    ExtractedTask,
    Extraction,
)
from digest.models import (
    Authority,
    Claim,
    Event,
    Evidence,
    Frozen,
    Glossary,
    Item,
    TaskLink,
    ts_micros,
)
from digest.project import ChecklistTask
from digest.store import ThreadDelta

DROP_FIELDS = (
    "evidence.unknown_message",
    "evidence.quote",
    "item_id.unknown",
    "entities.unresolved",
    "claims.entity",
    "claims.message",
    "owner.unresolved",
    "due.format",
    "tasks.unknown",
    "tasks.message",
    "evidence.stale",
    "claims.stale",
    "tasks.stale",
)

# Plan Q5: a revision token with an optional "rev" prefix; a first word that is a whole number,
# with optional thousands commas and an optional "/" count.
_REV = re.compile(r"(?:(?i:rev)\.?\s*)?([A-Za-z0-9]+)")
_QTY = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:/(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+))?")


class Verified(Frozen):
    items: tuple[Item, ...]
    drops: dict[str, int]  # every DROP_FIELDS key present, zero when nothing dropped


def verify(
    extraction: Extraction,
    delta: ThreadDelta,
    citable: Mapping[str, Event],
    glossary: Glossary,
    tasks: Mapping[str, ChecklistTask],
    existing: Mapping[str, Item],
) -> Verified:
    drops = dict.fromkeys(DROP_FIELDS, 0)
    # One entry per item id. A later output for the same id merges against the earlier result.
    items: dict[str, Item] = {}
    for n, raw in enumerate(extraction.items):
        evidence = _evidence(raw.evidence, citable, drops)
        if evidence is None:
            continue
        if raw.item_id is not None and raw.item_id not in existing:
            drops["item_id.unknown"] += 1
            continue
        old_claims: list[Claim] = []
        old_links: list[TaskLink] = []
        if raw.item_id is None:
            item_id = f"{delta.thread_id}#{delta.last_ts}#{n}"
        else:
            item_id = raw.item_id
            old = items.get(item_id, existing[item_id])
            evidence, old_claims, old_links = _merge(old, evidence, citable, drops)
        # A claim or a link cites a message the item holds and the delta carries.
        cited = {_key(entry) for entry in evidence if _in_delta(entry, citable)}
        claims = old_claims + _claims(raw.claims, cited, citable, glossary, drops)
        links = old_links + _task_links(raw.tasks, cited, citable, tasks, drops)
        items[item_id] = _item(
            raw, item_id, delta.thread_id, evidence, claims, links, glossary, drops
        )
    return Verified(items=tuple(items.values()), drops=drops)


def _evidence(
    entries: Iterable[ExtractedEvidence], citable: Mapping[str, Event], drops: dict[str, int]
) -> list[Evidence] | None:
    """The verified entries, one per message: a later entry replaces an earlier one in place.

    None when an entry cites an unknown message or misquotes its message; the item drops.
    """
    verified: dict[str, Evidence] = {}  # the delta is one thread, so one channel
    for entry in entries:
        event = citable.get(entry.message_ts)
        if event is None:
            drops["evidence.unknown_message"] += 1
            return None
        if not quotes(entry.quote, event):
            drops["evidence.quote"] += 1
            return None
        verified[entry.message_ts] = Evidence(
            channel_id=event.channel_id,
            message_ts=entry.message_ts,
            quote=entry.quote,
            permalink=event.permalink,
        )
    return list(verified.values())


def _key(cited: Evidence | Claim | TaskLink) -> tuple[str, str]:
    """The message a citation names; a Slack ts is unique only within a channel."""
    return cited.channel_id, cited.message_ts


def _in_delta(entry: Evidence, citable: Mapping[str, Event]) -> bool:
    event = citable.get(entry.message_ts)
    return event is not None and event.channel_id == entry.channel_id


def _cited_event(
    message_ts: str, cited: set[tuple[str, str]], citable: Mapping[str, Event]
) -> Event | None:
    """The delta's event for a message the item cites, else None."""
    event = citable.get(message_ts)
    if event is None or (event.channel_id, message_ts) not in cited:
        return None
    return event


def quotes(quote: str, event: Event) -> bool:
    """True when the quote, spaces normalized, is in the text of the message."""
    # An empty quote is a substring of every text, so it proves nothing.
    span = normalize_space(quote)
    return bool(span) and span in normalize_space(event.text)


def _claims(
    raw: Iterable[ExtractedClaim],
    cited: set[tuple[str, str]],
    citable: Mapping[str, Event],
    glossary: Glossary,
    drops: dict[str, int],
) -> list[Claim]:
    claims = []
    for claim in raw:
        entity = resolve_entity(claim.entity, glossary)
        if entity is None:
            drops["claims.entity"] += 1
        elif (event := _cited_event(claim.message_ts, cited, citable)) is None:
            drops["claims.message"] += 1
        else:
            claims.append(
                Claim(
                    entity=entity,
                    attribute=claim.attribute,
                    value=_claim_value(claim.attribute, claim.value),
                    channel_id=event.channel_id,
                    message_ts=claim.message_ts,
                    authority=_authority(event.user_id, glossary),
                )
            )
    return claims


def _claim_value(attribute: str, value: str) -> str:
    """The value in its compared form (plan Q5). Only rev and qty change.

    A value that does not match its form is kept as given.
    """
    if attribute == "rev" and (match := _REV.fullmatch(value.strip())):
        return match.group(1).upper()
    words = value.split()
    if attribute == "qty" and words and (match := _QTY.fullmatch(words[0])):
        return match.group(1).replace(",", "")
    return value


def _authority(user_id: str, glossary: Glossary) -> Authority:
    person = next((p for p in glossary.people if p.user_id == user_id), None)
    return "hearsay" if person is None else person.authority


def _task_links(
    raw: Iterable[ExtractedTask],
    cited: set[tuple[str, str]],
    citable: Mapping[str, Event],
    tasks: Mapping[str, ChecklistTask],
    drops: dict[str, int],
) -> list[TaskLink]:
    links = []
    for link in raw:
        if link.task not in tasks:
            drops["tasks.unknown"] += 1
        elif (event := _cited_event(link.message_ts, cited, citable)) is None:
            drops["tasks.message"] += 1
        else:
            links.append(
                TaskLink(
                    task=link.task,
                    effect=link.effect,
                    channel_id=event.channel_id,
                    message_ts=link.message_ts,
                )
            )
    return links


def _merge(
    old: Item,
    evidence: list[Evidence],
    citable: Mapping[str, Event],
    drops: dict[str, int],
) -> tuple[list[Evidence], list[Claim], list[TaskLink]]:
    """The merged evidence, and the old claims and links whose message the merged evidence holds.

    The merged evidence is the old evidence, then the new entries for messages not cited before.
    A new entry for a cited message replaces the old entry in place. An old entry whose message
    the delta carries is checked again against the current text.
    """
    incoming = {_key(entry): entry for entry in evidence}
    merged = []
    for entry in old.evidence:
        if _key(entry) in incoming:
            merged.append(incoming.pop(_key(entry)))
        elif _in_delta(entry, citable) and not quotes(entry.quote, citable[entry.message_ts]):
            drops["evidence.stale"] += 1
        else:
            merged.append(entry)
    merged += incoming.values()
    held = {_key(entry) for entry in merged}
    old_claims = [claim for claim in old.claims if _key(claim) in held]
    old_links = [link for link in old.tasks if _key(link) in held]
    drops["claims.stale"] += len(old.claims) - len(old_claims)
    drops["tasks.stale"] += len(old.tasks) - len(old_links)
    return merged, old_claims, old_links


def _item(
    raw: ExtractedItem,
    item_id: str,
    thread_id: str,
    evidence: list[Evidence],
    claims: list[Claim],
    links: list[TaskLink],
    glossary: Glossary,
    drops: dict[str, int],
) -> Item:
    stamps = sorted((entry.message_ts for entry in evidence), key=ts_micros)
    return Item(
        item_id=item_id,
        thread_id=thread_id,
        type=raw.type,
        title=raw.title,
        summary=raw.summary,
        entities=_entities(raw.entities, glossary, drops),
        owner=_owner(raw.owner, glossary, drops),
        due=_due(raw.due, drops),
        affected_roles=tuple(raw.affected_roles),
        severity=raw.severity,
        confidence=raw.confidence,
        evidence=tuple(evidence),
        first_seen=stamps[0],
        last_updated=stamps[-1],
        claims=_unique_claims(claims),
        tasks=tuple(dict.fromkeys(links)),
        status=raw.status,
        resolved_by=stamps[-1] if raw.status == "resolved" else None,
    )


def _entities(raw: Iterable[str], glossary: Glossary, drops: dict[str, int]) -> tuple[str, ...]:
    resolved = []
    for text in raw:
        entity = resolve_entity(text, glossary)
        if entity is None:
            drops["entities.unresolved"] += 1
        else:
            resolved.append(entity)
    return tuple(dict.fromkeys(resolved))


def _owner(raw: str | None, glossary: Glossary, drops: dict[str, int]) -> str | None:
    if raw is None:
        return None
    owner = resolve_person(raw, glossary)
    if owner is None:
        drops["owner.unresolved"] += 1
    return owner


def _due(raw: str | None, drops: dict[str, int]) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        drops["due.format"] += 1
        return None


def _unique_claims(claims: Iterable[Claim]) -> tuple[Claim, ...]:
    """The first claim for each (entity, attribute, value, channel_id, message_ts), in order."""
    first: dict[tuple[str, str, str, str, str], Claim] = {}
    for claim in claims:
        first.setdefault((claim.entity, claim.attribute, claim.value, *_key(claim)), claim)
    return tuple(first.values())

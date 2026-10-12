"""Choose a reader's lines: the visibility filter, the must-see lane and the ranked lane (plan B5.2 to B5.5).

When the digest shows an ask absence line, the line of the ask its id names is left out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from digest.daily.profile import Profile, can_see
from digest.models import Frozen, Item, ItemType, Severity, micros_since_epoch, ts_micros
from digest.priors import Priors
from digest.world.schema import World

Section = Literal["needs_you", "not_happened", "decisions", "risks", "fyi"]

SECTION_CAPS: Mapping[Section, int] = {
    "needs_you": 5,
    "not_happened": 3,
    "decisions": 5,
    "risks": 5,
    "fyi": 5,
}

SECTIONS: Mapping[ItemType, Section] = {
    "ask": "needs_you",
    "absence": "not_happened",
    "decision": "decisions",
    "schedule_change": "decisions",
    "spec_change": "decisions",
    "bom_change": "decisions",
    "blocker": "risks",
    "risk": "risks",
    "fyi": "fyi",
}

TYPE_WEIGHTS: Mapping[ItemType, float] = {
    "blocker": 1.0,
    "decision": 0.9,
    "schedule_change": 0.9,
    "spec_change": 0.8,
    "bom_change": 0.8,
    "risk": 0.7,
    "ask": 0.6,
    "absence": 0.9,
    "fyi": 0.3,
}

SEVERITY_WEIGHTS: Mapping[Severity, float] = {"high": 1.0, "medium": 0.7, "low": 0.4}

DEFAULT_ROLE_PRIOR = 0.5  # when the priors table has no entry for the role and type

ASK_ABSENCE_PREFIX = "absence:ask:"  # the item_id prefix `_ask_item` in digest.absence writes


class Line(Frozen):
    item: Item
    section: Section
    must_see: bool
    reason: str  # the must-see rule, or the score terms with their values
    score: float  # 0 for a must-see line


class Selection(Frozen):
    profile: Profile
    day: date
    lines: tuple[Line, ...]  # section order; must-see by item_id, then ranked in the B5.4 order
    hidden: int  # items dropped by the visibility filter


def select(
    world: World,
    profile: Profile,
    items: Sequence[Item],
    absences: Sequence[Item],
    priors: Priors,
    day: date,
) -> Selection:
    """Choose the reader's lines for the day. Items must be open and updated by the end of the day."""
    end = micros_since_epoch(datetime.combine(day, time.max, ZoneInfo(world.timezone)))
    current = [
        item for item in items if item.status == "open" and ts_micros(item.last_updated) <= end
    ]
    gates = {gate.id for gate in world.project.gates}
    hidden = 0
    must_see: dict[Section, list[Line]] = {section: [] for section in SECTION_CAPS}
    ranked: dict[Section, list[Line]] = {section: [] for section in SECTION_CAPS}
    for item in [*current, *absences]:
        trimmed = _visible(world, profile, item)
        if trimmed is None:
            hidden += 1
            continue
        section = _section_of(trimmed, profile)
        rule = _must_see_rule(trimmed, profile, gates)
        if rule is not None:
            line = Line(item=trimmed, section=section, must_see=True, reason=rule, score=0)
            must_see[section].append(line)
            continue
        line = _ranked_line(trimmed, section, profile, priors)
        if line.score >= priors.daily.min_score:
            ranked[section].append(line)
    lines: list[Line] = []
    for section, cap in SECTION_CAPS.items():
        lines += sorted(must_see[section], key=lambda line: line.item.item_id)
        lines += sorted(ranked[section], key=_rank_key)[:cap]
    # After the caps and the threshold: a shown ask absence line replaces the one ask its id names.
    absent_asks = {_cited_ask(line.item) for line in lines} - {None}
    lines = [
        line for line in lines if not (line.item.type == "ask" and line.item.item_id in absent_asks)
    ]
    return Selection(profile=profile, day=day, lines=tuple(lines), hidden=hidden)


def _cited_ask(item: Item) -> str | None:
    """The item_id of the ask an ask absence names, or None for any other item."""
    if item.absence is None or item.absence.source != "ask":
        return None
    return item.item_id.removeprefix(ASK_ABSENCE_PREFIX)


def _visible(world: World, profile: Profile, item: Item) -> Item | None:
    """The item with only the evidence from its thread's channel, or None when the reader sees none.

    The reader must see the thread's channel (plan B5.2). An evidence entry whose `channel_id`
    is another channel is removed. The check fails closed.
    """
    channel_id = _thread_channel(item)
    if not can_see(world, profile, channel_id):
        return None
    kept = tuple(entry for entry in item.evidence if entry.channel_id == channel_id)
    if not kept:
        return None
    return item.model_copy(update={"evidence": kept})


def _thread_channel(item: Item) -> str:
    """The channel id in the item's `thread_id`, which has the form `<channel>:<thread_ts>`."""
    return item.thread_id.partition(":")[0]


def _section_of(item: Item, profile: Profile) -> Section:
    """The item's section (plan B5.5). An ask absence goes to needs_you for the person asked."""
    if item.absence is not None and item.absence.source == "ask" and item.owner == profile.user_id:
        return "needs_you"
    return SECTIONS[item.type]


def _rank_key(line: Line) -> tuple[float, int, str]:
    """Score to six significant figures, highest first; then newest evidence; then item_id."""
    newest = max(ts_micros(entry.message_ts) for entry in line.item.evidence)
    return (-float(f"{line.score:.6g}"), -newest, line.item.item_id)


def _must_see_rule(item: Item, profile: Profile, gates: set[str]) -> str | None:
    """The must-see rule the item meets for the reader (plan B5.3), or None."""
    owned = set(item.entities) & set(profile.owned_entities)
    if item.type == "ask" and item.owner == profile.user_id:
        return "must see: an open ask addressed to you"
    if item.type == "blocker" and owned:
        return f"must see: an open blocker on an owned entity ({', '.join(sorted(owned))})"
    if item.type == "schedule_change" and owned & gates:
        return f"must see: a schedule change on an owned gate ({', '.join(sorted(owned & gates))})"
    if item.absence is None:
        return None
    if item.absence.source == "checklist" and profile.role in item.affected_roles:
        return "must see: a checklist absence for your role"
    if item.absence.source == "ask" and item.absence.asker == profile.user_id:
        return "must see: an ask absence, you asked"
    if item.absence.source == "ask" and item.owner == profile.user_id:
        return "must see: an ask absence, you were asked"
    return None


def _ranked_line(item: Item, section: Section, profile: Profile, priors: Priors) -> Line:
    """Score the item as importance * role_prior * personal_relevance (plan B5.4)."""
    importance = TYPE_WEIGHTS[item.type] * SEVERITY_WEIGHTS[item.severity]
    role_prior = priors.daily.role_priors.get(profile.role, {}).get(item.type, DEFAULT_ROLE_PRIOR)
    if set(item.entities) & set(profile.owned_entities):
        relevance = 1.0
    elif profile.role in item.affected_roles:
        relevance = 0.8
    else:
        relevance = 0.4
    reason = (
        f"importance={round(importance, 3)} role_prior={round(role_prior, 3)} "
        f"personal_relevance={relevance}"
    )
    score = importance * role_prior * relevance
    return Line(item=item, section=section, must_see=False, reason=reason, score=score)

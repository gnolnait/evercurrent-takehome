"""Choose a reader's lines: the visibility filter, the must-see lane and the ranked lane (plan B5.2 to B5.5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from digest.daily.profile import Profile, can_see
from digest.models import Evidence, Frozen, Item, ItemType, Severity, micros_since_epoch, ts_micros
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


class Line(Frozen):
    item: Item
    section: Section
    must_see: bool
    reason: str  # the must-see rule, or the score terms with their values
    score: float  # 0 for a must-see line


class Selection(Frozen):
    profile: Profile
    day: date
    lines: tuple[Line, ...]  # section order, must-see first, then score descending, then item_id
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
        visible = tuple(
            entry for entry in item.evidence if can_see(world, profile, _channel_of(entry))
        )
        if not visible:
            hidden += 1
            continue
        trimmed = item.model_copy(update={"evidence": visible})
        section = SECTIONS[trimmed.type]
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
        lines += sorted(ranked[section], key=lambda line: (-line.score, line.item.item_id))[:cap]
    return Selection(profile=profile, day=day, lines=tuple(lines), hidden=hidden)


def _channel_of(entry: Evidence) -> str:
    """The channel id in the permalink; an empty string, which no reader can see, when absent."""
    return entry.permalink.partition("/archives/")[2].partition("/")[0]


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

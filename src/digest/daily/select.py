"""Choose a reader's lines: the visibility filter, the must-see lane and the ranked lane (plan B5.2 to B5.5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Literal

from digest.daily.profile import Profile
from digest.models import Frozen, Item
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
    raise NotImplementedError

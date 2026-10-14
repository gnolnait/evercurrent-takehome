"""Constants the pipeline reads from `config/priors.yaml` (SPEC section 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from digest.models import Frozen, ItemType, Role

DEFAULT_PRIORS_PATH = Path("config/priors.yaml")

RoleWeight = Annotated[float, Field(gt=0, le=1)]  # plan B5.4: above 0 and at most 1


class TriagePriors(Frozen):
    miss_cost_ratio: float = Field(gt=0)  # a missed thread costs this many false keeps

    @property
    def keep_threshold(self) -> float:
        return 1 / (1 + self.miss_cost_ratio)


class AbsencePriors(Frozen):
    ask_reply_cadence_days: int = Field(ge=1)  # working days before an unanswered ask is silent


class WindowPriors(Frozen):
    """How far back a digest window starts, in working days before the digest day."""

    first_working_days: int = Field(default=5, ge=1)  # a first digest
    max_working_days: int = Field(default=10, ge=1)  # a digest after an old previous digest

    @model_validator(mode="after")
    def _first_within_max(self) -> WindowPriors:
        if self.first_working_days > self.max_working_days:
            raise ValueError("first_working_days must be at most max_working_days")
        return self


# The ranking terms the ablation can switch off (SPEC section 7). recent_activity is the
# lift inside personal_relevance, not a factor of its own.
TermName = Literal["importance", "role_prior", "personal_relevance", "recent_activity"]


class RecentActivityPriors(Frozen):
    """The reader's own recent messages lift `personal_relevance` (SPEC section 5 step 6)."""

    working_days: int = Field(default=3, ge=1)  # the window, back from the digest day
    relevance: float = Field(default=0.8, gt=0.4, le=0.8)  # never above the affected-role level


class DailyPriors(Frozen):
    # Each value is above 0 and at most 1. The ranked lane uses 0.5 for a missing entry.
    role_priors: dict[Role, dict[ItemType, RoleWeight]] = Field(default_factory=dict)
    min_score: float = Field(default=0.15, ge=0)  # a ranked line below this is left out
    window: WindowPriors = WindowPriors()
    recent_activity: RecentActivityPriors = RecentActivityPriors()
    terms_off: frozenset[TermName] = frozenset()  # set by the ablation only; each is 1


class AblationPriors(Frozen):
    """The ranking ablation's keep rule (SPEC section 7)."""

    min_effect: float = Field(default=0.01, gt=0, le=1)  # a relative drop, 0.01 is 1%


class Priors(Frozen):
    triage: TriagePriors
    absence: AbsencePriors
    daily: DailyPriors = DailyPriors()
    ablation: AblationPriors = AblationPriors()


def load_priors(path: Path = DEFAULT_PRIORS_PATH) -> Priors:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)  # a stream, so a YAML error's mark names the file
    try:
        return Priors.model_validate(data)
    except ValidationError as error:
        error.add_note(f"file: {path}")
        raise

"""Constants the pipeline reads from `config/priors.yaml` (SPEC section 5)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, ValidationError

from digest.models import Frozen, ItemType, Role

DEFAULT_PRIORS_PATH = Path("config/priors.yaml")


class TriagePriors(Frozen):
    miss_cost_ratio: float = Field(gt=0)  # a missed thread costs this many false keeps

    @property
    def keep_threshold(self) -> float:
        return 1 / (1 + self.miss_cost_ratio)


class AbsencePriors(Frozen):
    ask_reply_cadence_days: int = Field(ge=1)  # working days before an unanswered ask is silent


class DailyPriors(Frozen):
    role_priors: dict[Role, dict[ItemType, float]] = Field(default_factory=dict)  # 0.5 when absent
    min_score: float = Field(default=0.15, ge=0)  # a ranked line below this is left out


class Priors(Frozen):
    triage: TriagePriors
    absence: AbsencePriors
    daily: DailyPriors = DailyPriors()


def load_priors(path: Path = DEFAULT_PRIORS_PATH) -> Priors:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)  # a stream, so a YAML error's mark names the file
    try:
        return Priors.model_validate(data)
    except ValidationError as error:
        error.add_note(f"file: {path}")
        raise

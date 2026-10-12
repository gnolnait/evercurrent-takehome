"""Constants the pipeline reads from `config/priors.yaml` (SPEC section 5)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field

from digest.models import Frozen

DEFAULT_PRIORS_PATH = Path("config/priors.yaml")


class TriagePriors(Frozen):
    miss_cost_ratio: float = Field(gt=0)  # a missed thread costs this many false keeps

    @property
    def keep_threshold(self) -> float:
        return 1 / (1 + self.miss_cost_ratio)


class AbsencePriors(Frozen):
    ask_reply_cadence_days: int = Field(ge=1)  # working days before an unanswered ask is silent


class Priors(Frozen):
    triage: TriagePriors
    absence: AbsencePriors


def load_priors(path: Path = DEFAULT_PRIORS_PATH) -> Priors:
    return Priors.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

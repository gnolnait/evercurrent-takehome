"""Ranking metrics and the ranking ablation (plan docs/plans/m4-ranking-eval.md).

Must-see recall at 5 and nDCG at 5 per persona per day, against the golden labels of the
world script, for the configured ranker, each score term switched off, and two baselines.
The labels come from the world script, and the personas are simulated.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from digest.llm.client import LLM
from digest.models import Frozen
from digest.priors import Priors

Verdict = Literal["keep", "remove", "no vote"]


class Scores(Frozen):
    recall_at_5: float | None
    recall_n: int  # persona-days with a value
    ndcg_at_5: float | None
    ndcg_n: int
    recall_without: int  # persona-days with no must-see fact
    ndcg_without: int  # persona-days with no fact of grade above 0
    recall_personas: int  # personas in the recall mean
    ndcg_personas: int  # personas in the nDCG mean


class PersonaScores(Frozen):
    user_id: str
    name: str
    role: str
    overall: Scores
    ranked_lane: Scores
    must_see_lane_facts: int  # must-see facts C0 delivers through the must-see lane
    ranked_lane_facts: int  # must-see facts C0 delivers through the ranked lane


class ConfigScores(Frozen):
    name: str
    terms_off: tuple[str, ...]
    overall: Scores
    ranked_lane: Scores
    changed_days: int  # persona-days whose overall or ranked-lane top 5 differs from C0
    per_persona: tuple[PersonaScores, ...]


class AblationRow(Frozen):
    term: str
    drops: dict[str, float | None]  # metric name -> relative drop; None: no vote
    verdict: Verdict


class RankingReport(Frozen):
    configs: tuple[ConfigScores, ...]  # C0, C1 to C4, role-only, send-everything
    ablation: tuple[AblationRow, ...]
    min_effect: float


def recall_at_5(lines: Sequence[int], facts: Sequence[int]) -> float | None:
    """Grade-2 lines in the first five over grade-2 facts; None without one (plan R5)."""
    raise NotImplementedError


def ndcg_at_5(lines: Sequence[int], facts: Sequence[int]) -> float | None:
    """DCG of the first five line grades over the best DCG of the facts (plan R6)."""
    raise NotImplementedError


def verdict(
    term: str,
    c0: Mapping[str, float | None],
    off: Mapping[str, float | None],
    min_effect: float,
) -> AblationRow:
    """Keep, remove or no vote for one term, from each metric's relative drop (plan R8)."""
    raise NotImplementedError


def run_ranking_eval(world_dir: Path, llm: LLM, priors: Priors, store_path: Path) -> RankingReport:
    """Run the world day by day in a fresh store and score every configuration (plan R1 to R7b)."""
    raise NotImplementedError

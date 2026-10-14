"""Ranking metrics and the ranking ablation (plan docs/plans/m4-ranking-eval.md).

Must-see recall at 5 and nDCG at 5 per persona per day, against the golden labels of the
world script, for the configured ranker, each score term switched off, and two baselines.
The labels come from the world script, and the personas are simulated.
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from digest.daily.profile import Profile, can_see, profile_for
from digest.daily.render import newest_evidence, select_digest
from digest.daily.select import Line, Selection, window_start
from digest.eval.extraction import (
    _absence_key,
    _AbsenceKey,
    _pairs,
    _run_days,
    _table,
    _thread_of,
)
from digest.llm.client import LLM
from digest.models import Frozen, Item, micros_since_epoch
from digest.priors import Priors, TermName
from digest.store import Store
from digest.world.golden import Golden, read_golden
from digest.world.load import load_world
from digest.world.manifest import load_manifest
from digest.world.schema import World

Verdict = Literal["keep", "remove", "no vote"]

# The selected configurations, C0 first: each term off in turn, then the role-only baseline.
CONFIGS: tuple[tuple[str, frozenset[TermName]], ...] = (
    ("full", frozenset()),
    ("importance", frozenset({"importance"})),
    ("role_prior", frozenset({"role_prior"})),
    ("personal_relevance", frozenset({"personal_relevance"})),
    ("recent_activity", frozenset({"recent_activity"})),
    ("role-only", frozenset({"importance", "personal_relevance"})),
)
SEND_EVERYTHING = "send-everything"
METRICS = ("overall recall", "overall nDCG", "ranked-lane recall", "ranked-lane nDCG")

# A golden item's event id, or the key of a golden absence.
_FactKey = str | _AbsenceKey
# One persona-day: overall recall and nDCG, then ranked-lane recall and nDCG.
_DayValues = tuple[float | None, float | None, float | None, float | None]


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
    must_see = sum(1 for grade in facts if grade == 2)
    if not must_see:
        return None
    return sum(1 for grade in lines[:5] if grade == 2) / must_see


def ndcg_at_5(lines: Sequence[int], facts: Sequence[int]) -> float | None:
    """DCG of the first five line grades over the best DCG of the facts (plan R6)."""
    ideal = _dcg(sorted(facts, reverse=True))
    if not ideal:
        return None
    return _dcg(lines) / ideal


def _dcg(grades: Sequence[int]) -> float:
    return sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades[:5], 1))


def verdict(
    term: str,
    c0: Mapping[str, float | None],
    off: Mapping[str, float | None],
    min_effect: float,
) -> AblationRow:
    """Keep, remove or no vote for one term, from each metric's relative drop (plan R8)."""
    drops: dict[str, float | None] = {}
    for metric, base in c0.items():
        value = off[metric]
        drops[metric] = None if not base or value is None else (base - value) / base
    votes = [drop >= min_effect for drop in drops.values() if drop is not None]
    result: Verdict = "no vote" if not votes else "keep" if any(votes) else "remove"
    return AblationRow(term=term, drops=drops, verdict=result)


def write_ranking_section(report: RankingReport) -> str:
    """The "Ranking" section of the eval report, as Markdown (plan R9)."""
    metrics = ("recall", "nDCG", "ranked-lane recall", "ranked-lane nDCG")
    counts = ("n", "without", "personas")
    unchanged = [
        f"`{config.name}` changes no top 5 on this world, so it cannot move any metric."
        for config in report.configs[1:]
        if not config.changed_days
    ]
    c0 = report.configs[0]
    return "\n".join(
        [
            *_table(
                "Ranking",
                (
                    "configuration",
                    *(cell for metric in metrics for cell in (metric, *counts)),
                    "changed days",
                ),
                [
                    (
                        config.name,
                        *_score_cells(config.overall, personas=True),
                        *_score_cells(config.ranked_lane, personas=True),
                        str(config.changed_days),
                    )
                    for config in report.configs
                ],
            ),
            "Must-see recall at 5 and nDCG at 5 per persona per day, against the golden labels."
            " A value is the mean over personas of each persona's mean over its days with a"
            " value. `n` counts the persona-days with a value, `without` the persona-days with"
            " none, and `personas` the personas in the mean.",
            "",
            "The ranked lane leaves out the must-see lines. Its facts leave out the facts that"
            " the must-see lane of `full` delivers. `changed days` counts the persona-days whose"
            " top 5 or ranked-lane top 5 differs from `full`. `send-everything` has no must-see"
            " lane, so its ranked lane is the whole list.",
            "",
            *(line for text in unchanged for line in (text, "")),
            "The must-see rule shows every checklist absence to the EM. The golden audience of a"
            " checklist absence is the role of the task owner, so these EM lines have grade 0."
            " This is a disagreement between the labels and the must-see rule.",
            "",
            *_table(
                "Ranking ablation",
                ("term", *(f"{metric} drop" for metric in METRICS), "verdict"),
                [
                    (row.term, *(_number(drop) for drop in row.drops.values()), row.verdict)
                    for row in report.ablation
                ],
            ),
            f"Keep rule: a term stays when a metric drops by at least {report.min_effect} of"
            " its value with the term on.",
            "",
            "A drop is (full - off) / full. A metric whose `full` value is 0 or n/a gives no vote.",
            "",
            *_table(
                "Ranking per persona (full)",
                (
                    "persona",
                    "role",
                    *(cell for metric in metrics for cell in (metric, "n")),
                    "must-see facts, must-see lane",
                    "must-see facts, ranked lane",
                ),
                [
                    (
                        f"{row.name} ({row.user_id})",
                        row.role,
                        *_score_cells(row.overall, personas=False),
                        *_score_cells(row.ranked_lane, personas=False),
                        str(row.must_see_lane_facts),
                        str(row.ranked_lane_facts),
                    )
                    for row in c0.per_persona
                ],
            ),
            "Labels come from the world script; personas are simulated.",
            "",
        ]
    )


def _score_cells(scores: Scores, *, personas: bool) -> list[str]:
    """Recall and nDCG with their n; with `personas`, also the without and persona counts."""
    cells: list[str] = []
    for value, n, without, count in (
        (scores.recall_at_5, scores.recall_n, scores.recall_without, scores.recall_personas),
        (scores.ndcg_at_5, scores.ndcg_n, scores.ndcg_without, scores.ndcg_personas),
    ):
        cells += [_number(value), str(n), *((str(without), str(count)) if personas else ())]
    return cells


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def run_ranking_eval(world_dir: Path, llm: LLM, priors: Priors, store_path: Path) -> RankingReport:
    """Run the world day by day in a fresh store and score every configuration (plan R1 to R7b)."""
    world = load_world(world_dir / "world.yaml")
    manifest = load_manifest(world_dir / "manifest.json")
    golden = read_golden(world_dir / "golden.jsonl")
    asks = {_thread_of(item): item.event_id for item in golden.items if item.type == "ask"}
    cited = {row.cites: manifest.threads[row.cites].channel_id for row in golden.absences}
    readers = [persona.user_id for persona in world.personas if persona.role is not None]
    profiles = {user: profile_for(world, user) for user in readers}
    names = [name for name, _ in CONFIGS] + [SEND_EVERYTHING]
    config_priors = {
        name: priors.model_copy(
            update={"daily": priors.daily.model_copy(update={"terms_off": off})}
        )
        for name, off in CONFIGS
    }
    days: dict[str, dict[str, list[_DayValues]]] = {
        name: {user: [] for user in readers} for name in names
    }
    changed = dict.fromkeys(names, 0)
    lane_facts = {user: [0, 0] for user in readers}
    zone = ZoneInfo(world.timezone)

    with Store.open(store_path) as store:

        def score_day(day: int, today: date, as_of: datetime) -> None:
            previous = world.date_of(day - 1) if day > 1 else None
            items = store.items(as_of)
            by_thread: dict[str, list[Item]] = {}
            for item in items:
                by_thread.setdefault(item.thread_id, []).append(item)
            event_of = {
                item.item_id: event_id for event_id, item in _pairs(golden.items, by_thread).items()
            }
            newest = newest_evidence(store, items, as_of)

            def fact_key(item: Item) -> _FactKey | None:
                if item.absence is not None:
                    return _absence_key(day, item, asks)
                return event_of.get(item.item_id)

            for user in readers:
                profile = profiles[user]
                start = window_start(priors.daily.window, today, previous)
                facts = _facts(golden, world, profile, cited, start, today)
                selections = {
                    name: select_digest(
                        store, world, config_priors[name], user, today, previous_digest=previous
                    )
                    for name, _ in CONFIGS
                }
                keys = {
                    name: [(fact_key(line.item), line.must_see) for line in selection.lines]
                    for name, selection in selections.items()
                }
                full = keys["full"]
                grades = _grades([key for key, _ in full], facts)
                for (_, must_see), grade in zip(full, grades, strict=True):
                    if grade == 2:
                        lane_facts[user][0 if must_see else 1] += 1
                lane_set = {
                    key: grade
                    for key, grade in facts.items()
                    if key not in {key for key, must_see in full if must_see}
                }
                full_ids = _top5_ids(selections["full"].lines)
                for name, selection in selections.items():
                    days[name][user].append(_day_values(keys[name], facts, lane_set))
                    changed[name] += _top5_ids(selection.lines) != full_ids
                send = _send_everything(
                    world, profile, items, newest, selections["full"], start, today, zone
                )
                days[SEND_EVERYTHING][user].append(
                    _day_values([(fact_key(item), False) for item in send], facts, lane_set)
                )
                send5 = tuple(item.item_id for item in send[:5])
                changed[SEND_EVERYTHING] += send5 != full_ids[0] or send5 != full_ids[1]

        _run_days(store, world, world_dir, llm, priors, asks, after_extract=score_day)

    personas = {persona.user_id: persona for persona in world.personas}
    terms = dict(CONFIGS)
    configs: list[ConfigScores] = []
    for name in names:
        rows = tuple(
            PersonaScores(
                user_id=user,
                name=personas[user].name,
                role=profiles[user].role,
                overall=_persona_scores([(d[0], d[1]) for d in days[name][user]]),
                ranked_lane=_persona_scores([(d[2], d[3]) for d in days[name][user]]),
                must_see_lane_facts=lane_facts[user][0],
                ranked_lane_facts=lane_facts[user][1],
            )
            for user in readers
        )
        configs.append(
            ConfigScores(
                name=name,
                terms_off=tuple(sorted(terms.get(name, frozenset()))),
                overall=_mean_scores([row.overall for row in rows]),
                ranked_lane=_mean_scores([row.ranked_lane for row in rows]),
                changed_days=changed[name],
                per_persona=rows,
            )
        )
    c0 = _metric_values(configs[0])
    return RankingReport(
        configs=tuple(configs),
        ablation=tuple(
            verdict(config.name, c0, _metric_values(config), priors.ablation.min_effect)
            for config in configs[1:5]
        ),
        min_effect=priors.ablation.min_effect,
    )


def _facts(
    golden: Golden,
    world: World,
    profile: Profile,
    cited: Mapping[str, str],
    start: date,
    today: date,
) -> dict[_FactKey, int]:
    """The reader's facts in the window `start` (excluded) to `today`, with grades (plan R2)."""
    facts: dict[_FactKey, int] = {}
    for item in golden.items:
        if start < item.date <= today and can_see(world, profile, item.channel_id):
            facts[item.event_id] = (
                2
                if profile.user_id in item.must_see_for
                else 1
                if profile.role in item.affected_roles
                else 0
            )
    for row in golden.absences:
        if start < row.date <= today and can_see(world, profile, cited[row.cites]):
            reader = profile.user_id if row.source == "ask" else profile.role
            key = (row.day, row.source, row.task if row.source == "checklist" else row.cites)
            facts[key] = 2 if reader in row.audience else 0
    return facts


def _grades(keys: Sequence[_FactKey | None], facts: Mapping[_FactKey, int]) -> list[int]:
    """Each line's grade: its fact's grade on the first line that delivers it, else 0 (plan R3)."""
    seen: set[_FactKey] = set()
    grades: list[int] = []
    for key in keys:
        grades.append(0 if key is None or key in seen else facts.get(key, 0))
        if key is not None:
            seen.add(key)
    return grades


def _day_values(
    lines: Sequence[tuple[_FactKey | None, bool]],
    facts: Mapping[_FactKey, int],
    lane_set: Mapping[_FactKey, int],
) -> _DayValues:
    """Overall and ranked-lane recall and nDCG of one persona-day (plan R5, R6 and R6a)."""
    overall = _grades([key for key, _ in lines], facts)
    ranked = _grades([key for key, must_see in lines if not must_see], lane_set)
    return (
        recall_at_5(overall, list(facts.values())),
        ndcg_at_5(overall, list(facts.values())),
        recall_at_5(ranked, list(lane_set.values())),
        ndcg_at_5(ranked, list(lane_set.values())),
    )


def _top5_ids(lines: Sequence[Line]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Item ids of the overall top 5 and of the ranked-lane top 5, in digest order."""
    ranked = [line for line in lines if not line.must_see]
    return (
        tuple(line.item.item_id for line in lines[:5]),
        tuple(line.item.item_id for line in ranked[:5]),
    )


def _send_everything(
    world: World,
    profile: Profile,
    items: Sequence[Item],
    newest: Mapping[str, int],
    full: Selection,
    start: date,
    today: date,
    zone: ZoneInfo,
) -> list[Item]:
    """The send-everything list: C0's absence items, then every visible item new in the window.

    Absence items come by item id. The other items come newest evidence first, then by item id.
    """
    low = micros_since_epoch(datetime.combine(start, time.max, zone))
    high = micros_since_epoch(datetime.combine(today, time.max, zone))
    absences = sorted(
        (line.item for line in full.lines if line.item.absence is not None),
        key=lambda item: item.item_id,
    )
    shown = sorted(
        (
            item
            for item in items
            if low < newest[item.item_id] <= high
            and can_see(world, profile, item.thread_id.partition(":")[0])
        ),
        key=lambda item: (-newest[item.item_id], item.item_id),
    )
    return [*absences, *shown]


def _persona_scores(values: Sequence[tuple[float | None, float | None]]) -> Scores:
    """One persona's mean recall and nDCG over its days with a value."""
    recalls = [recall for recall, _ in values if recall is not None]
    ndcgs = [ndcg for _, ndcg in values if ndcg is not None]
    return Scores(
        recall_at_5=statistics.mean(recalls) if recalls else None,
        recall_n=len(recalls),
        ndcg_at_5=statistics.mean(ndcgs) if ndcgs else None,
        ndcg_n=len(ndcgs),
        recall_without=len(values) - len(recalls),
        ndcg_without=len(values) - len(ndcgs),
        recall_personas=int(bool(recalls)),
        ndcg_personas=int(bool(ndcgs)),
    )


def _mean_scores(rows: Sequence[Scores]) -> Scores:
    """The mean of the persona means that have a value, with summed counts (plan R7)."""
    recalls = [row.recall_at_5 for row in rows if row.recall_at_5 is not None]
    ndcgs = [row.ndcg_at_5 for row in rows if row.ndcg_at_5 is not None]
    return Scores(
        recall_at_5=statistics.mean(recalls) if recalls else None,
        recall_n=sum(row.recall_n for row in rows),
        ndcg_at_5=statistics.mean(ndcgs) if ndcgs else None,
        ndcg_n=sum(row.ndcg_n for row in rows),
        recall_without=sum(row.recall_without for row in rows),
        ndcg_without=sum(row.ndcg_without for row in rows),
        recall_personas=len(recalls),
        ndcg_personas=len(ndcgs),
    )


def _metric_values(config: ConfigScores) -> dict[str, float | None]:
    """The four ablation metrics of a configuration, keyed as METRICS names them."""
    return dict(
        zip(
            METRICS,
            (
                config.overall.recall_at_5,
                config.overall.ndcg_at_5,
                config.ranked_lane.recall_at_5,
                config.ranked_lane.ndcg_at_5,
            ),
            strict=True,
        )
    )

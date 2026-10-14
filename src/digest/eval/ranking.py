"""Ranking metrics and the ranking ablation (plan docs/plans/m4-ranking-eval.md).

Must-see recall at 5 and nDCG at 5 per persona per day, against the golden labels of the
world script, for the configured ranker, each score term switched off, and two baselines.
The labels come from the world script, and the personas are simulated.
"""

import math
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from digest.daily.profile import Profile, can_see, profile_for
from digest.daily.render import newest_evidence, select_digest
from digest.daily.select import Selection, window_start
from digest.eval.extraction import (
    _absence_key,
    _AbsenceKey,
    _qualifies,
    _row,
    _run_days,
    _thread_of,
)
from digest.llm.client import LLM
from digest.models import Frozen, Item, micros_since_epoch
from digest.priors import Priors, TermName
from digest.store import Store
from digest.world.golden import Golden, GoldenItem, read_golden
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
NAMES = (*(name for name, _ in CONFIGS), SEND_EVERYTHING)
METRICS = ("overall recall", "overall nDCG", "ranked-lane recall", "ranked-lane nDCG")

# A golden item's event id, or the key of a golden absence.
_FactKey = str | _AbsenceKey
# One persona-day: overall recall and nDCG, then ranked-lane recall and nDCG.
_DayValues = tuple[float | None, float | None, float | None, float | None]
# Item ids of the overall top 5 and of the ranked-lane top 5.
_Top5 = tuple[tuple[str, ...], tuple[str, ...]]
# The facts an item can deliver, in golden order.
_Candidates = Callable[[Item], Sequence[_FactKey]]


class RankingRefused(ValueError):
    """select_digest refused a day, so the ranking eval cannot run (plan R7b)."""


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
    moved: dict[str, int]  # metric name -> persona-days whose value differs from C0 (plan R8a)
    verdict: Verdict


class BelowThreshold(Frozen):
    """C0's must-see facts in the ranked lane that score below the configured min_score (plan R11)."""

    type: str
    severity: str
    count: int


class RankingReport(Frozen):
    configs: tuple[ConfigScores, ...]  # C0, C1 to C4, role-only, send-everything
    ablation: tuple[AblationRow, ...]
    min_effect: float
    min_score: float  # the configured daily.min_score
    zero_threshold: tuple[ConfigScores, ...]  # the seven configurations at min_score 0 (plan R10)
    zero_ablation: tuple[AblationRow, ...]
    below_threshold: tuple[BelowThreshold, ...]  # by count descending, then type, then severity
    must_see_facts: int  # must-see facts in the readers' sets over all persona-days


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
    *,
    moved: Mapping[str, int] | None = None,
) -> AblationRow:
    """Keep, remove or no vote for one term, from each metric's relative drop (plan R8).

    `moved` holds, per metric, the persona-days whose value differs from C0 (plan R8a).
    """
    drops: dict[str, float | None] = {}
    for metric, base in c0.items():
        value = off[metric]
        drops[metric] = None if not base or value is None else (base - value) / base
    votes = [drop >= min_effect for drop in drops.values() if drop is not None]
    result: Verdict = "no vote" if not votes else "keep" if any(votes) else "remove"
    return AblationRow(term=term, drops=drops, moved=dict(moved or {}), verdict=result)


def write_ranking_section(report: RankingReport) -> str:
    """The "Ranking" section of the eval report, as Markdown (plan R9 to R11)."""
    return "\n".join(
        [
            "## Ranking",
            "",
            *_config_grid(report.configs),
            "Must-see recall at 5 and nDCG at 5 per persona per day, against the golden labels."
            " A value is the mean over personas of each persona's mean over its days with a"
            " value. `n` counts the persona-days with a value, `without` the persona-days with"
            " none, and `personas` the personas in the mean.",
            "",
            "The ranked lane leaves out the must-see lines. Its facts leave out the facts that"
            " the must-see lane of `full` delivers, and before the cut at five it leaves out"
            " the lines that deliver one of them. `changed days` counts the persona-days whose"
            " top 5 or ranked-lane top 5 differs from `full`. `send-everything` has no must-see"
            " lane, so its ranked lane is the whole list without those lines.",
            "",
            *_unchanged(report.configs),
            "The must-see rule shows every checklist absence to the EM. The golden audience of a"
            " checklist absence is the role of the task owner, so these EM lines have grade 0."
            " This is a disagreement between the labels and the must-see rule.",
            "",
            "## Ranking ablation",
            "",
            *_ablation_grid(report.ablation),
            f"Keep rule: a term stays when a metric drops by at least {report.min_effect} of"
            " its value with the term on.",
            "",
            "A drop is (full - off) / full. A metric whose `full` value is 0 or n/a gives no"
            " vote. `moved` counts the persona-days whose value with the term off differs from"
            " `full`. The verdict is descriptive: `min_effect` is an effect size, not a test of"
            " uncertainty.",
            "",
            "## Ranking per persona (full)",
            "",
            *_grid(
                (
                    "persona",
                    "role",
                    *(cell for metric in _COLUMNS for cell in (metric, "n")),
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
                    for row in report.configs[0].per_persona
                ],
            ),
            "## With min_score 0 (admission fixed)",
            "",
            "The configured rows mix admission with order; these rows fix admission and show"
            " order only.",
            "",
            *_config_grid(report.zero_threshold),
            *_unchanged(report.zero_threshold),
            *_ablation_grid(report.zero_ablation),
            f"## Must-see facts below min_score {report.min_score}",
            "",
            *_grid(
                ("type", "severity", "count"),
                [(row.type, row.severity, str(row.count)) for row in report.below_threshold],
            ),
            f"`full` at min_score 0: the ranked-lane lines of grade 2 whose score is below"
            f" {report.min_score}, by item type and severity. The readers' sets hold"
            f" {report.must_see_facts} must-see facts over all persona-days.",
            "",
            "Labels come from the world script; personas are simulated.",
            "",
        ]
    )


_COLUMNS = ("recall", "nDCG", "ranked-lane recall", "ranked-lane nDCG")


def _config_grid(configs: Sequence[ConfigScores]) -> list[str]:
    return _grid(
        (
            "configuration",
            *(cell for metric in _COLUMNS for cell in (metric, "n", "without", "personas")),
            "changed days",
        ),
        [
            (
                config.name,
                *_score_cells(config.overall, personas=True),
                *_score_cells(config.ranked_lane, personas=True),
                str(config.changed_days),
            )
            for config in configs
        ],
    )


def _ablation_grid(rows: Sequence[AblationRow]) -> list[str]:
    return _grid(
        ("term", *(cell for metric in METRICS for cell in (f"{metric} drop", "moved")), "verdict"),
        [
            (
                row.term,
                *(
                    cell
                    for metric in METRICS
                    for cell in (_number(row.drops[metric]), str(row.moved[metric]))
                ),
                row.verdict,
            )
            for row in rows
        ],
    )


def _unchanged(configs: Sequence[ConfigScores]) -> list[str]:
    return [
        line
        for config in configs[1:]
        if not config.changed_days
        for line in (
            f"`{config.name}` changes no top 5 on this world, so it cannot move any metric.",
            "",
        )
    ]


def _grid(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [_row(header), _row(["---"] * len(header)), *(_row(row) for row in rows), ""]


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


@dataclass
class _Tally:
    """The day values, changed days and C0 lane facts of one threshold, per reader."""

    readers: Sequence[str]
    days: dict[str, dict[str, list[_DayValues]]] = field(default_factory=dict)
    changed: dict[str, int] = field(default_factory=dict)
    lane_facts: dict[str, list[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.days = {name: {user: [] for user in self.readers} for name in NAMES}
        self.changed = dict.fromkeys(NAMES, 0)
        self.lane_facts = {user: [0, 0] for user in self.readers}

    def add(
        self,
        user: str,
        lists: Mapping[str, Sequence[tuple[Item, bool]]],
        facts: Mapping[_FactKey, int],
        candidates: _Candidates,
    ) -> None:
        """Score one persona-day of every configuration; `lists` holds (item, must_see) lines."""
        c0 = lists["full"]
        delivered = _deliveries([item for item, _ in c0], candidates)
        removed = {
            fact
            for (_, must_see), fact in zip(c0, delivered, strict=True)
            if must_see and fact is not None and fact in facts
        }
        for (_, must_see), fact in zip(c0, delivered, strict=True):
            if fact is not None and facts.get(fact) == 2:
                self.lane_facts[user][0 if must_see else 1] += 1
        lane_set = {key: grade for key, grade in facts.items() if key not in removed}
        views = {
            name: _view(lines, facts, lane_set, removed, candidates)
            for name, lines in lists.items()
        }
        for name, (values, top5) in views.items():
            self.days[name][user].append(values)
            self.changed[name] += top5 != views["full"][1]

    def configs(
        self, personas: Mapping[str, str], profiles: Mapping[str, Profile]
    ) -> tuple[ConfigScores, ...]:
        terms = dict(CONFIGS)
        configs: list[ConfigScores] = []
        for name in NAMES:
            rows = tuple(
                PersonaScores(
                    user_id=user,
                    name=personas[user],
                    role=profiles[user].role,
                    overall=_persona_scores([(d[0], d[1]) for d in self.days[name][user]]),
                    ranked_lane=_persona_scores([(d[2], d[3]) for d in self.days[name][user]]),
                    must_see_lane_facts=self.lane_facts[user][0],
                    ranked_lane_facts=self.lane_facts[user][1],
                )
                for user in self.readers
            )
            configs.append(
                ConfigScores(
                    name=name,
                    terms_off=tuple(sorted(terms.get(name, frozenset()))),
                    overall=_mean_scores([row.overall for row in rows]),
                    ranked_lane=_mean_scores([row.ranked_lane for row in rows]),
                    changed_days=self.changed[name],
                    per_persona=rows,
                )
            )
        return tuple(configs)

    def ablation(
        self, configs: Sequence[ConfigScores], min_effect: float
    ) -> tuple[AblationRow, ...]:
        c0 = _metric_values(configs[0])
        return tuple(
            verdict(
                config.name,
                c0,
                _metric_values(config),
                min_effect,
                moved={
                    metric: sum(
                        full[index] != off[index]
                        for user in self.readers
                        for full, off in zip(
                            self.days["full"][user], self.days[config.name][user], strict=True
                        )
                    )
                    for index, metric in enumerate(METRICS)
                },
            )
            for config in configs[1:5]
        )


def run_ranking_eval(world_dir: Path, llm: LLM, priors: Priors, store_path: Path) -> RankingReport:
    """Run the world day by day in a fresh store and score every configuration (plan R1 to R11).

    Each configuration is selected twice: with the configured `min_score`, and with 0.
    """
    world = load_world(world_dir / "world.yaml")
    manifest = load_manifest(world_dir / "manifest.json")
    golden = read_golden(world_dir / "golden.jsonl")
    asks = {_thread_of(item): item.event_id for item in golden.items if item.type == "ask"}
    cited = {row.cites: manifest.threads[row.cites].channel_id for row in golden.absences}
    by_thread: dict[str, list[GoldenItem]] = {}
    for fact in golden.items:
        by_thread.setdefault(_thread_of(fact), []).append(fact)
    readers = [persona.user_id for persona in world.personas if persona.role is not None]
    profiles = {user: profile_for(world, user) for user in readers}
    # The configured min_score, then 0 (plan R10).
    runs = [
        (
            _Tally(readers),
            {
                name: priors.model_copy(
                    update={
                        "daily": priors.daily.model_copy(
                            update={"terms_off": off, "min_score": min_score}
                        )
                    }
                )
                for name, off in CONFIGS
            },
        )
        for min_score in (priors.daily.min_score, 0)
    ]
    below: Counter[tuple[str, str]] = Counter()
    must_see_facts = 0
    zone = ZoneInfo(world.timezone)

    with Store.open(store_path) as store:

        def score_day(day: int, today: date, as_of: datetime) -> None:
            nonlocal must_see_facts
            previous = world.date_of(day - 1) if day > 1 else None
            items = store.items(as_of)
            newest = newest_evidence(store, items, as_of)
            start = window_start(priors.daily.window, today, previous)

            def candidates(item: Item) -> list[_FactKey]:
                if item.absence is not None:
                    return [_absence_key(day, item, asks)]
                return [
                    fact.event_id
                    for fact in by_thread.get(item.thread_id, [])
                    if _qualifies(fact, item)
                ]

            for user in readers:
                profile = profiles[user]
                facts = _facts(golden, world, profile, cited, start, today)
                must_see_facts += sum(1 for grade in facts.values() if grade == 2)
                for zero, (tally, config_priors) in enumerate(runs):
                    selections = {
                        name: _select(store, world, config_priors[name], user, today, previous)
                        for name, _ in CONFIGS
                    }
                    send = _send_everything(
                        world, profile, items, newest, selections["full"], start, today, zone
                    )
                    lists = {
                        **{
                            name: [(line.item, line.must_see) for line in selection.lines]
                            for name, selection in selections.items()
                        },
                        SEND_EVERYTHING: [(item, False) for item in send],
                    }
                    tally.add(user, lists, facts, candidates)
                    if zero:
                        below.update(_below(selections["full"], facts, candidates, priors))

        _run_days(store, world, world_dir, llm, priors, asks, after_extract=score_day)

    names = {persona.user_id: persona.name for persona in world.personas}
    (configured, _), (zero, _) = runs
    configs = configured.configs(names, profiles)
    zero_configs = zero.configs(names, profiles)
    min_effect = priors.ablation.min_effect
    return RankingReport(
        configs=configs,
        ablation=configured.ablation(configs, min_effect),
        min_effect=min_effect,
        min_score=priors.daily.min_score,
        zero_threshold=zero_configs,
        zero_ablation=zero.ablation(zero_configs, min_effect),
        below_threshold=tuple(
            BelowThreshold(type=kind, severity=severity, count=count)
            for (kind, severity), count in sorted(
                below.items(), key=lambda entry: (-entry[1], *entry[0])
            )
        ),
        must_see_facts=must_see_facts,
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


def _deliveries(items: Sequence[Item], candidates: _Candidates) -> list[_FactKey | None]:
    """The fact each line delivers: its first candidate no earlier line delivered (plan R3)."""
    used: set[_FactKey] = set()
    delivered: list[_FactKey | None] = []
    for item in items:
        match = next((fact for fact in candidates(item) if fact not in used), None)
        delivered.append(match)
        if match is not None:
            used.add(match)
    return delivered


def _grades(
    items: Sequence[Item], facts: Mapping[_FactKey, int], candidates: _Candidates
) -> list[int]:
    """Each line's grade: the grade of the fact it delivers in `facts`, else 0 (plan R3)."""
    return [0 if fact is None else facts.get(fact, 0) for fact in _deliveries(items, candidates)]


def _view(
    lines: Sequence[tuple[Item, bool]],
    facts: Mapping[_FactKey, int],
    lane_set: Mapping[_FactKey, int],
    removed: set[_FactKey],
    candidates: _Candidates,
) -> tuple[_DayValues, _Top5]:
    """One configuration's day values and top-5 ids (plan R5, R6 and R6a).

    The ranked lane drops, before the cut at five, the lines that deliver a fact of `removed`.
    Deliveries are assigned once on the whole ranked list; the kept lines keep their facts.
    """
    overall = [item for item, _ in lines]
    ranked = [item for item, must_see in lines if not must_see]
    kept = [
        (item, fact)
        for item, fact in zip(ranked, _deliveries(ranked, candidates), strict=True)
        if fact is None or fact not in removed
    ]
    ranked = [item for item, _ in kept]
    overall_grades = _grades(overall, facts, candidates)
    ranked_grades = [0 if fact is None else lane_set.get(fact, 0) for _, fact in kept]
    values = (
        recall_at_5(overall_grades, list(facts.values())),
        ndcg_at_5(overall_grades, list(facts.values())),
        recall_at_5(ranked_grades, list(lane_set.values())),
        ndcg_at_5(ranked_grades, list(lane_set.values())),
    )
    return values, (
        tuple(item.item_id for item in overall[:5]),
        tuple(item.item_id for item in ranked[:5]),
    )


def _select(
    store: Store, world: World, priors: Priors, user: str, day: date, previous: date | None
) -> Selection:
    """The reader's selection; a refusal of select_digest raises RankingRefused (plan R7b)."""
    try:
        return select_digest(store, world, priors, user, day, previous_digest=previous)
    except ValueError as error:
        raise RankingRefused(str(error)) from error


def _below(
    full: Selection, facts: Mapping[_FactKey, int], candidates: _Candidates, priors: Priors
) -> Counter[tuple[str, str]]:
    """Grade-2 ranked-lane lines whose score is below the configured min_score (plan R11)."""
    delivered = _deliveries([line.item for line in full.lines], candidates)
    return Counter(
        (line.item.type, line.item.severity)
        for line, fact in zip(full.lines, delivered, strict=True)
        if not line.must_see
        and fact is not None
        and facts.get(fact) == 2
        and line.score < priors.daily.min_score
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

"""Unit 14: B14.1 to B14.7 of docs/plans/m4-ranking-eval.md (v0.2.1).

B14.1 to B14.3 test the pure helpers with numbers worked out by hand. B14.4 to B14.6 run the
ranking eval on the Kestrel world and the committed cassettes (replay mode, no network) and
compare the report with values read by hand from `golden.jsonl`, `world.yaml` and the digests
that `select_digest` gives on a store this file builds day by day. The expected values never
come from `digest.eval.ranking`.

The report holds one mean per persona and no per-day value. So B14.4 and B14.6 check a persona's
whole row. Each expected day value below comes from one persona-day worked out in the comments.
Each persona-day also pins its selected top 5 (the premise), so a changed recording fails on the
premise and not on a grade.

A run is lazy (`Run.report()`), so the stubs raise NotImplementedError inside each test and not
in a fixture. The spy assumes `ranking.py` imports `select_digest` at module level (AGENTS.md
asks for top-level imports).
"""

import math
import re
import shutil
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

import test_room
from digest.cli import app
from digest.daily.render import select_digest
from digest.daily.select import Selection
from digest.eval import ranking
from digest.eval.ranking import (
    ConfigScores,
    PersonaScores,
    RankingReport,
    Scores,
    ndcg_at_5,
    recall_at_5,
    verdict,
)
from digest.extract.stage import run_extract
from digest.llm.client import LLM, from_settings
from digest.priors import Priors
from digest.replay import replay_world
from digest.settings import Settings
from digest.store import Store
from digest.world.load import load_world
from digest.world.schema import World

# The room tests' fixtures. Plain assignments, so ruff sees no shadowed import.
priors = test_room.priors
world = test_room.world

ROOT = Path(__file__).parents[1]
WORLD_DIR = ROOT / "data" / "world" / "kestrel"
CASSETTES = ROOT / "data" / "cassettes"
TERMS = ("importance", "role_prior", "personal_relevance", "recent_activity")
# The six distinct `terms_off` sets that reach select_digest (C0, C1 to C4, role-only).
CONFIGS: dict[str, frozenset[str]] = {
    "full": frozenset(),
    "importance": frozenset({"importance"}),
    "role_prior": frozenset({"role_prior"}),
    "personal_relevance": frozenset({"personal_relevance"}),
    "recent_activity": frozenset({"recent_activity"}),
    "role-only": frozenset({"importance", "personal_relevance"}),
}
LABELS_LINE = "Labels come from the world script; personas are simulated."

L3 = math.log2(3)  # 1.58496, the discount at rank 2 is 1 / log2(3) after rank 1 has 1 / log2(2)
L5 = math.log2(5)  # 2.32193

# Item ids of the digest lines in the hand tables. They come from the day-by-day store.
EVT2_EXIT = "C0KPROG:1791823500.010005#1791823500.010005#0"
ECO_BOM = "C0PLMFD:1791819000.010000#1791819000.010000#0"
LIDAR = "C0ENGME:1791911662.000400#1791911662.000400#0"
PLM_11020C = "C0PLMFD:1791908400.010010#1791908400.010010#0"
THM_RUN1 = "C0ENGEF:1791911400.010019#1791911400.010019#0"
LTB = "C0SUPCH:1791909900.010014#1791909900.010014#0"
SLIP = "C0EXTCM:1791994800.010031#1791994800.010031#0"
EMC = "C0ENGEF:1791993300.010026#1791993300.010026#0"
PO = "C0SUPCH:1791991800.010021#1791991800.010021#0"
ASK_SAF = "C0ENGEF:1791997800.010040#1791997800.010040#0"
BOM_11034 = "C0DVT1B:1792081200.010045#1792081200.010045#0"
MLCC = "C0SUPCH:1792079700.010041#1792079700.010041#0"
FAI = "C0EXTCM:1792164600.010061#1792164600.010061#0"
HARNESS = "C0DVT1B:1792426800.010087#1792426800.010087#0"
UN383 = "C0SUPCH:1792425300.010082#1792425300.010082#0"
BUFFER = "C0KPROG:1792423800.010077#1792423800.010077#0"
DFM = "C0KPROG:1792423800.010077#1792423800.010077#1"
BRACKET = "C0ENGME:1792513200.010102#1792513200.010102#0"
LOT = "C0DVT1B:1792510200.010093#1792510440.010094#0"
TEXTURE = "C0ENGME:1792511700.010098#1792511700.010098#0"
FLAG_ASK = "C0ENGME:1792513200.010102#1792513680.010105#0"
DEBOUNCE = "C0ENGEF:1792596600.010107#1792596600.010107#0"
FILTER = "C0ENGEF:1792598100.010111#1792598100.010111#0"
BENCH_ASK = "C0ENGEF:1792598100.010111#1792599180.010115#0"
GO = "C0KPROG:1792683000.010116#1792683000.010116#0"
ASK_SAF_ABSENCE = f"absence:ask:{ASK_SAF}"
FAI_ABSENCE = "absence:G-DVT1.fai"

Top5 = list[tuple[str, bool]]  # (item_id, must_see) of the first five lines of a digest
# (user_id, world day, config name in CONFIGS) -> the selection of that reader and day
Built = dict[tuple[str, int, str], Selection]


# ---------------------------------------------------------------------------------------------
# B14.1 and B14.2: the metric helpers. The arithmetic is in the comment of each row.
# Gain 2^g - 1 (grade 1 gives 1, grade 2 gives 3); discount 1 / log2(rank + 1).
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "facts", "expected"),
    [
        # Ideal order: DCG equals the ideal DCG, so the ratio is 1.
        pytest.param([2, 1, 0, 0, 0], [2, 1, 0, 0, 0], 1.0, id="ideal-order"),
        # Facts in any order: the ideal sorts them. Same ideal as above.
        pytest.param([2, 1, 0], [0, 1, 2], 1.0, id="facts-unsorted"),
        # The best fact at rank 2 and the next best at rank 1:
        # DCG = 1/log2(2) + 3/log2(3) = 1 + 1.89279 = 2.89279; ideal = 3/1 + 1/log2(3) = 3.63093;
        # ratio 0.79671.
        pytest.param([1, 2], [2, 1], (1 + 3 / L3) / (3 + 1 / L3), id="best-fact-at-rank-2"),
        # Fewer than five lines: one line of grade 2. DCG = 3; ideal = 3 + 1/log2(3) = 3.63093;
        # ratio 0.82624.
        pytest.param([2], [2, 1], 3 / (3 + 1 / L3), id="one-line"),
        # One line of grade 1 at rank 2: DCG = 1/log2(3) = 0.63093; ideal = 1; ratio 0.63093.
        pytest.param([0, 1], [1], 1 / L3, id="grade-1-at-rank-2"),
        # No lines: DCG = 0, so 0 (the facts have a grade above 0, so there is a value).
        pytest.param([], [2], 0.0, id="no-lines"),
        # A line at rank 6 does not count: DCG = 0.
        pytest.param([0, 0, 0, 0, 0, 2], [2], 0.0, id="rank-6-ignored"),
        # More than five facts: the ideal takes the best five. Five lines of grade 2 equal it.
        pytest.param([2, 2, 2, 2, 2], [2, 2, 2, 2, 2, 2, 2], 1.0, id="ideal-takes-best-five"),
    ],
)
def test_ndcg_at_5_matches_the_hand_computed_value(
    lines: list[int], facts: list[int], expected: float
) -> None:
    """Setup: line and fact grades with a hand-worked DCG.

    Action: ndcg_at_5(lines, facts).
    Assert: the value equals DCG over the first five lines divided by the ideal DCG.
    """
    assert ndcg_at_5(lines, facts) == pytest.approx(expected)


@pytest.mark.parametrize("facts", [[0, 0, 0], [0], []], ids=["zeros", "one-zero", "no-facts"])
def test_ndcg_at_5_is_none_when_no_fact_has_a_grade_above_zero(facts: list[int]) -> None:
    """Setup: facts of grade 0 only, and lines that score.

    Action: ndcg_at_5.
    Assert: None (no ideal order exists), not 0.0 and not an error.
    """
    assert ndcg_at_5([2, 1, 0], facts) is None


@pytest.mark.parametrize(
    ("lines", "facts", "expected"),
    [
        # One grade-2 fact, its line at rank 1: 1 of 1.
        pytest.param([2, 0, 0, 0, 0], [2], 1.0, id="found"),
        # The grade-2 line is at rank 6: 0 of 1.
        pytest.param([0, 0, 0, 0, 0, 2], [2], 0.0, id="rank-6-does-not-count"),
        # The grade-2 line is at rank 5: 1 of 1.
        pytest.param([0, 0, 0, 0, 2], [2], 1.0, id="rank-5-counts"),
        # Two grade-2 lines in the top 5, three grade-2 facts, one fact of grade 1: 2 of 3.
        pytest.param([2, 0, 2, 0, 0], [2, 2, 2, 1, 0], 2 / 3, id="two-of-three"),
        # Grade-1 lines are not must-see: 0 of 1.
        pytest.param([1, 1, 1, 1, 1], [2, 1], 0.0, id="grade-1-lines-do-not-count"),
        # No lines at all: 0 of 1.
        pytest.param([], [2], 0.0, id="no-lines"),
    ],
)
def test_recall_at_5_counts_grade_2_lines_in_the_first_five(
    lines: list[int], facts: list[int], expected: float
) -> None:
    """Setup: line and fact grades with the grade-2 line inside or outside the first five.

    Action: recall_at_5(lines, facts).
    Assert: grade-2 lines in the first five over grade-2 facts.
    """
    assert recall_at_5(lines, facts) == pytest.approx(expected)


@pytest.mark.parametrize("facts", [[1, 1, 0], [0], []], ids=["grade-1-only", "zeros", "no-facts"])
def test_recall_at_5_is_none_when_no_fact_is_must_see(facts: list[int]) -> None:
    """Setup: facts without a grade-2 fact, and a grade-2 line (a line cannot make a fact).

    Action: recall_at_5.
    Assert: None, not 0.0 and not a division error.
    """
    assert recall_at_5([2, 0, 0], facts) is None


# ---------------------------------------------------------------------------------------------
# B14.3: the verdict. Relative drop = (c0 - off) / c0. Keep when the drop is at least min_effect.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("c0", "off", "min_effect", "drops", "expected"),
    [
        # recall 0.5 -> 0.49 is a drop of 0.01 / 0.5 = 2%, at least 1%: keep. nDCG is unchanged
        # (drop 0), which votes remove; one keep vote is enough.
        pytest.param(
            {"recall": 0.5, "ndcg": 0.6},
            {"recall": 0.49, "ndcg": 0.6},
            0.01,
            {"recall": 0.02, "ndcg": 0.0},
            "keep",
            id="two-percent-keeps",
        ),
        # recall 0.5 -> 0.4975 is a drop of 0.0025 / 0.5 = 0.5%, below 1%: remove.
        pytest.param(
            {"recall": 0.5, "ndcg": 0.6},
            {"recall": 0.4975, "ndcg": 0.6},
            0.01,
            {"recall": 0.005, "ndcg": 0.0},
            "remove",
            id="half-percent-removes",
        ),
        # 1.0 -> 0.75 is a drop of exactly 0.25 (binary fractions), equal to min_effect: keep.
        pytest.param(
            {"recall": 1.0},
            {"recall": 0.75},
            0.25,
            {"recall": 0.25},
            "keep",
            id="drop-equal-to-min-effect-keeps",
        ),
        # A rise: 0.5 -> 0.55 gives (0.5 - 0.55) / 0.5 = -10%, which votes remove.
        pytest.param(
            {"recall": 0.5},
            {"recall": 0.55},
            0.01,
            {"recall": -0.1},
            "remove",
            id="rise-removes",
        ),
        # C0 value 0: no vote from recall (None drop). nDCG drops 0.6 -> 0.5 = 16.7%: keep.
        pytest.param(
            {"recall": 0.0, "ndcg": 0.6},
            {"recall": 0.0, "ndcg": 0.5},
            0.01,
            {"recall": None, "ndcg": 1 / 6},
            "keep",
            id="zero-c0-gives-no-vote-others-decide",
        ),
        # C0 value missing (None): no vote from recall. nDCG drops 0.5%: remove.
        pytest.param(
            {"recall": None, "ndcg": 0.6},
            {"recall": 0.2, "ndcg": 0.597},
            0.01,
            {"recall": None, "ndcg": 0.005},
            "remove",
            id="missing-c0-gives-no-vote",
        ),
        # Every metric lacks a vote (one C0 value is 0, one is None): no vote at all.
        pytest.param(
            {"recall": 0.0, "ndcg": None},
            {"recall": 0.3, "ndcg": 0.3},
            0.01,
            {"recall": None, "ndcg": None},
            "no vote",
            id="no-metric-votes",
        ),
    ],
)
def test_verdict_applies_min_effect_to_the_relative_drop(
    c0: dict[str, float | None],
    off: dict[str, float | None],
    min_effect: float,
    drops: dict[str, float | None],
    expected: str,
) -> None:
    """Setup: C0 and switched-off metric values with a hand-worked relative drop.

    Action: verdict("importance", c0, off, min_effect).
    Assert: the row names the term, holds each metric's drop (None without a vote) and gives
    keep, remove or no vote.
    """
    row = verdict("importance", c0, off, min_effect)

    assert row.term == "importance"
    assert row.verdict == expected
    assert row.drops.keys() == drops.keys()
    for metric, drop in drops.items():
        if drop is None:
            assert row.drops[metric] is None
        else:
            assert row.drops[metric] == pytest.approx(drop)


# ---------------------------------------------------------------------------------------------
# The runs. A fixture builds the lazy object; the stub raises inside the test.
# ---------------------------------------------------------------------------------------------


@dataclass
class SelectCall:
    user_id: str
    day: date
    previous_digest: date | None
    priors: Priors


@dataclass
class Run:
    """One lazy `run_ranking_eval` on a world, with a spy on the `select_digest` it calls."""

    world_dir: Path
    store_path: Path
    llm: LLM
    priors: Priors
    wrap: Callable[[str, date, Selection], Selection] | None = None
    calls: list[SelectCall] = field(default_factory=list)
    seconds: float | None = None
    _report: RankingReport | None = None

    def report(self) -> RankingReport:
        if self._report is None:
            patch = pytest.MonkeyPatch()
            # raising=False: the stub module does not import select_digest yet.
            patch.setattr(ranking, "select_digest", self._spy, raising=False)
            try:
                start = perf_counter()
                self._report = ranking.run_ranking_eval(
                    self.world_dir, self.llm, self.priors, self.store_path
                )
                self.seconds = perf_counter() - start
            finally:
                patch.undo()
        return self._report

    def _spy(
        self,
        store: Store,
        world: World,
        priors: Priors,
        user_id: str,
        day: date,
        *,
        previous_digest: date | None = None,
    ) -> Selection:
        self.calls.append(SelectCall(user_id, day, previous_digest, priors))
        selection = select_digest(
            store, world, priors, user_id, day, previous_digest=previous_digest
        )
        return selection if self.wrap is None else self.wrap(user_id, day, selection)


@pytest.fixture(scope="module")
def llm() -> LLM:
    return from_settings(
        Settings(
            _env_file=None,
            mode="replay",
            cassettes=CASSETTES,
            ANTHROPIC_API_KEY=None,
            OPENROUTER_API_KEY=None,
        )
    )


@pytest.fixture(scope="module")
def base_run(tmp_path_factory: pytest.TempPathFactory, llm: LLM, priors: Priors) -> Run:
    path = tmp_path_factory.mktemp("ranking-base") / "ranking.sqlite"
    return Run(WORLD_DIR, path, llm, priors)


# The channel line of C0ENGME in world.yaml. The hidden-channel run makes it private.
MECH_CHANNEL_TAIL = 'mechanical FA"}'


@pytest.fixture(scope="module")
def hidden_run(tmp_path_factory: pytest.TempPathFactory, llm: LLM, priors: Priors) -> Run:
    """A copy of the world where C0ENGME is private; supply chain is not a member."""
    root = tmp_path_factory.mktemp("ranking-hidden")
    copy = root / "kestrel"
    copy.mkdir()
    for name in ("world.yaml", "events.jsonl", "manifest.json", "golden.jsonl", "anchor.jsonl"):
        shutil.copy2(WORLD_DIR / name, copy / name)
    text = (copy / "world.yaml").read_text(encoding="utf-8")
    assert text.count(MECH_CHANNEL_TAIL) == 1
    (copy / "world.yaml").write_text(
        text.replace(MECH_CHANNEL_TAIL, 'mechanical FA", private: true}'),
        encoding="utf-8",
        newline="\n",
    )
    changed = load_world(copy / "world.yaml")
    channel = next(c for c in changed.channels if c.id == "C0ENGME")
    supply = next(p for p in changed.personas if p.user_id == "U0SC01")
    assert channel.private and "C0ENGME" not in supply.channels
    return Run(copy, root / "ranking.sqlite", llm, priors)


def _duplicate_ask_line(user_id: str, day: date, selection: Selection) -> Selection:
    """On the firmware reader's day 3, show the must-see ask line twice, one after the other."""
    if user_id != "U0FW01" or day != date(2026, 10, 14):
        return selection
    first = selection.lines[0]
    assert first.item.item_id == ASK_SAF and first.must_see
    return selection.model_copy(update={"lines": (first, first, *selection.lines[1:])})


@pytest.fixture(scope="module")
def doubled_run(tmp_path_factory: pytest.TempPathFactory, llm: LLM, priors: Priors) -> Run:
    path = tmp_path_factory.mktemp("ranking-doubled") / "ranking.sqlite"
    return Run(WORLD_DIR, path, llm, priors, wrap=_duplicate_ask_line)


@pytest.fixture(scope="module")
def built(
    tmp_path_factory: pytest.TempPathFactory, world: World, priors: Priors, llm: LLM
) -> Built:
    """The world day by day in a store of this file: every reader's selection for each config.

    Key: (user_id, world day, config name in CONFIGS). The selection for a day uses the previous
    world day as the previous digest, and none on day 1, as `digest demo` does.
    """
    zone = ZoneInfo(world.timezone)
    purposes = {channel.id: channel.purpose for channel in world.channels}
    readers = [persona.user_id for persona in world.personas if persona.role is not None]
    selections: Built = {}
    path = tmp_path_factory.mktemp("ranking-built") / "kestrel.sqlite"
    with Store.open(path) as store:
        for day in range(1, world.days + 1):
            today = world.date_of(day)
            replay_world(store, WORLD_DIR, today)
            as_of = datetime.combine(today, time.max, zone).astimezone(UTC)
            run_extract(store, llm, world.glossary(), world.project, priors, as_of, zone, purposes)
            previous = world.date_of(day - 1) if day > 1 else None
            for name, off in CONFIGS.items():
                daily = priors.daily.model_copy(update={"terms_off": off})
                config_priors = priors.model_copy(update={"daily": daily})
                for user in readers:
                    selections[(user, day, name)] = select_digest(
                        store, world, config_priors, user, today, previous_digest=previous
                    )
    return selections


def _persona(config: ConfigScores, user_id: str) -> PersonaScores:
    return next(row for row in config.per_persona if row.user_id == user_id)


def _top5(built: Built, user: str, day: int) -> Top5:
    lines = built[(user, day, "full")].lines
    return [(line.item.item_id, line.must_see) for line in lines[:5]]


def _assert_scores(
    scores: Scores, recall: Sequence[float | None], ndcg: Sequence[float | None]
) -> None:
    """The persona's mean over the days with a value, the days with a value and those without."""
    recalls = [value for value in recall if value is not None]
    ndcgs = [value for value in ndcg if value is not None]
    assert scores.recall_n == len(recalls)
    assert scores.recall_without == len(recall) - len(recalls)
    assert scores.recall_at_5 == pytest.approx(sum(recalls) / len(recalls))
    assert scores.ndcg_n == len(ndcgs)
    assert scores.ndcg_without == len(ndcg) - len(ndcgs)
    assert scores.ndcg_at_5 == pytest.approx(sum(ndcgs) / len(ndcgs))


# ---------------------------------------------------------------------------------------------
# B14.4: the firmware engineer U0FW01 (Ana Folau), C0, ten days.
#
# Named persona-day 1: day 3 (2026-10-14), the must-see lane.
#   golden.jsonl facts of day 3: ASK-SAF (ask on C0ENGEF, must_see_for U0FW01) grade 2;
#   S12-SLIP (roles PM, EM, ME), S2-PO (supply_chain, EE), S6-EMC (EE, supply_chain, EM, PM):
#   grade 0 for the FW role. Fact grades [2, 0, 0, 0]; one must-see fact.
#   The digest: line 1 is the ASK-SAF ask in needs_you, a must-see line (an open ask addressed
#   to you), grade 2. Lines 2 to 4 are the S12-SLIP schedule change (a fact with grade 0 for FW)
#   and two blockers that no golden item pairs with (grade 0). Line grades [2, 0, 0, 0].
#   recall = 1 / 1 = 1. nDCG = DCG 3 / ideal DCG 3 = 1.
#   Ranked lane: the must-see lane delivers ASK-SAF, so the lane's set has no must-see fact
#   and no fact above 0: both ranked values have no value on this day.
#
# Named persona-day 2: day 9 (2026-10-22), the ranked lane.
#   Facts of day 9: S15-GO (decision, must_see_for lists U0FW01) grade 2; the ask absence of
#   ASK-SAF (golden absences, day 9, audience U0EM01 and U0FW01) grade 2; the checklist
#   absence G-DVT1.safety (audience role FW) grade 2; other absences are for other roles (0).
#   Fact grades [2, 2, 2]; three must-see facts.
#   The digest: line 1 is the Bench log ask (no golden fact, grade 0), line 2 is the ask
#   absence line for ASK-SAF (must-see lane, grade 2), line 3 is the S15-GO decision in the
#   ranked lane (grade 2), lines 4 and 5 are blockers without a fact in the window (grade 0).
#   Line grades [0, 2, 2, 0, 0]. recall = 2 / 3. nDCG = DCG (3 / log2 3 + 3 / log2 4) divided by
#   ideal DCG (3 + 3 / log2 3 + 3 / log2 4) = 3.39279 / 6.39279 = 0.53072.
#   Ranked lane: the lane's lines are S15-GO and four blockers, grades [2, 0, 0, 0, 0]; the
#   lane's set drops the ASK-SAF absence (the must-see lane delivers it), so its must-see facts
#   are S15-GO and the safety absence. recall = 1 / 2. nDCG = 3 / (3 + 3 / log2 3) = 0.61315.
#
# The other days, same method (facts from golden.jsonl, lines from the pinned top 5):
#   day  line grades   fact grades      recall  nDCG
#   1    [1]           [1,0,0,0]        none    1 / 1
#   2    [0,0,0]       [2,0,0,0]        0 / 1   0 / 3         (S14-LTB is not in the digest)
#   3    [2,0,0,0]     [2,0,0,0]        1 / 1   3 / 3
#   4    [0,0,0,0]     [0,0,0,0,0,0]    none    none          (the old ASK-SAF ask has grade 0)
#   5    [0,0,0,0,0]   [2,0,0,0,0]      0 / 1   0 / 3         (the safety absence is never shown)
#   6    [2,0,0,0,0]   [2,2,0...]       1 / 2   3 / (3 + 3 / log2 3)
#   7    [2,0,0,0,0]   [2,2,0...]       1 / 2   3 / (3 + 3 / log2 3)
#   8    [0,2,0,1,0]   [2,2,1,1,0..]    1 / 2   (3 / log2 3 + 1 / log2 5) / (3 + 3 / log2 3 + 1 / 2 + 1 / log2 5)
#   9    [0,2,2,0,0]   [2,2,2,0,0..]    2 / 3   (3 / log2 3 + 3 / 2) / (3 + 3 / log2 3 + 3 / 2)
#   10   [0,2,0,0,0]   [2,0]            1 / 1   (3 / log2 3) / 3
# Facts the extraction misses (S11-DEBOUNCE, S10-UN383) stay in the set with their grade: they
# are in the "missed golden events" row of reports/eval-2026-10-23.md, so no line delivers them.
# ---------------------------------------------------------------------------------------------

FW_TOP5: dict[int, Top5] = {
    1: [(EVT2_EXIT, False)],
    2: [(LIDAR, False), (PLM_11020C, False), (THM_RUN1, False)],
    3: [(ASK_SAF, True), (SLIP, False), (EMC, False), (PO, False)],
    4: [(ASK_SAF, True), (MLCC, False), (EMC, False), (PO, False)],
    5: [(ASK_SAF, True), (FAI, False), (MLCC, False), (EMC, False), (PO, False)],
    6: [(ASK_SAF_ABSENCE, True), (HARNESS, False), (UN383, False), (BUFFER, False), (DFM, False)],
    7: [(ASK_SAF_ABSENCE, True), (BRACKET, False), (LOT, False), (HARNESS, False), (UN383, False)],
    8: [
        (BENCH_ASK, True),
        (ASK_SAF_ABSENCE, True),
        (DEBOUNCE, False),
        (FILTER, False),
        (HARNESS, False),
    ],
    9: [(BENCH_ASK, True), (ASK_SAF_ABSENCE, True), (GO, False), (HARNESS, False), (UN383, False)],
    10: [
        (BENCH_ASK, True),
        (ASK_SAF_ABSENCE, True),
        (HARNESS, False),
        (UN383, False),
        (BUFFER, False),
    ],
}
FW_RECALL = [None, 0.0, 1.0, None, 0.0, 1 / 2, 1 / 2, 1 / 2, 2 / 3, 1.0]
FW_NDCG = [
    1.0,
    0.0,
    1.0,
    None,
    0.0,
    3 / (3 + 3 / L3),
    3 / (3 + 3 / L3),
    (3 / L3 + 1 / L5) / (3 + 3 / L3 + 1 / 2 + 1 / L5),
    (3 / L3 + 3 / 2) / (3 + 3 / L3 + 3 / 2),
    (3 / L3) / 3,
]
# Ranked lane, same days. Day 3 and day 10: the must-see lane delivers the only must-see fact,
# so the lane has no must-see fact (recall none) and no fact above 0 (nDCG none). Day 8: lane
# lines [0,1,0,0,0] against facts [2,1,1]: (1 / log2 3) / (3 + 1 / log2 3 + 1 / 2).
FW_RANKED_RECALL = [None, 0.0, None, None, 0.0, 0.0, 0.0, 0.0, 1 / 2, None]
FW_RANKED_NDCG = [
    1.0,
    0.0,
    None,
    None,
    0.0,
    0.0,
    0.0,
    (1 / L3) / (3 + 1 / L3 + 1 / 2),
    3 / (3 + 3 / L3),
    None,
]


def test_c0_persona_scores_match_golden_by_hand_for_two_named_persona_days(
    base_run: Run, built: Built
) -> None:
    """B14.4. Setup: the Kestrel world on cassettes; the firmware reader's ten days worked by hand.

    Action: run_ranking_eval; read the C0 row of U0FW01.
    Assert: recall and nDCG at 5, overall and ranked-lane, with their n and without counts, and
    the facts delivered per lane equal the mean of the hand-worked days (day 3: a must-see fact
    through the must-see lane; day 9: a must-see fact through the ranked lane). The pinned
    selection of each day is what the hand grades read.
    """
    report = base_run.report()

    fw = _persona(report.configs[0], "U0FW01")
    assert (fw.name, fw.role) == ("Ana Folau", "FW")
    _assert_scores(fw.overall, FW_RECALL, FW_NDCG)
    _assert_scores(fw.ranked_lane, FW_RANKED_RECALL, FW_RANKED_NDCG)
    # Must-see lane: ASK-SAF on day 3, then its ask absence on days 6 to 10. Ranked lane: S15-GO.
    assert (fw.must_see_lane_facts, fw.ranked_lane_facts) == (6, 1)
    for day, top5 in FW_TOP5.items():
        assert _top5(built, "U0FW01", day) == top5, f"premise: day {day}"


# ---------------------------------------------------------------------------------------------
# B14.5: the priors that reach select_digest, and the sensitivity count.
# ---------------------------------------------------------------------------------------------


def test_each_configuration_selects_with_its_named_terms_off(
    base_run: Run, priors: Priors, world: World
) -> None:
    """B14.5. Setup: a pass-through spy on the select_digest that ranking.py calls.

    Action: run_ranking_eval on the Kestrel world.
    Assert: the calls carry exactly the six `terms_off` sets of C0, C1 to C4 and role-only; each
    set covers all seven readers on all ten days with the previous world day as previous digest
    (none on day 1); no other priors field changes; the report names the configurations and
    their terms in order.
    """
    report = base_run.report()

    readers = [persona.user_id for persona in world.personas if persona.role is not None]
    assert len(readers) == 7
    every_day = {
        (user, world.date_of(day), world.date_of(day - 1) if day > 1 else None)
        for user in readers
        for day in range(1, world.days + 1)
    }
    assert base_run.calls, "ranking.py must call a module-level select_digest"
    seen: dict[frozenset[str], set[tuple[str, date, date | None]]] = {}
    for call in base_run.calls:
        seen.setdefault(call.priors.daily.terms_off, set()).add(
            (call.user_id, call.day, call.previous_digest)
        )
        cleared = call.priors.daily.model_copy(update={"terms_off": frozenset()})
        assert call.priors.model_copy(update={"daily": cleared}) == priors
    assert seen == dict.fromkeys(CONFIGS.values(), every_day)

    configs = report.configs
    assert len(configs) == 7
    assert (configs[0].name, configs[-2].name, configs[-1].name) == (
        "full",
        "role-only",
        "send-everything",
    )
    assert [set(config.terms_off) for config in configs[:5]] == [set(), *({t} for t in TERMS)]
    assert set(configs[-2].terms_off) == {"importance", "personal_relevance"}


def _ids(selection: Selection, *, ranked_only: bool) -> tuple[str, ...]:
    lines = [line for line in selection.lines if not (ranked_only and line.must_see)]
    return tuple(line.item.item_id for line in lines[:5])


def _changed_days(built: Built, name: str) -> int:
    """Persona-days whose overall top 5 or ranked-lane top 5 differs from C0 (item ids, in order)."""
    pairs = {(user, day) for user, day, config in built if config == "full"}
    return sum(
        1
        for user, day in pairs
        if any(
            _ids(built[(user, day, name)], ranked_only=lane)
            != _ids(built[(user, day, "full")], ranked_only=lane)
            for lane in (False, True)
        )
    )


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("importance", id="C1"),
        pytest.param("role_prior", id="C2"),
        pytest.param("personal_relevance", id="C3"),
        pytest.param("recent_activity", id="C4"),
        pytest.param("role-only", id="role-only"),
    ],
)
def test_changed_days_equals_the_count_from_comparing_the_top_5_lists(
    name: str, base_run: Run, built: Built
) -> None:
    """B14.5. Setup: this file's own selections for C0 and each config, day by day.

    Action: run_ranking_eval; count the persona-days whose overall or ranked-lane top 5 differs
    from C0's (item ids in order), 7 readers by 10 days.
    Assert: the config's changed_days equals that count. The count is above 0, so a stub that
    returns 0 cannot pass.
    """
    report = base_run.report()

    expected = _changed_days(built, name)
    assert 0 < expected < 70
    config = next(
        c for c in report.configs if set(c.terms_off) == set(CONFIGS[name]) and c.terms_off
    )
    assert config.changed_days == expected


# ---------------------------------------------------------------------------------------------
# B14.6: visibility, an old open blocker, a fact delivered twice.
# ---------------------------------------------------------------------------------------------

# The mechanical engineer U0ME01 (Nadia Brenner), C0, overall metrics. Premise: on day 5 the
# FAI blocker (golden S4-FAI, risk, must_see_for lists U0ME01) is a must-see line of grade 2.
# It stays open: on days 6 to 10 it is a must-see blocker line again, and on day 6 it is the
# FIRST line. Its fact is from day 5, outside the window of day 6, so that line has grade 0.
# Day 6 by hand: facts of day 6 for ME: S3-BUFFER (must_see_for lists U0ME01) grade 2; the rest
# 0. Top 5 grades [0, 0, 2, 0, 0]: FAI blocker 0, DFM blocker (no golden fact) 0, S3-BUFFER
# blocker 2, the old EMC blocker 0, the harness blocker (fact N4-MISSING, grade 0) 0.
# recall = 1 / 1; nDCG = (3 / log2 4) / 3 = 0.5. If the old blocker scored 2, day 6 recall
# would be 2 / 1.
# The other days (facts from golden.jsonl, lines from the pinned top 5):
#   day  line grades   fact grades   recall  nDCG
#   1    [1,0]         [1,1,1,0]     none    1 / (1 + 1 / log2 3 + 1 / 2)
#   2    [1,1,1,0]     [1,1,1,0]     none    1
#   3    [2,0,0]       [2,0,0,0]     1 / 1   1
#   4    [0,0,0,0]     [2,2,1,0..]   0 / 2   0
#   5    [2,0,0,0]     [2,2,0...]    1 / 2   3 / (3 + 3 / log2 3)
#   6    [0,0,2,0,0]   [2,0...]       1 / 1   0.5
#   7    [0,0,1,1,0]   [2,1,1,0..]   0 / 1   (1 / 2 + 1 / log2 5) / (3 + 1 / log2 3 + 1 / 2)
#   8    [0,2,1,0,0]   [2,1,0...]     1 / 1   (3 / log2 3 + 1 / 2) / (3 + 1 / log2 3)
#   9    [0,2,2,0,0]   [2,2,0...]     2 / 2   (3 / log2 3 + 3 / 2) / (3 + 3 / log2 3)
#   10   [0,0,0,0,0]   [0,0]          none    none
ME_TOP5: dict[int, Top5] = {
    1: [(EVT2_EXIT, False), (ECO_BOM, False)],
    2: [(LIDAR, False), (PLM_11020C, False), (THM_RUN1, False), (LTB, False)],
    3: [(SLIP, False), (EMC, False), (PO, False)],
    4: [(BOM_11034, False), (EMC, False), (MLCC, False), (PO, False)],
    5: [(FAI, True), (EMC, False), (MLCC, False), (PO, False)],
    6: [(FAI, True), (DFM, True), (BUFFER, False), (EMC, False), (HARNESS, False)],
    7: [(FLAG_ASK, True), (LOT, False), (BRACKET, False), (TEXTURE, True), (FAI, True)],
    8: [(FLAG_ASK, True), (FAI_ABSENCE, True), (FILTER, False), (DEBOUNCE, False), (TEXTURE, True)],
    9: [(FLAG_ASK, True), (FAI_ABSENCE, True), (GO, False), (TEXTURE, True), (FAI, True)],
    10: [(FLAG_ASK, True), (TEXTURE, True), (FAI, True), (DFM, True), (EMC, False)],
}
ME_RECALL = [None, None, 1.0, 0.0, 1 / 2, 1.0, 0.0, 1.0, 1.0, None]
ME_NDCG = [
    1 / (1 + 1 / L3 + 1 / 2),
    1.0,
    1.0,
    0.0,
    3 / (3 + 3 / L3),
    0.5,
    (1 / 2 + 1 / L5) / (3 + 1 / L3 + 1 / 2),
    (3 / L3 + 1 / 2) / (3 + 1 / L3),
    (3 / L3 + 3 / 2) / (3 + 3 / L3),
    None,
]


def test_an_open_blocker_from_before_the_window_scores_zero_on_a_later_day(
    base_run: Run, built: Built
) -> None:
    """B14.6. Setup: ME's S4-FAI blocker, a grade-2 delivery on day 5 and an open must-see line
    on days 6 to 10 (first line of day 6).

    Action: run_ranking_eval; read the C0 row of U0ME01.
    Assert: the mean over the ten days equals the hand-worked days, where the old blocker line
    scores 0. A set that kept facts from earlier days would give day 6 a recall of 2.
    """
    report = base_run.report()

    me = _persona(report.configs[0], "U0ME01")
    _assert_scores(me.overall, ME_RECALL, ME_NDCG)
    for day, top5 in ME_TOP5.items():
        assert _top5(built, "U0ME01", day) == top5, f"premise: day {day}"


def test_a_fact_on_a_channel_the_reader_cannot_see_is_not_in_the_reader_set(
    base_run: Run, hidden_run: Run
) -> None:
    """B14.6. Setup: a copy of the world where C0ENGME (eng-mech) is private; U0SC01 (supply
    chain) is not a member. On day 2 the S1-AGREE decision (C0ENGME, must_see_for lists
    U0SC01) is a fact of grade 2 that the reader delivered in the base run.

    Action: run_ranking_eval on both worlds; compare the C0 row of U0SC01.
    Assert: against the base run, only day 2 changes. Base day 2: lines [0,1,2,0,0] against
    facts [2,1] (recall 1 / 1, nDCG (1 / log2 3 + 3 / 2) / (3 + 1 / log2 3) = 0.58688; ranked
    lane lines [1,2,0,0], recall 1 / 1, nDCG (1 + 3 / log2 3) / (3 + 1 / log2 3) = 0.79671).
    Hidden day 2: S1-AGREE leaves the set and the line leaves the digest. Lines [0,1,0,0]
    against facts [1]: recall has no must-see fact (no value), nDCG = (1 / log2 3) / 1 = 0.63093;
    ranked lane lines [1,0,0], recall no value, nDCG = 1 / 1 = 1.
    """
    base = _persona(base_run.report().configs[0], "U0SC01")
    hidden = _persona(hidden_run.report().configs[0], "U0SC01")

    def total(scores: Scores, metric: str) -> float:
        mean, n = (
            (scores.recall_at_5, scores.recall_n)
            if metric == "recall"
            else (scores.ndcg_at_5, scores.ndcg_n)
        )
        assert mean is not None
        return mean * n

    for lane in ("overall", "ranked_lane"):
        before, after = getattr(base, lane), getattr(hidden, lane)
        # Recall: day 2 had a value of 1 and now has none.
        assert after.recall_n == before.recall_n - 1
        assert after.recall_without == before.recall_without + 1
        assert total(after, "recall") == pytest.approx(total(before, "recall") - 1.0)
        # nDCG: day 2 keeps a value; only its number changes.
        assert (after.ndcg_n, after.ndcg_without) == (before.ndcg_n, before.ndcg_without)
    overall_move = 1 / L3 - (1 / L3 + 3 / 2) / (3 + 1 / L3)
    ranked_move = 1.0 - (1 + 3 / L3) / (3 + 1 / L3)
    assert total(hidden.overall, "ndcg") == pytest.approx(
        total(base.overall, "ndcg") + overall_move
    )
    assert total(hidden.ranked_lane, "ndcg") == pytest.approx(
        total(base.ranked_lane, "ndcg") + ranked_move
    )


def test_a_fact_delivered_by_two_lines_scores_once(doubled_run: Run) -> None:
    """B14.6. Setup: the firmware reader's day 3 digest shows the ASK-SAF ask line twice in a
    row (a spy returns the selection with the first line repeated). No real Kestrel digest
    repeats a fact, so the repeat is injected.

    Action: run_ranking_eval; read the C0 row of U0FW01.
    Assert: the row equals the hand-worked B14.4 row. The first line scores 2, the repeat 0,
    and the repeat only moves the zero-grade lines down one place; day 3 recall stays 1 / 1 and
    nDCG stays 3 / 3. If both lines scored 2, day 3 recall would be 2 / 1.
    """
    fw = _persona(doubled_run.report().configs[0], "U0FW01")

    _assert_scores(fw.overall, FW_RECALL, FW_NDCG)
    _assert_scores(fw.ranked_lane, FW_RANKED_RECALL, FW_RANKED_NDCG)
    assert (fw.must_see_lane_facts, fw.ranked_lane_facts) == (6, 1)


# ---------------------------------------------------------------------------------------------
# B14.7: the digest eval command writes the Ranking section.
# ---------------------------------------------------------------------------------------------


def _cells(row: str) -> list[str]:
    return [cell.strip().strip("`") for cell in row.strip().strip("|").split("|")]


def _tables(section: list[str]) -> list[list[list[str]]]:
    """The markdown tables of the section as data rows of cells (header and rule rows dropped)."""
    tables: list[list[list[str]]] = []
    block: list[str] = []
    for line in [*section, ""]:
        if line.startswith("|"):
            block.append(line)
        elif block:
            tables.append([_cells(row) for row in block[2:]])
            block = []
    return tables


@dataclass
class CliRun:
    """One lazy `digest eval` in a scratch root with the cassettes and the network blocked."""

    root: Path
    _text: str | None = None

    def text(self) -> str:
        if self._text is None:
            self._text = _invoke_eval(self.root)
        return self._text


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory: pytest.TempPathFactory) -> CliRun:
    root = tmp_path_factory.mktemp("ranking-cli")
    shutil.copytree(
        WORLD_DIR, root / "data" / "world" / "kestrel", ignore=shutil.ignore_patterns("scripts")
    )
    (root / "config").mkdir()
    shutil.copy2(ROOT / "config" / "priors.yaml", root / "config" / "priors.yaml")
    return CliRun(root)


def _invoke_eval(root: Path) -> str:
    out = root / "out.md"

    def refuse_connection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("digest eval must run offline")

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        patch.setenv("DIGEST_MODE", "replay")
        patch.setenv("DIGEST_WORLD", "kestrel")
        patch.setenv("DIGEST_CASSETTES", str(CASSETTES))
        patch.delenv("DIGEST_DB", raising=False)
        patch.delenv("ANTHROPIC_API_KEY", raising=False)
        patch.delenv("OPENROUTER_API_KEY", raising=False)
        patch.setattr(socket.socket, "connect", refuse_connection)
        result = CliRunner().invoke(app, ["eval", "--world", "kestrel", "--out", str(out)])
    assert result.exit_code == 0, (result.output, result.exception)
    return out.read_text(encoding="utf-8")


def test_digest_eval_writes_the_ranking_section_offline(
    base_run: Run, cli_run: CliRun, world: World
) -> None:
    """B14.7. Setup: `digest eval` in a scratch root with replay mode, no API keys and
    socket.connect blocked; the in-process report names the rows to look for.

    Action: read the report the command wrote.
    Assert: the Ranking section holds a configuration table with seven rows (the report's
    names, in order), an ablation table with the four terms and the verdict of each, a
    persona table with one row per human persona and its role, the min_effect value and the
    labels line; the command made the ranking store beside the extraction store. The committed
    report is compared in tests/test_corpus_data.py.
    """
    report = base_run.report()
    text = cli_run.text()

    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"#{1,6}\s+Ranking\b", line))
    section = lines[start:]
    tables = _tables(section)

    names = [config.name for config in report.configs]
    config_tables = [rows for rows in tables if [row[0] for row in rows] == names]
    assert len(names) == 7 and len(config_tables) == 1

    ablation_tables = [rows for rows in tables if [row[0] for row in rows] == list(TERMS)]
    assert len(ablation_tables) == 1
    for row, expected in zip(ablation_tables[0], report.ablation, strict=True):
        assert expected.term == row[0]
        assert any(expected.verdict in cell for cell in row)

    readers = [(p.name, p.role) for p in world.personas if p.role is not None]
    assert len(readers) == 7
    persona_tables = [
        rows
        for rows in tables
        if len(rows) == 7
        and all(
            any(name in " | ".join(row) and role in " | ".join(row) for row in rows)
            for name, role in readers
        )
    ]
    assert len(persona_tables) == 1

    assert LABELS_LINE in section
    assert str(report.min_effect) in "\n".join(section)
    assert (cli_run.root / "data" / "eval-ranking-kestrel.sqlite").is_file()
    assert (cli_run.root / "data" / "eval-kestrel.sqlite").is_file()

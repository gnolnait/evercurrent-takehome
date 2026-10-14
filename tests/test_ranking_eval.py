"""Unit 14: B14.1 to B14.12 of docs/plans/m4-ranking-eval.md (v0.3.1).

B14.1 to B14.3 test the pure helpers with numbers worked out by hand. B14.4 to B14.6 run the
ranking eval on the Kestrel world and the committed cassettes (replay mode, no network) and
compare the report with values read by hand from `golden.jsonl`, `world.yaml` and the digests
that `select_digest` gives on a store this file builds day by day. The expected values never
come from `digest.eval.ranking`.

The report holds one mean per persona and no per-day value. So B14.4 and B14.6 check a persona's
whole row. Each expected day value below comes from one persona-day worked out in the comments.
Each persona-day also pins its selected top 5 (the premise), so a changed recording fails on the
premise and not on a grade.

A run is lazy (`Run.report()`), so incomplete code fails inside each test and not
in a fixture. The spy assumes `ranking.py` imports `select_digest` at module level (AGENTS.md
asks for top-level imports).
"""

import math
import re
import shutil
import socket
from collections import Counter
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
from digest.daily.profile import can_see, profile_for
from digest.daily.render import select_digest
from digest.daily.select import Selection
from digest.eval import ranking
from digest.eval.extraction import _absence_key, _qualifies
from digest.eval.ranking import (
    ConfigScores,
    PersonaScores,
    RankingReport,
    Scores,
    ndcg_at_5,
    recall_at_5,
    verdict,
    write_ranking_section,
)
from digest.extract.stage import run_extract
from digest.llm.client import LLM, from_settings
from digest.models import Item, micros_since_epoch, ts_micros
from digest.priors import Priors
from digest.project import add_working_days
from digest.replay import replay_world
from digest.settings import Settings
from digest.store import Store
from digest.world.golden import Golden, read_golden
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
ON_HAND_ASK = "C0SUPCH:1791909900.010014#1791910020.010015#0"
ANODIZE_ASK = "C0EXTCM:1792068738.000800#1792068738.000800#1"
PO_AMEND_ASK = "C0SUPCH:1792087200.010059#1792087200.010059#0"
DROP_IN_ASK = "C0SUPCH:1792079700.010041#1792080180.010043#0"
HARNESS_ASK = "C0DVT1B:1792426800.010087#1792427520.010090#0"
LOT_ASK = "C0DVT1B:1792510200.010093#1792510860.010096#1"
LTB_REPLY = "C0SUPCH:1791909900.010014#1791910320.010017#0"
SUPPLIER_NOISE = "C0SUPCH:1792082700.010050#1792082700.010050#0"
DOC_PACK = "C0PLMFD:1791822000.010003#1791822000.010003#0"
SCHEDULE_SYNC = "C0PLMFD:1791820500.010002#1791820500.010002#0"
PAYLOAD = "C0KPROG:1792166100.010066#1792166100.010066#0"
LOT_BLOCK = "C0DVT1B:1792510200.010093#1792510200.010093#0"
BUILD_D1 = "C0DVT1B:1792769400.010121#1792769400.010121#0"
ASK_SAF_ABSENCE = f"absence:ask:{ASK_SAF}"
HARNESS_ASK_ABSENCE = f"absence:ask:{HARNESS_ASK}"
LOT_ASK_ABSENCE = f"absence:ask:{LOT_ASK}"
FAI_ABSENCE = "absence:G-DVT1.fai"

Top5 = list[tuple[str, bool]]  # (item_id, must_see) of the first five lines of a digest


class Built(dict[tuple[str, int, str], Selection]):
    """(user_id, world day, config name in CONFIGS) -> the selection of that reader and day.

    `send_ids` holds, per (user_id, world day), the item ids of the send-everything list this
    file builds itself (see `_replay_days`).
    """

    def __init__(self) -> None:
        super().__init__()
        self.send_ids: dict[tuple[str, int], list[str]] = {}
        self.snapshots: dict[int, dict[str, Item]] = {}


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
    applied: int = 0  # selections that `wrap` changed
    _report: RankingReport | None = None
    _error: Exception | None = field(default=None, repr=False)

    def report(self) -> RankingReport:
        if self._error is not None:
            raise self._error.with_traceback(None)
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
            except Exception as error:
                # A failed eval can leave a complete store. Do not replay it from day 1.
                self._error = error
                raise
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
        if self.wrap is None:
            return selection
        wrapped = self.wrap(user_id, day, selection)
        self.applied += wrapped is not selection
        return wrapped


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
def hidden_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the world where C0ENGME is private; supply chain is not a member."""
    copy = tmp_path_factory.mktemp("ranking-hidden") / "kestrel"
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
    return copy


@pytest.fixture(scope="module")
def hidden_run(
    hidden_world: Path, tmp_path_factory: pytest.TempPathFactory, llm: LLM, priors: Priors
) -> Run:
    path = tmp_path_factory.mktemp("ranking-hidden-run") / "ranking.sqlite"
    return Run(hidden_world, path, llm, priors)


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


def _replay_days(
    world_dir: Path,
    store_path: Path,
    llm: LLM,
    priors: Priors,
    configs: dict[str, frozenset[str]],
) -> Built:
    """The world day by day in a store of this file: every reader's selection for each config.

    A selection for a day uses the previous world day as the previous digest, and none on day 1,
    as `digest demo` does. With a "full" config it also records the send-everything list of the
    plan: the absence items of the C0 selection by item id, then every item the reader can
    see whose newest evidence falls in the window, newest first, then by item id.
    """
    world = load_world(world_dir / "world.yaml")
    zone = ZoneInfo(world.timezone)
    purposes = {channel.id: channel.purpose for channel in world.channels}
    readers = [persona.user_id for persona in world.personas if persona.role is not None]
    built = Built()
    with Store.open(store_path) as store:
        for day in range(1, world.days + 1):
            today = world.date_of(day)
            replay_world(store, world_dir, today)
            as_of = datetime.combine(today, time.max, zone).astimezone(UTC)
            run_extract(store, llm, world.glossary(), world.project, priors, as_of, zone, purposes)
            previous = world.date_of(day - 1) if day > 1 else None
            for name, off in configs.items():
                daily = priors.daily.model_copy(update={"terms_off": off})
                config_priors = priors.model_copy(update={"daily": daily})
                for user in readers:
                    built[(user, day, name)] = select_digest(
                        store, world, config_priors, user, today, previous_digest=previous
                    )
            if "full" not in configs:
                continue
            start_day = previous or add_working_days(today, -priors.daily.window.first_working_days)
            start = micros_since_epoch(datetime.combine(start_day, time.max, zone))
            end = micros_since_epoch(datetime.combine(today, time.max, zone))
            items = list(store.items(as_of))
            built.snapshots[day] = {item.item_id: item for item in items}
            times = {
                (e.channel_id, e.ts): ts_micros(e.event_ts)
                for thread in {i.thread_id for i in items}
                for e in store.events_in_thread(thread, as_of)
            }
            newest = {
                i.item_id: max(
                    times.get((e.channel_id, e.message_ts), ts_micros(e.message_ts))
                    for e in i.evidence
                )
                for i in items
            }
            for user in readers:
                profile = profile_for(world, user)
                shown = sorted(
                    (
                        i
                        for i in items
                        if ts_micros(i.last_updated) <= end
                        and start < newest[i.item_id] <= end
                        and can_see(world, profile, i.thread_id.partition(":")[0])
                    ),
                    key=lambda i: (-newest[i.item_id], i.item_id),
                )
                absences = sorted(
                    line.item.item_id
                    for line in built[(user, day, "full")].lines
                    if line.item.absence is not None
                )
                built.snapshots[day].update(
                    (line.item.item_id, line.item)
                    for line in built[(user, day, "full")].lines
                    if line.item.absence is not None
                )
                built.send_ids[(user, day)] = [*absences, *(i.item_id for i in shown)]
    return built


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory, priors: Priors, llm: LLM) -> Built:
    path = tmp_path_factory.mktemp("ranking-built") / "kestrel.sqlite"
    return _replay_days(WORLD_DIR, path, llm, priors, CONFIGS)


@pytest.fixture(scope="module")
def zero_built(tmp_path_factory: pytest.TempPathFactory, priors: Priors, llm: LLM) -> Built:
    path = tmp_path_factory.mktemp("ranking-zero-built") / "kestrel.sqlite"
    zero = priors.model_copy(update={"daily": priors.daily.model_copy(update={"min_score": 0})})
    return _replay_days(WORLD_DIR, path, llm, zero, CONFIGS)


@pytest.fixture(scope="module")
def golden() -> Golden:
    return read_golden(WORLD_DIR / "golden.jsonl")


@pytest.fixture(scope="module")
def hidden_built(
    hidden_world: Path, tmp_path_factory: pytest.TempPathFactory, priors: Priors, llm: LLM
) -> Built:
    path = tmp_path_factory.mktemp("ranking-hidden-built") / "kestrel.sqlite"
    return _replay_days(hidden_world, path, llm, priors, {"full": CONFIGS["full"]})


def _persona(config: ConfigScores, user_id: str) -> PersonaScores:
    return next(row for row in config.per_persona if row.user_id == user_id)


def _top5(built: Built, user: str, day: int) -> Top5:
    lines = built[(user, day, "full")].lines
    return [(line.item.item_id, line.must_see) for line in lines[:5]]


def _ranked5(built: Built, user: str, day: int) -> list[str]:
    """The first five ranked-lane (not must-see) item ids of C0's digest, in digest order."""
    lines = built[(user, day, "full")].lines
    return [line.item.item_id for line in lines if not line.must_see][:5]


def _assert_scores(
    scores: Scores, recall: Sequence[float | None], ndcg: Sequence[float | None]
) -> None:
    """The persona's mean over the days with a value, the days with a value and those without."""
    recalls = [value for value in recall if value is not None]
    ndcgs = [value for value in ndcg if value is not None]
    assert scores.recall_n == len(recalls)
    assert scores.recall_without == len(recall) - len(recalls)
    assert scores.recall_at_5 == pytest.approx(sum(recalls) / len(recalls) if recalls else None)
    assert scores.ndcg_n == len(ndcgs)
    assert scores.ndcg_without == len(ndcg) - len(ndcgs)
    assert scores.ndcg_at_5 == pytest.approx(sum(ndcgs) / len(ndcgs) if ndcgs else None)


Fact = str | tuple[int, str, str | None]
METRIC_NAMES = ("overall recall", "overall nDCG", "ranked-lane recall", "ranked-lane nDCG")


def _facts(selection: Selection, world: World, golden: Golden) -> dict[Fact, int]:
    """Read the window, visibility and audience from the selection and world labels."""
    start = selection.previous_digest or add_working_days(
        selection.day, -selection.first_working_days
    )
    profile = selection.profile
    facts: dict[Fact, int] = {}
    for item in golden.items:
        if start < item.date <= selection.day and can_see(world, profile, item.channel_id):
            facts[item.event_id] = (
                2
                if profile.user_id in item.must_see_for
                else 1
                if profile.role in item.affected_roles
                else 0
            )
    channels = {item.event_id: item.channel_id for item in golden.items}
    for absence in golden.absences:
        if start < absence.date <= selection.day and can_see(
            world, profile, channels[absence.cites]
        ):
            audience = profile.user_id if absence.source == "ask" else profile.role
            key = (absence.day, absence.source, absence.task or absence.cites)
            facts[key] = 2 if audience in absence.audience else 0
    return facts


def _deliveries(items: Sequence[Item], day: int, golden: Golden) -> list[Fact | None]:
    """Credit the first unused qualifying fact in golden order for each item."""
    asks = {
        f"{item.channel_id}:{item.thread_ts}": item.event_id
        for item in golden.items
        if item.type == "ask"
    }
    used: set[Fact] = set()
    deliveries: list[Fact | None] = []
    for item in items:
        candidates: list[Fact] = (
            [_absence_key(day, item, asks)]
            if item.absence is not None
            else [
                fact.event_id
                for fact in golden.items
                if item.thread_id == f"{fact.channel_id}:{fact.thread_ts}"
                and _qualifies(fact, item)
            ]
        )
        match = next((fact for fact in candidates if fact not in used), None)
        deliveries.append(match)
        if match is not None:
            used.add(match)
    return deliveries


def _day_view(
    built: Built,
    world: World,
    golden: Golden,
    user: str,
    day: int,
    name: str,
    *,
    ranked: bool,
) -> tuple[list[str], list[int], list[int]]:
    """Return ids, line grades and fact grades before the cut at five (R3 and R6a)."""
    c0 = built[(user, day, "full")]
    facts = _facts(c0, world, golden)
    if name == "send-everything":
        ids = built.send_ids[(user, day)]
    else:
        ids = [
            line.item.item_id
            for line in built[(user, day, name)].lines
            if not (ranked and line.must_see)
        ]
    if ranked:
        c0_facts = _deliveries([line.item for line in c0.lines], day, golden)
        removed = {
            fact
            for line, fact in zip(c0.lines, c0_facts, strict=True)
            if line.must_see and fact in facts
        }
        facts = {fact: grade for fact, grade in facts.items() if fact not in removed}
        # Remove deliveries before the top-five cut. Zero grades would still consume slots.
        delivered = _deliveries([built.snapshots[day][item_id] for item_id in ids], day, golden)
        ids = [item_id for item_id, fact in zip(ids, delivered, strict=True) if fact not in removed]
    delivered = _deliveries([built.snapshots[day][item_id] for item_id in ids], day, golden)
    return (
        ids,
        [facts.get(fact, 0) if fact is not None else 0 for fact in delivered],
        list(facts.values()),
    )


def _values(grades: Sequence[int], facts: Sequence[int]) -> tuple[float | None, float | None]:
    """R5/R6 arithmetic; do not use the metric helpers under test."""
    must_see = facts.count(2)
    recall = grades[:5].count(2) / must_see if must_see else None
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 2)
        for rank, grade in enumerate(sorted(facts, reverse=True)[:5])
    )
    dcg = sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades[:5]))
    return recall, dcg / ideal if ideal else None


def _day_metrics(
    built: Built, world: World, golden: Golden, user: str, day: int, name: str
) -> tuple[float | None, ...]:
    values: list[float | None] = []
    for ranked in (False, True):
        _, grades, facts = _day_view(built, world, golden, user, day, name, ranked=ranked)
        values.extend(_values(grades, facts))
    return tuple(values)


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
# The first five ranked-lane lines (must-see lines removed) of each day, which the ranked-lane
# grades below read: the lane has no must-see line of grade 2 except S15-GO on day 9.
FW_RANKED_TOP5: dict[int, list[str]] = {
    1: [EVT2_EXIT],
    2: [LIDAR, PLM_11020C, THM_RUN1],
    3: [SLIP, EMC, PO],
    4: [MLCC, EMC, PO],
    5: [FAI, MLCC, EMC, PO],
    6: [HARNESS, UN383, BUFFER, DFM, FAI],
    7: [BRACKET, LOT, HARNESS, UN383, BUFFER],
    8: [DEBOUNCE, FILTER, HARNESS, UN383, BUFFER],
    9: [GO, HARNESS, UN383, BUFFER, DFM],
    10: [HARNESS, UN383, BUFFER, DFM, FAI],
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
    for day, ids in FW_RANKED_TOP5.items():
        assert _ranked5(built, "U0FW01", day) == ids, f"premise (ranked lane): day {day}"


# ---------------------------------------------------------------------------------------------
# B14.5: the priors that reach select_digest, and the sensitivity count.
# ---------------------------------------------------------------------------------------------


def test_each_configuration_selects_with_its_named_terms_off(
    base_run: Run, priors: Priors, world: World
) -> None:
    """B14.5. Setup: a pass-through spy on the select_digest that ranking.py calls.

    Action: run_ranking_eval on the Kestrel world.
    Assert: for each of the six `terms_off` sets of C0, C1 to C4 and role-only, the calls cover
    all seven readers on all ten days with the previous world day as previous digest (none on
    day 1) and change no other priors field; the report names the configurations and their
    terms in order. The test says nothing about how send-everything gets its list.
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
        if call.priors.daily.min_score != priors.daily.min_score:
            continue  # B14.8 checks the additional min_score 0 selections.
        seen.setdefault(call.priors.daily.terms_off, set()).add(
            (call.user_id, call.day, call.previous_digest)
        )
        cleared = call.priors.daily.model_copy(update={"terms_off": frozenset()})
        assert call.priors.model_copy(update={"daily": cleared}) == priors
    for name, off in CONFIGS.items():
        assert seen.get(off, set()) >= every_day, name

    configs = report.configs
    assert len(configs) == 7
    assert (configs[0].name, configs[-2].name, configs[-1].name) == (
        "full",
        "role-only",
        "send-everything",
    )
    assert [set(config.terms_off) for config in configs[:5]] == [set(), *({t} for t in TERMS)]
    assert set(configs[-2].terms_off) == {"importance", "personal_relevance"}


def _changed_days(built: Built, name: str, world: World, golden: Golden) -> int:
    """Persona-days whose overall top 5 or ranked-lane top 5 differs from C0 (item ids, in order)."""
    pairs = {(user, day) for user, day, config in built if config == "full"}
    return sum(
        1
        for user, day in pairs
        if any(
            _day_view(built, world, golden, user, day, name, ranked=lane)[0][:5]
            != _day_view(built, world, golden, user, day, "full", ranked=lane)[0][:5]
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
    name: str, base_run: Run, built: Built, world: World, golden: Golden
) -> None:
    """B14.5. Setup: this file's own selections for C0 and each config, day by day.

    Action: run_ranking_eval; count the persona-days whose overall or ranked-lane top 5 differs
    from C0's (item ids in order), 7 readers by 10 days.
    Assert: the config's changed_days equals that count.
    """
    report = base_run.report()

    expected = _changed_days(built, name, world, golden)
    config = next(
        c for c in report.configs if set(c.terms_off) == set(CONFIGS[name]) and c.terms_off
    )
    assert config.changed_days == expected


# ---------------------------------------------------------------------------------------------
# R7 and R8: the means across personas, and the verdict of each term from the configurations.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lane", ["overall", "ranked_lane"])
def test_every_configuration_value_is_the_mean_of_the_persona_means_that_have_one(
    lane: str, base_run: Run
) -> None:
    """R7. Setup: the report of the Kestrel run, all seven configurations.

    Action: for each configuration and each metric, take the persona rows' means.
    Assert: the configuration's value is the mean of the persona means that are not None, its
    persona count is the number of those personas, a persona with no day with a value is
    left out, and n and without are the sums over the personas.
    """
    report = base_run.report()

    assert len(report.configs) == 7
    for config in report.configs:
        assert len(config.per_persona) == 7, config.name
        scores = getattr(config, lane)
        rows = [getattr(persona, lane) for persona in config.per_persona]
        for metric in ("recall", "ndcg"):
            means = [getattr(row, f"{metric}_at_5") for row in rows]
            have = [mean for mean in means if mean is not None]
            where = f"{config.name} {lane} {metric}"
            # A persona has a mean exactly when it has a day with a value.
            assert len(have) == sum(1 for row in rows if getattr(row, f"{metric}_n") > 0), where
            value = getattr(scores, f"{metric}_at_5")
            if have:
                assert value == pytest.approx(sum(have) / len(have)), where
            else:
                assert value is None, where
            assert getattr(scores, f"{metric}_personas") == len(have), where
            for count in ("n", "without"):
                assert getattr(scores, f"{metric}_{count}") == sum(
                    getattr(row, f"{metric}_{count}") for row in rows
                ), where


METRIC_FIELDS = (
    ("overall", "recall_at_5"),
    ("overall", "ndcg_at_5"),
    ("ranked_lane", "recall_at_5"),
    ("ranked_lane", "ndcg_at_5"),
)


def test_each_ablation_verdict_follows_from_the_configuration_values(base_run: Run) -> None:
    """R8. Setup: the report of the Kestrel run; C0 and the four term configurations.

    Action: compute the verdict of each term in the test. A metric's drop is (c0 - off) / c0 for
    overall recall, overall nDCG, ranked-lane recall and ranked-lane nDCG, with no vote when c0 is
    0 or None. A drop of at least min_effect votes keep; the term is keep when any metric votes
    keep, remove when a metric votes and none votes keep, and no vote when none votes.
    Assert: report.ablation holds the four terms in order with that verdict and those drops.
    """
    report = base_run.report()

    assert report.min_effect == 0.01
    assert [row.term for row in report.ablation] == list(TERMS)
    c0 = report.configs[0]
    for row in report.ablation:
        off_config = next(c for c in report.configs[1:5] if set(c.terms_off) == {row.term})
        drops: list[float | None] = []
        for lane, field_name in METRIC_FIELDS:
            base = getattr(getattr(c0, lane), field_name)
            off = getattr(getattr(off_config, lane), field_name)
            if base is None or base == 0:
                drops.append(None)
                continue
            assert off is not None, f"premise: {row.term} {lane} {field_name}"
            drops.append((base - off) / base)
        votes = [drop >= report.min_effect for drop in drops if drop is not None]
        expected = "no vote" if not votes else "keep" if any(votes) else "remove"
        assert row.verdict == expected, row.term
        assert len(row.drops) == 4
        assert sorted(d for d in row.drops.values() if d is not None) == pytest.approx(
            sorted(d for d in drops if d is not None)
        ), row.term
        assert sum(d is None for d in row.drops.values()) == drops.count(None), row.term


# ---------------------------------------------------------------------------------------------
# The send-everything baseline, for the firmware reader U0FW01, ten days.
#
# Named persona-day: day 2 (2026-10-13). The window holds world day 2. U0FW01 reads every
# channel that carries an item on that day (all are public), and no absence exists, so the
# list is the items whose newest evidence is on day 2, newest first, then by item id:
#   1  C0ENGME 1791911662.000400#...#0  newest evidence 1791913127 (the post was edited)
#   2  C0ENGEF 1791911400.010019        1791911400
#   3  C0SUPCH 1791909900.010014#1791910320.010017#0   1791910320
#   4  C0SUPCH 1791909900.010014#1791910020.010015#0   1791910200
#   5  C0SUPCH 1791909900.010014#1791909900.010014#0   1791909900  (S14-LTB)
#   (6  C0PLMFD 1791908400.010010                      1791908880)
# Facts of day 2 for the FW role (golden.jsonl): S14-LTB must_see_for lists U0FW01, grade 2;
# S1-AGREE, PLM-11020C and S3-THERMAL-RUN1 do not name FW, grade 0. Line grades [0,0,0,0,2]:
# only item 5 delivers a fact. recall = 1 / 1. nDCG = (3 / log2 6) / 3 = 0.38685. C0's digest
# drops the S14-LTB item for FW (its score is below the threshold); send-everything shows it.
#
# The other days, same method. The two ways to read "every item" (open items only, or also
# resolved ones) give the same grades for this reader; resolved items come after the five
# lines or change no grade.
#   day  top-5 grades   fact grades      recall  nDCG
#   1    [1,0,0,0]      [1,0,0,0]        none    1
#   2    [0,0,0,0,2]    [2,0,0,0]        1 / 1   1 / log2 6
#   3    [2,0,0,0]      [2,0,0,0]        1 / 1   1
#   4    [0,0,0,0,0]    [0,0,0,0,0,0]    none    none
#   5    [0,0]          [2,0,0,0,0]      0 / 1   0
#   6    [2,0,0,0,0]    [2,2,0...]       1 / 2   3 / (3 + 3 / log2 3)
#   7    [2,0,0,0,0]    [2,2,0...]       1 / 2   3 / (3 + 3 / log2 3)
#   8    [2,0,1,0]      [2,2,1,1,0..]    1 / 2   (3 + 1 / 2) / (3 + 3 / log2 3 + 1 / 2 + 1 / log2 5)
#   9    [2,2]          [2,2,2,0..]      2 / 3   (3 + 3 / log2 3) / (3 + 3 / log2 3 + 3 / 2)
#   10   [2,0]          [2,0]            1 / 1   1
# R6a removes deliveries from C0's must-see lane before the ranked-lane top-five cut.
# ---------------------------------------------------------------------------------------------

SEND_FW_TOP5: dict[int, list[str]] = {
    1: [EVT2_EXIT, DOC_PACK, SCHEDULE_SYNC, ECO_BOM],
    2: [LIDAR, THM_RUN1, LTB_REPLY, ON_HAND_ASK, LTB],
    3: [ASK_SAF, SLIP, EMC, PO],
    4: [PO_AMEND_ASK, SUPPLIER_NOISE, BOM_11034, DROP_IN_ASK, MLCC],
    5: [PAYLOAD, FAI],
    6: [ASK_SAF_ABSENCE, HARNESS, HARNESS_ASK, UN383, BUFFER],
    7: [ASK_SAF_ABSENCE, FLAG_ASK, BRACKET, TEXTURE, LOT_BLOCK],
    8: [ASK_SAF_ABSENCE, BENCH_ASK, FILTER, DEBOUNCE],
    9: [ASK_SAF_ABSENCE, GO],
    10: [ASK_SAF_ABSENCE, BUILD_D1],
}
SEND_RECALL = [None, 1.0, 1.0, None, 0.0, 1 / 2, 1 / 2, 1 / 2, 2 / 3, 1.0]
SEND_NDCG = [
    1.0,
    1 / math.log2(6),
    1.0,
    None,
    0.0,
    3 / (3 + 3 / L3),
    3 / (3 + 3 / L3),
    (3 + 1 / 2) / (3 + 3 / L3 + 1 / 2 + 1 / L5),
    (3 + 3 / L3) / (3 + 3 / L3 + 3 / 2),
    1.0,
]


def test_send_everything_scores_the_firmware_reader_by_hand(
    base_run: Run, built: Built, world: World, golden: Golden
) -> None:
    """Baseline send-everything. Setup: the firmware reader's ten days worked by hand; the top 5
    of each day is pinned and is rebuilt from the store by this file.

    Action: run_ranking_eval; read the send-everything row of U0FW01 and its changed_days.
    Assert: recall and nDCG in both lanes equal the hand-worked days; changed_days equals this file's
    count of persona-days whose send-everything top 5 differs from C0's overall top 5 or C0's
    ranked-lane top 5.
    """
    report = base_run.report()

    send = report.configs[-1]
    assert send.name == "send-everything"
    _assert_scores(_persona(send, "U0FW01").overall, SEND_RECALL, SEND_NDCG)
    # R6a: day 8 loses ASK-SAF, so FILTER moves from rank 3 to rank 2.
    # Day 9 loses ASK-SAF, so GO moves from rank 2 to rank 1. Other ranked values
    # equal C0's hand table, except day 2 delivers LTB at rank 5.
    ranked_recall = [*FW_RANKED_RECALL]
    ranked_ndcg = [*FW_RANKED_NDCG]
    ranked_recall[1], ranked_ndcg[1] = 1.0, 1 / math.log2(6)
    _assert_scores(_persona(send, "U0FW01").ranked_lane, ranked_recall, ranked_ndcg)
    for day, ids in SEND_FW_TOP5.items():
        assert built.send_ids[("U0FW01", day)][:5] == ids, f"premise: day {day}"
    expected = _changed_days(built, "send-everything", world, golden)
    assert send.changed_days == expected


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
#   7    [1,0,0,1,0]   [2,1,1,0..]   0 / 1   (1 + 1 / log2 5) / (3 + 1 / log2 3 + 1 / 2)
# R3 credits FLAG_ASK at rank 1 for S13-BRACKET; BRACKET at rank 3 repeats it.
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
    (1 + 1 / L5) / (3 + 1 / L3 + 1 / 2),
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


# The supply-chain reader U0SC01 (Hamid Rostami), C0: the top 5 and the ranked-lane top 5 of
# every day. The hidden-channel test compares two runs, so every day it relies on is pinned, in
# the base world and (day 2 only differs) in the world where C0ENGME is private.
SC_TOP5: dict[int, Top5] = {
    1: [(ECO_BOM, False), (EVT2_EXIT, False)],
    2: [
        (ON_HAND_ASK, True),
        (PLM_11020C, False),
        (LIDAR, False),
        (LTB, False),
        (THM_RUN1, False),
    ],
    3: [(ON_HAND_ASK, True), (SLIP, False), (PO, True), (EMC, False)],
    4: [
        (ANODIZE_ASK, True),
        (ON_HAND_ASK, True),
        (PO_AMEND_ASK, True),
        (BOM_11034, False),
        (PO, True),
    ],
    5: [(ANODIZE_ASK, True), (ON_HAND_ASK, True), (PO, True), (FAI, False), (MLCC, False)],
    6: [
        (HARNESS_ASK, True),
        (ANODIZE_ASK, True),
        (ON_HAND_ASK, True),
        (PO, True),
        (HARNESS, False),
    ],
    7: [
        (HARNESS_ASK, True),
        (LOT_ASK, True),
        (ANODIZE_ASK, True),
        (ON_HAND_ASK, True),
        (LOT, False),
    ],
    8: [(HARNESS_ASK, True), (LOT_ASK, True), (ANODIZE_ASK, True), (ON_HAND_ASK, True), (PO, True)],
    9: [
        (LOT_ASK, True),
        (ANODIZE_ASK, True),
        (ON_HAND_ASK, True),
        (HARNESS_ASK_ABSENCE, True),
        (GO, False),
    ],
    10: [
        (ANODIZE_ASK, True),
        (ON_HAND_ASK, True),
        (HARNESS_ASK_ABSENCE, True),
        (LOT_ASK_ABSENCE, True),
        (PO, True),
    ],
}
SC_RANKED5: dict[int, list[str]] = {
    1: [ECO_BOM, EVT2_EXIT],
    2: [PLM_11020C, LIDAR, LTB, THM_RUN1],
    3: [SLIP, EMC],
    4: [BOM_11034, MLCC, EMC, DROP_IN_ASK],
    5: [FAI, MLCC, EMC],
    6: [HARNESS, UN383, FAI, MLCC, BUFFER],
    7: [LOT, HARNESS, UN383, FAI, MLCC],
    8: [HARNESS, UN383, FAI, MLCC, BUFFER],
    9: [GO, HARNESS, UN383, FAI, MLCC],
    10: [HARNESS, UN383, FAI, MLCC, BUFFER],
}
# Day 2 with C0ENGME private: the LiDAR decision is not visible to U0SC01.
HIDDEN_SC_DAY2_TOP5: Top5 = [
    (ON_HAND_ASK, True),
    (PLM_11020C, False),
    (LTB, False),
    (THM_RUN1, False),
]
HIDDEN_SC_DAY2_RANKED5 = [PLM_11020C, LTB, THM_RUN1]


def test_a_fact_on_a_channel_the_reader_cannot_see_is_not_in_the_reader_set(
    base_run: Run, hidden_run: Run, built: Built, hidden_built: Built
) -> None:
    """B14.6. Setup: a copy of the world where C0ENGME (eng-mech) is private; U0SC01 (supply
    chain) is not a member. On day 2 the S1-AGREE decision (C0ENGME, must_see_for lists
    U0SC01) is a fact of grade 2 that the reader delivered in the base run.

    Action: run_ranking_eval on both worlds; compare the C0 row of U0SC01.
    Assert: the pinned top 5 of every day is the same in both worlds except day 2, so against the
    base run only day 2 changes. Base day 2: lines [0,1,2,0,0] against
    facts [2,1] (recall 1 / 1, nDCG (1 / log2 3 + 3 / 2) / (3 + 1 / log2 3) = 0.58688; ranked
    lane lines [1,2,0,0], recall 1 / 1, nDCG (1 + 3 / log2 3) / (3 + 1 / log2 3) = 0.79671).
    Hidden day 2: S1-AGREE leaves the set and the line leaves the digest. Lines [0,1,0,0]
    against facts [1]: recall has no must-see fact (no value), nDCG = (1 / log2 3) / 1 = 0.63093;
    ranked lane lines [1,0,0], recall no value, nDCG = 1 / 1 = 1.
    """
    base = _persona(base_run.report().configs[0], "U0SC01")
    hidden = _persona(hidden_run.report().configs[0], "U0SC01")

    for day in range(1, 11):
        assert _top5(built, "U0SC01", day) == SC_TOP5[day], f"premise (base): day {day}"
        assert _ranked5(built, "U0SC01", day) == SC_RANKED5[day], f"premise (base): day {day}"
        top5 = HIDDEN_SC_DAY2_TOP5 if day == 2 else SC_TOP5[day]
        ranked5 = HIDDEN_SC_DAY2_RANKED5 if day == 2 else SC_RANKED5[day]
        assert _top5(hidden_built, "U0SC01", day) == top5, f"premise (hidden): day {day}"
        assert _ranked5(hidden_built, "U0SC01", day) == ranked5, f"premise (hidden): day {day}"

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

    assert doubled_run.applied > 0, (
        "the repeat was never injected: no select_digest call reached it"
    )
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


ZERO_HEADING = "With min_score 0 (admission fixed)"
ADMISSION_NOTE = (
    "The configured rows mix admission with order; these rows fix admission and show order only."
)


def _table_pair(section: list[str], *, zero: bool) -> tuple[list[list[str]], list[list[str]]]:
    """Find each pair within its heading, since both pairs have the same row names."""
    split = next(i for i, line in enumerate(section) if line.lstrip("# ") == ZERO_HEADING)
    tables = _tables(section[split:] if zero else section[:split])
    names = [*CONFIGS, "send-everything"]
    configs = [rows for rows in tables if [row[0] for row in rows] == names]
    ablations = [rows for rows in tables if [row[0] for row in rows] == list(TERMS)]
    assert len(configs) == len(ablations) == 1
    return configs[0], ablations[0]


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


def _fmt(value: float | None) -> str:
    """How the Ranking section prints a number: 3 decimals, or n/a."""
    return "n/a" if value is None else f"{value:.3f}"


def test_digest_eval_writes_the_ranking_section_offline(
    base_run: Run, cli_run: CliRun, world: World
) -> None:
    """B14.7. Setup: `digest eval` in a scratch root with replay mode, no API keys and
    socket.connect blocked; the in-process report names the rows to look for.

    Action: read the report the command wrote.
    Assert: the file holds write_ranking_section(report) of the same run; the Ranking section
    holds each threshold's configuration table with seven rows (the report's names, in order) whose numbers
    are the report's at 3 decimals (or n/a), an ablation table with the four terms, their drops
    and verdicts, a persona table with one row per human persona, its role and its C0 numbers,
    the exact keep-rule line with min_effect 0.01 and the labels line; the command made the
    ranking store beside the extraction store. The committed report is compared in
    tests/test_corpus_data.py.
    """
    report = base_run.report()
    text = cli_run.text()

    assert report.min_effect == 0.01
    assert write_ranking_section(report) in text
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"#{1,6}\s+Ranking\b", line))
    section = lines[start:]
    tables = _tables(section)

    for zero, configs, ablation in (
        (False, report.configs, report.ablation),
        (True, report.zero_threshold, report.zero_ablation),
    ):
        config_rows, ablation_rows = _table_pair(section, zero=zero)
        assert len(configs) == 7
        for row, config in zip(config_rows, configs, strict=True):
            for scores in (config.overall, config.ranked_lane):
                assert _fmt(scores.recall_at_5) in row, config.name
                assert _fmt(scores.ndcg_at_5) in row, config.name
                assert str(scores.recall_n) in row and str(scores.ndcg_n) in row, config.name
            assert str(config.changed_days) in row, config.name
        for row, expected in zip(ablation_rows, ablation, strict=True):
            assert expected.term == row[0]
            assert expected.verdict in row
            for drop in expected.drops.values():
                assert any(_fmt(drop) in cell for cell in row), expected.term
    assert ADMISSION_NOTE in section

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
    for persona in report.configs[0].per_persona:
        row = next(r for r in persona_tables[0] if persona.name in " | ".join(r))
        assert persona.role in row
        assert _fmt(persona.overall.recall_at_5) in row, persona.name
        assert _fmt(persona.overall.ndcg_at_5) in row, persona.name
        assert _fmt(persona.ranked_lane.recall_at_5) in row, persona.name
        assert _fmt(persona.ranked_lane.ndcg_at_5) in row, persona.name

    keep_rule = (
        "Keep rule: a term stays when a metric drops by at least 0.01 of its value with the "
        "term on."
    )
    assert keep_rule in section
    assert LABELS_LINE in section
    assert (cli_run.root / "data" / "eval-ranking-kestrel.sqlite").is_file()
    assert (cli_run.root / "data" / "eval-kestrel.sqlite").is_file()


# B14.8 to B14.12 use the same day-by-day store and the labels, never ranking's helpers.


def _expected_days(
    built: Built, world: World, golden: Golden, name: str
) -> dict[tuple[str, int], tuple[float | None, ...]]:
    return {
        (user, day): _day_metrics(built, world, golden, user, day, name)
        for user, day, config in built
        if config == "full"
    }


def _persona_mean(
    days: dict[tuple[str, int], tuple[float | None, ...]], metric: int
) -> float | None:
    means = []
    for user in sorted({user for user, _ in days}):
        values = [
            row[metric]
            for (reader, _), row in days.items()
            if reader == user and row[metric] is not None
        ]
        if values:
            means.append(sum(values) / len(values))
    return sum(means) / len(means) if means else None


def test_zero_threshold_selects_all_configs_and_reports_order_only(
    base_run: Run, zero_built: Built, world: World, golden: Golden, priors: Priors
) -> None:
    """B14.8. Setup: own min_score 0 selections on each world day, with caps unchanged.

    Action: run the eval and read the zero-threshold selections, scores and ablation.
    Assert: priors, all seven rows, drops, verdicts and both printed table pairs match R10.
    """
    expected = {
        name: _expected_days(zero_built, world, golden, name)
        for name in (*CONFIGS, "send-everything")
    }
    report = base_run.report()
    assert report.min_score == priors.daily.min_score
    zero_calls = [call for call in base_run.calls if call.priors.daily.min_score == 0]
    for name, off in CONFIGS.items():
        calls = [call for call in zero_calls if call.priors.daily.terms_off == off]
        assert {(call.user_id, call.day, call.previous_digest) for call in calls} == {
            (user, world.date_of(day), world.date_of(day - 1) if day > 1 else None)
            for user, day in expected[name]
        }, name
        daily = priors.daily.model_copy(update={"terms_off": off, "min_score": 0})
        assert all(call.priors == priors.model_copy(update={"daily": daily}) for call in calls)
    assert [config.name for config in report.zero_threshold] == [*CONFIGS, "send-everything"]
    for config in report.zero_threshold:
        if config.name in CONFIGS:
            assert set(config.terms_off) == CONFIGS[config.name]
        days = expected[config.name]
        assert {persona.user_id for persona in config.per_persona} == {user for user, _ in days}
        assert config.changed_days == _changed_days(zero_built, config.name, world, golden)
        for persona in config.per_persona:
            rows = [days[persona.user_id, day] for day in range(1, world.days + 1)]
            _assert_scores(persona.overall, [r[0] for r in rows], [r[1] for r in rows])
            _assert_scores(persona.ranked_lane, [r[2] for r in rows], [r[3] for r in rows])
        for index, (lane, field_name) in enumerate(METRIC_FIELDS):
            assert getattr(getattr(config, lane), field_name) == pytest.approx(
                _persona_mean(days, index)
            ), (config.name, lane, field_name)
    assert [row.term for row in report.zero_ablation] == list(TERMS)
    for row in report.zero_ablation:
        drops = {}
        for index, metric in enumerate(METRIC_NAMES):
            c0 = _persona_mean(expected["full"], index)
            off = _persona_mean(expected[row.term], index)
            drops[metric] = (c0 - off) / c0 if c0 else None
        assert row.drops == pytest.approx(drops)
        votes = [drop >= priors.ablation.min_effect for drop in drops.values() if drop is not None]
        assert row.verdict == ("keep" if any(votes) else "remove" if votes else "no vote")
    section = write_ranking_section(report).splitlines()
    assert ADMISSION_NOTE in section
    _table_pair(section, zero=False)
    _table_pair(section, zero=True)


def test_below_threshold_counts_grade_two_ranked_lines_by_type_and_severity(
    base_run: Run, zero_built: Built, world: World, golden: Golden, priors: Priors
) -> None:
    """B14.9. Setup: C0 at min_score 0 on the test's own store, plus the world labels.

    Action: count grade-2 ranked lines below the configured score, and all must-see facts.
    Assert: the sorted type/severity counts, total denominator and printed table match R11.
    """
    counts: Counter[tuple[str, str]] = Counter()
    must_see_facts = 0
    for (_user, day, name), selection in zero_built.items():
        if name != "full":
            continue
        facts = _facts(selection, world, golden)
        must_see_facts += list(facts.values()).count(2)
        delivered = _deliveries([line.item for line in selection.lines], day, golden)
        for line, fact in zip(selection.lines, delivered, strict=True):
            if not line.must_see and facts.get(fact) == 2 and line.score < priors.daily.min_score:
                counts[line.item.type, line.item.severity] += 1
    expected = [
        (kind, severity, count)
        for (kind, severity), count in sorted(
            counts.items(), key=lambda entry: (-entry[1], *entry[0])
        )
    ]
    assert expected and 0 < sum(counts.values()) < must_see_facts
    report = base_run.report()
    assert [(row.type, row.severity, row.count) for row in report.below_threshold] == expected
    assert report.must_see_facts == must_see_facts
    section = write_ranking_section(report).splitlines()
    heading = f"Must-see facts below min_score {priors.daily.min_score}"
    start = next(i for i, line in enumerate(section) if line.lstrip("# ") == heading)
    end = next(
        (i for i in range(start + 1, len(section)) if section[i].startswith("#")), len(section)
    )
    block = section[start:end]
    assert _tables(block) == [[[kind, severity, str(count)] for kind, severity, count in expected]]
    assert re.search(rf"\b{must_see_facts}\b", "\n".join(block))


@pytest.mark.parametrize(
    "chosen",
    [(BRACKET,), (FLAG_ASK,), (BRACKET, FLAG_ASK), (FLAG_ASK, BRACKET)],
    ids=["bracket-only", "ask-only", "both-bracket-first", "both-ask-first"],
)
def test_each_qualifying_bracket_item_delivers_once_and_old_facts_score_zero(
    chosen: tuple[str, ...],
    built: Built,
    world: World,
    golden: Golden,
    tmp_path: Path,
    llm: LLM,
    priors: Priors,
) -> None:
    """B14.10. Setup: both S13-BRACKET items in the independent store on days 7 to 9.

    Action: select either item, or both in either order, for the EM on those days.
    Assert: day 7 credits one grade-2 fact; days 8 and 9 credit zero for the old fact.
    """
    fact = next(fact for fact in golden.items if fact.event_id == "S13-BRACKET")
    user = "U0EM01"
    assert user in fact.must_see_for
    for day in (7, 8, 9):
        qualifying = {
            item.item_id
            for item in built.snapshots[day].values()
            if item.thread_id == f"{fact.channel_id}:{fact.thread_ts}" and _qualifies(fact, item)
        }
        assert qualifying == {BRACKET, FLAG_ASK}

    def select_bracket(reader: str, date_: date, selection: Selection) -> Selection:
        if reader != user or date_ not in {world.date_of(d) for d in (7, 8, 9)}:
            return selection
        day = next(d for d in (7, 8, 9) if world.date_of(d) == date_)
        template = built[(user, 7, "full")].lines[0]
        lines = tuple(
            template.model_copy(
                update={
                    "item": built.snapshots[day][item_id],
                    "must_see": False,
                }
            )
            for item_id in chosen
        )
        return selection.model_copy(update={"lines": lines})

    expected = Built()
    expected.update(
        {
            key: select_bracket(key[0], world.date_of(key[1]), selection)
            for key, selection in built.items()
        }
    )
    expected.snapshots = built.snapshots
    for day in (7, 8, 9):
        _, grades, _ = _day_view(expected, world, golden, user, day, "full", ranked=False)
        assert grades == ([2] + [0] * (len(chosen) - 1) if day == 7 else [0] * len(chosen))
    rows = [
        _day_metrics(expected, world, golden, user, day, "full") for day in range(1, world.days + 1)
    ]
    run = Run(WORLD_DIR, tmp_path / "ranking.sqlite", llm, priors, wrap=select_bracket)
    persona = _persona(run.report().configs[0], user)
    assert run.applied > 0
    _assert_scores(persona.overall, [r[0] for r in rows], [r[1] for r in rows])
    _assert_scores(persona.ranked_lane, [r[2] for r in rows], [r[3] for r in rows])


def test_send_everything_drops_c0_deliveries_before_the_ranked_top_five(
    base_run: Run, built: Built, world: World, golden: Golden
) -> None:
    """B14.11. Setup: U0SC01 on day 4; C0 delivers ASK-PO through its must-see lane.

    Action: remove that fact's line from send-everything before taking five, on every day.
    Assert: the named list excludes ASK-PO; each persona's ranked means match the own store.
    """
    c0 = built[("U0SC01", 4, "full")]
    must = [line.item for line in c0.lines if line.must_see]
    assert "ASK-PO" in _deliveries(must, 4, golden)
    assert built.send_ids[("U0SC01", 4)][0] == PO_AMEND_ASK
    ids, _, _ = _day_view(built, world, golden, "U0SC01", 4, "send-everything", ranked=True)
    assert ids[:5] == [SUPPLIER_NOISE, BOM_11034, DROP_IN_ASK, MLCC, ANODIZE_ASK]
    assert PO_AMEND_ASK not in ids
    expected = _expected_days(built, world, golden, "send-everything")
    send = base_run.report().configs[-1]
    for persona in send.per_persona:
        rows = [expected[persona.user_id, day] for day in range(1, world.days + 1)]
        _assert_scores(persona.ranked_lane, [r[2] for r in rows], [r[3] for r in rows])


@pytest.mark.parametrize("zero", [False, True], ids=["configured", "zero-threshold"])
def test_ablation_moved_counts_differing_persona_days_and_prints_each_metric(
    zero: bool, base_run: Run, built: Built, zero_built: Built, world: World, golden: Golden
) -> None:
    """B14.12. Setup: independent day values for C0 and each term, at both thresholds.

    Action: count differing values for each metric, including days without a value.
    Assert: each moved entry and its printed count match; unchanged metrics report zero.
    """
    selections = zero_built if zero else built
    c0 = _expected_days(selections, world, golden, "full")
    expected = {}
    for term in TERMS:
        off = _expected_days(selections, world, golden, term)
        expected[term] = {
            metric: sum(c0[key][index] != off[key][index] for key in c0)
            for index, metric in enumerate(METRIC_NAMES)
        }
    assert len(c0) == world.days * sum(persona.role is not None for persona in world.personas)
    report = base_run.report()
    rows = report.zero_ablation if zero else report.ablation
    assert {row.term: row.moved for row in rows} == expected
    section = write_ranking_section(report).splitlines()
    split = next(i for i, line in enumerate(section) if line.lstrip("# ") == ZERO_HEADING)
    block = section[split:] if zero else section[:split]
    header_line = next(line for line in block if line.startswith("|") and _cells(line)[0] == "term")
    headers = _cells(header_line)
    _, printed = _table_pair(section, zero=zero)
    for cells in printed:
        for metric, count in expected[cells[0]].items():
            index = next(
                i
                for i, header in enumerate(headers)
                if metric.lower() in header.lower() and "drop" in header.lower()
            )
            # Accept a count in the drop cell or a separate adjacent moved column.
            if "moved" in headers[index + 1].lower():
                assert cells[index + 1] == str(count), (cells[0], metric)
            else:
                assert "moved" in (headers[index] + cells[index]).lower()
                numbers = re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])", cells[index])
                assert str(count) in numbers[1:], (cells[0], metric)

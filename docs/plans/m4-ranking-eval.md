# Plan: M4 unit 14, the ranking metrics and the ablation table

Status: v0.2.2, 2026-10-14, 05:00 PT (test review: the report interface and format). Process stage 1 (`docs/process.md`), the full process
the user set for the overnight units. Stacked on the ablation switch (branch
`ablation-switch`, PR #80), which adds `daily.terms_off` and `ablation.min_effect`.

v0.2 folds in the plan reviews and the ranking owner's notes: a line that repeats a fact
from before the window has grade 0; facts the reader cannot see leave the denominator;
the must-see lane and the ranked lane are reported apart, and the verdict reads both;
each configuration reports how many persona-days its top 5 changes; the metric helpers
take grades, not ids; the report shows each persona; the verdict can be "no vote".

## Goal

SPEC section 7 asks for must-see recall at 5 and nDCG at 5 per persona per day, the
role-only and send-everything baselines beside them, and a ranking ablation: each score
term switched off in turn, with a keep or remove verdict by `ablation.min_effect`. None
of these is built. This unit builds them in `src/digest/eval/ranking.py` and adds the
table to the eval report. Stale-belief person-days (the headline metric) is out of
scope; until it exists, must-see recall or mean nDCG at 5 decide (SPEC section 7, D21).

## Definitions

The run: one store, world day by world day, as the extraction eval runs it
(`_run_days`: replay to the end of the day, extract, on cassettes). After each day d,
for each human persona P with a role and each configuration C, the eval selects P's
digest for d with `select_digest(store, world, priors_C, P, d, previous_digest=d-1)`
(a first digest on world day 1), as `digest demo` does.

- R1. The window of (P, d) is the window of that selection: the world days after the
  previous digest day up to d, or the first-digest working days on day 1.
- R2. Golden facts in the window, with a grade for P. A fact is in P's set only when P
  can read its channel (`can_see`, as the selection's visibility check reads it; for an
  absence, the channel of the event it cites). In P's set:
  - a golden item G with `G.day` in the window: grade 2 when P is in `G.must_see_for`,
    grade 1 when P's role is in `G.affected_roles`, else 0;
  - a golden absence A with `A.day` in the window: grade 2 when P (an ask) or P's role
    (a checklist absence) is in `A.audience`, else 0. The selection shows every
    checklist absence to the EM as must-see, but the golden audience is the task owner's
    role, so the EM's extra absence lines score 0. The report shows this; it is a
    disagreement between the labels and the must-see rule, not an eval error.
  Must-see facts are the facts of grade 2.
- R3. A digest line delivers a golden item when the line's item is the item that the
  extraction eval's pairing (`_pairs`, on the items as of d) gives that event. It delivers
  a golden absence when the line is an absence item with the key `_absence_key` gives
  (day, source, task or ask event). A line delivers at most one fact, and each fact
  counts once (its first line). A line's grade is the grade of the fact it delivers when
  that fact is in P's set for the window, else 0: a line that repeats an older fact, such
  as an open blocker from an earlier day, adds nothing.
- R4. The top 5 are the first five lines of the selection in digest order (sections in
  order, must-see lines first in each section). The rendered digest shows that order.
- R5. Must-see recall at 5 for (P, d): lines of grade 2 in the top 5 over must-see facts
  in P's set. A (P, d) with no must-see fact has no value; `n_without` counts it.
- R6. nDCG at 5 for (P, d): DCG over the line grades of the top 5, with gain
  `2^grade - 1` and discount `log2(rank + 1)`, over the DCG of the five best grades in
  P's set. A (P, d) whose set has no grade above 0 has no value; `n_without` counts it.
  Both values are at most 1 because a fact counts once and only facts in the set score.
- R6a. The lanes. For each configuration the report also gives the ranked-lane metrics:
  recall and nDCG computed as R5 and R6 over the ranked lines only (must-see lane lines
  removed, top 5 of what is left), against P's set without the facts that C0's must-see
  lane delivers on (P, d). The switch never touches the must-see lane (ablation-switch
  AB9). Sections come in order with must-see lines first in each, so on a day where
  must-see lines take the first five places the overall top 5 is the same in every
  configuration, and only the ranked lane can show a term's effect there. Per persona, the
  report counts the must-see facts that C0 delivers through each lane.
- R7. A configuration's value for a metric is the mean over personas of each persona's
  mean over its days with a value (SPEC: "mean across personas"). A persona with no day
  with a value leaves the mean. The report gives n (persona-days with a value) and the
  number of personas beside every value.

Configurations:
- C0 `full`: the configured priors.
- C1 to C4: `terms_off` holds one of `importance`, `role_prior`, `personal_relevance`,
  `recent_activity`.
- Baseline `role-only`: `terms_off` holds `importance` and `personal_relevance`, so the
  role prior alone ranks, with the must-see lane unchanged.
- Baseline `send-everything`: every stored item as of d, of any status, visible to P,
  whose newest evidence falls in the window, newest evidence first, then `item_id`; before them, the absence items that
  C0's selection holds for (P, d) (their evidence is old by design), by `item_id`. No
  must-see lane and no cap.
- R7a. Sensitivity: for C1 to C4 and the baselines, the report gives the number of
  persona-days whose overall top 5 or ranked-lane top 5 (item ids in order) differs from
  C0's. A term whose row is 0
  cannot move any metric on this world, and the table says so.
- R7b. A day that `select_digest` refuses (for example extract deltas left pending by a
  failed extraction) stops the eval with that refusal. On cassettes no day is refused.

- R8. The verdict for each term C1 to C4, per metric (overall recall, overall nDCG,
  ranked-lane recall, ranked-lane nDCG): the relative drop is `(c0 - off) / c0`. A metric
  votes keep when the drop is at least `ablation.min_effect`, and remove otherwise (a rise
  votes remove). A metric whose C0 value is 0 or missing gives no vote. The term's verdict
  is keep when any metric votes keep, remove when at least one metric votes and none votes
  keep, and "no vote" when no metric votes.

## Interface

```python
class Scores(Frozen):
    recall_at_5: float | None
    recall_n: int        # persona-days with a value
    ndcg_at_5: float | None
    ndcg_n: int
    recall_without: int  # persona-days with no must-see fact
    ndcg_without: int    # persona-days with no fact of grade above 0
    recall_personas: int # personas in the recall mean
    ndcg_personas: int   # personas in the nDCG mean

class PersonaScores(Frozen):
    user_id: str
    name: str
    role: str
    overall: Scores
    ranked_lane: Scores
    must_see_lane_facts: int   # must-see facts C0 delivers through the must-see lane
    ranked_lane_facts: int     # must-see facts C0 delivers through the ranked lane

class ConfigScores(Frozen):
    name: str
    terms_off: tuple[str, ...]
    overall: Scores
    ranked_lane: Scores
    changed_days: int          # persona-days whose overall or ranked-lane top 5 differs from C0
    per_persona: tuple[PersonaScores, ...]

class AblationRow(Frozen):
    term: str
    drops: dict[str, float | None]   # metric name -> relative drop; None: no vote
    verdict: Literal["keep", "remove", "no vote"]

class RankingReport(Frozen):
    configs: tuple[ConfigScores, ...]   # C0, C1 to C4, role-only, send-everything
    ablation: tuple[AblationRow, ...]
    min_effect: float

def run_ranking_eval(world_dir: Path, llm: LLM, priors: Priors, store_path: Path) -> RankingReport
def recall_at_5(lines: Sequence[int], facts: Sequence[int]) -> float | None
def ndcg_at_5(lines: Sequence[int], facts: Sequence[int]) -> float | None
def verdict(term: str, c0: Mapping[str, float | None], off: Mapping[str, float | None], min_effect: float) -> AblationRow
```

`lines` are the grades of the lines in digest order (R3); `facts` are the grades of the
facts in P's set. Both helpers and `verdict` are pure, so their tests need no store.

- R9. `digest eval` runs the ranking eval after the extraction eval, in its own fresh
  store `data/eval-ranking-<world>.sqlite`, and appends a "Ranking" section to the report:
  the configuration table (overall and ranked-lane recall and nDCG with n, n_without,
  changed days), the ablation table (each drop and the verdict, with the `min_effect`
  value), the per-persona table for C0 (both metric families with n, and the facts per
  lane), and the line "Labels come from the world script; personas are simulated." The
  committed report `reports/eval-2026-10-23.md` is regenerated in this unit, since
  `tests/test_corpus_data.py` compares it byte for byte.
  The section is written by `write_ranking_section(report: RankingReport) -> str`;
  `write_report(report, path, ranking=None)` appends it when `ranking` is given, and
  `tests/test_corpus_data.py` passes the ranking report so the byte comparison covers it.
  Numbers print with 3 decimals; a missing value prints as "n/a". The `min_effect` line
  reads exactly "Keep rule: a term stays when a metric drops by at least <min_effect>
  of its value with the term on." with `min_effect` printed as given in the priors.

## Behaviours

- B14.1 `ndcg_at_5`: ideal order gives 1.0; the best fact at rank 2 instead of 1 gives
  the hand-computed value; facts all of grade 0 give None; fewer than five lines work.
- B14.2 `recall_at_5`: a grade-2 line at rank 6 does not count; no grade-2 fact gives None.
- B14.3 `verdict`: a 2% drop keeps at `min_effect` 0.01; a 0.5% drop removes; a C0 value
  of 0 gives no vote; all metrics without a vote give "no vote"; a rise votes remove.
- B14.4 On the Kestrel world with cassettes, for two named persona-days, one of them
  with a ranked-lane must-see fact: C0's line grades, recall and nDCG match values the
  test computes by hand from `golden.jsonl` and the recorded selection.
- B14.5 The priors that reach `select_digest` for each configuration carry the named
  `terms_off` (a spy on the call); C4's `changed_days` equals the count the test computes
  by comparing C0 and C4 top-5 lists itself.
- B14.6 A fact on a channel the reader cannot see is not in the reader's set; an open
  blocker from before the window has grade 0 on a later day; a fact delivered twice
  scores once.
- B14.7 `digest eval` writes the Ranking section with seven configuration rows, four
  ablation rows and one row per human persona; the run is offline (cassettes); the
  committed report matches.

## Out of scope

Stale-belief person-days, time-to-told and novelty rate (SPEC section 7 headline, M4
stretch); role separation and phase sensitivity; the regression gate; pairwise judging.

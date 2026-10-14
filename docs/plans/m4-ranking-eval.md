# Plan: M4 unit 14, the ranking metrics and the ablation table

Status: v0.1, 2026-10-14, 02:10 PT. Process stage 1 (`docs/process.md`), the full process
the user set for the overnight units. Stacked on the ablation switch (branch
`ablation-switch`, PR #80), which adds `daily.terms_off` and `ablation.min_effect`.

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
- R2. Golden facts in the window, with a grade for P:
  - a golden item G with `G.day` in the window: grade 2 when P is in `G.must_see_for`,
    grade 1 when P's role is in `G.affected_roles`, else 0;
  - a golden absence A with `A.day` in the window: grade 2 when P (an ask) or P's role
    (a checklist absence) is in `A.audience`, else 0.
  Must-see facts are the facts of grade 2.
- R3. A digest line delivers a golden item when the line's item is the item that the
  extraction eval's pairing (`_pairs`, on the items as of d) gives that event. It delivers
  a golden absence when the line is an absence item with the same key as
  `_absence_key` gives (day, source, task or ask event). A line delivers at most one fact.
- R4. The top 5 are the first five lines of the selection in digest order (sections in
  order, must-see lines first in each section). The rendered digest shows that order.
- R5. Must-see recall at 5 for (P, d): delivered must-see facts in the top 5 over
  must-see facts in the window. A (P, d) with no must-see fact has no value and is
  counted in `n_without`.
- R6. nDCG at 5 for (P, d): DCG over the top 5, with gain `2^grade - 1` and discount
  `log2(rank + 1)`, over the DCG of the five best grades of the facts in the window. A
  (P, d) whose facts all have grade 0 has no value and is counted in `n_without`.
- R7. A configuration's value for a metric is the mean over personas of each persona's
  mean over its days with a value (SPEC: "mean across personas"). The report gives n
  (persona-days with a value) beside every value.

Configurations:
- C0 `full`: the configured priors.
- C1 to C4: `terms_off` holds one of `importance`, `role_prior`, `personal_relevance`,
  `recent_activity`.
- Baseline `role-only`: `terms_off` holds `importance` and `personal_relevance`, so the
  role prior alone ranks, with the must-see lane unchanged.
- Baseline `send-everything`: every item visible to P whose newest evidence falls in the
  window, ordered by newest evidence then `item_id`, with no must-see lane and no cap.

- R8. The verdict for each term C1 to C4: keep when its recall or its nDCG with the term
  off is lower than C0's by at least `ablation.min_effect` times C0's value (a relative
  drop); else remove. When C0's value is 0 or missing for a metric, that metric gives no
  vote and the table says so.

## Interface

```python
class ConfigScores(Frozen):
    name: str
    terms_off: tuple[str, ...]
    recall_at_5: float | None
    recall_n: int
    ndcg_at_5: float | None
    ndcg_n: int
    per_persona: dict[str, tuple[float | None, float | None]]  # user id -> (recall, ndcg)

class AblationRow(Frozen):
    term: str
    recall_drop: float | None   # relative drop against C0; None when C0 gives no vote
    ndcg_drop: float | None
    verdict: Literal["keep", "remove"]

class RankingReport(Frozen):
    configs: tuple[ConfigScores, ...]   # C0, C1 to C4, role-only, send-everything
    ablation: tuple[AblationRow, ...]
    min_effect: float

def run_ranking_eval(world_dir: Path, llm: LLM, priors: Priors, store_path: Path) -> RankingReport
def recall_at_5(lines: Sequence[str | None], grades: Sequence[int]) -> float | None
def ndcg_at_5(lines: Sequence[int], grades: Sequence[int]) -> float | None
```

`recall_at_5` and `ndcg_at_5` are pure functions over the grades of the delivered lines
in order and the grades of all facts in the window, so their tests need no store.

- R9. `digest eval` runs the ranking eval after the extraction eval, in its own fresh
  store `data/eval-ranking-<world>.sqlite`, and appends a "Ranking" section to the report:
  one table of the seven configurations (recall, n, nDCG, n) and one ablation table
  (term, recall drop, nDCG drop, verdict), with the `min_effect` value, and the line
  "Labels come from the world script; personas are simulated."

## Behaviours

- B14.1 `ndcg_at_5`: ideal order gives 1.0; the best fact at rank 2 instead of 1 gives
  the hand-computed value; grades all 0 give None; fewer than five lines work.
- B14.2 `recall_at_5`: a must-see fact at rank 6 does not count; no must-see fact gives
  None.
- B14.3 On the Kestrel world with cassettes: C0's must-see recall and nDCG and their n
  match values computed by hand in the test from `golden.jsonl` and the recorded digests
  for two named persona-days.
- B14.4 Each C1 to C4 selection has `terms_off` set as named; `role-only` sets both terms.
- B14.5 The verdict rule: with stub scores, a 1% drop keeps at `min_effect` 0.01, a
  0.5% drop removes; a C0 value of 0 gives no vote.
- B14.6 `digest eval` writes the Ranking section with seven configuration rows and four
  ablation rows; the run is offline (cassettes).

## Out of scope

Stale-belief person-days, time-to-told and novelty rate (SPEC section 7 headline, M4
stretch); role separation and phase sensitivity; the regression gate; pairwise judging.

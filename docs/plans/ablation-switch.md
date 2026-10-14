# Plan: a switch that turns ranking terms off, for the ablation

Status: v0.2, 2026-10-14. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, the code is written outside the test writer, every test is RED-GREEN proven,
both review lanes run. v0.2 folds in the advisor review and an independent Codex plan
review.

## Goal

SPEC section 7 asks for a ranking ablation: each score term switched off in turn, "off"
meaning the term is set to 1 with everything else fixed. A term stays when switching it
off lowers must-see recall or mean nDCG at 5 (until stale-belief person-days exists) by at
least `ablation.min_effect` of the value with the term on: a relative drop, 0.01 meaning
1%, averaged across personas. Computing the metrics, the averages and a zero baseline
belongs to the eval unit in `digest/eval`. This unit adds only the switch the ranker
reads and the configured minimum. It changes no digest: the switch is empty unless the
eval sets it.

## Rule

Names and meaning:
- Score terms: `importance`, `role_prior` and `personal_relevance`, the three factors of
  today's score.
- One component: `recent_activity`, the lift inside `personal_relevance`
  (`docs/plans/recent-activity.md`). It is not a fourth factor. Switching it off removes
  only the lift. When both `personal_relevance` and `recent_activity` are off,
  `personal_relevance` off decides: relevance is 1.

The switch:
- `DailyPriors.terms_off: frozenset[Literal["importance", "role_prior",
  "personal_relevance", "recent_activity"]] = frozenset()`.
- It accepts a Python set or frozenset, or a YAML list, and stores a frozenset; duplicates
  collapse. It rejects an unknown name (a literal error on that member), a scalar string
  and null. `config/priors.yaml` does not set it.
- `Priors.ablation: AblationPriors = AblationPriors()`, so priors built without the key
  keep loading. `AblationPriors.min_effect: float = 0.01`, above 0 and at most 1.
  `config/priors.yaml` sets `ablation: {min_effect: 0.01}`.

The score and the reason in `_ranked_line`:
- Each switched-off factor is 1.0 in the score. The score keeps full precision; the
  threshold compares that score, and ordering rounds to six significant figures, as
  today.
- The reason keeps today's tokens, order and formatting (`round(value, 3)` for
  importance and role prior, plain float formatting for relevance). A switched-off factor
  shows as `<term>=1.0 (off)`. Full strings for an item with importance 0.63, role prior
  0.5 and a recent-activity lift on entities A and Z:
  - nothing off: `importance=0.63 role_prior=0.5 personal_relevance=0.8 (recent activity: A, Z)`
  - `importance` off: `importance=1.0 (off) role_prior=0.5 personal_relevance=0.8 (recent activity: A, Z)`
  - `role_prior` off: `importance=0.63 role_prior=1.0 (off) personal_relevance=0.8 (recent activity: A, Z)`
  - `personal_relevance` off (with or without `recent_activity`): `importance=0.63 role_prior=0.5 personal_relevance=1.0 (off)`
  - `recent_activity` off: `importance=0.63 role_prior=0.5 personal_relevance=0.4`
  - `importance` and `role_prior` off: `importance=1.0 (off) role_prior=1.0 (off) personal_relevance=0.8 (recent activity: A, Z)`
- Unchanged: the must-see lane, the window, the visibility check, the caps and the order
  rule.

Consequences in selection, which the eval measures (not bugs):
- The three factors are each at most 1, so switching one off never lowers a score. More
  items can cross `min_score`, and within a section's cap a raised item can push out
  another.
- Switching `recent_activity` off can lower a score, so a lifted item can fall below the
  threshold or out of the cap.
- With all three factors off, every ranked score is 1 and lines follow the tie-break
  order (newest evidence, then item id).

## Doc changes (first commit)

- SPEC section 7: the ranker reads `daily.terms_off`; the three term names and the
  `recent_activity` component; `personal_relevance` off covers the lift; the switch is
  empty outside the ablation; `min_effect` is a relative fraction.
- `config/priors.yaml`: `ablation.min_effect: 0.01` with a comment.

## Behaviours

Feature checks (each fails on today's code, which rejects the `terms_off` field):
- AB1. Each of `importance`, `role_prior` and `personal_relevance` off, on an item with
  non-neutral factors and a qualifying lift: the exact score and the exact reason string
  above. Paired with the empty set.
- AB2. `recent_activity` off: a lifted item scores at its static relevance with the exact
  reason above; an item that is not lifted keeps its score and reason.
- AB3. `personal_relevance` off: owned, affected-role, other and lifted items all score
  with relevance 1 and the reason `personal_relevance=1.0 (off)`; with
  `recent_activity` also off, the same.
- AB4. `importance` and `role_prior` off together: the exact score and reason.
- AB5. Validation, built with `model_validate` (not `model_copy`, which skips
  validation): a valid set is accepted and stored as a frozenset; a YAML list with a
  duplicate is accepted; in the same test an unknown member fails with a literal error on
  `terms_off`, and a scalar string and null fail.
- AB6. Threshold and cap: an item below `min_score` crosses it with `importance` off; a
  lifted item falls below it with `recent_activity` off (paired cases).
- AB7. `AblationPriors` defaults and validation (above 0, at most 1), loading from YAML,
  the bundled value 0.01, and priors without the `ablation` key still loading.

Preservation checks (they pass today; the RED proof is a mutation after the code exists):
- AB8. With the empty set, every line's score and reason equal today's, including a role
  prior with more than three decimals (displayed rounded, scored in full).
- AB9. Must-see lines, compared by item id, section and reason, are the same with any
  terms off.

## Acceptance

- Tests in `tests/test_select.py` and `tests/test_priors.py` cover AB1 to AB9; each new or
  changed test is RED-GREEN proven.
- `uv run digest demo` output with the default (empty) switch is byte-identical before and
  after.
- The extraction eval report does not change.

## Not in scope

The ablation eval and its golden must-see sets (another unit), terms that are not built
(novelty, sender affinity, decision exposure and the rest of SPEC section 5 step 6), and a
CLI flag.

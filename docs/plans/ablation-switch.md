# Plan: a switch that turns ranking terms off, for the ablation

Status: v0.1, 2026-10-14. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, the code is written outside the test writer, every test is RED-GREEN proven,
both review lanes run.

## Goal

SPEC section 7 asks for a ranking ablation: "each score term switched off in turn ... Off
means the term is set to 1 with everything else fixed", with a term kept only when
switching it off lowers must-see recall or nDCG by at least `ablation.min_effect`. The
eval side is built in another unit, in `digest/eval`. This unit adds only the switch the
ranker reads and the configured minimum. It changes no digest: the switch is empty unless
the eval sets it.

## Rule

- Terms: `importance`, `role_prior`, `personal_relevance` and `recent_activity`, the names
  `_ranked_line` uses (the last is the lift of `docs/plans/recent-activity.md`).
- `DailyPriors.terms_off: frozenset[Literal["importance", "role_prior",
  "personal_relevance", "recent_activity"]]`, default empty. An unknown name fails
  validation, so a typo cannot ablate nothing. `config/priors.yaml` does not set it; the
  eval builds priors with the set it needs.
- In `_ranked_line`, each listed term is 1.0 and the score is the product of the others:
  - `importance` off: importance is 1; the reason shows `importance=1 (off)`.
  - `role_prior` off: role prior is 1; the reason shows `role_prior=1 (off)`.
  - `personal_relevance` off: relevance is 1, lift included; the reason shows
    `personal_relevance=1 (off)` and no recent-activity suffix.
  - `recent_activity` off: the lift is not applied, so relevance keeps its static level
    (1.0, 0.8 or 0.4); the reason shows that level and no suffix.
  - Several terms can be off together.
- A reason never shows a value the score did not use.
- Unchanged: the must-see lane, the window, the visibility check, the threshold
  `min_score` (it applies to the score with terms off, so a line can enter or leave the
  ranked lane), the caps and the order rule.
- `Priors.ablation: AblationPriors` with `min_effect: float = 0.01`, above 0 and at most 1,
  read from a top-level `ablation:` key in `config/priors.yaml`, which sets 0.01.

## Doc changes (first commit)

- SPEC section 7: the ranker reads `daily.terms_off`; the term names; the switch is empty
  outside the ablation.
- `config/priors.yaml`: `ablation.min_effect: 0.01` with a comment.

## Behaviours

- AB1. Each of `importance`, `role_prior` and `personal_relevance` off: that factor is 1 in
  the score and the reason shows `<term>=1 (off)`; the other factors and their reason
  text do not change. Paired with the empty set. Fails on today's code.
- AB2. `recent_activity` off: an item that would be lifted scores at its static relevance
  and its reason has no suffix; an item that is not lifted does not change.
- AB3. `personal_relevance` off: owned, affected-role, other and lifted items all score
  with relevance 1, and no reason carries the suffix.
- AB4. Two terms off together multiply as expected.
- AB5. An unknown term name fails validation; the field is absent from the bundled config
  and defaults to empty.
- AB6. Preservation: with the empty set, every line's score and reason equal today's.
- AB7. Must-see lines are the same with any terms off; the threshold applies to the score
  with terms off (a paired case where a line crosses it).
- AB8. `AblationPriors` defaults and validation (above 0, at most 1), loading from YAML,
  and the bundled value 0.01.

## Acceptance

- Tests in `tests/test_select.py` and `tests/test_priors.py` cover AB1 to AB8; each new or
  changed test is RED-GREEN proven, and AB1 fails on today's code.
- `uv run digest demo` output is byte-identical before and after.
- The extraction eval report does not change.

## Not in scope

The ablation eval itself (another unit), terms that are not built (novelty, sender
affinity, decision exposure and the rest of SPEC section 5 step 6), and a CLI flag.

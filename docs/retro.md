# Retro

One short entry per unit (docs/process.md, stage 5): what the reviews caught, what they
missed, and one process change at most. The newest entry is first.

## 2026-10-12: channel_id on evidence, claims and task links (PR #20)

Caught. An independent adversarial review mutated the fix. Two pair-keyed lookups in
`src/digest/extract/verify.py` had no test: the `held` filter in `_merge` and the key in
`_unique_claims`. A revert of either one to the ts alone passed all 173 tests in the
changed files. Two tests were added before the merge. The review found no defect in
the code.

Missed. The first test commit covered the lookups that the task named and did not cover
the merge filters. prove-test can only blank source lines, so 6 of 10 RED checks were
manual patches. A suggestion to rebase `m2-eval` from origin did not see a local
worktree with 3 unpushed commits. The suggestion was withdrawn, and the owning session
did the rebase.

Change. When a diff changes a lookup key, run a mutation pass that reverts each changed
key to the old key, and add a test for each revert that passes.

## M2 units 1 to 4 (2026-10-12)

Caught. Test reviews found that tests were impossible or blind before any code
existed: a golden order asserted two incompatible ways, a fixture that extracted an ask
as an `fyi` so the ask absence source never ran, cassette helpers built from inputs the
stage would not pass, and an anchor rule (the detector on mentions, the golden labels on
claims) that the plan itself stated two ways. Code reviews found reachable gaps the
tests did not reach: overflow from model-supplied offsets, a missing anchor file read as
empty, two outputs for one item overwriting each other, and a resume cursor that skipped
a failed thread.

Missed. Fuzzy checks on prompt prose took four rounds to settle; a regex over free
wording kept accepting a wrong count until the ruling became one exact phrase. One fix
round wrote a test that contradicted an M1 test, because the brief changed a shared
store rule without checking the tests that pin it.

Change. Before a brief changes the behaviour of a module outside the unit, grep the
tests that pin that module and name them in the brief.

## M4 unit 14, ranking metrics and the ablation (2026-10-14)

Caught. The first run said three of four score terms should go and send-everything beat
the full ranker. The ranking owner asked for a confound check before anyone believed it:
with `min_score` 0, most of the effect of two terms was admission through the score
threshold, not order. An independent review of the grading then found three real
defects (credit tied to one of two duplicate items, send-everything lines that took
ranked-lane slots at grade 0, and no count of the persona-days behind each verdict), and
the final review found that the ranked lane reassigned facts after it dropped lines.

Missed. The plan named "off means 1" without asking what that does to a fixed threshold;
the admission confound was visible from the ablation-switch plan's own consequences
section.

Change. When a metric compares configurations that change score scale, run one
configuration that holds admission fixed before reading any verdict.

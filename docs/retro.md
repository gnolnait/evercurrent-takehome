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

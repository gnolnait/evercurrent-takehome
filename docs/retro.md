# Retro

One short entry per unit (docs/process.md, stage 5): what the reviews caught, what they
missed, and one process change at most. The newest entry is first.

## 2026-10-13: the demo room, units 8 to 10 (PRs #62, #67, #68, #72 and unit 10)

Caught. Measuring the plan's examples on the real day-6 store before any test was
written corrected three expected effects, and measuring again after the digest window
merged corrected two more; the test author pinned measured numbers, not guesses. Plan
review found the central constraint early: the extraction request carries every
person's role and every gate date, so the room keeps two views of the world. Code and
adversarial reviews found a toggle that passed every set-value test, a curated edit that
missed its cassette after another change, a reseed reference that could never push a
revert back to Slack, and a save that could empty the copy, or delete the corpus's
parent folder, on a failure or a bad path.

Missed. The first tests read a cached state after a refusal, so a half-written store
would have passed. The implementer worked against a fake that the protocol change broke,
because the brief changed a shared protocol without naming the test fake that implements
it. Codex rate-limited once (429) and the Claude lens took that lane.

Change. When a brief changes a protocol or a shared type, name every test fake that
implements it, and give the fake's change to the test author in the same round.

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

## M3 unit 11, failures become refusals (2026-10-14)

Caught. The plan review found that a failed reset dropped the room's message changes,
because `Room.reset` cleared them before it rebuilt; the test for it now saves after the
failure. The adversarial pass found that a gateway reply of valid JSON in the wrong
shape still crashed `digest extract` with a raw error, the same symptom the unit set out
to remove. Seven wrong-shape cases now become one refusal.

Missed. The second-model lane ran out of quota near the end; the local lens panel took
the final review. Three proofs needed manual RED runs, because a blank-line break cannot
widen an `except` clause or reorder two assignments.

Change. When a unit wraps a parser's errors, list every field the code reads after the
parse and give each a wrong-shape case in the first test commit.

## M4 unit 12a, the query function layer (2026-10-14)

Caught. A probe before the plan review showed that a `mode=ro` connection still lets
`VACUUM INTO` and `ATTACH` create files, so `query` got an allowlist authorizer. The
plan review found that a day other than the store's day would read later item versions,
so the day now comes from the store. The code review found that a per-cell length limit
does not bound a result of many wide rows; `query` now has a column limit and an 8 MB
result budget.

Missed. The first test round pinned only the must-see line of a digest; empty ranked
lists passed. The test review caught it before any code existed.

Change. When a function takes caller SQL, probe each write path (`VACUUM INTO`,
`ATTACH`, pragmas, temp tables) and the result size on a real connection before the
plan is reviewed.

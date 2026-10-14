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

## M4 unit 12b, the MCP server (2026-10-14)

Caught. The plan review found that a context kept for the server's life would read a
stale day after a replay and would hold a Windows file handle, so each tool call now
opens the store and closes it. A spike before the plan found that SDK 2.x cannot be
imported on this laptop, which set the SDK to 1.30.0.

Missed. The first test round compared each tool's result with the bare view, but the
SDK wraps a tuple or an optional view in a "result" field; two reviewers caught it from
a probe of the installed SDK.

Change. Before a plan names an SDK's output shape, call the SDK once on each return type
the plan uses and paste what comes back into the plan.

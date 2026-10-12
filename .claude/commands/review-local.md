---
description: Run the review-lens panel locally on the current branch and write a verified verdict
argument-hint: "[--base <ref>] [--lenses a,b]"
---

Run the code-review panel on this branch. You are the orchestrator: follow
`.claude/review-lenses/ORCHESTRATOR.md` end to end, with the choices below.

Arguments: $ARGUMENTS


## 1. Compute the range

- BASE: `--base <ref>` if given, else `git merge-base HEAD main`. Resolve to a SHA.
- HEAD: `git rev-parse HEAD`.
- **Dirty tree** — if `git status --porcelain --untracked-files=no` is non-empty, stop and say so: a verdict
  would describe code no commit holds. Commit or stash first. Do not work around it.
- `<root>`: the first `worktree` path that `git worktree list --porcelain` prints. This is
  the main checkout, also when you run in a linked worktree. Every `.review/` path in
  this command and in `ORCHESTRATOR.md` is under `<root>`, never under the current checkout.

To see the change first: `git diff --stat <base>...<head>`.


## 2. Choose the lenses

If `--lenses` was passed, use exactly those (primary or secondary names, e.g.
`adversarial,tests` or `vacuity`). Otherwise apply `.claude/review-lenses/SELECTION.md`
to this diff — the same file the orchestrator reads, so the selection is the same.

Match `.claude/rules/*.md` against the changed files by each rule's `paths:` globs; the
matched rule paths go into the dispatch prompt.


## 3. Dispatch and verify: the `review-local` workflow

Run `.claude/workflows/review-local.js` with the Workflow tool. Pass it as `scriptPath`,
as an absolute path under `git rev-parse --show-toplevel` (the current checkout, not
`<root>`). Do not run it by name. The name lookup does not read this checkout's file.
Pass these `args`:

- `base`, `head`: the SHAs from step 1.
- `lenses`: the lens names from step 2.
- `rule_paths`: the matched rule paths.
- `prior`: the `findings` of an earlier verdict at this head (a re-run), else `[]`.
- `carried`: the `findings` of verdicts at older heads of this branch, each with that
  verdict's `head` added, else `[]`.

Read the earlier verdicts from `<root>/.review/*/verdict.json` and match on the
verdict's `branch` key, as in the STEP 4 ratchet of `ORCHESTRATOR.md`.
- `max_verify`: 8, unless the user asks for another cap.

The workflow does STEP 3 and the per-lead part of STEP 4 of `ORCHESTRATOR.md`. It sends
every lens the same prompt through its own `review-lens-<lens>` agent type, and
dispatches a lens once more when its reply does not name that lens. It merges leads that
one fix resolves, gives each lead one verifier, and checks each carried finding against
HEAD.

If the Workflow tool is not available, do STEP 3 and STEP 4 of `ORCHESTRATOR.md` by
hand: one `Agent` call per lens with `subagent_type: review-lens-<lens>`, all in one
message, the same prompt for each, `run_in_background: false`. A reply that is not
`{"coverage": {...}, "findings": [...]}` is a broken dispatch: dispatch that lens once
more, and if it fails again, report it as not run.


## 4. Finish verification

If you ran the workflow, take its result from a file. The task notification can omit
part of `<result>`. It also encodes `<`, `>` and `&` as HTML entities. Read the JSON
file named in `<output-file>`. Use its `result` key. Copy quotes from that value.

Map that result onto the STEP 5 keys:

- `kept` goes to `findings`. Merge two kept findings that one fix resolves; the grouping
  step can miss a pair. Remove the `anchor`, `from_prior` and `from_lens` keys.
- A `dropped` entry with a non-empty `prior` was a finding of the earlier verdict. Read
  its `refuter` and `anchor`. If the refuter holds, put it in `retractions` with that
  reason. If it does not hold, keep the finding. One verifier can drop on one run what
  it kept on the last.
- `carried`: `still_present` goes to `findings` with `carried_from` set to the original
  file and line. If a kept finding names the same defect, merge the two into one entry
  and keep `carried_from`. `addressed` goes to `addressed`; `retracted` goes to
  `retractions`. A carried entry with `result: null` was not checked: keep its original
  finding unchanged in `findings`.
- A lens with `ok: false` did not run. Say so in `coverage`.
- If `unverified` is not empty or `verification_complete` is false, set
  `verification_complete: false`. An `unverified` entry with a non-empty `prior` was a
  finding of the earlier verdict: keep that finding unchanged in `findings`.


## 5. Write the verdict

Write the STEP 5 JSON to `<root>/.review/<head-sha>/verdict.json`. `.review/` is local
scratch and must stay gitignored; never commit it. If verification did not finish, set
`verification_complete: false` and say the review is incomplete — never present
partial results as a clean review.


## 6. Report

Lead with the DIRECTION judgment. Then group findings by lens, each as
`file:line — severity — reason`, and state which files each lens read (from its
`coverage.files_read`). A finding about a file its lens never read is out of that
lens's evidence — say so rather than presenting it as a gap in the code. List any
"Unverified leads" (findings no lens raised) separately, then the full verdict path
under `<root>`.

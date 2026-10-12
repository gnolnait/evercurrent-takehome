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

To see the change first: `git diff --stat <base>...<head>`.


## 2. Choose the lenses

If `--lenses` was passed, use exactly those (primary or secondary names, e.g.
`adversarial,tests` or `vacuity`). Otherwise apply `.claude/review-lenses/SELECTION.md`
to this diff — the same file the orchestrator reads, so the selection is the same.

Match `.claude/rules/*.md` against the changed files by each rule's `paths:` globs; the
matched rule paths go into the dispatch prompt.


## 3. Dispatch and verify: the `review-local` workflow

Run the saved workflow `.claude/workflows/review-local.js` with the Workflow tool, by
name (`review-local`). The workflow list loads when a session starts; if the name does
not resolve, pass the file as `scriptPath`. Pass these `args`:

- `base`, `head`: the SHAs from step 1.
- `lenses`: the lens names from step 2.
- `rule_paths`: the matched rule paths.
- `prior`: the `findings` of an earlier verdict at this head (a re-run), else `[]`.
- `carried`: the `findings` of verdicts at older heads of this branch, each with that
  verdict's `head` added, else `[]`.
- `max_verify`: 8, unless the user asks for another cap.

The workflow does STEP 3 and the per-lead part of STEP 4 of `ORCHESTRATOR.md`. It sends
every lens the same prompt through its own `review-lens-<lens>` agent type, and
dispatches a lens once more when its reply does not name that lens. It merges leads that
one fix resolves, gives each lead one verifier, and checks each carried finding against
HEAD.

If the Workflow tool is not available, do STEP 3 and STEP 4 of `ORCHESTRATOR.md` by
hand: one `Agent` call per lens with `subagent_type: review-lens-<lens>`, all in one
message, the same prompt for each, `run_in_background: false`.


## 4. Finish verification

Map the workflow result onto the STEP 5 keys:

- `kept` goes to `findings`. Merge two kept findings that one fix resolves; the grouping
  step can miss a pair. Remove the `anchor`, `from_prior` and `from_lens` keys.
- A `dropped` entry with a non-empty `prior` goes to `retractions`, with its `reason`.
- `carried`: `still_present` goes to `findings` with `carried_from` set to the original
  file and line; `addressed` goes to `addressed`; `retracted` goes to `retractions`.
- A lens with `ok: false` did not run. Say so in `coverage`.
- If `unverified` is not empty or `verification_complete` is false, set
  `verification_complete: false`.


## 5. Write the verdict

Write the STEP 5 JSON to `.review/<head-sha>/verdict.json`. `.review/` is local scratch
and must stay gitignored; never commit it. If verification did not finish, set
`verification_complete: false` and say the review is incomplete — never present
partial results as a clean review.


## 6. Report

Lead with the DIRECTION judgment. Then group findings by lens, each as
`file:line — severity — reason`, and state which files each lens read (from its
`coverage.files_read`). A finding about a file its lens never read is out of that
lens's evidence — say so rather than presenting it as a gap in the code. List any
"Unverified leads" (findings no lens raised) separately, then the verdict path.

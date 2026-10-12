---
name: ci-workflows
description: |
  Traps that make a CI check pass without running, never report, or report nothing
  useful, plus the coupling between prek hooks and CI. Loads when a GitHub Actions
  workflow or the prek config is read.
paths:
  - ".github/workflows/**"
  - ".pre-commit-config.yaml"
---
# CI workflow traps

`ci.yml` is the one workflow. It runs the gate set from `AGENTS.md` (ruff, ty,
lint-imports, pytest in replay mode, the hook guard tests) on every pull request and on
push to `main`. Flag a change here only when it matches a signature below.


## A skipped job reads as a pass

A job that an `if:` skips still reports, with conclusion `skipped`, and GitHub shows it
green. A gate that skips on some event has run nothing for that event.

- **Signature:** a new `if:` on a job or step in `ci.yml`, or an aggregate job with
  `if: always()` that reads the result of a matrix.
- **Check:** `gh run view <run-id> --json jobs` shows each job's conclusion. Green with
  every step skipped is not a pass.


## A trigger filter means the check is never created

A `paths:`, `paths-ignore:` or `branches:` filter under `on:` stops the workflow from
starting. No check appears at all: no red, no skip.

- **Signature:** any filter added under `on: pull_request:`.
- **Why it breaks:** a `branches:` filter on the base drops stacked pull requests (a PR
  into a feature branch) silently. If a check later becomes required, a `paths:` filter
  holds every non-matching PR at "Expected" and blocks merge forever.
- **Fix:** keep `on: pull_request:` unfiltered, as it is now. Filter inside a job with
  `if:` only when a skip is acceptable (see above).


## A new job reaches an open PR only on the PR's next run

A `pull_request` run uses the workflow file at the PR's merge ref, which includes the
base branch. A job added to `main` does not start a run on open PRs by itself; it runs
on each PR's next event (a push, a reopen).

- **Check:** read the workflow at the run's merge commit, not at the PR branch. To get
  the new job on a PR, push to it; a rebase is not required.


## `|| true` in a `run:` step fails open

`cmd || true` (and `|| :`, `|| continue`) turns a failed command into an empty result
that the next line reads as fact. A guard meant to fail closed then fails open.

- **Fix:** make the fallible command its own checked step. When one exit code is a real
  answer (grep's 1 for "no match"), accept only that code:
  `x=$(grep -E 're' <<< "$input") || [ $? -eq 1 ]`.


## Pre-push hooks and CI check the same gates

prek runs ruff, the hygiene hooks, the AGENTS.md copy check and the token scan
(`no-secrets`) at commit, and ty, lint-imports, pytest, the hook guard tests and
`pr-not-merged` at push (`stages: [pre-push]`). CI runs ruff, every pre-push gate except
`pr-not-merged`, and gitleaks (the `secrets` job, configured by `.gitleaks.toml`).
`pr-not-merged` checks the local branch against GitHub before a push, so it has no CI
step. The commit-stage hygiene
hooks and the copy check run only locally; that is accepted (`AGENTS.md`, "Speed first").

- **Signature:** a new `stages: [pre-push]` hook, or a step removed from `ci.yml`.
- **Check:** each pre-push hook has a `ci.yml` step that runs the same command, or the
  gate holds only on machines that installed the hooks.
- A hook earns a place at pre-push only if it is cheap and diff-scoped, or it stops
  something that CI cannot undo once pushed (a secret, a private key). A whole-repo check
  that CI already runs belongs in CI only.


## If required checks are added later

The repo has no branch protection today: GitHub offers it for this private repository
only on a paid plan. If it is turned on:

- A required check must come from a workflow that starts on every PR (no filter under
  `on:`), or merge waits at "Expected" forever.
- With a merge queue, every required workflow needs `merge_group:` under `on:`. Without
  it the queue never gets the check and ejects the entry after its timeout. A job that
  an `if:` skips on the queue reports `skipped`, which passes: that is the opposite
  failure, a merge with nothing checked.

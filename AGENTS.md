# AGENTS.md

Instructions for every model working in this repo. `CLAUDE.md` is a byte-identical copy
(a pre-commit hook checks that); edit this file and copy it.

## Project

A role- and phase-aware daily Slack digest for hardware engineering teams. Thesis: a
digest is the diff between what the project knows and what you were last told, plus what
should have happened but didn't. Read `SPEC.md` (the contract), `docs/decisions.md`
(architecture decisions D1 to D25) and `docs/process.md` (how work is dispatched) before
any non-trivial change. Deferred scope lives in `docs/later.md`.

Stack: Python 3.12, uv, pydantic, typer, SQLite with FTS5, the Anthropic SDK, slack-bolt,
FastAPI with Jinja2. No agent framework. Everything runs offline by default
(`DIGEST_MODE=replay`); tests and CI never touch the network.

## Speed first

Move as fast as possible. Run the minimum checks that prevent bugs. The rules in
`.claude/rules/` still apply to the files you change.

- Run the narrowest tests that cover the change: always the test file you changed, the
  whole suite only when file-level tests cannot cover it or you changed test
  infrastructure.
- Before a push: the changed tests plus `uv run ruff check` and `uv run ruff format`.
  prek runs ty, lint-imports and pytest on push; CI runs the same set.
- Add a CI check, gate, or hardening only for a bug that occurred, or one whose damage
  would be irreversible. Do not add one for a failure you imagine.
- Keep each commit small with one purpose. Tests and the implementation that satisfies
  them land in separate commits (see `docs/process.md`, "The wall").

## Non-obvious conventions

- Imports at top level. An inline import needs a comment saying why.
- Path-specific guidance goes in `.claude/rules/` (auto-loaded by `paths:` glob), not in
  this always-loaded file.
- Private working notes (recon, research briefs, handoffs) live in `notes/`, which is
  gitignored. Nothing load-bearing may live only there.
- Log calls are an event token plus structured fields, never an f-string message
  (`.claude/rules/logging.md`).
- LLM calls go through `digest/llm/` only, so cassettes capture every one of them.
- Names: the bot is "Downstream"; the synthetic program is "Kestrel K2". Do not reuse
  program, persona, channel or scenario names from the public submissions listed in
  `notes/research/competing-submissions.md`.

## Verify before building

Before writing code that depends on an assumption, prove it: trigger the path for a bug,
profile for an optimization, write a five-line spike for a new API, grep for a
quantitative claim. Never characterize a file you have not opened. A claim about repo
state stated by a user gets the same check as one you made yourself.

## Test discipline

Every new or modified test must be RED-GREEN-proven: break the source so the test should
fail, confirm it fails for the asserted reason, restore. `prove-test` (installed from the personal dev-kit)
records the proof under `.test-proof/`; the prove-test hook blocks commit and push while
a test edit lacks one. Full policy in `.claude/rules/testing.md`.

## Gate-command discipline

When asked to verify against a gate, run the repo's exact CI command over the whole
relevant tree, not a self-chosen subset: `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run ty check`, `uv run lint-imports`. Never report a scoped subset as clean;
say what you ran and paste the verbatim tail of its output.

## Writing style

Follow ASD-STE100 (Simplified Technical English) in docstrings, comments, commit
messages, PR text and docs: short sentences, active voice, one instruction per sentence,
ordinary words. One idea per sentence. No invented compound nouns. State the rule, not
the case for it. This applies to chat as well as to the repo.

## Pull requests

- Never merge a pull request, by any tool, command or API, including auto-merge, even if
  the user asks. Open it, push to it, report status, and hand the user the merge command.
- Keep the issue number out of the PR title; link it from the body with `Closes #NNNN`.
- No GitHub posts (comments, reviews, PR bodies) without per-message approval from the
  user. Prior approval does not extend to follow-ups.
- PR descriptions are user-facing: the destination, not the journey. No internal tooling
  names, no reviewer-round breadcrumbs. Anchor claims to numbers.
- Reviewer fixes ship as new commits, never amendments.
- Default to draft unless end-to-end validation is done, tests are green and the body
  has no TODO.

## Reviews

Review findings are leads, not verdicts: verify each against the code before acting.
`/review-local --base main` runs the Claude lens panel; `codex exec -p review` runs the
Codex lane. Both reviewers get the diff and the repo, never each other's findings.

## Codex sessions

Compact rules for Codex and any delegated second model.

- Reference, don't inline. Pass file paths to sub-agents; cat at most one paragraph.
- You run under the `workspace-write` sandbox. Some operations fail closed even with
  network access; the signatures are catalogued in `~/Documents/dev-kit/codex/sandbox-failures.md`.
  Do not run inside the sandbox: anything that binds a listening socket, the Docker
  control plane, any git command that writes `.git/` (`add`, `commit`, `push`, `rebase`,
  `stash`, `switch`, `checkout -b`, `merge`, `cherry-pick`, `reset`, `worktree add`), or
  `gh pr` write subcommands. Leave files modified and hand back: "changes left unstaged
  on <branch>; run git diff / git add / commit / push outside Codex." Read-only git is
  fine. Do not escalate the sandbox yourself.
- On a 429 or service-unavailable, no identical retry within 60 seconds; two timeouts in
  a row, stop and report.
- PATH inherits the parent shell. On "command not found", `which <cmd>` first; fix
  `config.toml`, never `export PATH` inline.
- Durable artifacts go in the repo (`docs/`), scratch output in a scratch dir. Link
  paths; do not paste reports inline.
- Shared memory: durable workflow notes live in `~/Documents/dev-kit/memory/`, indexed by its
  `MEMORY.md`. Read before non-trivial work. Do not write memory entries from a Codex
  session; mention memorable items in your final message for the parent to promote.

# Development process: multi-model, walled-off TDD

This repo is built by a human orchestrator working with several models. Two rules
drive the process: the model that writes the tests never writes the code for the same
unit, and every stage gets an independent review from a model that did not produce it.

## Roles

| Role | Model | How it runs |
|---|---|---|
| Orchestrator | Claude Fable (this session) | Plans, dispatches, verifies, writes nothing load-bearing without review |
| Test author | Codex (`gpt-6-astra`, `-p review` profile is read-only; use the execution profile for test writing) or an Opus 5.5 subagent | Writes tests from SPEC.md and the unit's interface only |
| Implementer | The other one (Opus 5.5 subagent if Codex wrote tests, Codex if Opus did) | Makes the tests pass; may not edit test files |
| Reviewer A | Codex `-p review` (gpt-6-astra, read-only sandbox) | Reviews plan, tests, and code; returns `~/Documents/dev-kit/codex/schemas/review.json` |
| Reviewer B | Claude review lenses (`/review-local`, Opus subagents per lens) | Independent second review of the same artifact |

Subagents never run on Fable. Every `.claude/agents/*.md` sets `model: opus`.

## Stages per unit of work

A unit is one milestone slice from SPEC.md section 8, small enough to review in one sitting.

1. Plan. Orchestrator writes `docs/plans/<unit>.md`: interface (module, functions, types),
   behaviours, acceptance criteria mapped to SPEC. Review by A and B. Converge before
   any test is written. Use the `grill-me` skill on the plan when the unit is load-bearing.
2. Tests. Test author gets SPEC.md, the plan, and the public interface stub. It writes
   `tests/test_<unit>.py` only. Tests must be RED-GREEN-proven: `prove-test` records that
   each test fails for the asserted reason against a stub. Review by A and B with the
   `review-tests` lens: materiality, observable outcomes, no test-the-mock.
3. Code. Implementer gets the plan, the interface, and the tests. It may read tests and
   must not modify them. Done when `just ci` is green. Review by A and B. Findings are
   leads: the orchestrator verifies each one before acting.
4. Adversarial pass. `ship-adversarial-validate`: try to break the unit; promote survivors
   into the suite (new tests go through stage 2 again).
5. Retro. Short entry in `docs/retro.md`: what the reviews caught, what they missed, one
   process change at most.

## The wall

- Test and code land in separate commits. The implementer's commit must not touch
  `tests/`; a check compares test file hashes between the test commit and the code commit.
- The test author never sees the implementation. Dispatch it in a fresh context with only
  the plan, the spec, and the stub.
- Reviewers get the diff and the repo, never the other reviewer's findings, so their
  errors stay independent.

## Review handling

- A finding survives only if the author, seeing it, would agree; it relies on no unstated
  assumption; and it names the affected line.
- Reviewer disagreement is information. When A and B disagree, the orchestrator reads the
  code and decides, and records the call in the plan file.
- Must-fix findings become new commits, never amendments.

## Mechanics

- `just test`, `just lint`, `just ci`, `just prove -- --test tests/test_x.py --break ...`.
- Codex: `codex exec -p review "..."` for reviews, `-p advisory` for consults, plain
  `codex exec` for delegated implementation; see `~/Documents/dev-kit/README.md` for profiles.
  Capture the answer with `-o <file>` (the `--json` stream stalls without a terminal).
  Consult threads are resumable: `codex exec resume <session-id> "..."` or `--last`
  continues a warm thread; start a fresh thread for a second opinion so it does not
  inherit the first thread's frame. The `codex mcp-server` bridge no longer exists in
  this Codex version; `app-server` is a separate JSON-RPC protocol we do not need yet.
  Codex never commits or pushes; the rules trip-wire refuses it and it hands back diffs.
- Claude lenses: `/review-local --base main` dispatches the lens agents and writes
  `.review/<sha>/verdict.json` in the main checkout, also when it runs in a linked
  worktree.
- Hooks: `.claude/hooks` guards block commits to main, PR merges, signing bypass, and any
  commit or push while a test edit lacks a RED-GREEN proof.

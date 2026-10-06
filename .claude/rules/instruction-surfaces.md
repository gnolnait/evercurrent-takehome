---
name: instruction-surfaces
description: |
  Prose that agents and operators OBEY — rule files, review-lens roles, skills, hooks,
  memory notes, AGENTS.md. Editing it is a behaviour change, not a docs change. Loads
  when a markdown file under .claude/rules, .claude/review-lenses, .claude/hooks or
  docs changes, or a root AGENTS.md / CLAUDE.md.
paths:
  - ".claude/rules/**/*.md"
  - ".claude/review-lenses/**/*.md"
  - ".claude/hooks/**"
  - "docs/**/*.md"
  - "AGENTS.md"
  - "CLAUDE.md"
---
# A change to obeyed prose is a behaviour change

These files are not documentation about the system. For an agent they ARE the system: a
rule file's text is the review it performs, a lens role is the review's scope, a skill
is the procedure it runs. A false clause here does not mislead a reader who can check
the code — it changes what an agent does, silently, on every later run.

The reflex to treat a `.md` diff as low-stakes is the trap.

The obeyed surfaces in this repo: `AGENTS.md` / `CLAUDE.md` at the root, `.claude/rules/`,
`.claude/review-lenses/`, `.claude/hooks/`, `docs/`, and the user-level skills and memory
installed from the personal dev-kit.


## Trap signatures to reject in review

Each is a CONTRADICTION you can demonstrate, not a procedure to perform. Report only
what you verified; a claim you could not check is not a finding.

**A trim dropped an imperative clause.** A compression or rewrite where the new text no
longer carries a `never` / `do not` / `must` / `always` clause the old text carried, and
the change does not say it was dropped on purpose. A surviving heading proves nothing
about its body — compare bodies.

**A factual claim about a repo tool is contradicted by that tool.** Prose here is quoted
back as fact by agents that cannot verify it, so an invented capability propagates.
Open the workflow, script, or CLI and confirm the contradiction before reporting it.

**A repo-local path, symbol, or command named here does not exist.** A wrong path sends
an agent looking for a guard that is really there and reporting it absent. Applies to
paths and symbols in THIS repo only — not to external commands, placeholders,
illustrative examples, or generated paths.

**A snippet copied from a script drops a step that script performs, while the prose
presents it as equivalent.** Only when the text claims to replace or reproduce the
original; a deliberately shorter example is not a finding.

**An explicit count disagrees with what follows it.** "Two mechanics" above three
bullets tells a reader to drop one. Same for "three steps", "both cases", "the four
rules".

**A precondition this change loosened is still asserted here.** Only where the same diff
relaxes the constraint the prose still states.


## Not a finding

- Wording, tone, and formatting preferences.
- Prose that merely DESCRIBES code and has no reader who acts on it — ordinary comment
  rot, which the broad lenses already cap as a low-severity note.
- Duplication between two files, on its own. Drift is a maintenance risk, not a defect.
- A deliberate deletion the change explains.
- A claim you would have to leave the repo to check.

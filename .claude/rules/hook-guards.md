---
name: hook-guards
description: |
  The command guards read Bash with shlex and a hand-written segmenter, not a shell
  parser, and point fixes to a parse escape have opened wider ones. Loads when editing
  .claude/hooks/.
paths:
  - ".claude/hooks/**"
---
# Command-guard gotchas

Bugs of this kind that shipped here or in dev-harness. Each entry has a signature you
can see in a diff. The full record is `docs/guard-parser/README.md` in dev-kit.

**Discipline:** flag a change only when the diff matches a signature below, or a clear
variant. No match, no comment. When you flag one, name the signature, cite the changed
line, and state the visible effect.

## A point fix to a parse escape opens a wider one (dev-harness 2026-07; dev-kit 2026-10-12)

**Signature:** a diff adds a special case to `shell_segments.py`,
`command_word_and_tokens`, or a guard's own token reading, to close one construct (a
quote form, a redirection, a `cd`, a wrapper flag).

**Why it breaks:** shlex tokens are not a shell parse. A fix that removes or re-reads
text moves what the guard takes as the command word. dev-harness counted 27 or more
escape classes in about 24 rounds; one dev-kit parser branch caused 5 regressions.

**Fix:** add the construct to the escape list in dev-kit `docs/guard-parser/README.md`
and to the guard's docstring. Leave the code. A change that only adds detection, and
cannot remove a block, may ship with a replay against main.

## An exemption that trusts a parsed directory (dev-kit 2026-10-12)

**Signature:** a guard allows a command because of a directory or identity it parsed
from the command (a `cd`, `-C`, `--repo`).

**Why it breaks:** `CDPATH`, `||`, a background `&`, a subshell, an env prefix and a
`.name` path each moved the real directory away from the parsed one. Four Codex rounds
found them one by one.

**Fix:** accept one exact, fully resolved shape and fail closed on every other form.
`no_unreviewed_pr_impl._leading_cd` accepts only a leading `cd <path> &&` with an
absolute, `./` or `../` path.

## A green suite proves little for a parsing change (dev-harness 2026-07-26)

**Signature:** a parsing change whose test plan is the unit suite only.

**Why it breaks:** dev-harness shipped two flawed fixes at 336 tests green.

**Fix:** replay the distinct Bash commands from the local transcripts through main's and
the branch's guards, and compare the verdicts (method in dev-kit
`docs/guard-parser/README.md`).

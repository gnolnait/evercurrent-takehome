---
description: A mechanism change falsifies the prose around it — comments, docstrings, help text, runbooks, and operator-facing strings describe the OLD behavior and no test fails. Fires at edit time on any source or doc file, because the memory entry only loads when someone thinks to recall it, and by then the diff is pushed.
paths:
  - "src/**"
  - "tests/**"
  - ".github/**"
  - ".claude/**"
  - "docs/**"
  - "*.md"
---
# Changed HOW it works? The prose around it is now wrong

When you change a mechanism — an exit-code contract, a default, an allowlist becoming a
denylist, a constant that stops being read, a marker that stops being emitted — every
comment, docstring, help string, runbook line, and test-file banner describing the old
behavior is false in the same diff. **No test fails.** The code is right and the words
next to it lie.

Before pushing a mechanism change, re-read the prose in every file the diff touches, plus
any file that *names* the thing you changed. Grep the old identifier, the old flag, the
old marker string.

## The four surfaces this hides in, worst first

1. **Operator-facing strings** — a CLI refusal message, a runbook step, an alert's
   remediation. These are read mid-incident by someone acting on them under pressure.
2. **Test-file banners and docstrings** — these instruct the *next editor*. A stale one
   tells them to delete a guard you just added.
3. **Config comments** — they tell a reader what is safe to add back. A stale one invites
   a change that breaks the build.
4. **Ordinary code comments** — cheapest, and the ones review actually catches.

## Anchors

- One PR: 4 findings, all prose the author had falsified.
- Another PR: **5 instances in one PR**, across three of the four surfaces above, none
  caught by a test, all caught at review across five separate rounds — the single
  largest defect class in that PR.
  - A skill file still told agents a class of databases was disposable after the PR
    established they are long-lived and curated in place.
  - A CLI refusal message still told an operator to "wipe and rebuild" the same database.
  - A test-file banner still described the exact inverse of what shipped, instructing
    the next editor to delete a live guard.
  - A config comment still said its lists mirrored a constant after they were re-keyed to
    a different one; a reader following it would re-add entries and break the build.
  - A CI step comment claimed it "cannot diverge on flags" from the job it copied, while
    copying one of that job's two passes.

## Related

Content rules for comments live in `.claude/rules/anti-sprawl.md`
(§ "Comments state what the code can't"). That governs whether a comment should exist and
how it should read. This rule is the orthogonal check: whether prose that already exists
is still *true* after your diff.

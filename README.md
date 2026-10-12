# Downstream: a role- and phase-aware daily digest for hardware teams on Slack

Thesis: a digest is the diff between what the project knows and what you were last told,
plus what should have happened but didn't.

Downstream reads a team's Slack threads once, extracts structured items (decisions,
blockers, risks, asks, schedule, spec and BOM changes) with claims such as
`(PN-31010, rev) = C`, finds the checklist tasks and asks that went silent, and then
selects and renders a different digest for each reader.

## Run the demo

No API key is needed. The demo replays recorded model calls (cassettes) for a synthetic
ten-day robotics program, Kestrel K2, and prints three digests for day 6: a mechanical
engineer, a supply chain lead and an engineering manager.

```bash
uv sync --all-extras --dev
uv run digest demo
```

The run takes a few seconds. A digest shows the sections that have lines (Needs you,
Not happened, Decisions, Risks, FYI), with a source link for every line. The last line
gives the pairwise item overlap of the three digests. `--day N` picks another world day
(1 to 10).

On Windows, clone into a short path such as `C:\dc`: one dependency installs a file
whose full path can pass the 260-character limit, and the import then fails.

## What a reader sees, and why

- Visibility fails closed: a line reaches a reader only when its thread is in a public
  channel or one they are a member of.
- Must-see lines are chosen by rule: an ask made of the reader, an open blocker on an
  entity they own, a schedule change on a gate they own, a checklist task of their role
  (or any, for the engineering manager) that went silent, and an ask they made or
  received that has no reply.
- Other lines are ranked by importance, the role's interest in the item type, and the
  reader's ownership, above a threshold.

## How well extraction works

`reports/eval-2026-10-23.md` scores the recorded run against golden labels. The file
name carries the world's last day. Every rate is given with its n.

| Metric | Value | n |
|---|---|---|
| Thread recall | 0.967 | 30 |
| Claim recall | 0.629 | 35 |
| Claim precision | 0.537 | 41 |
| Absence recall | 0.516 | 31 |
| Absence false-alarm rate | 0.304 | 23 |
| Citation validity | 1.0 | 73 |

Claim values match exactly on revision, gate date, quantity and owner; other attributes
match on entity and attribute, and an advisory row reports how many of those also agree
on value. The corpus is small and synthetic: 132 messages in 35 threads.

## Scope

The belief ledger and stale-belief lines (what each reader was last told about a key)
are not built yet; the demo slice took the kill-criterion path in `SPEC.md`. See
`docs/plans/m3-demo.md` for the slice and its known gaps, and `docs/later.md` for
deferred work.

## Develop

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run lint-imports
```

Everything runs offline by default (`DIGEST_MODE=replay`); the tests and CI never call a
model. Read `SPEC.md` (the contract), `docs/decisions.md` and `docs/process.md` before a
change.

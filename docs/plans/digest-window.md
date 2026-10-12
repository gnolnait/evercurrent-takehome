# Plan: the digest window

Status: v0.1, 2026-10-12. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run. Source: SPEC section 5 step 6 (ranked-lane candidates) and step 9 (the digest
window). Stable item ids (PR #49) came first so that a window over items is well defined.

## Goal

A digest tells the reader what is new since their last digest, plus what stays open and
needs them. Today every digest is a first digest: the ranked lane takes every open item,
so day-6 digests run 13 to 16 lines and two roles share up to 71% of their items.

## Rule (from SPEC section 5)

- Must-see rules run over all open items, as today. The window does not touch the
  must-see lane.
- A ranked-lane candidate is an item whose newest evidence (`last_updated`) falls after
  the window start, or, whatever the age of its evidence, an open blocker, an open ask, or
  an ask absence. Checklist absences reach a reader only through must-see, as today.
- The window starts at the end of the day of the reader's previous digest, at most
  `window.max_working_days` working days back (default 10). A first digest starts
  `window.first_working_days` working days back (default 5). Both are constants in
  `config/priors.yaml` under `daily.window`.

## Replay assumption

Replay records no delivery. `select_digest` assumes the reader got a digest at the end of
every earlier working day of the world, so the previous digest of world day N is world day
N - 1, and world day 1 is a first digest. A live deployment will read the previous digest
from delivery records (D7), which are not built.

## Interface

- `DailyPriors.window: WindowPriors` with `first_working_days: int = 5` and
  `max_working_days: int = 10`, both at least 1, `first_working_days <= max_working_days`.
- `select(world, profile, items, absences, priors, day, previous_digest: date | None)`:
  `previous_digest` is the day of the reader's previous digest, or None for a first
  digest. The new parameter has no default, so every caller states it.
- `select_digest(store, world, priors, user_id, day)` keeps its signature (the Slack post
  branch calls it) and passes the previous working day of the world, or None on day 1.

## Behaviours

- W1. An open non-blocker item whose newest evidence is on or before the window start is
  not in the ranked lane.
- W2. The same item updated after the window start is a candidate again.
- W3. Open blockers, open asks and ask absences stay candidates whatever the age of their
  evidence.
- W4. Must-see lines do not change with the window.
- W5. A first digest uses `first_working_days`; a previous digest older than
  `max_working_days` is capped there; working days skip weekends.
- W6. `select_digest` on world day 1 passes None and on day N passes day N - 1.

## Acceptance

- `tests/test_select.py` and `tests/test_priors.py` cover W1 to W6; each new or changed
  test is RED-GREEN proven.
- `uv run digest demo` runs offline. Its day-6 lines and overlap change; the PR reports
  the before and after counts per reader.
- The extraction eval report does not change (it does not call `select`).

## Not in scope

Delivery records, `seen` per item, the `window_recency` term, and a footer that counts
lines left out by the window.

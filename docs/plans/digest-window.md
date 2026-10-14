# Plan: the digest window

Status: v0.2, 2026-10-12. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run. Source: SPEC section 5 step 6 (ranked-lane candidates, newest evidence) and
step 9 (the digest window). Stable item ids (PR #49) came first. v0.2 folds in the advisor
review and an independent Codex plan review.

## Goal

A digest tells the reader what is new since their last digest, plus what stays open and
needs them. Today every digest is a first digest: the ranked lane takes every open item,
so day-6 digests run 13 to 16 lines and two roles share up to 71% of their items.

## Rule (from SPEC section 5)

- Must-see rules run over all open items, as today. The window does not touch the
  must-see lane.
- A ranked-lane candidate is an item whose newest evidence falls in the window, or,
  whatever the age of its evidence, an open blocker. Absences reach a reader only through
  must-see (amended 2026-10-13: ask absences followed checklist absences,
  `docs/plans/ask-audience.md`).
- Asks: an ask made of the reader is already must-see. Any other open ask follows the
  window like other items. An unanswered ask comes back as an ask absence once its reply
  cadence passes (`digest.absence`), so selection needs no reply state.
- Newest evidence (SPEC section 5 step 6): the greatest `event_ts` among the current
  versions of the messages the item's evidence cites, so an edit counts as new evidence.
  `last_updated` is the newest cited `message_ts` and misses edits, so the window does not
  use it. `select_digest` reads the cited messages' `event_ts` from the store and passes
  one value per item.
- The window, as one formula in the world's time zone:
  - `start_day = previous_digest` when the caller gives one, else
    `add_working_days(day, -window.first_working_days)`;
  - then `start_day = max(start_day, add_working_days(day, -window.max_working_days))`;
  - an item is in the window when its newest evidence is strictly after the end of
    `start_day` (23:59:59.999999 local) and at or before the end of `day`.
  A first digest therefore covers `first_working_days` working days up to and including
  `day`. A Monday window after a Friday digest spans the weekend. Working days are Monday
  to Friday; the calendar has no holidays. Defaults: 5 and 10, in `config/priors.yaml`
  under `daily.window`.

## Delivery

No delivery is recorded yet (D7 is designed). The caller states the reader's previous
digest; when it does not, the digest is a first digest. Missing history never hides an
item. The demo states its assumption: a reader who got the previous working day's
digest.

## Interface

- `DailyPriors.window: WindowPriors`, fields `first_working_days: int = 5` and
  `max_working_days: int = 10`, both at least 1, with
  `first_working_days <= max_working_days`.
- `select(world, profile, items, absences, priors, day, previous_digest: date | None,
  newest_evidence: Mapping[str, int])`: `newest_evidence` maps each item id to its
  newest evidence in microseconds since the epoch; an absence item, or an item missing
  from the map, uses `ts_micros(last_updated)`. Both parameters are required.
- `Selection.previous_digest: date | None` and `Selection.first_working_days: int`
  (additive fields; `select` sets the second from `priors.daily.window`, so the header
  line can name the configured first-digest length without the priors).
- `select_digest(store, world, priors, user_id, day, *, previous_digest: date | None =
  None)`: the new keyword defaults to a first digest, so existing callers (the Slack post
  branch) keep working.
- `render_digest` adds one line under the header: "Since your digest of <date>" or
  "First digest: the last <n> working days".
- `digest demo` and `digest post` share `_demo_selections`, which passes the previous
  working day as `previous_digest`, except on world day 1. `slack_message` prints the
  same window line as `render_digest` (W9).

## Behaviours

- W1. An ordinary item (a decision or risk with no must-see rule) whose newest evidence
  is at or before the window start is not in the ranked lane; the same item with newest
  evidence after the window start is (paired test across the boundary).
- W2. An edit: an item whose cited message has `ts` before the window start but
  `event_ts` after it is in the window.
- W3. Old open blockers stay candidates, tested beside an old ordinary item that is
  excluded (ask absences: amended 2026-10-13, `docs/plans/ask-audience.md`). A candidate can still fall to the score threshold or a
  section cap, as today.
- W4. Must-see lines are the same with and without the window (preservation test).
- W5. Boundaries: an item exactly at the end of `start_day` is out, one microsecond later
  is in; a first digest uses `first_working_days`; a previous digest older than
  `max_working_days` is capped there; a Friday-to-Monday window includes the weekend.
- W6. `select_digest` with no `previous_digest` builds a first digest; with one, the
  window starts at its end; it reads newest evidence from the store, including an edited
  message's `event_ts`.
- W7. The header line names the previous digest's date, or says "First digest".
- W8. `WindowPriors` defaults and validation.
- W9. `slack_message` (`digest post`) holds the same window line as the terminal digest:
  - one function, `window_line(selection) -> str` in `daily/render.py`, builds the line;
    `render_digest` and `slack_message` both call it, so the wording cannot drift;
  - in Slack it is the first block after the header, a `section` block through the
    existing `_section` helper (escaped mrkdwn, `verbatim`), for both a previous digest
    and a first digest, and also on a day with no lines, before "Nothing new";
  - the top-level `text` fallback (notifications, screen readers) is the header, a
    newline, then the window line, each part escaped separately;
  - the header and the window block are fixed blocks: the overflow check becomes
    `2 + len(body) + len(footer) > MAX_BLOCKS` and the kept slice
    `MAX_BLOCKS - 3 - len(footer)`; orphan-title removal and the footer stay.
  - Amended 2026-10-13 (`docs/plans/phase-line.md`): `context_lines` returns the window
    line and the phase line. The window block and the `text` fallback hold both, and the
    block arithmetic does not change.

## Acceptance

- `tests/test_select.py`, `tests/test_priors.py` and `tests/test_daily_render.py` cover
  W1 to W8, and `tests/test_slack_post.py` covers W9: a parity test compares the Slack
  window block with line 2 of `render_digest` for both variants; the existing
  assertions on block order, item index, the `text` fallback and the 50-block overflow
  move by one block; each new or changed test is RED-GREEN proven, and W1, W2, W3 and W5 fail on
  today's code.
- `uv run digest demo` runs offline. The PR reports, per reader, must-see, ranked and
  total lines for days 5, 6 and 7 before and after. Must-see counts must not change.
- The extraction eval report does not change.

## Known limit

An item appears in the ranked lane on the day its newest evidence lands. If the score
threshold or a section cap cuts it that day, a later digest does not bring it back. A
dogfood read of the demo (days 1 to 6) found no such item; a seen record (later.md) is
the fix.

## Not in scope

Delivery records, `seen` per item, the `novelty` score term of the D6 amendment (this
plan is a candidate filter, not a score term), the `window_recency` term, closing lines
for resolved items, and a footer that counts lines left out by the window.

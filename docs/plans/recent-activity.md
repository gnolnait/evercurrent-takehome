# Plan: a reader's recent activity lifts relevance

Status: v0.2, 2026-10-13. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run. v0.2 folds in the advisor review and an independent Codex plan review. The
branch builds on `docs/plans/ask-audience.md`, because both change `select`.

## Goal

The brief asks for a digest that recognizes "that people's focus changes over time". Today
`personal_relevance` reads only static facts: owned entities (1.0), the reader's role in the
item's `affected_roles` (0.8), else 0.4. A reader who starts working on an entity they do
not own, outside their role's usual items, gets no lift.

This unit adds the smallest behaviour-based signal the store already holds: the messages
the reader wrote in the last few working days. It is a step window, not the decayed
interest model of SPEC section 2, which needs feedback events.

## Exploratory evidence (spike, 2026-10-13)

`notes/research/spikes/phase_focus_spike.py` patched the score on the demo days with a
looser rule than this plan (no visibility trimming of source items, every stored status).
With relevance 0.4 raised to 0.8 for an item sharing an entity with the reader's recent
messages: the PM's day-6 digest gained "TST-THM-02 thermal review open and blocking G-DVT1"
and "Top-cover DFM review open and blocking G-DVT1" and lost "G-DVT1 schedule buffer
eroding with two open reviews" and "PN-31020 signal harness shortage for DVT-1 kit"; the EM's
day-6 digest gained one line; the PM's day-8 digest swapped one. ME, EE and supply chain
changed order only. These results are not the expected output of this plan; acceptance
re-measures them with the exact rule.

## Rule

- Recent messages: messages whose author `user_id` is the reader, roots and replies, with
  message time `ts` after the end of `add_working_days(day, -n)` and at or before the end of
  `day` in the world's zone. Reactions do not count. `n` is
  `daily.recent_activity.working_days` in `config/priors.yaml`, default 3, at least 1.
- Edits and deletes: the store keeps the current version of each message. An edit does not
  move a message's `ts`, so it does not refresh the window. A message deleted by the end of
  the day does not count.
- Source items: the items `select` already treats as current (status open, `last_updated`
  at or before the end of the day), each trimmed by `_visible` for the reader. An item the
  reader cannot see supplies nothing. A trimmed item supplies its entities when its
  remaining evidence cites a recent message. Must-see items, items outside the window and
  items below the threshold or past a cap supply entities like any other. Absences supply
  nothing.
- Active entities: the union of the entities of the source items. It is computed in one
  pass before scoring.
- Lift: in the ranked lane, a non-absence item whose relevance would be 0.4 and whose
  entities intersect the active entities gets relevance
  `daily.recent_activity.relevance` (default 0.8, validated above 0.4 and at most 0.8).
  An item with no entities gets no lift. Owned (1.0) and affected-role (0.8) items do not
  change. With no recent messages, nothing changes.
- The line's `reason` names the lift: `personal_relevance=<value> (recent activity:
  <shared entities, sorted, comma-separated>)`. A line without the lift keeps today's
  reason text.
- The must-see lane, the window and the caps do not change.

## Interface

- `Store.authored_messages(user_id: str, start: datetime, end: datetime) ->
  frozenset[tuple[str, str]]`: (channel_id, ts) of the user's current, not deleted
  messages with `start < ts <= end`. It does not rebuild history; the caller's replay
  cutoff bounds what the store holds. Daily code does not read `Store._db`.
- `select(..., newest_evidence, recent_messages: frozenset[tuple[str, str]])`: a new
  required parameter. `select_digest` builds it with the window above.
- `DailyPriors.recent_activity: RecentActivityPriors` with `working_days: int = 3` (at least
  1) and `relevance: float = 0.8` (above 0.4, at most 0.8).

## Doc changes (first commit)

- SPEC section 5 step 6: `personal_relevance` lists the recent-activity lift, its window,
  its configured value and ceiling, that it is neutral with no recent messages, and that
  it is a step window, not the decayed interest of section 2. The 0.4, 0.8 and 1.0 levels
  are still constants in code; `docs/later.md` records that gap against the band rule.
- README "What a reader sees, and why": one sentence on the lift.

## Behaviours

In `select` (given `recent_messages`):
- RA1. A visible item with relevance 0.4 shares an entity with a visible source item whose
  evidence cites a message in `recent_messages`: relevance is the configured value and the
  reason names the shared entities. Paired: the same fixture with an empty set gives 0.4.
  Fails on today's code.
- RA2. Preservation: owned (1.0) and affected-role (0.8) items do not change.
- RA3. An item that cites a recent message but has no entities gets no lift.
- RA4. Privacy: a source item hidden from the reader does not lift a visible target with the
  same entity (paired with the same source visible, which lifts). A mixed item whose only
  matching citation is in a channel the reader cannot see supplies nothing.
- RA5. Source set: a resolved item supplies nothing; a must-see item and an item outside
  the window supply entities (each paired with a qualifying case).
- RA6. Absences neither supply entities nor get the lift.
- RA7. Must-see lines are the same with and without recent messages (preservation).

In `select_digest` and the store:
- RA8. The window: a reader's message one microsecond after the end of
  `add_working_days(day, -n)` counts, one at that end does not; a reply counts; another
  user's message does not; a deleted message does not; an edited message counts by its
  original `ts`.
- RA9. `Store.authored_messages` bounds: strictly after `start`, at or before `end`.
- RA10. `RecentActivityPriors` defaults and validation.

## Existing tests to check

`tests/test_room.py` pins exact reason strings (near lines 461 to 531). A room fixture whose
reader has recent messages would gain the parenthetical; the test writer checks each case.
`tests/test_select.py` cases that call `select` gain the new required argument.

## Acceptance

- Tests in `tests/test_select.py`, `tests/test_daily_render.py`, `tests/test_store.py` and
  `tests/test_priors.py` cover RA1 to RA10; each new or changed test is RED-GREEN proven.
- Before and after, with the demo's previous-day window, for all six readers on days 5 to 8:
  the item ids added and removed per reader, with section and lane.
- A dogfood read of every added and removed line, each with a one-line verdict. If the
  removed lines read better than the added ones, lower `relevance` and measure again. The
  PR states the value chosen and why.
- No ranking metric exists yet (nDCG at 5 is designed, not built), so the read is the
  evidence; the PR says so.
- Must-see lines do not change. The extraction eval report does not change.

## Not in scope

Decay, reactions and reply counts as signals, feedback and watch events, configured bands
for the existing 0.4, 0.8 and 1.0 levels, and the phase line.

# Plan: selection reads the feedback log

Status: v0.1, 2026-10-14. Process stage 1 (`docs/process.md`), full process. Depends on
the interactive slice of `docs/plans/m5-bot.md` (branch `m5-bot-plan`), which builds
`digest.feedback`. Tests start after that slice merges.

## Goal

SPEC section 5 step 8 gives digest actions effects: an owner's done resolves the item, a
snooze hides it until a date, and "it happened" suppresses one absence episode for one
reader. The interactive slice records these as events and derives
`feedback.active(store, reader, day) -> FeedbackState` from the log. Selection does not
read it yet, so a click changes nothing in the next digest. This unit makes selection
read it. It writes nothing: the items table and the log stay as they are (D26).

## Interface (from `digest.feedback`, owned by the interactive slice)

- `FeedbackState(resolved: frozenset[str], snoozed: Mapping[str, date],
  suppressed: frozenset[Episode])`.
- `Episode(source, subject, last_evidence)`, built only by
  `episode(source, subject, channel, message_ts)`; `last_evidence` is
  `"<channel id>:<message ts>"`; `subject` is the task id for a checklist absence and the
  ask item id (without `absence:ask:`) for an ask absence.

## Rule

- `select(..., recent_messages, feedback: FeedbackState)`: a new required parameter.
  `select_digest` passes `feedback.active(store, user_id, day)`.
- The filter runs after the visibility check and before both lanes, so it applies to
  must-see and ranked lines alike. A line it removes does not count in `hidden`, which
  counts only the visibility filter.
- Resolved: an item whose id is in `resolved` shows to no reader.
- Snoozed: an item whose id is in `snoozed` with a date after `day` does not show to that
  reader. On the date itself and after, it shows again. Snooze applies to the reader the
  state was built for only.
- Suppressed: an absence whose episode is in `suppressed` does not show to that reader.
  An absence's episode is `episode(source, subject, channel, message_ts)` of the one
  evidence entry it cites (`digest.absence` builds each absence with exactly one). A later
  silence that cites newer evidence is a new episode and shows again.
- The ask-replacement rule still runs after the filter: an ask whose absence line is
  suppressed or snoozed keeps its own line, under its own rules.
- With an empty `FeedbackState`, every digest is the same as today.

## Doc changes (first commit)

- SPEC section 5 step 8: one sentence that selection reads the derived feedback state
  before both lanes, and that the filter is not counted in `hidden`.
- `docs/plans/m3-demo.md` B5.5: a pointer to this plan.

## Behaviours

- FS1. A resolved item shows to no reader, in either lane (a must-see case and a ranked
  case), paired with the same item not resolved.
- FS2. A snoozed item is hidden for the snoozing reader on a day before its date and shows
  on the date; another reader still sees it on both days.
- FS3. A suppressed episode hides its absence for that reader; the same absence citing
  newer evidence (a new episode) shows.
- FS4. An ask whose absence is suppressed keeps its ask line under the ask's own rules.
- FS5. `hidden` does not change when feedback removes a line.
- FS6. Preservation: an empty `FeedbackState` gives today's selection (the RED proof is a
  mutation after the code exists).
- FS7. `select_digest` reads `feedback.active` for the reader and day (with a store
  holding feedback events, after the slice merges).

## Acceptance

- Tests in `tests/test_select.py` and `tests/test_daily_render.py` cover FS1 to FS7; each
  new or changed test is RED-GREEN proven.
- `uv run digest demo` is byte-identical: the demo store holds no feedback events.
- The extraction eval report does not change.

## Not in scope

The closing line of SPEC step 7 for a resolved item a reader saw while open (it needs the
seen record), exposure records, the "it happened" button itself, and resolution that new
evidence reopens (a resolution holds until undo).

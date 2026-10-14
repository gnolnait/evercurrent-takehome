# Plan: selection reads the feedback log

Status: v0.3, 2026-10-14. Process stage 1 (`docs/process.md`), full process. v0.2 folds
in the advisor review and an independent Codex plan review; v0.3 reads a feedback store only when the caller names it. Depends on the interactive
slice of `docs/plans/m5-bot.md` (branch `m5-bot-plan`), which builds `digest.feedback`
and the feedback store. Tests start after that slice merges.

## Goal

SPEC section 5 step 8 gives digest actions effects: an owner's done resolves the item, a
snooze hides it until a date, and "it happened" suppresses one absence episode for one
reader. The interactive slice records done, snooze and undo as events in a feedback store
and derives `feedback.active(store, reader, day) -> FeedbackState`. Selection does not
read it yet, so a click changes nothing in the next digest. This unit makes selection read
it. It writes nothing: the items table and the log stay as they are (D26).

## Interface (from `digest.feedback`, owned by the interactive slice)

- `FeedbackState(resolved: frozenset[str], snoozed: Mapping[str, date],
  suppressed: frozenset[Episode])`. `resolved` holds item ids resolved by their owner and
  applies to every reader. `snoozed` and `suppressed` belong to the one reader the state
  was built for.
- `Episode(source, subject, last_evidence)`, built only by
  `episode(source, subject, channel, message_ts)`: `source` is `"checklist"` or `"ask"`,
  `subject` is the task id or the ask item id (without `absence:ask:`), `last_evidence` is
  `"<channel id>:<message ts>"`.
- Contract this unit depends on: `active()` returns an empty state for a store with no
  `feedback_events` table (a read-only or older store). Requested of the slice on
  2026-10-14.

## Where the feedback comes from

- The feedback store is its own file, `.digest/feedback.sqlite` by default (the bot
  plan's `--db`). Digests are built from the replayed store, which holds no feedback. So
  the caller opens the feedback store, calls `active(feedback_store, user_id, day)` for each
  reader, and passes the result in.
- `select_digest(..., *, previous_digest=None, feedback=None)`: `None` means an empty
  state. `select(..., recent_messages, feedback: FeedbackState)`: a new required
  parameter.
- Callers: a feedback store is read only when the caller names it, never because a file
  exists, so real clicks from a live bot check cannot change a replay. `digest post`
  reads its `--db` when `--db` is given or `--interactive` is on, the same condition under
  which it writes the store (`docs/plans/m5-exposure.md`). `digest demo` reads one only
  with `--feedback-db PATH`. `digest run`, `digest diff`, `digest serve`, the web page and
  the eval pass no feedback: they show the recorded world.
- Precondition, stated in the docstrings: the state passed for a reader was built for that
  reader and day.

## Rule

- Order inside `select`:
  1. the visibility check, as today;
  2. resolved: an item whose id is in `resolved` is removed for every reader, and so is an
     ask absence whose cited ask (`_cited_ask`) is in `resolved`. Resolved items supply no
     entities to the recent-activity pre-pass;
  3. the recent-activity pre-pass, over the remaining current items;
  4. snoozed: an item or absence whose id is in `snoozed` with a date after `day` is
     removed for this reader. Absence ids are `absence:<task id>` and
     `absence:ask:<ask id>`. Expiry removes only this exclusion: on the date and after, the
     line still needs its lane's rules (must-see, or the window, threshold and cap);
  5. suppressed: an absence whose episode is in `suppressed` is removed for this reader.
     Its episode is `episode(item.absence.source, item.absence.task if the source is
     "checklist" else _cited_ask(item), item.evidence[0].channel_id,
     item.evidence[0].message_ts)`; `digest.absence` builds each absence with exactly one
     evidence entry;
  6. the must-see lane and the ranked lane, as today, so a removed ranked line frees its
     cap slot for the next one;
  7. the ask-replacement rule, as today: an ask whose absence line was removed keeps its
     own line under its own rules.
- A line removed by feedback does not count in `hidden`.
- With an empty state, every digest is the same as today.

## Doc changes (first commit)

- SPEC section 5 step 8: selection reads the derived feedback state of the reader's
  feedback store, before both lanes; a resolved ask also removes its absence; the filter
  is not counted in `hidden`.
- `docs/plans/m3-demo.md` B5.5: a pointer to this plan.

## Behaviours

Feature checks (built with constructed `FeedbackState` values, then one integration case):
- FS1. Resolved: an item with `status` open and evidence later than the resolution shows
  to no reader in either lane (a must-see case and a ranked case), paired with the same
  item not resolved.
- FS2. A resolved ask removes its ask absence for the asker and for the person asked;
  paired with the ask not resolved.
- FS3. Snooze: for the snoozing reader, an eligible item is removed on a day before the
  date and shows on the date; another reader's selection, built with its own state, shows
  it on both days. An expired snooze on a ranked item outside the window does not show
  (expiry restores eligibility only).
- FS4. Suppress: an absence that shows as must-see without feedback is removed when its
  exact episode is suppressed, for a checklist absence and an ask absence; the same absence
  citing newer evidence (a new episode) shows.
- FS5. Cap: with a section at its cap plus one, feedback that removes a leading ranked
  line lets the next one in.
- FS6. Replacement: an ask whose absence is suppressed or snoozed keeps its own line; an
  ask whose absence still shows does not.
- FS7. Recent activity: a resolved item supplies no entities; a snoozed item still does.
- FS8. `hidden` is unchanged when feedback removes a line that showed without feedback.

Preservation (the RED proof is a mutation after the code exists):
- FS9. An empty state, and `feedback=None`, give today's selection.

Integration (after the slice merges):
- FS10. A done recorded with `feedback.record` in a feedback store removes the item from
  the next `digest post` selection for every reader; an undo restores it. A store without
  the table gives today's selection.

## Acceptance

- Tests in `tests/test_select.py`, `tests/test_daily_render.py` and the CLI tests cover
  FS1 to FS10; each new or changed test is RED-GREEN proven.
- `uv run digest demo` with no feedback store is byte-identical.
- The extraction eval report does not change.

## Not in scope

- The "it happened" producer (the button, the event and its derivation into `suppressed`):
  a later unit of the bot plan. This unit consumes `suppressed` as a contract.
- The closing line of SPEC step 7 for a resolved item a reader saw while open: it needs
  the seen record.
- Exposure records, and resolution that new evidence reopens (a resolution holds until
  undo).
- When the person asked suppresses an ask absence, the ask itself stays must-see for them
  ("an open ask addressed to you") until extraction reads their reply. That is the SPEC's
  rule, not a defect.

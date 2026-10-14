# Plan: M5 slice, the interactive digest (`digest bot`)

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`). Stacked on the layout unit
(`docs/plans/m5-layout.md` v0.4, branch `m5-layout`): it adds menus to the v6 top message
and thread reply that `slack_digest` builds. Rebased onto `main` after the layout unit
merges. This is "Next" of the layout plan and step 1 of the Interaction section of
`docs/later.md`, without `not mine` and without the seen record. Section "Out of scope"
lists what it does not do.

## Goal

A reader can act on each line of a posted digest. The actions are `Mark done`, `Snooze
until tomorrow`, `It happened` and `Why this item?`. Each action except `Why this item?`
is a feedback event in the store (SPEC section 5 step 8, D27). The message then shows
the action on the line, with an `Undo` button. The next digest does not change yet: the
unit that makes selection honour snooze, done and suppression comes next (Next units 1).

## Commands

```
digest post --dm USER_ID --interactive [--db PATH] [--day N] [--seed-map PATH]
digest bot [--db PATH]
```

- `--interactive` (on `post`): build the messages with `interactive=True` (I10) and keep a
  record of each posted message in the store (I11). It needs `--dm`, else the command
  exits 1 before it builds a digest: `--interactive needs --dm: the bot cannot update a
  webhook message`.
- `--db PATH` (on `post` and `bot`): the feedback store. Default `.digest/feedback.sqlite`.
  The existing `*.sqlite` rule in `.gitignore` covers it, so `.gitignore` does not change.
  `digest post` opens it only with `--interactive`: after the `--dm` and token checks and
  before it builds a digest, so a refused store sends nothing. Both commands open it with
  `_open_store`, so a file of another schema version, or a file that is not SQLite,
  exits 1 with today's text.
- `digest bot` runs a slack-bolt `App` in Socket Mode until Ctrl-C. It reads
  `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` with `_env_value` (environment, then `.env`).
  Refusals, each exit 1 before any connection: a missing token gives `_not_set(<key>)`;
  `SLACK_BOT_TOKEN must start with xoxb-`; `SLACK_APP_TOKEN must start with xapp-`; no
  slack extra gives `digest bot needs the slack extra; run uv sync --extra slack`. When the
  handler starts, the command prints `listening for digest actions; press Ctrl-C to stop`.
  Ctrl-C closes the handler and prints `stopped`; the exit code is 0.

## Behaviours

### Feedback log (`digest/feedback.py`, no Slack)

I1. **Events.** One row per event in `feedback_events` (Data). `event` is `done`,
`snooze`, `it_happened` or `undo`. `item_id` is the item's `item_id`. For an absence it is
the absence's own key (D26): the absence item's `item_id`, `absence:<task id>` or
`absence:ask:<ask item id>`, which holds the source and the task or ask; the `day` column
holds the day. `reader` is the persona `user_id` of the digest (I12), never the clicker.
`slack_user` is the clicker's Slack id. `run_id` and `day` come from the message (I9).
`ts` is the UTC time of the write, ISO 8601 with offset, from an injected clock.

I2. **Payload, by event** (a JSON object; keys not listed are absent):

| Event | Payload |
|---|---|
| `done` | `{"resolution": "owner", "item_resolved": true \| false}` or `{"resolution": "exposure"}` (I3) |
| `snooze` | `{"until": "<YYYY-MM-DD>"}` (I5) |
| `it_happened` | `{"episode": {"source": "checklist" \| "ask", "task": <id> \| "ask": <item id>, "last_evidence": "<channel_id>:<message_ts>"}}` (I6) |
| `undo` | `{"undoes": <event id>}` (I7) |

SPEC's optional permalink on `it happened` and the profile version are not in this unit:
a menu click carries no permalink, and profiles have no version yet.

I3. **Done.** The subject's kind decides (`Subject.kind`, Interface):

- Kind `item`, and the item's `owner` equals the reader: `resolution: owner`. The
  function also resolves the item in the store given (I4).
- Kind `item`, any other owner (another person, `None`): `resolution: exposure`. Nothing
  else is written.
- Kind `checklist_absence`: `resolution: exposure`. The task stays open (SPEC section 4:
  a task closes only when a message carries a `done` link). The menu of I10 offers `It
  happened` on absence lines, not `Mark done`, so only another adapter (web, MCP) can send
  this event. The rule lives in `feedback.py` for them.
- Kind `ask_absence`: `record` raises ValueError (`done is not an action on an ask
  absence; use it_happened`). The menu never offers it (SPEC section 5 step 8).

I4. **What an owner's done writes to `items`.** One statement, in the same transaction
as the event row:

```sql
UPDATE items SET status = 'resolved', resolved_by = 'feedback:<event id>'
 WHERE item_id = ? AND status = 'open'
```

`last_updated` and `last_updated_us` do not change (`Store.items(as_of)` filters on them).
`item_resolved` in the payload is true when the statement changed one row, else false.
`select.py` keeps only `status == "open"` items, so a digest built from this store leaves
the item out for every reader. For a kept item with members (layout 4.3), the statement
touches the absorber's row only, never a member's row. Two limits, stated, not fixed here:

- `digest post` builds the demo digests from a temporary store that it replays each run
  (`_demo_store`), and that store is not `--db`. So in the demo, `--db` holds no item row,
  the statement changes nothing, and `item_resolved` is false. Next units 1 decides how
  selection reads owner resolutions from the log.
- The next extract run of the item's thread writes every column again (`_UPSERT_ITEM`),
  `status` included, so it can reopen the item.

I5. **Snooze.** `until` is `add_working_days(<day>, 1)` (`digest.project`), where `<day>`
is the digest day of the message, not the time of the click. Day 6 of Kestrel K2 (Mon 19
Oct 2026) gives `2026-10-20`; a Friday gives the Monday after.

I6. **It happened.** Allowed on kinds `checklist_absence` and `ask_absence` only; on kind
`item`, `record` raises ValueError, as it does for a subject with no episode. The episode
is built at post time in `daily/slack.py`, which already reads `digest.daily.select`, and
travels in `Subject.episode`; `feedback.py` imports nothing from `daily`. Its fields:
`source` from `absence.source`; `task` from `absence.task` (checklist), or `ask` = the
absence's `item_id` without `ASK_ABSENCE_PREFIX`; `last_evidence` = the key of the
absence item's evidence entry (the detector writes exactly one). The event changes no task
state, no ledger, no item row and no other reader's feedback.

I7. **Undo.** `undo(event_id, ...)` records an `undo` event that copies `item_id`,
`reader`, `run_id` and `day` from the target, with `slack_user`, `ts` and the
idempotency key of the click. It raises ValueError, and records nothing, when the target
does not exist, is itself an `undo`, or is already undone. When the target is a `done`
that changed an item row (I4), the same transaction runs:

```sql
UPDATE items SET status = 'open', resolved_by = NULL
 WHERE item_id = ? AND resolved_by = 'feedback:<target id>'
```

So an undo never reopens an item that extraction resolved, or that another done
resolved.

I8. **Idempotency.** `idempotency_key` is unique in the table:
`<action_ts>:<block_id>` of the Slack action. `record` and `undo` check the key first:
a key already in the table returns None and writes nothing, before any ValueError check.
The `items` update of I4 and I7 runs only when the event row is written.

`active(reader, day)` returns the reader's events for that digest day whose `event` is
not `undo` and that no `undo` event names, in id order.

### Posting (`digest post --dm --interactive`)

I9. **Run id and metadata.** `digest post` mints one `run_id` per run:
`uuid.uuid4().hex` (32 lower-case hex characters). On the DM path, every
`chat.postMessage` (top message and thread reply) carries
`metadata = {"event_type": "downstream_digest", "event_payload": {"reader": <persona
user_id>, "day": "<YYYY-MM-DD>", "run_id": <run_id>, "world": "kestrel"}}`, with or without
`--interactive` (SPEC section 5 step 9 puts the run id in each sent part). The webhook path
sends no metadata.

I10. **Item menu** (`slack_digest(..., interactive=True)`). Each item's two blocks change
as follows; nothing else in the payload changes:

- The section gets `block_id` `i<n>` and the context gets `block_id` `c<n>`, where `<n>`
  is the item's index in that message, from 0, in block order. A group heading and the
  other blocks get no `block_id`.
- The section gets an `overflow` accessory, `action_id` `item_menu`, with three options
  (plain_text, in this order):

| Item | Options |
|---|---|
| Not an absence (needs_you, decisions, risks, fyi; a kept item with members included) | `Mark done`, `Snooze until tomorrow`, `Why this item?` |
| Ask absence (in needs_you or not_happened) and checklist absence | `It happened`, `Snooze until tomorrow`, `Why this item?` |

- Option `value`: `<action>:<key>`, with action `done`, `snooze`, `it_happened` or `why`,
  and key the kept item's `item_id` (the absorber's, for a kept item with members). When
  that string is longer than 150 characters (Slack's option value limit), the value is
  `<action>:#<n>` with the `<n>` of the section's `block_id`; the listener reads the key
  of item `<n>` from the posted record (I11). No item id starts with `#`.
- With `interactive=False` (the default), the payload is byte for byte what the layout
  unit builds.
- Each returned message dict gains the key `items` only with `interactive=True`: the list
  of `PostedItem` records (Interface) of that message, in `<n>` order. The CLI removes it
  before the Slack call.

I11. **Posted record.** With `--interactive`, after each `chat.postMessage` that Slack
accepts, the command writes one `posted_messages` row (Data): the DM channel, the
response `ts`, the reader, day, run id and world, the `text` and `blocks` as sent, the
metadata, and the `PostedItem` records. A response with no string `ts` is a failure in
this mode, for the top message too (in the layout plan 11.1 only a reply needs it): `cannot
record the digest for <reader>: the message has no ts; its message was sent<note>`, with
`<note>` as in layout 11.1. A row with the same `(channel, ts)` is replaced.

### Listener (`digest bot`)

I12. **Accept a click.** The bolt adapter registers `item_menu` and `item_undo`. Each
calls `ack()` first, then `handle_action(body, ...)`. The handler ignores the click, with
the log event `bot_action_ignored` and a `reason` field, and calls no Slack method, when:

| Reason | Condition |
|---|---|
| `not_a_message` | `container.type` is not `message`, or no `container.channel_id` or `container.message_ts` |
| `no_metadata` | `message.metadata` is missing, or its `event_type` is not `downstream_digest`, or its payload lacks a string `reader`, `day`, `run_id` or `world` |
| `unknown_message` | no `posted_messages` row for `(channel, ts)` |
| `metadata_mismatch` | the row's reader, day, run id or world differs from the metadata |
| `bad_value` | the value does not parse as I10 or I15 says, or names no item of the row |
| `bad_undo` | `undo` raised ValueError (I7) |

The reader is always the metadata's `reader`. The clicker's id goes only to `slack_user`.

I13. **Why this item?** `views.open` with the click's `trigger_id` and a modal: title
`Why this item?`, close button `Close`, no submit, and three blocks, each a mrkdwn section
with `verbatim: true`: `*<title>*`; `*Why it is here*\n<reason>`; `*Sources*\n<one line
per source>`. `<title>` is the displayed title of the line (layout 5.2), `<reason>` the
line's `reason` from `select.py`. The sources are the evidence entries of the kept item,
then of each member, each once, at most 10 lines. A line follows layout 6.1: mapped,
`<permalink|Message> in <#C...>` (`Thread` for a thread root); unmapped, `In <#C...>` or
`In #<name>`, with no link. Every text passes the layout pipeline (flatten, escape, the
3000 cut). The click records nothing (it is not a SPEC feedback event).

I14. **Record and update.** For `done`, `snooze` and `it_happened`: build the `Subject`
from the `PostedItem`, call `feedback.record`. A None result (a repeated delivery) logs
`bot_action_repeated` and stops: no update. Else call `chat.update` with the row's
channel, ts, `text` and metadata, and the blocks of I15.

I15. **The updated message.** The blocks are the row's posted blocks, with, for each item
`<n>` that has an event in `active(reader, day)` (the newest, by id, when there are more):

- the context element's text gets the suffix ` · Done`, ` · Snoozed until <Day d Mon>`
  (the payload's `until`) or ` · Marked as happened` (separator and date text as in
  layout 2). `feedback_blocks` in `daily/slack.py` builds the suffix from the event, with
  the layout's own date text. When the result would pass 3000 characters, the original
  text is cut first with `slack._cut` to `3000 - len(suffix)`;
- the section's overflow is replaced by a button accessory: text plain_text `Undo`,
  `action_id` `item_undo`, `value` the event id as a decimal string. It has no `url`.

An item with no active event keeps its posted blocks. So an undo restores the line, and
a click on another item of the same message keeps the earlier marks. For `item_undo`, the
handler calls `feedback.undo(int(value), ...)`; a value that is not a decimal integer is
`bad_value`. None logs `bot_action_repeated`; ValueError is `bad_undo`; else the handler
sends `chat.update` as in I14.

I16. **One click at a time.** Bolt can run listeners on worker threads, and `Store.open`
makes a connection that only its own thread may use. So the handler holds one
process-wide `threading.Lock` from the store open to the end of the Slack call, and opens
and closes the store (`Store.open(db)`) inside the lock for each click. Two clicks on one
message then update it in turn, and the second update shows both events.

I17. **Slack failures.** A failed `views.open` or `chat.update` logs `bot_slack_failed`
with `method` and `error` (the Slack error code, else the exception type; the
`_slack_failure` rule), and the listener goes on. A recorded event stays recorded; the
next update of that message shows it. The adapter turns every exception of a Slack call
into `BotCallFailed(<that text>)`, raised `from None`, so no exception chain reaches a
log.

I18. **Safety.**

- No output, log record or exception text holds a token. The `App`, the Socket Mode
  handler and the `WebClient` each get a disabled logger, as `_slack_web_client` does.
  Log calls are an event token plus fields (`.claude/rules/logging.md`); no field holds a
  payload, a token or message text.
- Every update keeps the layout's rules (9.1, 9.2): the posted blocks were built by the
  layout pipeline; the suffixes and the button text hold no `&`, `<` or `>`.
- The A17 safety helper of the layout plan gains an `interactive` flag. With it, an item
  section may hold exactly one accessory: an overflow with `action_id` `item_menu`, 2 to
  5 options, each value at most 150 characters and of the I10 form; or a button with
  `action_id` `item_undo`, text `Undo` and a decimal value. No element has a `url`. There
  is still no `actions` block. Without the flag, the helper is unchanged.

## Data

Two tables join `schema.sql`. They use `CREATE TABLE IF NOT EXISTS`, so `Store.open`
adds them to an existing store. `SCHEMA_VERSION` stays `"2"`: a bump would make
`_check_version` refuse every existing store. No read-only path reads them.

```sql
CREATE TABLE IF NOT EXISTS feedback_events (
  id              INTEGER PRIMARY KEY,
  event           TEXT NOT NULL,   -- done | snooze | it_happened | undo
  item_id         TEXT NOT NULL,   -- an item id, or an absence's own key (I1)
  reader          TEXT NOT NULL,   -- persona user_id from the message metadata
  slack_user      TEXT NOT NULL,   -- the clicker's Slack user id
  run_id          TEXT NOT NULL,
  day             TEXT NOT NULL,   -- ISO date of the digest shown
  payload         TEXT NOT NULL,   -- JSON object (I2)
  ts              TEXT NOT NULL,   -- UTC ISO 8601 time of the write
  idempotency_key TEXT NOT NULL UNIQUE  -- <action_ts>:<block_id>
);
CREATE INDEX IF NOT EXISTS feedback_reader_day ON feedback_events (reader, day);

CREATE TABLE IF NOT EXISTS posted_messages (
  channel  TEXT NOT NULL,   -- Slack channel id of the DM
  ts       TEXT NOT NULL,   -- Slack ts of the posted message
  reader   TEXT NOT NULL,
  day      TEXT NOT NULL,
  run_id   TEXT NOT NULL,
  world    TEXT NOT NULL,
  text     TEXT NOT NULL,   -- the notification text as sent
  blocks   TEXT NOT NULL,   -- JSON array, the blocks as sent
  metadata TEXT NOT NULL,   -- JSON object, as sent
  items    TEXT NOT NULL,   -- JSON array of PostedItem
  PRIMARY KEY (channel, ts)
) WITHOUT ROWID;
```

`store.py` holds the SQL: insert an event (`INSERT ... ON CONFLICT (idempotency_key) DO
NOTHING`, then the new id or None), read one event by id, read the events of a reader and
day, the two `items` updates of I4 and I7, and save or read a posted message.
`feedback.py` holds the rules. `daily/bot.py` holds the `PostedMessage` model and turns
it into a row for the store.

## Interface (stubs land before tests)

```python
# digest/feedback.py (no Slack)
EventType = Literal["done", "snooze", "it_happened", "undo"]
SubjectKind = Literal["item", "checklist_absence", "ask_absence"]

class Subject(Frozen):              # what a line is about
    key: str                        # item_id; the absence's own key for an absence (I1)
    kind: SubjectKind
    owner: str | None               # the item's owner (a persona user_id)
    episode: dict[str, str] | None  # I6, for an absence only

class FeedbackEvent(Frozen):
    id: int
    event: EventType
    item_id: str
    reader: str
    slack_user: str
    run_id: str
    day: date
    payload: dict[str, Any]
    ts: datetime
    idempotency_key: str

def record(store: Store, event: Literal["done", "snooze", "it_happened"], subject: Subject, *,
           reader: str, slack_user: str, run_id: str, day: date, idempotency_key: str,
           at: datetime) -> int | None: ...
    # I2 to I6, I8; the event id, or None for a key already seen
def undo(store: Store, event_id: int, *, slack_user: str, idempotency_key: str,
         at: datetime) -> int | None: ...
    # I7, I8; the undo event id, or None for a key already seen; ValueError per I7
def active(store: Store, reader: str, day: date) -> list[FeedbackEvent]: ...   # I8

# digest/daily/slack.py
def slack_digest(world: World, selection: Selection, *, links: SeedMap | None,
                 authors: Mapping[str, str], threaded: bool = True,
                 interactive: bool = False) -> list[dict[str, Any]]: ...
    # layout plan 12; interactive=True adds I10 and the "items" key
def feedback_blocks(blocks: list[dict[str, Any]],
                    marks: Mapping[int, FeedbackEvent]) -> list[dict[str, Any]]: ...
    # I15: marks maps item <n> to its newest active event; returns new blocks, input unchanged

# PostedSource and PostedItem live in daily/slack.py, which builds them. daily/bot.py
# imports them and feedback_blocks from there; slack.py imports nothing from bot.py.
class PostedSource(Frozen):
    label: str                      # "Message" or "Thread"
    url: str | None                 # the seed-map permalink; None when unmapped
    channel: str                    # "<#C...>" or "#<world channel name>"

class PostedItem(Frozen):
    n: int                          # the index in block_id i<n>
    subject: Subject
    title: str                      # displayed title, raw (layout 5.2)
    reason: str                     # Line.reason
    sources: tuple[PostedSource, ...]

# digest/daily/bot.py
class PostedMessage(Frozen):
    channel: str
    ts: str
    reader: str
    day: date
    run_id: str
    world: str
    text: str
    blocks: list[dict[str, Any]]
    metadata: dict[str, Any]
    items: tuple[PostedItem, ...]

class BotCallFailed(Exception): ...  # its message is a Slack error code or a type name

class BotSlackClient(Protocol):
    def open_view(self, trigger_id: str, view: dict[str, Any]) -> None: ...
    def update(self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]],
               metadata: dict[str, Any]) -> None: ...

def save_posted(store: Store, message: PostedMessage) -> None: ...              # I11
def handle_action(body: Mapping[str, Any], db: Path, client: BotSlackClient, *,
                  now: Callable[[], datetime]) -> None: ...                       # I12 to I17
def why_view(item: PostedItem) -> dict[str, Any]: ...                            # I13

# digest/cli.py
# post gains --interactive and --db; bot is a new command; _BoltBotClient implements
# BotSlackClient over WebClient (views_open, chat_update) and raises BotCallFailed with
# _slack_failure(error). The slack_bolt imports are inline: slack-bolt is in the optional
# slack extra, as slack_sdk is.
```

- `digest.feedback` joins the layer `digest.replay | digest.extract | digest.absence |
  digest.seed` in the import-linter layers contract: above `digest.store`, below
  `digest.daily`.
- `handle_action` takes the bolt `body` (a `block_actions` payload) so the tests build it
  as Slack sends it. It reads `user.id`, `trigger_id`, `container`, `message.metadata`
  and `actions[0]` (`action_id`, `block_id`, `action_ts`, `selected_option.value` or
  `value`).
- `_post_dm` gains the run id, the metadata, and with `--interactive` the record of I11.
  `select.py` does not change.
- Tests the test author updates, because I9 adds `metadata` to every DM
  `chat.postMessage`: in `tests/test_slack_post.py` as the layout branch has it (commit
  `ae852ce`; the line numbers can move), the fakes `FakeSlackWebClient.chat_postMessage`
  (near line 339) and `TrackingSlackWebClient.chat_postMessage` (near line 666) take only
  `channel`, `text` and `blocks`, and the exact call list near line 457 holds no
  `metadata`. Both fakes gain a `metadata` keyword and the expected calls gain the I9
  metadata, with the run id read back from the first recorded call.

## Before tests

Each item is a short spike in Scratch, recorded in this file before the test author
starts. A result that differs changes the plan first.

1. Does a `block_actions` payload from an overflow click hold `message.metadata`? If it
   does not, I12 reads reader, day, run id and world from the `posted_messages` row
   alone, and `no_metadata` becomes "no row".
2. Does `chat.update` without `metadata` keep the message's metadata? The plan resends it
   on every update either way; the answer only tells whether a failed resend loses it.
3. The Scratch app has Socket Mode and Interactivity on, and an app-level token with
   `connections:write` in `SLACK_APP_TOKEN`. `slack_bolt.adapter.socket_mode
   .SocketModeHandler` connects with only the `slack` extra installed.
4. A section with an overflow accessory and verbatim mrkdwn renders on desktop and on the
   phone, and `views.open` with the I13 modal opens within the 3-second `trigger_id`
   limit.

Results, 2026-10-13 (Scratch):
- Item 3: `slack_sdk.socket_mode.SocketModeClient` with `SLACK_APP_TOKEN` connected
  (`is_connected()` true) and closed cleanly. Interactivity in the app settings is not
  verified by this spike; the live row (A-live) checks it.
- Item 2: a message posted with `metadata`, then updated with `chat.update` and no
  `metadata`, still held the same metadata when read back with `include_all_metadata`.
- Items 1 and 4 need a person to click. Until then the bot reads reader, day, run id and
  world from the `posted_messages` row first (the fallback of item 1 is the design), and
  uses the payload metadata only as a cross-check. Item 4 is part of the live row.

## Acceptance

Each row is tested offline with a temporary store, hand-made `PostedMessage` rows, a fake
`BotSlackClient` that records calls, and hand-built Slack payloads, unless it says
otherwise.

- A1 (I1, I2). Each event type writes one row with the I2 payload; `reader` is the
  metadata reader, `slack_user` the clicker, `ts` the injected time; an absence's
  `item_id` is `absence:<task>` or `absence:ask:<id>`.
- A2 (I3). Done by the owner: `resolution: owner`; an open item row in the store becomes
  `resolved` with `resolved_by = feedback:<id>` and keeps `last_updated`; `select` then
  leaves it out for another reader. No item row: `item_resolved` false, no error. Done by
  a non-owner and on an item with no owner: `exposure`, the item row unchanged. Done on a
  checklist absence: `exposure`, no task or row change. Done on an ask absence: ValueError,
  no row.
- A3 (I7). Undo of the owner's done reopens the row; undo does not reopen a row whose
  `resolved_by` is another value; undo of an unknown id, of an undo, and of an undone
  event each raise ValueError and write nothing.
- A4 (I5). Snooze on day 6 (Mon 19 Oct 2026) gives `until` 2026-10-20; on a Friday it
  gives the Monday after.
- A5 (I6). `it_happened` on a checklist absence and on an ask absence records the episode
  (`ask` without the prefix); on an item it raises ValueError; no item row changes.
- A6 (I8). The same idempotency key twice: one row, the second call returns None, also for
  undo and also when the second call would raise; `active` leaves out undone events and
  undo events and keeps id order.
- A7 (I9). On the DM path, each `chat.postMessage` (top and reply) carries the metadata
  with one run id for the whole run, with and without `--interactive`; the webhook path
  sends none.
- A8 (I10). With `interactive=True`: the block ids `i<n>` and `c<n>`; the options and their
  order for an item, an ask absence in needs_you, an ask absence in not_happened and a
  checklist absence; the values; a key that makes the value longer than 150 characters
  gives `<action>:#<n>`; the `items` key lists the records in `<n>` order. With
  `interactive=False`, the payload equals the layout unit's output and has no `items` key.
  Every message passes the A17 helper with the `interactive` flag; the helper fails on an
  overflow with a `url`, on an `actions` block, and on a value of another form.
- A9 (I11, Commands). `--interactive` without `--dm` exits 1 before any build; a refused
  `--db` exits 1 with no DM opened and nothing sent; with `--interactive`, one
  `posted_messages` row per accepted message, with the blocks and metadata as sent; a
  response with no `ts` gives the refusal of I11; the CLI sends no `items` key to Slack.
- A10 (I12). Each ignore reason in the table gives `bot_action_ignored` with that reason,
  no row and no Slack call; the reader comes from the metadata when the clicker differs.
- A11 (I13). `Why this item?` calls `open_view` with the click's `trigger_id` and the I13
  modal; mapped and unmapped sources; escaped text; no row is written.
- A12 (I14, I15). Each action updates the message once with the suffix and an `Undo`
  button holding the event id; the update keeps `text` and metadata; an undo click
  restores the posted blocks of that item; marks on two items of one message both show;
  a repeated delivery makes no update.
- A13 (I16). Two handler calls from two threads on one message both write their rows, and
  the last update shows both marks.
- A14 (I17, I18). A `BotCallFailed` from `update` or `open_view` logs `bot_slack_failed`
  and keeps the row; with a token in the fake error, no captured log record and no output
  holds it; the `bot` command refuses a missing or malformed `SLACK_APP_TOKEN` or
  `SLACK_BOT_TOKEN` without printing the value.
- A15 (live, manual, recorded in the PR). In Scratch: run `digest bot`; in a second
  terminal run `digest post --dm <member> --interactive --seed-map <map>` for day 6. On
  the three digests, click `Why this item?`, `Mark done`, `Snooze until tomorrow` and `It
  happened`, each on a line where it shows, and `Undo` after each of the last three. Read
  the rows back with `uv run python -m sqlite3 .digest/feedback.sqlite "SELECT id, event,
  item_id, reader, day, payload FROM feedback_events"` and record them. Restart the bot
  and click once more: the update still works (the state is in the store).

## Test seams

- `feedback.record`, `undo` and `active` over `Store.open(tmp_path / "f.sqlite")`, with
  item rows from `Store.upsert_items` for A2 and A3.
- `slack_digest(..., interactive=True)` with hand-made selections, as the layout tests
  build them, and the layout A17 helper with its new flag.
- `handle_action` with a fake `BotSlackClient`, a fixed clock, and payload dicts in the
  shape of Slack's `block_actions`. No bolt `App` in a test: constructing one calls
  `auth.test`.
- The CLI with `CliRunner` and the Slack clients patched, as `tests/test_slack_post.py`
  does. No test touches the network.

## Next units

1. **Selection honours feedback.** The next digest leaves out a snoozed item until its
   date, an item an owner marked done (from the log, so the demo works without an item
   row in `--db`), and a suppressed absence episode until the reader undoes it; a new
   episode shows again (SPEC section 5 step 8). It changes `daily/select.py`, which
   another session owns now.
2. **Exposure on Slack's acceptance** (SPEC M5 core). `posted_messages` is the delivered
   part; the unit adds the item and claim ids shown, as D26 says.
3. **Not mine, confirm, thumbs and "more like this".** Confirm waits for contradiction
   lines.

## Out of scope

- Any change to selection, ranking or the next digest (Next units 1).
- `not mine`, `confirm`, thumbs, "more like this", a free-text reply.
- Profile weights recomputed from the log, the profile version, real-time alerts.
- The `it happened` permalink, a seen record, App Home and `/digest` commands.
- Interactive webhook messages: the bot cannot update them.
- Keeping an owner's done across the next extract run of the item's thread (I4).

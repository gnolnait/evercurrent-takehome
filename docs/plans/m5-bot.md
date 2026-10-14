# Plan: M5 slice, feedback recording and Slack interaction (`digest bot`)

Status: v0.6 (final plan review applied as rules and stated limits), 2026-10-13. Process stage 1 (`docs/process.md`).
Stacked on the layout unit (`docs/plans/m5-layout.md` v0.4, branch `m5-layout`): it adds
menus to the v6 top message and thread reply that `slack_digest` builds. Rebased onto
`main` after the layout unit merges. This is "Next" of the layout plan and part of step 1
of the Interaction section of `docs/later.md`. "Out of scope" lists what it does not do.

## Goal

This slice is feedback recording and Slack interaction; effects on the next digest come
in the next unit.

A reader can act on each line of a posted digest with `Mark done`, `Snooze until
tomorrow` and `Why this item?`. `Mark done` and `Snooze until tomorrow` each record a
feedback event in the store (SPEC section 5 step 8, D27). The message then shows the
action on the line, with an `Undo` button, and `Undo` records an `undo` event. `digest
/feedback.py` derives a `FeedbackState` from the log (I6).

This slice writes no item status and changes no digest. Applying the events is the next
unit (Next units 1), built with the session that owns `daily/select.py`. That unit makes
an owner's done resolve the item, filters done and snoozed items, and adds `It happened`
with its episode suppression.

This slice is a prerequisite. It does not pass the SPEC step 8 acceptance: the absence
actions (`It happened`, and `Mark done` on a checklist absence line) and the exposure
records come in later units (Next units 1 and 2).

## Commands

```
digest post --dm USER_ID --interactive [--db PATH] [--day N] [--seed-map PATH]
digest bot [--db PATH]
```

- `--interactive` (on `post`): build the messages with `interactive=True` (I9) and keep a
  record of each posted message in the store (I10). It needs `--dm`, else the command
  exits 1 before it builds a digest: `--interactive needs --dm: the bot cannot update a
  webhook message`.
- `--db PATH` (on `post` and `bot`): the feedback store. Default `.digest/feedback.sqlite`.
  The existing `*.sqlite` rule in `.gitignore` covers it, so `.gitignore` does not change.
  `digest post` opens it only with `--interactive`: after the `--dm` and token checks and
  before it builds a digest, so a refused store sends nothing. Both commands open it with
  `_open_store`, so a file of another schema version, or a file that is not SQLite,
  exits 1 with today's text. `digest bot`: the worker opens it once and keeps it
  open (I15); a Why click opens it read-only (I12).
- `digest bot` runs a slack-bolt `App` in Socket Mode until Ctrl-C. It reads
  `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` with `_env_value` (environment, then `.env`).
  Refusals, each exit 1 before any connection: a missing token gives `_not_set(<key>)`;
  `SLACK_BOT_TOKEN must start with xoxb-`; `SLACK_APP_TOKEN must start with xapp-`; no
  slack extra gives `digest bot needs the slack extra; run uv sync --extra slack`. When the
  handler connects, the command prints `listening for digest actions; press Ctrl-C to stop`.
  The command does not call `SocketModeHandler.start()`: on Windows `start()` sets SIGINT
  to `SIG_DFL`, so Ctrl-C kills the process with no cleanup. The command calls
  `handler.connect()`, then waits in its own loop of `time.sleep(1)` calls, which Ctrl-C
  interrupts with `KeyboardInterrupt`. Before it builds the handler, the command opens the
  store with `_open_store` and closes it, so a bad store exits 1 before any connection.
  An exception from `_socket_handler` (building the bolt `App` calls `auth.test`) or from
  `connect` exits 1 with `digest bot could not start: <exception type name>`; the text of
  the exception is not printed, because it can hold a token. A `finally` calls
  `handler.close()`; an exception from `close` logs `bot_close_failed` with the exception
  type only. On Ctrl-C the command closes the handler, puts the stop job (`None`) on the
  queue, waits for the worker for at most 10 seconds, prints `stopped`, and the exit code
  is 0. The worker is a daemon thread, so a worker that is still busy after 10 seconds
  does not keep the process alive. A click that arrives during shutdown can be lost; the
  clicker sees no mark (I11).
  `_socket_handler(bot_token, app_token, jobs, db)` in `cli.py` builds the bolt `App`,
  registers the listeners of I11 and returns the handler; the tests patch it.
- `--demo-actor` (on `bot`, off by default): the actor rule of I7. Without it, only a
  clicker whose Slack user id is the item's owner resolves the item.

## Behaviours

### Feedback log (`digest/feedback.py`, no Slack)

I1. **Events.** One row per event in `feedback_events` (Data). `event` is `done`,
`snooze` or `undo`. `item_id` is the item's `item_id`. For an absence it is the absence's
own key (D26): the absence item's `item_id`, `absence:<task id>` or `absence:ask:<ask item
id>`, which holds the source and the task or ask; the `day` column holds the day.
`reader` is the persona `user_id` of the digest, from the posted record (I10), never the
clicker. `slack_user` is the clicker's Slack id. `run_id` and `day` come from the posted
record. `ts` is the UTC time of the write, ISO 8601 with offset, from an injected clock.

I2. **Payload, by event** (a JSON object; keys not listed are absent):

| Event | Payload |
|---|---|
| `done` | `{"resolution": "owner"}` or `{"resolution": "exposure"}` (I3) |
| `snooze` | `{"until": "<YYYY-MM-DD>"}` (I4) |
| `undo` | `{"undoes": <event id>}` (I5) |

The profile version of SPEC step 8 is not in the payload: profiles have no version yet.

I3. **Done.** The subject's kind decides (`Subject.kind`, Interface). The event is the
only write.

- Kind `item`, and the item's `owner` equals the actor (I7): `resolution: owner`.
- Kind `item`, any other owner (another person, `None`): `resolution: exposure`.
- Kind `checklist_absence`: `resolution: exposure`. The task stays open (SPEC section 4:
  a task closes only when a message carries a `done` link). The menu of I9 does not offer
  `Mark done` on absence lines, so only another adapter (web, MCP) can send this event.
  The rule lives in `feedback.py` for them.
- Kind `ask_absence`: `record` raises ValueError (`done is not an action on an ask
  absence`). SPEC step 8 gives an ask absence `it happened` in place of done, and that
  action is deferred.

For a kept item with members (layout 4.3), the subject is the absorber; the event names
the absorber's key only.

`resolution: exposure` is a label on the feedback event only. This slice writes no
exposure record. The exposure unit (Next units 2) defines the exposure records, and it
reads these events.

I4. **Snooze.** `until` is `add_working_days(<day>, 1)` (`digest.project`), where `<day>`
is the digest day of the message, not the time of the click. Day 6 of Kestrel K2 (Mon 19
Oct 2026) gives `2026-10-20`; a Friday gives the Monday after.

I5. **Undo.** `undo(event_id, *, reader, day, item_id, ...)` records an `undo` event that
copies `item_id`, `reader`, `run_id` and `day` from the target, with `slack_user`, `ts`
and the idempotency key of the click. It raises `UndoMismatch` (a ValueError), and
records nothing, when the target's `reader`, `day` or `item_id` differs from the values
given: the posted record's reader and day, and the key of the clicked item. It raises
ValueError, and records nothing, when the target does not exist, is itself an `undo`, or
is already undone.

`record` and `undo` each run their checks and their insert inside one `BEGIN IMMEDIATE`
transaction. So two calls on two connections (another adapter, such as the MCP server)
cannot both undo one event: the second sees the first undo and raises ValueError.

I6. **Idempotency and the derived state.** `idempotency_key` is unique in the table:
`<channel>:<message ts>:<action_ts>:<block_id>` of the Slack action. `record` and `undo`
check the key first: a key already in the table returns None and writes nothing, before
any other check.

An event is **live** when it is not an `undo` and no `undo` event names it. So an undo
removes the effect of exactly its target.

`record` writes nothing when a live event of the same `event` already exists for the same
`reader`, `day` and `item_id`: it returns that event's id, and the caller updates the
message as for a new event. So a second click after a failed update repairs the line, and
one undo removes the done (or the snooze) completely. The check runs inside the same
`BEGIN IMMEDIATE` as the insert (I5).

`active(store, reader, day) -> FeedbackState` reads the event log only, never the
`items` table. When the store has no `feedback_events` table (a store built before
this slice, opened with `Store.open_readonly`, which runs no schema script), `active`
returns the empty state and `shown_marks` returns an empty dict. The check reads
`sqlite_master`; it writes nothing. It reads the live events whose `day` is at or before `day`:

- `resolved`: the `item_id` of each live `done` with `resolution: owner`, of any reader.
  It applies to every reader.
- `snoozed`: for each live `snooze` of `reader` whose `until` is after `day`, the
  `item_id` with its `until`. When one item has more than one, the latest `until` wins.
  On the `until` day the item is no longer in the map: it shows again.
- `suppressed`: always empty in this slice, because `It happened` is deferred.

`shown_marks(store, reader, day) -> dict[str, FeedbackEvent]` gives, for the message
update (I14), each `item_id` of `reader` with its newest live event of that exact `day`.

I7. **Actor rule.** The owner rule of I3 compares the item's `owner` with the actor.
`digest bot` has two modes:

- Default: the actor is the clicker's Slack user id (SPEC step 8: "when the clicker owns
  it"). In a real workspace the persona ids are Slack user ids. So only the owner resolves
  an item; another clicker's done is `exposure`.
- `--demo-actor`: in the demo, one person receives the digests of several readers (three
  personas, one Slack user). The actor is the posted row's reader.

In both modes the event's `reader` is the posted row's reader and `slack_user` is the
clicker. `record` takes the actor as the keyword `actor`.

### Posting (`digest post --dm --interactive`)

I8. **Run id and metadata.** `digest post` mints one `run_id` per run:
`uuid.uuid4().hex` (32 lower-case hex characters). On the DM path, every
`chat.postMessage` (top message and thread reply) carries
`metadata = {"event_type": "downstream_digest", "event_payload": {"reader": <persona
user_id>, "day": "<YYYY-MM-DD>", "run_id": <run_id>, "world": "kestrel"}}`, with or without
`--interactive` (SPEC section 5 step 9 puts the run id in each sent part). The webhook path
sends no metadata.

I9. **Item menu** (`slack_digest(..., interactive=True, token=...)`). Each item's two
blocks change as follows; nothing else in the payload changes:

- Block ids. The section gets `i.<message token>.<revision>.<n>` and the context gets
  `c.<message token>.<revision>.<n>`. `<n>` is the item's index in that message, from 0,
  in block order. `<revision>` is `0` at post time; each update uses a new one (I14), as
  Slack asks for new block ids on each update. `<message token>` is the `token` argument
  plus `t` for the top message or `r` for the thread reply. The CLI passes
  `<first 8 characters of run_id><reader index>`, so each message of a run has its own
  token. A token holds letters and digits only, never a `.`. A group heading and the
  other blocks get no `block_id`.
- The section gets an `overflow` accessory, `action_id` `item_menu`, with these options
  (plain_text, in this order):

| Item | Options |
|---|---|
| Not an absence (needs_you, decisions, risks, fyi; a kept item with members included) | `Mark done`, `Snooze until tomorrow`, `Why this item?` |
| Ask absence (in needs_you or not_happened) and checklist absence | `Snooze until tomorrow`, `Why this item?` |

- Option `value`: always `<action>:#<n>`, with action `done`, `snooze` or `why`, and the
  `<n>` of the section's block id. The listener reads the item from the posted record.
- With `interactive=False` (the default), the payload is byte for byte what the layout
  unit builds, and `token` is not used.
- Each returned message dict gains the key `items` only with `interactive=True`: the list
  of `PostedItem` records (Interface) of that message, in `<n>` order. The CLI removes it
  before the Slack call.

I10. **Posted record.** With `--interactive`, after each `chat.postMessage` that Slack
accepts, the command writes one `posted_messages` row (Data): the DM channel, the
response `ts`, the reader, day, run id and world, the `text` and `blocks` as sent, the
metadata, the `PostedItem` records, and `revision` 0. The row is the authority for
reader, day, run id and world. A response with no string `ts` is a failure in this mode,
for the top message too (in the layout plan 11.1 only a reply needs it): `cannot record
the digest for <reader>: the message has no ts; its message was sent<note>`, with
`<note>` as in layout 11.1. A row with the same `(channel, ts)` is replaced.

### Listener (`digest bot`)

I11. **Accept a click.** The bolt adapter registers `item_menu` and `item_undo`. Each
listener calls `ack()` first, then:

- for the `why:#<n>` option of `item_menu`: handles it at once, in the listener (I12);
- for every other click: puts the payload on the job queue of I15 and returns.

The listener does no other work, so it acks within Slack's 3-second limit, and it never
waits for the worker, the store or another click.

Ack first is a decision against the D7 amendment's "store before ack", which is about
Events API events. A crash after the ack loses a queued click. For a click, the loss is
visible: the line shows no mark, and the reader clicks again. A click whose event was
stored but whose update failed (I16) also shows no mark; the second click finds the live
event (I6) and only updates the line. An Events API event that is lost has no such sign.

Why runs in the bolt listener after the ack, as Slack's Bolt documents for modals. Bolt
runs listeners on its own thread pool, so a slow Why read or `views.open` holds one pool
thread, not the worker and not another click's ack.

The worker handles one job at a time (`handle_action`). It reads the `posted_messages`
row for `(container.channel_id, container.message_ts)`. The payload's `message.metadata`
is a cross-check only: missing metadata is accepted. The worker ignores the click, with
the log event `bot_action_ignored` and a `reason` field, and calls no Slack method, when:

| Reason | Condition |
|---|---|
| `not_a_message` | `container.type` is not `message`, or no `container.channel_id` or `container.message_ts` |
| `unknown_message` | no `posted_messages` row for `(channel, ts)` |
| `metadata_mismatch` | `message.metadata` is present, and its `event_type` is not `downstream_digest`, or a `reader`, `day`, `run_id` or `world` in its payload differs from the row |
| `bad_value` | the block id or the value does not parse as I9 or I14 says, or names no item of the row |
| `undo_mismatch` | `undo` raised `UndoMismatch` (I5): the target is another reader's, another day's or another line's event |
| `bad_undo` | `undo` raised another ValueError (I5) |

The reader is always the row's `reader` (I7).

I12. **Why this item?** The listener opens its own read-only connection
(`Store.open_readonly(db)`), reads the posted row, closes the connection, and calls
`views.open` at once with the click's `trigger_id`. The store is in WAL mode, so the read
never waits for a commit of the worker, and the job queue cannot delay the modal past
Slack's 3-second `trigger_id` limit. The modal: title `Why this item?`, close button
`Close`, no submit, and three blocks, each a mrkdwn section with `verbatim: true`:
`*<title>*`; `*Why it is here*\n<reason>`; `*Sources*\n<one line per source>`. `<title>`
is the displayed title of the line (layout 5.2), `<reason>` the line's `reason` from
`select.py`. The sources are the evidence entries of the kept item, then of each member,
each once, at most 10 lines. A line follows layout 6.1: mapped, `<permalink|Message> in
<#C...>` (`Thread` for a thread root); unmapped, `In <#C...>` or `In #<name>`, with no
link. Every text passes the layout pipeline (flatten, escape, the 3000 cut). The click
records nothing (it is not a SPEC feedback event). A Why value that names no item of the
row logs `bot_action_ignored` with `bad_value`.

I13. **Record and update.** For `done` and `snooze`, the worker builds the `Subject` from
the `PostedItem` and calls `feedback.record`. A None result (a repeated delivery) logs
`bot_action_repeated` and stops: no update. Else it adds 1 to the row's `revision` and
calls `chat.update` with the row's channel, ts, `text` and metadata, and the blocks of
I14. When the store write raises (`sqlite3.Error`), the worker logs `bot_store_failed`
with the exception type, makes no update and no retry, and goes on to the next job: the
line shows no mark, so the reader sees that the click did not apply. The order is a rule: `chat.update` runs only after `record` or `undo` has committed. So a
mark on a line always has its event in the store, and a lost click never shows a mark.

I14. **The updated message.** The blocks are the row's posted blocks, with every item
block id carrying the new revision, and, for each item `<n>` whose key is in
`shown_marks(reader, day)`:

- the context element's text gets the suffix ` · Done` or ` · Snoozed until <Day d Mon>`
  (the payload's `until`), with the separator and date text of layout 2.
  `feedback_blocks` in `daily/slack.py` builds the suffix from the event. When the result
  would pass 3000 characters, the original text is cut first with `slack._cut` to `3000 -
  len(suffix)`;
- the section's overflow is replaced by a button accessory: text plain_text `Undo`,
  `action_id` `item_undo`, `value` the event id as a decimal string. It has no `url`.

An item with no mark keeps its posted blocks, apart from the revision. So an undo
restores the line, and a click on another item of the same message keeps the earlier
marks. For `item_undo`: the clicked item is item `<n>` of the block id;
a value that is not a decimal integer is `bad_value`. The handler calls
`feedback.undo(int(value), reader=<row reader>, day=<row day>, item_id=<clicked item
key>, ...)`. None logs `bot_action_repeated`; `UndoMismatch` is `undo_mismatch`; another
ValueError is `bad_undo`; a `sqlite3.Error` is `bot_store_failed`, as in I13; else the
worker updates the message as in I13.

I15. **One worker.** `digest bot` starts one worker thread before the handler connects.
The worker opens the store once (`Store.open(db)`, which creates the tables) on its own
thread, keeps that one connection for the life of the process, and takes jobs from a
`queue.Queue` in order. There is no lock: one thread makes every feedback write and every
`chat.update`. Two clicks on one message are two jobs; the second update shows both
marks. A slow `chat.update` delays the jobs behind it, never an ack or a Why modal. A
`None` job stops the worker. Each job runs inside a `try`: an exception that I13, I14 or
I16 do not name rolls back the open transaction, logs `bot_job_failed` with the exception
type only, and the worker takes the next job. An exception never ends the worker.

I16. **Slack failures.** A failed `views.open` or `chat.update` logs `bot_slack_failed`
with `method` and `error` (the Slack error code, else the exception type; the
`_slack_failure` rule), and the listener goes on. A recorded event stays recorded; the
next update of that message shows it. The adapter turns every exception of a Slack call
into `BotCallFailed(<that text>)`, raised `from None`, so no exception chain reaches a
log.

I17. **Safety.**

- No output, log record or exception text holds a token. The `App`, the Socket Mode
  handler and the `WebClient` each get a disabled logger, as `_slack_web_client` does.
  Log calls are an event token plus fields (`.claude/rules/logging.md`); no field holds a
  payload, a token or message text.
- Every update keeps the layout's rules (9.1, 9.2): the posted blocks were built by the
  layout pipeline; the suffixes and the button text hold no `&`, `<` or `>`.
- The A17 safety helper of the layout plan gains an `interactive` flag. With it, an item
  section may hold exactly one accessory: an overflow with `action_id` `item_menu`, 2 or 3
  options, each value of the form `<action>:#<n>`; or a button with `action_id`
  `item_undo`, text `Undo` and a decimal value. No element has a `url`. Item block ids
  have the I9 form. There is still no `actions` block. Without the flag, the helper is
  unchanged.

### Docs (land with this unit)

- `docs/decisions.md` D7: a dated amendment. Interactive actions ack first, then a serial
  worker applies them; a crash can lose a queued click, which the clicker sees as no mark;
  Events API events still go to the store before the ack.
- `ANSWERS.md` (the Slack answer, near "Write the raw event to the store before the ack"):
  one clause that a button click is acked first and applied by a serial worker, with the
  same reason.

## Data

Two tables join `schema.sql`. They use `CREATE TABLE IF NOT EXISTS`, so `Store.open`
adds them to an existing store. `SCHEMA_VERSION` stays `"2"`: a bump would make
`_check_version` refuse every existing store.

```sql
CREATE TABLE IF NOT EXISTS feedback_events (
  id              INTEGER PRIMARY KEY,
  event           TEXT NOT NULL,   -- done | snooze | undo
  item_id         TEXT NOT NULL,   -- an item id, or an absence's own key (I1)
  reader          TEXT NOT NULL,   -- persona user_id from the posted record
  slack_user      TEXT NOT NULL,   -- the clicker's Slack user id
  run_id          TEXT NOT NULL,
  day             TEXT NOT NULL,   -- ISO date of the digest shown
  payload         TEXT NOT NULL,   -- JSON object (I2)
  ts              TEXT NOT NULL,   -- UTC ISO 8601 time of the write
  idempotency_key TEXT NOT NULL UNIQUE  -- <channel>:<message ts>:<action_ts>:<block_id>
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
  blocks   TEXT NOT NULL,   -- JSON array, the blocks as sent (revision 0)
  metadata TEXT NOT NULL,   -- JSON object, as sent
  items    TEXT NOT NULL,   -- JSON array of PostedItem
  revision INTEGER NOT NULL DEFAULT 0,  -- the block id revision of the last update
  PRIMARY KEY (channel, ts)
) WITHOUT ROWID;
```

`store.py` holds the SQL: insert an event (`INSERT ... ON CONFLICT (idempotency_key) DO
NOTHING`, then the new id or None), read one event by id, read events by reader and day,
read events up to a day, save or read a posted message, and add 1 to its revision. No
statement writes the `items` table. `feedback.py` holds the rules. `daily/bot.py` holds
the `PostedMessage` model and turns it into a row for the store.

## Interface (stubs land before tests)

```python
# digest/feedback.py (no Slack)
EventType = Literal["done", "snooze", "undo"]
SubjectKind = Literal["item", "checklist_absence", "ask_absence"]

class Subject(Frozen):              # what a line is about
    key: str                        # item_id; the absence's own key for an absence (I1)
    kind: SubjectKind
    owner: str | None               # the item's owner (a persona user_id)

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

@dataclass(frozen=True)
class FeedbackState:
    resolved: frozenset[str]        # item ids an owner marked done and did not undo; every reader
    snoozed: Mapping[str, date]     # this reader's snoozed item ids, with the day they show again
    suppressed: frozenset[Episode]  # empty in this slice

class Episode(NamedTuple):          # one silence of one absence (SPEC step 8)
    source: str                     # AbsenceDetail.source: "checklist" or "ask"
    subject: str                    # the task id, or the ask item id; source h: "<part>@<rev>"
    last_evidence: str              # "<channel id>:<message ts>" of its last evidence

def episode(source: str, subject: str, channel: str, message_ts: str) -> Episode: ...
    # the one constructor; posting and selection both call it

class UndoMismatch(ValueError): ... # the target is another reader's, day's or item's event

def record(store: Store, event: Literal["done", "snooze"], subject: Subject, *,
           reader: str, actor: str, slack_user: str, run_id: str, day: date,
           idempotency_key: str, at: datetime) -> int | None: ...
    # I2 to I4, I6; the event id, or None for a key already seen
def undo(store: Store, event_id: int, *, reader: str, day: date, item_id: str,
         slack_user: str, idempotency_key: str, at: datetime) -> int | None: ...
    # I5, I6; the undo event id, or None for a key already seen
def active(store: Store, reader: str, day: date) -> FeedbackState: ...         # I6
def shown_marks(store: Store, reader: str, day: date) -> dict[str, FeedbackEvent]: ...  # I6

# digest/daily/slack.py
def slack_digest(world: World, selection: Selection, *, links: SeedMap | None,
                 authors: Mapping[str, str], threaded: bool = True,
                 interactive: bool = False, token: str = "") -> list[dict[str, Any]]: ...
    # layout plan 12; interactive=True adds I9 and the "items" key
def feedback_blocks(blocks: list[dict[str, Any]], marks: Mapping[int, FeedbackEvent],
                    revision: int) -> list[dict[str, Any]]: ...
    # I14: marks maps item <n> to its mark; returns new blocks, input unchanged

# PostedSource and PostedItem live in daily/slack.py, which builds them. daily/bot.py
# imports them and feedback_blocks from there; slack.py imports nothing from bot.py.
class PostedSource(Frozen):
    label: str                      # "Message" or "Thread"
    url: str | None                 # the seed-map permalink; None when unmapped
    channel: str                    # "<#C...>" or "#<world channel name>"

class PostedItem(Frozen):
    n: int                          # the item index in its block ids
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
    revision: int = 0

class BotCallFailed(Exception): ...  # its message is a Slack error code or a type name

class BotSlackClient(Protocol):
    def open_view(self, trigger_id: str, view: dict[str, Any]) -> None: ...
    def update(self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]],
               metadata: dict[str, Any]) -> None: ...

def save_posted(store: Store, message: PostedMessage) -> None: ...              # I10
ActorRule = Literal["clicker", "demo_reader"]                                 # I7

def handle_action(store: Store, body: Mapping[str, Any], client: BotSlackClient, *,
                  actor_rule: ActorRule, now: Callable[[], datetime]) -> None: ...  # I11, I13 to I16
def handle_why(body: Mapping[str, Any], db: Path, client: BotSlackClient) -> None: ...  # I12
def run_worker(jobs: Queue[Mapping[str, Any] | None], db: Path, client: BotSlackClient, *,
               actor_rule: ActorRule, now: Callable[[], datetime]) -> None: ...     # I15
def listeners(jobs: Queue[Mapping[str, Any] | None], db: Path, client: BotSlackClient
              ) -> tuple[Callable[..., None], Callable[..., None]]: ...
    # I11: the item_menu and item_undo listeners; each takes ack and body
def why_view(item: PostedItem) -> dict[str, Any]: ...                            # I12

# digest/cli.py
# post gains --interactive and --db; bot is a new command; _BoltBotClient implements
# BotSlackClient over WebClient (views_open, chat_update) and raises BotCallFailed with
# _slack_failure(error). The slack_bolt imports are inline: slack-bolt is in the optional
# slack extra, as slack_sdk is.
```

- `active` is the one interface the selection unit reads (Next units 1). It reads the
  event log only.
- `digest.feedback` joins the layer `digest.replay | digest.extract | digest.absence |
  digest.seed` in the import-linter layers contract: above `digest.store`, below
  `digest.daily`.
- `handle_action` takes the bolt `body` (a `block_actions` payload) so the tests build it
  as Slack sends it. It reads `user.id`, `trigger_id`, `container`, `message.metadata`
  when present, and `actions[0]` (`action_id`, `block_id`, `action_ts`,
  `selected_option.value` or `value`).
- `_post_dm` gains the run id, the metadata, and with `--interactive` the record of I10.
  `select.py` does not change.
- Tests the test author updates, because I8 adds `metadata` to every DM
  `chat.postMessage`: in `tests/test_slack_post.py` as the layout branch has it (commit
  `ae852ce`; the line numbers can move), the fakes `FakeSlackWebClient.chat_postMessage`
  (near line 339) and `TrackingSlackWebClient.chat_postMessage` (near line 666) take only
  `channel`, `text` and `blocks`, and the exact call list near line 457 holds no
  `metadata`. Both fakes gain a `metadata` keyword and the expected calls gain the I8
  metadata, with the run id read back from the first recorded call.

## Before tests

Each item is a short spike in Scratch, recorded in this file before the test author
starts. A result that differs changes the plan first.

1. Does a `block_actions` payload from an overflow click hold `message.metadata`? The
   design does not depend on it: the posted row is the authority (I11).
2. Does `chat.update` without `metadata` keep the message's metadata? The plan resends it
   on every update either way.
3. The Scratch app has Socket Mode and Interactivity on, and an app-level token with
   `connections:write` in `SLACK_APP_TOKEN`. `slack_bolt.adapter.socket_mode
   .SocketModeHandler` connects with only the `slack` extra installed.
4. A section with an overflow accessory and verbatim mrkdwn renders on desktop and on the
   phone, and `views.open` with the I12 modal opens within the 3-second `trigger_id`
   limit.

Results, 2026-10-13 (Scratch):
- Item 3: `slack_sdk.socket_mode.SocketModeClient` with `SLACK_APP_TOKEN` connected
  (`is_connected()` true) and closed cleanly. Interactivity in the app settings is not
  verified by this spike; the live row (A17) checks it.
- Item 2: a message posted with `metadata`, then updated with `chat.update` and no
  `metadata`, still held the same metadata when read back with `include_all_metadata`.
- Items 1 and 4 need a person to click. The live row (A17) records a sanitized overflow
  payload and a sanitized button payload (no token, no trigger id), and checks item 4.

## Acceptance

Each row is tested offline with a temporary store, hand-made `PostedMessage` rows, a fake
`BotSlackClient` that records calls, and hand-built Slack payloads, unless it says
otherwise.

- A1 (I1, I2, I7). `done` and `snooze` each write one row with the I2 payload; `reader`
  is the posted row's reader in both actor modes, also when the payload has no metadata
  and when the clicker is another Slack user; `slack_user` is the clicker; `ts` is the injected time; an
  absence's `item_id` is `absence:<task>` or `absence:ask:<id>`.
- A2 (I3, I7). Done by the owner: `resolution: owner`. Done by a non-owner and on an item
  with no owner: `exposure`. Through `handle_action`: by default, a clicker whose Slack id
  is not the owner gives `exposure` although the posted reader owns the item, and is not
  in `resolved` for any reader; with `--demo-actor`, the same click gives `owner`. Done on a checklist absence: `exposure`. Done on an ask absence:
  ValueError, no row. In every case the `items` table is unchanged.
- A3 (I4). Snooze on day 6 (Mon 19 Oct 2026) gives `until` 2026-10-20; on a Friday it
  gives the Monday after.
- A4 (I5, I6). Two threads, each with its own connection, undo one event at once with two
  keys: one `undo` row; the other call raises ValueError. A second `done` (new key) on the
  same reader, day and item while the first is live writes no row and returns the first
  id; after one undo of it, the item is not in `resolved`. Undo of a live event writes one
  `undo` row. Undo of an unknown id, of an undo,
  and of an undone event each raise ValueError and write nothing. Undo of another
  reader's event, of another day's event and of another line's event each raise
  `UndoMismatch` and write nothing.
- A5 (I6, active). An owner's done is in `resolved` for the owner and for another reader.
  A non-owner's done is not in `resolved`. A snooze is in `snoozed` for its reader only,
  with its `until`; on the `until` day and later it is not. An event of a later day than
  `day` does not count. An undo of an owner's done removes it from `resolved`; an undo of
  a snooze removes it from `snoozed`; each undo leaves the other events in effect.
  `suppressed` is empty. The result is the same with the `items` table empty or full.
  A store built without `feedback_events` and opened with `Store.open_readonly` gives the
  empty state and an empty `shown_marks`, and the file does not change.
- A6 (I6). The same idempotency key twice: one row, the second call returns None, also for
  undo and also when the second call would raise. Two clicks with the same action ts and
  block id on two messages are two keys. `shown_marks` gives the newest live event per
  item of that reader and day.
- A7 (I8). On the DM path, each `chat.postMessage` (top and reply) carries the metadata
  with one run id for the whole run, with and without `--interactive`; the webhook path
  sends none.
- A8 (I9). With `interactive=True`: the block ids of the I9 form, with revision 0, the
  token plus `t` or `r`, and `<n>` from 0 in block order; the options and their order for
  an item, an ask absence in needs_you, an ask absence in not_happened and a checklist
  absence (no `Mark done` and no `It happened` on an absence); every value is
  `<action>:#<n>`; the `items` key lists the records in `<n>` order. With
  `interactive=False`, the payload equals the layout unit's output and has no `items` key.
  Every message passes the layout A17 helper with the `interactive` flag; the helper fails on an
  overflow with a `url`, on an `actions` block, and on a value of another form.
- A9 (I10, Commands). `--interactive` without `--dm` exits 1 before any build; a refused
  `--db` exits 1 with no DM opened and nothing sent; with `--interactive`, one
  `posted_messages` row per accepted message, with the blocks and metadata as sent and
  revision 0; a response with no `ts` gives the refusal of I10; the CLI sends no `items`
  key to Slack.
- A10 (I11). Each ignore reason in the table gives `bot_action_ignored` with that reason,
  no row and no Slack call. A click with no `message.metadata` and a posted row is
  accepted. Metadata with another reader, day, run id, world or `event_type` than the row
  gives `metadata_mismatch`.
- A11 (I12). `Why this item?` calls `open_view` with the click's `trigger_id` and the I12
  modal; mapped and unmapped sources; escaped text; no row is written; nothing is put on
  the queue. While the worker is blocked in a fake `update` (it waits on a
  `threading.Event`), a Why click through the listener calls `open_view` and returns
  before the event is set.
- A12 (I13, I14). Each action updates the message once with the suffix and an `Undo`
  button holding the event id; the update keeps `text` and metadata; every item block id
  in the update carries the next revision, and the row's `revision` grows by 1; an undo
  click restores the posted blocks of that item (with the new revision); marks on two
  items of one message both show; a repeated delivery makes no update. A done whose
  update raises `BotCallFailed`, then a second done click on the same line: one event
  row, and the second job updates the line with the mark.
- A20 (I13, order). With a fake `update` that reads the store when it is called, the
  event row is committed before each `update` call, for done, snooze and undo.
- A13 (I15). Two jobs on one message, through `run_worker`: both rows are written, and
  the last update shows both marks. A store write that raises `sqlite3.OperationalError`
  logs `bot_store_failed`, makes no update, and the next job is still applied. A job
  whose posted row cannot be read (a patched read that raises `KeyError`) logs
  `bot_job_failed`, and the next job is still applied.
- A14 (I16, I17). `_BoltBotClient` over a fake raw `WebClient` whose `chat_update` and
  `views_open` raise `SlackApiError` with a token in its response: it raises
  `BotCallFailed` with the error code only, `from None`. A `BotCallFailed` from `update` or
  `open_view` logs `bot_slack_failed` and keeps the row; with a token in the fake error, no captured log record and no output
  holds it; the `bot` command refuses a missing or malformed `SLACK_APP_TOKEN` or
  `SLACK_BOT_TOKEN` without printing the value.
- A15 (I11, listeners). The listener functions of `listeners`, called with a fake `ack`
  and a payload: `ack` is called first, once; a done, snooze or undo payload is put on the
  queue unchanged and nothing else is called; a Why payload calls `open_view` and puts
  nothing on the queue. While the worker is blocked in a fake `update`, a done click
  through the listener calls `ack` and returns. No bolt `App` is constructed.
- A19 (feedback.episode). `episode("checklist", "TST-1", "C0X", "1.2")` equals
  `Episode("checklist", "TST-1", "C0X:1.2")`; another `message_ts` gives another episode.
- A16 (I14, undo binding). An `item_undo` click whose value names an event of another line
  of the same message gives `undo_mismatch`; one whose value names another reader's event
  gives `undo_mismatch`; neither writes a row or updates the message.
- A17 (live, manual, recorded in the PR). In Scratch: run `digest bot --demo-actor`; in a second
  terminal run `digest post --dm <member> --interactive --seed-map <map>` for day 6. On
  the three digests, click `Why this item?`, `Mark done` and `Snooze until tomorrow`, each
  on a line where it shows, and `Undo` after each of the last two. Record one sanitized
  overflow payload and one sanitized button payload (Before tests 1). Read the rows back
  with `uv run python -m sqlite3 .digest/feedback.sqlite "SELECT id, event, item_id,
  reader, day, payload FROM feedback_events"` and record them. Restart the bot and click
  once more: the update still works (the state is in the store).
- A18 (Commands, shutdown). `digest bot` with `_socket_handler` patched to return a fake
  handler and `time.sleep` patched to raise `KeyboardInterrupt` on its first call: the
  fake records `connect` then `close`, and no `start`; the output holds the listening
  line and then `stopped`; the exit code is 0. With a `connect` that raises an error
  whose text holds a token, `close` is still called, the output is `digest bot could not
  start: <exception type name>` with no token, and the exit code is 1. The same holds
  for a `_socket_handler` that raises an error whose text holds a token; a `close` that
  raises one logs `bot_close_failed` with no token in any log record or output. A job in
  the queue at Ctrl-C is applied before `stopped` is printed. A store of another schema
  version exits 1 before `_socket_handler` is called. With `--demo-actor`, the worker
  gets `actor_rule="demo_reader"`; without it, `"clicker"`.

## Test seams

- `feedback.record`, `undo`, `active` and `shown_marks` over `Store.open(tmp_path /
  "f.sqlite")`.
- `slack_digest(..., interactive=True, token=...)` with hand-made selections, as the
  layout tests build them, and the layout A17 helper with its new flag.
- `handle_action` with a fake `BotSlackClient`, a fixed clock, and payload dicts in the
  shape of Slack's `block_actions`. No bolt `App` in a test: constructing one calls
  `auth.test`. The adapter's listener functions take `ack` and `body` and are tested
  alone (A15).
- The CLI with `CliRunner` and the Slack clients patched, as `tests/test_slack_post.py`
  does. `digest bot` with `_socket_handler` and `time.sleep` patched (A18). `run_worker`
  with a `Queue` filled by the test and a `None` job at the end. No test touches the
  network.

## Next units

1. **Selection applies feedback**, built with the session that owns `daily/select.py`.
   - `select()` takes the `FeedbackState` of `active(store, reader, day)` and filters
     before the section caps: it leaves out every item in `resolved` (an owner's done
     resolves the item for every reader), and each item in `snoozed` until its date.
   - The closing line of SPEC step 7 (an item the reader was shown while open and that
     has since resolved appears once more with its closing evidence) reads the same
     value.
   - `It happened` comes back with its suppression. Design kept from v0.1: the option
     replaces `Mark done` on an ask absence in needs_you and sits on every absence line.
     The event `it_happened` is keyed by the absence's own key and the day shown, and its
     payload holds the `Episode` from `feedback.episode()`, the one constructor that
     posting and selection both call: `source` (`absence.source`); `subject`, the task id
     (`absence.task`) for a checklist absence or the ask item id (the absence's `item_id`
     without `ASK_ABSENCE_PREFIX`) for an ask absence; and `last_evidence`,
     `<channel id>:<message ts>` of the absence's one evidence entry. Source h is stretch;
     its subject is `<part>@<rev>`. `daily/slack.py` builds the episode at post time and
     keeps it on the `PostedItem`, so `feedback.py` imports nothing from `daily`. `FeedbackState.suppressed` then holds the live episodes of the
     reader; selection leaves out an absence whose episode is in it, and new evidence
     followed by a new silence is a new episode, so it shows again. The click changes no
     task state, no ledger and no other reader's digest (SPEC step 8). The menu value
     stays `it_happened:#<n>`; the optional permalink of SPEC step 8 needs a modal input.
2. **Exposure records** (SPEC M5 core, D26): the `deliveries` table of
   `docs/plans/m5-exposure.md`, written on Slack's acceptance of each part, for
   interactive and plain posts. It joins `posted_messages` (mutable revision state) on
   `(channel, ts)`, and keys an absence by its `Episode`. It also reads the `done` events
   with `resolution: exposure` of this slice.
3. **Not mine, confirm, thumbs and "more like this".** Confirm waits for contradiction
   lines.

## Out of scope

- Any write to the `items` table, and any change to selection, ranking or the next
  digest (Next units 1).
- `It happened` and its episode suppression (Next units 1).
- `not mine`, `confirm`, thumbs, "more like this", a free-text reply.
- Profile weights recomputed from the log, the profile version, real-time alerts.
- A seen record, App Home and `/digest` commands.
- Interactive webhook messages: the bot cannot update them.
- A stricter actor rule than I7 (the clicker must be the reader's own Slack account).

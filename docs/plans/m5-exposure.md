# Plan: M5 core, the delivery record (exposure on Slack's acceptance)

Status: v0.3, 2026-10-13. Process stage 1 (`docs/process.md`). Source: SPEC section 5
step 9 (Delivery), section 9 ("M5. Core: exposure recorded on Slack's acceptance"), D7
(amended 2026-10-11) and D26 ("Ledger and exposure"). This is "Delivery records"
(`docs/later.md`, Next steps 5). Size: one night of work. v0.2 fixed the stack order,
keyed absences by the bot unit's `Episode`, and decided when recording is on. v0.3
applies the Codex plan review: claims only for displayed text (I4, I5), the two record
writes attempted one by one (I7), a Slack rejection apart from a missing `ts` (I7), the
opt-in wording (Goal), and Q1 before the tests (Before tests). It keeps the
`_open_store` refusal (Commands).

Stack: layout, then bot, then this unit. This unit builds on the layout unit
(`docs/plans/m5-layout.md` v0.4, `slack_digest`) and the bot unit
(`docs/plans/m5-bot.md` v0.5, b077e8b: `--db`, `--interactive`, the I8 run id and
metadata, `posted_messages`, `digest/feedback.py` with `Episode` and `episode()`). The bot
unit's v0.5 changes the listener only (ack first, one serial worker), not the posting
path or `posted_messages`. This unit is rebased onto `main` after both merge.

## Open questions

The SPEC does not settle these. Each has a proposed answer. The plan uses the proposed
answer until the user decides.

Q1. The absence split. `docs/later.md` (Structure 2) puts the absence split "before any
exposure record exists". Proposed: this unit does not wait for the split. The record keys
an absence by its `Episode` (bot plan Interface): `source`, `subject` and
`last_evidence`, built only with `feedback.episode()`. The day stays in its own column.
The record never keys an absence by an item id. The split must keep the episode subject
stable: the subject of a checklist absence is its task id, and the subject of an ask
absence is the ask item id (today the absence's `item_id` without
`ASK_ABSENCE_PREFIX`). An explicit ask id from the split must map to that ask item id.
The user confirms this reading of the trigger, or the split becomes a prerequisite and
this unit waits. The user settles Q1 before the tests start (Before tests). This unit
does not include the split.

Q2. A partial post. A digest has one or two parts: the top message and, when the thread
holds items, the thread reply. When Slack accepts the top message and refuses the reply,
does the digest count as "the reader's last delivered digest" for the window? Proposed:
no. The window reads only a run in which Slack accepted every part of that reader's
digest (I9). The record keeps the accepted parts either way, so the exposure of the
lines in those parts is not lost (I3).

Q3. The webhook. An incoming webhook returns status 200 and the body `ok`, with no `ts`
and no channel. Is that "Slack's acceptance"? Proposed: yes. The webhook path records rows
with no channel and no `ts` (I6), and those rows can end the window.

Q4. The demo recipient. `digest post --dm USER_ID` sends the digests of three personas to
one Slack user. Whose exposure is it? Proposed: the persona's. The bot plan's I7 gives
the same answer for feedback events: in both actor modes the event's `reader` is the
posted row's reader. The row also keeps the Slack user id (`recipient`).

Q5. Closed in v0.2. Commands decides when recording is on.

Q6. A second post for the same reader and day. D7 says delivery is idempotent on (user,
digest_date). D26 says exposure is append-only. Proposed: this unit appends. A rerun
mints a new run id and records new rows (I8). Refusing a second send is send
idempotency, an M5 stretch item, and stays out of this unit.

Q7. Claim ids. D26 says the record holds "the claim ids shown". Claims have no ids today:
they are JSON inside the `items` row, and the ledger is not built. Items are overwritten
in place, so D26 asks for "a kept item revision or a delivery snapshot". Proposed: the
record holds a snapshot of each shown claim's fields (I4). The ledger unit can map the
snapshot to claim ids later.

Q8. Folded members. The layout unit folds a related item into a kept item as a member
(layout plan 4.3). Is a member "shown"? Proposed, in two parts:

- The member's item id: yes. When the kept item's line is in the part, each member's
  item id goes in `items`, because the member leaves its own section and the kept line
  stands for it.
- The member's claims: only when the message displays the member's title or body. Layout
  plan 4.3 decides this. A kept ask item shows the title of its first member. A kept
  checklist absence shows the body of its best member. A kept ask absence shows no member
  text. Any other member's text is not displayed, so its claims are not recorded (D26:
  "the claims shown").

## Goal

When Slack accepts a digest part, the store records what the reader was shown: the
reader, the digest day, the run id, the world, the Slack channel and `ts`, the item ids,
the absence episodes and the claims of the lines in that part. One read function gives a
reader's last delivered digest day. With an explicit `--db`, `digest post` takes the
window of each digest from that read, so the window starts where the reader's last
delivered digest ended (SPEC section 5 step 9).

This unit is an opt-in prerequisite for M5 core. It records only when the operator asks
for it (Commands). M5 core ("exposure recorded on Slack's acceptance") stays incomplete
until recording is on by default for every delivery path (Next units 4).

This unit records. It does not change how selection uses exposure: novelty, stale
beliefs and the seen record read the record in later units (Next units).

## Rule (from SPEC section 5 step 9 and D26)

- Exposure is recorded when the digest is delivered. For Slack, that is Slack's
  acceptance of the message. (Exposure on render for the terminal, markdown, the web
  preview and replay is a next unit.)
- Each sent part carries the run id in message metadata, so a retry posts only the
  missing parts. The bot unit's I8 already puts the run id in the metadata of every DM
  part (top message and thread reply), with or without `--interactive`. The retry that
  reads it is send idempotency, which SPEC section 9 lists as v0.4 stretch. This unit does
  not build the retry (Out of scope).
- A digest's window starts where the reader's last delivered digest ended, at most
  `window.max_working_days` back. A first digest goes `window.first_working_days` back.
  `select()` already does this from `previous_digest`.
- Exposure is append-only. Digest code writes it at delivery: reader, run, delivered
  part, time, and the items and claims shown. An absence is exposed by its own key, never
  by an item id.

## Commands

```
digest post [--dm USER_ID] [--interactive] [--db PATH] [--seed-map PATH] [--day N]
```

No new option. This unit changes when `digest post` opens the store of the bot unit's
`--db` option and what it writes there.

- **Recording is on when `--db` is given explicitly, or when `--interactive` is on.**
  Without either, the command opens no store and records nothing. Reason: the bot unit
  gives `--db` the default `.digest/feedback.sqlite`. If recording followed the default,
  the plain demo command `digest post` would start to write a file in the working
  directory. With `--interactive`, the bot unit already opens that store, so the record
  adds rows to a file the command writes anyway.
- To tell an explicit `--db` from the default, the option's Typer default is None. The
  command uses `.digest/feedback.sqlite` when `--db` is None and `--interactive` is on (the
  bot unit's default, unchanged). `--help` still names the default.
- **The window comes from the record only when `--db` is given explicitly** (I10). With
  `--interactive` alone, the window keeps today's rule. Reason: `digest post --dm
  --interactive` is the bot unit's demo step (bot plan A17), and its digests must not
  change. An explicit `--db` is the operator's choice of real delivery semantics.
- With an explicit `--db` and no `--dm`, the webhook path records too (I6). `--interactive`
  still needs `--dm` (bot unit).
- The command opens the store as the bot unit does: with `_open_store`, after the
  destination checks and before it builds a digest. A refused store exits 1 with today's
  `_open_store` text, and nothing is sent. The text does not change. It prints the
  operator's own `--db` argument and the SQLite error text or the schema version check.
  Neither names content of the file, and the schema version is not secret. Every command
  that opens a writable store (`run`, `absences`, `replay`, `say`, and the bot unit's
  `post --interactive` and `bot`) uses this refusal.
- No other command changes. `digest demo`, `digest run`, `digest serve` and `digest bot`
  do not record.

## Behaviours

### Acceptance and the record

I1. **Run id.** Every row of a run holds the run id that the bot unit's I8 mints once per
run (`_new_run_id`, `uuid.uuid4().hex`) and puts in the metadata of every DM part. The
webhook path sends no metadata, but its rows hold the same run id.

I2. **A part.** A part is one Slack message of one reader's digest, in the order the
command sends them. Part 0 is the top message. Part 1 is the thread reply on the DM path,
or the second plain message on the webhook path (layout plan 11.2). `parts` is the number
of messages that `slack_digest` built for that reader's digest: 1 or 2.

I3. **Acceptance, DM path.** Three outcomes of one `chat.postMessage`:

- **Rejected:** the call raises, or it returns and `response.get("ok")` is not `True`.
  Slack did not accept the part, also when the response holds a `ts`. Nothing is recorded
  for the part (I7).
- **Accepted without a usable `ts`:** `ok` is `True` and `response.get("ts")` is not a
  non-empty string. In recording mode this is a failure, also for a top message of a
  digest with no reply, as the bot unit's I10 says for `--interactive` (layout plan 11.1
  accepts it outside recording mode). Nothing is recorded for the part (I7).
- **Accepted:** `ok` is `True` and `ts` is a non-empty string.

After each accepted part, and before the next send, the command attempts the writes of
I7a. It does not wait for the end of the run, so a later failure keeps the
earlier rows.

I4. **What a row holds.** `run_id`; `reader` (the persona `user_id` of the digest, never
the recipient); `part`; `parts`; `day` (the digest day, ISO date); `world` (`kestrel` for
`digest post`); `path` (`dm` or `webhook`); `recipient` (the `--dm` user id, else null);
`channel` (the DM channel id from `conversations.open`, else null); `ts` (the response
`ts`, else null); `accepted_at` (UTC, ISO 8601 with offset, from `_now()`); and `shown`,
from `part_shown(lines)` (I5) over the shown lines of that part:

- `items`: the `item_id` of each kept line that is not an absence, then the `item_id` of
  each of its members (Q8), in line order, each once. A member's id goes in `items` also
  when the message displays none of its text, and also when its kept line is an absence.
- `absences`: the `Episode` of each absence line, from `absence_episode(item)`, which
  calls `feedback.episode(source, subject, channel, message_ts)` and nothing else builds
  an episode:
  - `source` is `AbsenceDetail.source`: `checklist` or `ask`.
  - `subject` is `AbsenceDetail.task` for a checklist absence, and for an ask absence the
    absence's `item_id` without `ASK_ABSENCE_PREFIX` (the ask item id).
  - `channel` and `message_ts` are those of the absence's one evidence entry.
  An absence never enters `items`.
- `claims`: the claims of displayed content only. A claim is recorded for:
  - each kept line that is not an absence: the claims of its own item;
  - each member in the shown line's `displayed` set (I5): the claims of the member's
    item. Layout plan 4.3 gives the set: the first member of a kept ask item (its title
    is shown), the best member of a kept checklist absence (its body is shown), and no
    member of a kept ask absence. Every other member adds no claim.

  Of those items, a claim is recorded only when its `channel_id` is the channel of one of
  that item's evidence entries. Each item here is the copy that the visibility filter
  trimmed, so a claim from a channel the reader cannot see is not recorded. Each entry
  holds `item_id`, `entity`, `attribute`, `value`, `channel_id`, `message_ts` and
  `authority`, as stored at post time (Q7).

The bot unit's Next units 1 builds an episode at post time for `It happened`. It calls
the same `absence_episode`, so the two units cannot build different keys for one line.

I5. **The lines in a part.** `slack_digest(..., report=True)` adds one more key, `lines`,
to each message it returns: one `ShownLine` (Interface) for each kept item whose blocks
are in that message, in block order. A `ShownLine` holds the kept line, its members in
selection order (layout plan 4.3), and `displayed`: the item ids of the members whose
title or body the message shows. The renderer fills `displayed` from the same choice
that builds the text (the first member of an ask item, the best member of a checklist
absence, none for an ask absence), so the record and the text cannot differ. A line past
a cap is not in `lines`: a must-see line past the five top items is in the reply's
`lines`, and an item that the reply cap leaves out ("<n> more items not shown") is in no
`lines`. A top message with no item gives an empty `lines`. With `interactive=True` the
message also holds the bot unit's `items` key. The CLI removes `lines` (and `items`)
before the Slack call. With `report=False` (the default) each message is byte for byte
what the layout and bot units build.

I6. **Acceptance, webhook path.** Slack accepts a part when `send` returns a response
with `status_code` 200, the success test of today. The row has `path` `webhook` and a
null `recipient`, `channel` and `ts`. A refusal (another status) or a send exception
writes no row for that part.

I7. **Failures in recording mode.** Each refusal exits 1 and sends nothing more. The
rows already written stay.

| Case | Refusal text |
|---|---|
| A send exception, or a webhook status other than 200 | the layout unit's text (11.1, 11.2), unchanged; no row for that part |
| DM response with `ok` not `True` (a Slack rejection, also with a `ts`) | the layout unit's text for a failed send of that part (11.1), with `<failure>` the response's `error` value by the `_slack_failure` rule (a Slack error code, else `SlackApiError`); no row for that part |
| DM response with `ok` `True` and no string `ts` | `cannot record the digest for <reader>: the message has no ts; its message was sent<note>`; no row for that part |
| A write of I7a raises `sqlite3.Error` | `cannot record the digest for <reader>: <exception type name>; its message was sent<note>` |
| `record_delivery` returns False (I8) | `cannot record the digest for <reader>: a row with this key exists; its message was sent<note>` |

`<note>` is as in layout plan 11.1. The "has no ts" row is the bot unit's I10 text, so
the two units share one refusal. The exception text is not printed: a path or a value
from the store can be in it.

I7a. **Two writes, each on its own.** After an accepted DM part, the command attempts
two writes, in this order, each in its own `try` statement:

1. the `deliveries` row (`record_delivery`);
2. with `--interactive`, the bot unit's `posted_messages` row (`save_posted`, bot I10).

A failure of the first write does not stop the second. Each write that succeeds stays.
So an exposure failure never leaves a posted menu without its `posted_messages` row, and
a click on that message still works (bot I11). When either write failed, the command
refuses after both attempts (I7), with the reason of the first failed write. On the
webhook path, and on the DM path without `--interactive`, only the first write runs.

I8. **Idempotency.** The key of a row is `(run_id, reader, part)`. A DM row is also
unique on `(channel, ts)`. The insert is `INSERT ... ON CONFLICT DO NOTHING`, and
`record_delivery` returns False when a row with either key exists. The CLI writes each
key once, so False there means the message was sent and no new row records it; the
command refuses (I7). A rerun of the command mints a new run id, so it records new rows
(Q6).

### The read

I9. **Last delivered day.** `last_delivered(store, world=, reader=, before=)` returns the
greatest `day` that is earlier than `before`, for which one run holds a row for every
part from 0 to `parts - 1` for that world and reader. A run with a missing part does not
count (Q2). A day at or after `before` does not count, so a second post of the same day
does not shorten its own window. No such run gives None.

I10. **The window in `digest post`.** With an explicit `--db`, the command gives each
reader's digest `previous_digest = last_delivered(store, world="kestrel",
reader=<user_id>, before=<digest day>)`. None makes a first digest
(`window.first_working_days`). Otherwise (no `--db`, or `--interactive` with the default
store) the command keeps today's rule: the previous world day, and a first digest on
world day 1. So a first post with an explicit `--db` into an empty store is a first
digest for each reader. Its window line says "First digest", and it can show more lines
than the same post without `--db`.

I11. **Where the code goes.**

- `daily/exposure.py` (new, Digest context per D26): the models, `absence_episode`,
  `part_shown`, `record_delivery` and `last_delivered`. It imports `episode` and
  `Episode` from `digest.feedback`, which sits below `digest.daily` in the layers
  contract. It imports no Slack package and nothing from `cli.py`.
- `store.py`: the SQL only. Insert one delivery row, and read the rows of one world and
  reader.
- `daily/slack.py`: the `report` keyword of `slack_digest` (I5).
- `cli.py`: the explicit-`--db` rule, `_now` (unless the bot unit already has one), the
  record write after each accepted part, and one additive keyword on the private helper
  `_demo_selections` (Interface). The keyword's default keeps today's behaviour for
  `demo` and `serve`. No existing command or option changes.
- `daily/select.py` does not change.

### Unchanged behaviour

I12. Without an explicit `--db` and without `--interactive`, no store is opened or
written, and every Slack call and output line is what the bot unit leaves. With
`--interactive` and no `--db`, the Slack calls and output are also unchanged; the only
difference is the `deliveries` rows in the default store.

## Data

One table joins `schema.sql`. It uses `CREATE TABLE IF NOT EXISTS`, so `Store.open` adds
it to an existing store. `SCHEMA_VERSION` stays `"2"`: a bump makes `_check_version`
refuse every existing store. The bot unit uses the same rule.

```sql
CREATE TABLE IF NOT EXISTS deliveries (
  run_id      TEXT NOT NULL,     -- the bot unit's I8 run id
  reader      TEXT NOT NULL,     -- persona user_id of the digest
  part        INTEGER NOT NULL,  -- 0 the top message, 1 the reply or second message
  parts       INTEGER NOT NULL,  -- messages in the digest: 1 or 2
  day         TEXT NOT NULL,     -- ISO date of the digest
  world       TEXT NOT NULL,
  path        TEXT NOT NULL,     -- dm | webhook
  recipient   TEXT,              -- Slack user id of the DM; null for webhook
  channel     TEXT,              -- Slack channel id; null for webhook
  ts          TEXT,              -- Slack ts of the message; null for webhook
  accepted_at TEXT NOT NULL,     -- UTC ISO 8601 time of the write
  item_ids    TEXT NOT NULL,     -- JSON array of item ids (PartShown.items)
  absences    TEXT NOT NULL,     -- JSON array of Episode {source, subject, last_evidence}; source is checklist | ask
  claims      TEXT NOT NULL,     -- JSON array of {item_id, entity, attribute, value, channel_id, message_ts, authority}
  PRIMARY KEY (run_id, reader, part),
  UNIQUE (channel, ts)           -- SQLite treats nulls as distinct, so webhook rows pass
);
CREATE INDEX IF NOT EXISTS deliveries_reader ON deliveries (world, reader, day);
```

No statement updates or deletes a `deliveries` row. No statement writes the `items`
table.

### Relation to `posted_messages` (bot unit)

`deliveries` is a separate table from the bot unit's `posted_messages`, for four
reasons:

- The primary key of `posted_messages` is `(channel, ts)`. A webhook part has neither,
  but it is a delivery (Q3).
- The bot unit writes `posted_messages` only with `--interactive`. Exposure is recorded
  on every accepted part in recording mode.
- A `posted_messages` row changes: `revision` grows on each update, and a post with the
  same `(channel, ts)` replaces the row. D26 asks for an append-only exposure record.
- `posted_messages` holds what the bot needs to update a message (text, blocks,
  metadata, menu items). `deliveries` holds what the reader was told. Neither table
  copies the other's columns.

A DM part with both rows joins on `(channel, ts)`. The bot plan's Next units 2 already
names this table (06c2651).

## Interface (stubs land before tests)

```python
# digest/daily/exposure.py
from digest.feedback import Episode, episode

class ClaimShown(Frozen):
    item_id: str
    entity: str
    attribute: Attribute
    value: str
    channel_id: str
    message_ts: str
    authority: Authority

class ShownLine(Frozen):            # one kept item whose blocks are in a message (I5)
    line: Line                      # the kept line (the absorber, or a line with no members)
    members: tuple[Line, ...]       # folded members, in selection order (layout plan 4.3)
    displayed: frozenset[str]       # item ids of the members whose title or body is shown

class PartShown(Frozen):
    items: tuple[str, ...]
    absences: tuple[Episode, ...]
    claims: tuple[ClaimShown, ...]

class Delivery(Frozen):
    run_id: str
    reader: str
    part: int                       # at least 0, less than parts
    parts: int                      # 1 or 2
    day: date
    world: str
    path: Literal["dm", "webhook"]
    recipient: str | None
    channel: str | None             # None exactly when path is webhook
    ts: str | None                  # None exactly when path is webhook
    accepted_at: datetime           # aware
    shown: PartShown

def absence_episode(item: Item) -> Episode: ...                                # I4
    # ValueError for an item with no absence; calls feedback.episode only
def part_shown(lines: Sequence[ShownLine]) -> PartShown: ...                   # I4
def record_delivery(store: Store, delivery: Delivery) -> bool: ...             # I8
def last_delivered(store: Store, *, world: str, reader: str,
                   before: date) -> date | None: ...                           # I9

# digest/store.py
def insert_delivery(self, row: Mapping[str, Any]) -> bool: ...   # ON CONFLICT DO NOTHING
def delivery_rows(self, world: str, reader: str) -> list[dict[str, Any]]: ...

# digest/daily/slack.py
def slack_digest(world: World, selection: Selection, *, links: SeedMap | None,
                 authors: Mapping[str, str], threaded: bool = True,
                 interactive: bool = False, token: str = "",
                 report: bool = False) -> list[dict[str, Any]]: ...           # I5

# digest/cli.py
# post: --db keeps its bot-unit meaning; its Typer default becomes None (Commands).
# Reused from the bot unit: _new_run_id (I8), the I8 metadata, _open_store.
def _now() -> datetime: ...                                       # datetime.now(UTC)
def _demo_selections(day: int, *,
                     previous_of: Callable[[str, date], date | None] | None = None
                     ) -> tuple[World, list[Selection], dict[str, str]]: ...
    # the layout unit's 3-tuple; previous_of(user_id, digest_day) gives previous_digest,
    # and None keeps today's rule
```

- `digest.daily.exposure` sits in `digest.daily`, which already imports `digest.store`
  and, after the bot unit, may import `digest.feedback`. The import-linter contract needs
  no change. The implementer checks this with `uv run lint-imports`.
- `Delivery` validates the null rule of `channel` and `ts`, and `0 <= part < parts`.
- `ShownLine` validates that `displayed` holds only ids of its `members`. `daily/slack.py`
  imports `ShownLine` from `daily/exposure.py`; `exposure.py` imports nothing from
  `slack.py`.
- An `Episode` is a `NamedTuple`, so the JSON form is written field by field as an object
  with `source`, `subject` and `last_evidence`, and read back with `Episode(**row)`.

## Demo output

The unit does not change demo output. `digest demo` and `digest serve` do not change.
`digest post` without `--db` and without `--interactive` opens no store and sends the
same payloads (I12). `digest post --dm --interactive`, the bot unit's demo step, sends the
same payloads and prints the same lines; it only adds `deliveries` rows to the store it
already writes. Only an explicit `--db`, which no demo step uses, takes the window from
the record. Its first post into an empty store is a first digest (I10).

## Before tests

One decision, no spike. The user settles Q1 (the absence split trigger) before the test
author starts. If the user puts the split first, this unit waits for it; the split is
not part of this unit.

No spike is needed. The DM path already reads the `chat.postMessage` response through
`.get` in `_slack_failure`, and the layout plan (11.1) and bot plan (I10) already rely on
the response `ts`. The webhook success test (status 200) is today's code.
`feedback.episode` is a stub of the bot unit with its own tests.

## Acceptance

Each row is tested offline with `CliRunner`, a temporary store (`tmp_path`), and the
fakes of `tests/test_slack_post.py` as the bot unit leaves them (`FakeSlackWebClient`,
`FakeWebhookClient`, `FakeResponse`), unless it says otherwise. When the DM fake does not
return a response, the test author gives it one: `{"ok": True, "ts": <a ts per post>}` by
default, with a way to return a response without `ts`. Tests name the fakes by class,
not by line number. No test writes `.digest/feedback.sqlite` in the repo: a test of the
default store patches the default path to `tmp_path`.

- A1 (I4, `part_shown`, `absence_episode`). Hand-made lines: items in line order, each
  once; an absence line goes to `absences` and not to `items`; a checklist absence gives
  `episode("checklist", <task id>, <channel>, <ts>)` and an ask absence gives
  `episode("ask", <ask item id>, <channel>, <ts>)`, compared with values that the test
  builds with `feedback.episode`; `absence_episode` on an item with no absence raises
  ValueError; a claim whose `channel_id` is not a channel of its item's evidence is left
  out; an empty sequence gives three empty tuples.
- A1b (I4, Q8, members). Hand-made `ShownLine` values, each member with its own claim:
  - a kept ask item with three members and `displayed` = the first member: `items` holds
    the ask id and all three member ids; `claims` holds the ask's claims and the first
    member's claims only;
  - a kept checklist absence with two members and `displayed` = the second (the best):
    `items` holds both member ids; `absences` holds the absence's episode; `claims`
    holds the second member's claims only;
  - a kept ask absence with two members and an empty `displayed`: `items` holds both
    member ids; `claims` is empty.
  `ShownLine` refuses a `displayed` id that is not one of its members.
- A2 (I5, renderer). `slack_digest(..., report=True)`: each message holds `lines` in
  block order; a must-see line past the five top items is in the reply's `lines`; with
  more items than the reply cap, an item left out is in no `lines`; a kept item's members
  are in its `ShownLine`; `displayed` is the first member of a kept ask item, the best
  member of a kept checklist absence (must-see first, then the higher score), and empty
  for a kept ask absence, and in each case the displayed member's title or body is in the
  message text while no other member's title or body is; each message without `lines`
  equals the `report=False` message; with `interactive=True` both `lines` and `items`
  are present.
- A3 (I8, store). `record_delivery` returns True and writes one row; the same `(run_id,
  reader, part)` again returns False and writes nothing; another run with the same DM
  `(channel, ts)` returns False; two webhook rows with null `channel` and `ts` in two runs
  both write. `Delivery` refuses a webhook row with a `ts`, a DM row without one, and
  `part >= parts`. An `Episode` written and read back is equal to the one written.
- A4 (I9, read). A complete run for day D gives D for `before` after D; a run with part 1
  of 2 missing does not count; a run for day `before` or later does not count; another
  world or another reader does not count; two complete days give the later one; no rows
  give None; a complete run whose rows hold empty `item_ids`, `absences` and `claims`
  counts.
- A5 (I3, I4, I10, DM). The store at an explicit `--db` holds a complete day-4 delivery
  for each of the three readers. `post --dm U012ABC --db <tmp> --day 5` with
  `_new_run_id` and `_now` patched: one new row per accepted part, in send order (top,
  then reply), with the persona `reader`, `part` and `parts`, the day, the run id of the
  I8 metadata, `kestrel`, `dm`, the recipient, the DM channel, the fake's `ts`, the
  patched time, and the `shown` of the lines of that part. Both commands pass the same
  `previous_digest` (day 4), so the Slack calls and the output equal those of the same
  command without `--db`.
- A6 (I3, I7, partial). The reply `chat.postMessage` of the first reader raises: the top
  row of that reader stays (part 0 of 2), no row for the others, exit 1, the layout
  unit's refusal text, and `last_delivered` for that reader does not give that day.
- A7 (I3, I7, Slack rejection). The fake returns `{"ok": False, "error":
  "channel_not_found", "ts": "1700000000.000100"}` for the second reader's top message:
  the layout unit's send-failure text with `channel_not_found`, no "its message was sent"
  in the output, no row for that part, the first reader's rows stay, and nothing more is
  sent.
- A7b (I3, I7, no ts). The fake returns `{"ok": True}` for a top message of a digest with
  no reply: the "has no ts" refusal with its note, no row for that part, earlier rows
  stay, and nothing more is sent.
- A8 (I7, I7a, write failures). Each case runs with `--interactive` and checks the rows
  of the failing part, the refusal and that nothing more is sent:
  - `record_delivery` raises `sqlite3.OperationalError` whose text holds a marker: the
    `posted_messages` row of that part is written, no `deliveries` row; the refusal
    names the type and says the message was sent; the output does not hold the marker.
  - `save_posted` raises `sqlite3.OperationalError`: the `deliveries` row of that part
    is written, no `posted_messages` row; the same refusal shape.
  - `record_delivery` returns False: the `posted_messages` row is written; the "a row
    with this key exists" refusal.
  Without `--interactive`, a raising `record_delivery` gives the same refusal, and
  `save_posted` is not called.
- A9 (I6, webhook). `post --db <tmp>` on the webhook path: one row per accepted message
  with `path` `webhook` and null `recipient`, `channel` and `ts`; a two-part digest gives
  parts 0 and 1. A `FakeResponse(500, ...)` for the second reader's top message: only the
  first reader's rows, the layout unit's refusal text.
- A10 (Commands). An explicit `--db` pointing at a text file, and at a store of another
  schema version: exit 1 with the `_open_store` text, and no Slack call (no
  `conversations_open`, no `send`).
- A11 (Commands, I10, I12, the recording rule). With the default path patched to
  `tmp_path`: `post --dm ... --interactive` with no `--db` writes `deliveries` rows to the
  default store, and each reader gets the previous world day as `previous_digest`;
  `post --dm ...` with neither option creates no store file and writes no row. With an
  explicit `--db` whose store holds a complete day-4 delivery for the ME reader only,
  `post --dm ... --db <tmp> --day 6` with `select_digest` wrapped gives the ME reader the
  day-4 date and the other two readers None.
- A12 (Data). `Store.open` on a version-2 store file without `deliveries` adds the table,
  keeps the rows of the other tables, and `schema_version` stays `"2"`.
- A13 (live, manual, recorded in the PR). In Scratch: `digest post --dm <member> --db
  <tmp>/d.sqlite --seed-map <map> --day 6`, then read the rows back with `uv run python -m
  sqlite3 <tmp>/d.sqlite "SELECT run_id, reader, part, parts, day, path, channel, ts FROM
  deliveries"`. Each `ts` is the `ts` of a message in the DM, and each `run_id` is the
  run id in that message's metadata. Then post day 7 with the same store: each window
  line says "Since your digest of" day 6.

Each new or changed test is RED-GREEN proven with `prove-test`.

## Test seams

- `absence_episode`, `part_shown`, `record_delivery` and `last_delivered` over
  `Store.open(tmp_path / "d.sqlite")`, and hand-made `Line` and `ShownLine` objects, as
  `tests/test_daily_render.py` builds lines.
- `save_posted` and `record_delivery` patched one at a time to fail (A8).
- `slack_digest(..., report=True)` with hand-made selections, as the layout tests build
  them.
- The CLI with `CliRunner`, the Slack clients patched (`_slack_web_client`,
  `_webhook_client`), `_extract_days` patched (the `prepare_post` and `prepare_dm`
  helpers), `_new_run_id` and `_now` patched, and the default store path patched to
  `tmp_path`. No test touches the network.

## Next units

1. **Selection reads exposure** (the session that owns `daily/select.py`). The seen
   record (later.md, Interaction 1), `novelty` against the shown claims, and stale
   beliefs read `deliveries`. The exposure read also counts the bot unit's `done` events
   with `resolution: exposure`: SPEC section 5 step 8 says a non-owner's done records
   exposure for that reader. The read function and the record format are this unit's.
2. **Exposure on render** for the terminal, markdown, the web preview and replay (SPEC
   section 5 step 9). Same table, with a `path` value per surface.
3. **Send idempotency** (M5 stretch). A retry reads the run id from the I8 metadata and
   the accepted parts of that run in `deliveries`, and posts only the missing parts.
4. **Recording on by default**, and the live scheduler (D7).
5. **The absence split** (later.md, Structure 2). It keeps the episode subjects of Q1.
6. **The `exposed` map** (SPEC section 4, `how = shown_in_digest`) derived from the claim
   snapshot when the ledger exists (D26), and the exposure weight of later.md Next steps
   11.

## Out of scope

- Any change to `daily/select.py`, to ranking or to the content of a digest.
- Send idempotency (SPEC section 9, v0.4 stretch): the step 9 rule "each sent part
  carries the run id in message metadata so a retry posts only the missing parts". The
  bot unit's I8 already sends the metadata. The retry that posts only the missing parts,
  and refusing a second send for the same reader and day, wait for Next units 3.
- Dead-credential handling, the admin alert, the "nothing today" line (M5 stretch).
- Exposure on render, the `exposed` and `seen` profile maps, real-time alerts.
- The `posted_messages` table, the feedback log and anything else of the bot unit.
- The absence split.

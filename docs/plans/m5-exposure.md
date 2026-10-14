# Plan: M5 core, the delivery record (exposure on Slack's acceptance)

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`). Not reviewed yet. Source:
SPEC section 5 step 9 (Delivery), section 9 ("M5. Core: exposure recorded on Slack's
acceptance"), D7 (amended 2026-10-11) and D26 ("Ledger and exposure"). This is "Delivery
records" (`docs/later.md`, Next steps 5). Size: one night of work.

Base: `main` at 9ed8af5. Two units of the same session are open and not merged:
the layout unit (`docs/plans/m5-layout.md`, branch `m5-layout`) and the bot unit
(`docs/plans/m5-bot.md`, branch `m5-bot-plan`, stacked on the layout unit). This plan
does not need either of them. "Stacking" says what changes when one of them lands
first.

## Open questions

The SPEC does not settle these. Each has a proposed answer. The plan uses the proposed
answer until the user decides.

Q1. The absence split. `docs/later.md` (Structure 2) puts the absence split "before any
exposure record exists". Proposed: this unit does not wait for the split. The record
keys an absence by its own key string (D26: source and task or ask, with the day in its
own column), never by an item id. The string is the one the bot unit uses (bot plan I1):
`absence:<task id>` or `absence:ask:<ask item id>`. The split must keep these strings.
So the split does not redo the record. The user confirms this reading of the trigger, or
the split becomes a prerequisite and this unit waits.

Q2. A partial post. A digest can have more than one part (after the layout unit: the top
message and the thread reply). When Slack accepts the first part and refuses the second,
does the digest count as "the reader's last delivered digest" for the window? Proposed:
no. The window reads only a run in which Slack accepted every part of that reader's
digest (I9). The record keeps the accepted parts either way, so the exposure of the
lines in those parts is not lost (I3).

Q3. The webhook. An incoming webhook returns status 200 and the body `ok`, with no `ts`
and no channel. Is that "Slack's acceptance"? Proposed: yes. The webhook path records a
row with no channel and no `ts` (I6), and that row can end the window.

Q4. The demo recipient. `digest post --dm USER_ID` sends the digests of three personas to
one Slack user. Whose exposure is it? Proposed: the persona's, as in the bot plan's demo
actor rule (bot plan I7). The row also keeps the Slack user id (`recipient`).

Q5. Recording on by default. Proposed: not tonight. The command records only with
`--db PATH` (I1). The demo freezes at 2026-10-14 15:00 PT, and an opt-in option keeps
every default output the same. Recording by default is a next unit.

Q6. A second post for the same reader and day. D7 says delivery is idempotent on (user,
digest_date). D26 says exposure is append-only. Proposed: this unit appends. A rerun
mints a new run id and records new rows (I8). Refusing a second send is send
idempotency, an M5 stretch item, and stays out of this unit.

Q7. Claim ids. D26 says the record holds "the claim ids shown". Claims have no ids today:
they are JSON inside the `items` row, and the ledger is not built. Items are overwritten
in place, so D26 asks for "a kept item revision or a delivery snapshot". Proposed: the
record holds a snapshot of each shown claim's fields (I4). The ledger unit can map the
snapshot to claim ids later.

Q8. Folded members (layout unit only). The layout unit folds a related item into a kept
item as a member (layout plan 4.3). Is a member "shown"? Proposed: yes, when the kept
item's line is in the part, because the member leaves its own section and the kept line
carries it. This question applies only after the layout unit merges.

## Goal

When Slack accepts a digest part, the store records what the reader was shown: the
reader, the digest day, the run id, the world, the Slack channel and `ts`, the item ids,
the absence keys and the claims of the lines in that part. One read function gives a
reader's last delivered digest day. With `--db`, `digest post` takes the window of each
digest from that read, so the window starts where the reader's last delivered digest
ended (SPEC section 5 step 9).

This unit records. It does not change how selection uses exposure: novelty, stale
beliefs and the seen record read the record in later units (Next units).

## Rule (from SPEC section 5 step 9 and D26)

- Exposure is recorded when the digest is delivered. For Slack, that is Slack's
  acceptance of the message. (Exposure on render for the terminal, markdown, the web
  preview and replay is a next unit.)
- A digest's window starts where the reader's last delivered digest ended, at most
  `window.max_working_days` back. A first digest goes `window.first_working_days` back.
  `select()` already does this from `previous_digest`.
- Exposure is append-only. Digest code writes it at delivery: reader, run, delivered
  part, time, and the items and claims shown. An absence is exposed by its own key, never
  by an item id.

## Commands

```
digest post [--dm USER_ID] [--seed-map PATH] [--day N] [--db PATH]
```

- `--db PATH` (new on `post`): the delivery store. Without it, the command records
  nothing and behaves as today, byte for byte (I12). With it, the command opens the store
  with `_open_store` after the destination checks (webhook URL, or user id and token)
  and before it builds a digest. A file of another schema version, or a file that is not
  SQLite, exits 1 with today's `_open_store` text, and nothing is sent.
- The option has no default in this unit. The bot plan gives `--db` the default
  `.digest/feedback.sqlite`, and opens it only with `--interactive`. Merge rule: when the
  bot unit lands, `--interactive` without `--db` uses that default path, and recording is
  on whenever the store is open. One store holds both the delivery record and the
  feedback log.
- No other command changes. `digest demo`, `digest run` and `digest serve` do not record.

## Behaviours

### Acceptance and the record

I1. **Recording mode.** The command records only when `--db` is given. It mints one run
id per run with `_new_run_id()`: `uuid.uuid4().hex`, the bot plan's I8 rule. Every row of
the run holds it.

I2. **A part.** A part is one Slack message of one reader's digest, in the order the
command sends them. `part` counts from 0. On `main` each digest has one part. `parts` is
the number of messages that the renderer built for that reader's digest.

I3. **Acceptance, DM path.** Slack accepts a part when `chat.postMessage` returns without
an exception, `response.get("ok")` is `True`, and `response.get("ts")` is a non-empty
string. After each accepted part, and before the next send, the command writes one row
(Data). It does not wait for the end of the run, so a later failure keeps the earlier
rows.

I4. **What a row holds.** `run_id`; `reader` (the persona `user_id` of the digest, never
the recipient); `part`; `parts`; `day` (the digest day, ISO date); `world`
(`kestrel` for `digest post`); `path` (`dm` or `webhook`); `recipient` (the `--dm` user
id, else null); `channel` (the DM channel id from `conversations.open`, else null); `ts`
(the response `ts`, else null); `accepted_at` (UTC, ISO 8601 with offset, from `_now()`);
and `shown`, from `part_shown(lines)` (I5) over the lines in that part:

- `items`: the `item_id` of each line that is not an absence, in line order, each once.
- `absences`: for each absence line, its key (the absence item's `item_id`:
  `absence:<task id>` or `absence:ask:<ask item id>`), its `source` (`checklist` or
  `ask`), and `last_evidence`, the `<channel_id>:<message_ts>` of its one evidence entry.
  An absence key never enters `items`.
- `claims`: for each line that is not an absence, each claim of the line's item whose
  `channel_id` is the channel of one of the line's evidence entries. The line's item is
  the copy that the visibility filter trimmed, so a claim from a channel the reader cannot
  see is not recorded. Each entry holds `item_id`, `entity`, `attribute`, `value`,
  `channel_id`, `message_ts` and `authority`, as stored at post time (Q7).

I5. **The lines in a part.** The renderer reports the lines whose text is in each part.
`slack_message(..., report=True)` returns the message dict with one more key, `lines`:
the `Line` objects in block order. A line cut by the 50-block limit ("N lines left out")
is not in `lines`. The CLI removes the key before the Slack call. With `report=False`
(the default) the dict is byte for byte what it is today. A day with no lines gives an
empty `lines`.

I6. **Acceptance, webhook path.** Slack accepts a part when `send` returns a response
with `status_code` 200, the success test of today. The row has `path` `webhook` and a
null `recipient`, `channel` and `ts`. A refusal (another status) or a send exception
writes no row.

I7. **Failures in recording mode.** Each refusal exits 1 and sends nothing more. The
rows already written stay.

| Case | Refusal text |
|---|---|
| A send exception or a Slack refusal | today's text, unchanged; no row for that part |
| DM response with `ok` not `True` or no string `ts` | `cannot record the digest for <reader>: the message has no ts; its message was sent<note>` |
| The row write raises `sqlite3.Error` | `cannot record the digest for <reader>: <exception type name>; its message was sent<note>` |
| The row write returns False (I8) | `cannot record the digest for <reader>: a row with this key exists; its message was sent<note>` |

`<note>` is `, and a rerun sends all <total> again` when no digest was fully sent before,
else `_rerun_note(sent, total)`. This is the bot plan's I10 text, so the two units share
one refusal. The exception text is not printed: a path or a value from the store can be
in it.

I8. **Idempotency.** The key of a row is `(run_id, reader, part)`. A DM row is also
unique on `(channel, ts)`. The insert is `INSERT ... ON CONFLICT DO NOTHING`, and
`record_delivery` returns False when a row with either key exists. The CLI writes each
key once, so False there means the message was sent and no new row records it. The
command then refuses as I7 does: `cannot record the digest for <reader>: a row with this
key exists; its message was sent<note>`. A rerun of the command mints a new run id, so
it records new rows (Q6).

### The read

I9. **Last delivered day.** `last_delivered(store, world=, reader=, before=)` returns the
greatest `day` that is earlier than `before`, for which one run holds a row for every
part from 0 to `parts - 1` for that world and reader. A run with a missing part does not
count (Q2). A day at or after `before` does not count, so a second post of the same day
does not shorten its own window. No such run gives None.

I10. **The window in `digest post`.** With `--db`, the command gives each reader's
digest `previous_digest = last_delivered(store, world="kestrel", reader=<user_id>,
before=<digest day>)`. None makes a first digest (`window.first_working_days`). Without
`--db`, the command keeps today's rule: the previous world day, and a first digest on
world day 1. So a first `--db` post into an empty store is a first digest for each
reader. Its window line says "First digest", and it can show more lines than the same
post without `--db`.

I11. **Where the code goes.**

- `daily/exposure.py` (new, Digest context per D26): the models, `part_shown`,
  `record_delivery` and `last_delivered`. It imports no Slack package and nothing from
  `cli.py`.
- `store.py`: the SQL only. Insert one delivery row, and read the rows of one world and
  reader.
- `daily/slack.py`: the `report` keyword (I5).
- `cli.py`: the `--db` option, `_new_run_id`, `_now`, the record write after each
  accepted part, and one additive keyword on the private helper `_demo_selections`
  (Interface). The keyword's default keeps today's behaviour for `demo` and `serve`. No
  existing command or option changes.
- `daily/select.py` does not change.

### Unchanged behaviour

I12. Without `--db`: no file is opened or written, every Slack call and every output line
is the same as today, and the existing tests in `tests/test_slack_post.py` and
`tests/test_slack_links.py` pass with no change.

## Data

One table joins `schema.sql`. It uses `CREATE TABLE IF NOT EXISTS`, so `Store.open` adds
it to an existing store. `SCHEMA_VERSION` stays `"2"`: a bump makes `_check_version`
refuse every existing store. The bot plan uses the same rule.

```sql
CREATE TABLE IF NOT EXISTS deliveries (
  run_id      TEXT NOT NULL,
  reader      TEXT NOT NULL,     -- persona user_id of the digest
  part        INTEGER NOT NULL,  -- 0 for the first message of the digest
  parts       INTEGER NOT NULL,  -- messages in the digest
  day         TEXT NOT NULL,     -- ISO date of the digest
  world       TEXT NOT NULL,
  path        TEXT NOT NULL,     -- dm | webhook
  recipient   TEXT,              -- Slack user id of the DM; null for webhook
  channel     TEXT,              -- Slack channel id; null for webhook
  ts          TEXT,              -- Slack ts of the message; null for webhook
  accepted_at TEXT NOT NULL,     -- UTC ISO 8601 time of the write
  item_ids    TEXT NOT NULL,     -- JSON array of item ids (PartShown.items)
  absences    TEXT NOT NULL,     -- JSON array of {key, source, last_evidence}
  claims      TEXT NOT NULL,     -- JSON array of {item_id, entity, attribute, value, channel_id, message_ts, authority}
  PRIMARY KEY (run_id, reader, part),
  UNIQUE (channel, ts)           -- SQLite treats nulls as distinct, so webhook rows pass
);
CREATE INDEX IF NOT EXISTS deliveries_reader ON deliveries (world, reader, day);
```

No statement updates or deletes a `deliveries` row. No statement writes the `items` table.

### Relation to `posted_messages` (bot unit)

The bot plan adds `posted_messages`, and its Next units 2 says this unit could add the
item and claim ids to it. This plan keeps a separate table, for four reasons:

- The primary key of `posted_messages` is `(channel, ts)`. A webhook part has neither,
  but it is a delivery (Q3).
- The bot unit writes `posted_messages` only with `--interactive`. Exposure is recorded
  on every accepted part in recording mode.
- A `posted_messages` row changes: `revision` grows on each update, and a post with the
  same `(channel, ts)` replaces the row. D26 asks for an append-only exposure record.
- `posted_messages` holds what the bot needs to update a message (text, blocks,
  metadata, menu items). `deliveries` holds what the reader was told. Neither table
  copies the other's columns.

A DM part with both rows joins on `(channel, ts)`. The bot plan's Next units 2 line
needs an amendment in that unit's file: "Exposure is the `deliveries` table of
`docs/plans/m5-exposure.md`; it joins `posted_messages` on `(channel, ts)`."

## Interface (stubs land before tests)

```python
# digest/daily/exposure.py
class AbsenceShown(Frozen):
    key: str                        # "absence:<task id>" or "absence:ask:<ask item id>" (Q1)
    source: Literal["checklist", "ask"]
    last_evidence: str              # "<channel_id>:<message_ts>"

class ClaimShown(Frozen):
    item_id: str
    entity: str
    attribute: Attribute
    value: str
    channel_id: str
    message_ts: str
    authority: Authority

class PartShown(Frozen):
    items: tuple[str, ...]
    absences: tuple[AbsenceShown, ...]
    claims: tuple[ClaimShown, ...]

class Delivery(Frozen):
    run_id: str
    reader: str
    part: int                       # at least 0, less than parts
    parts: int                      # at least 1
    day: date
    world: str
    path: Literal["dm", "webhook"]
    recipient: str | None
    channel: str | None             # None exactly when path is webhook
    ts: str | None                  # None exactly when path is webhook
    accepted_at: datetime           # aware
    shown: PartShown

def part_shown(lines: Sequence[Line]) -> PartShown: ...                        # I4
def record_delivery(store: Store, delivery: Delivery) -> bool: ...             # I8
def last_delivered(store: Store, *, world: str, reader: str,
                   before: date) -> date | None: ...                           # I9

# digest/store.py
def insert_delivery(self, row: Mapping[str, Any]) -> bool: ...   # ON CONFLICT DO NOTHING
def delivery_rows(self, world: str, reader: str) -> list[dict[str, Any]]: ...

# digest/daily/slack.py
def slack_message(world: World, selection: Selection, links: SeedMap | None = None,
                  *, report: bool = False) -> dict[str, Any]: ...              # I5

# digest/cli.py
# post gains --db; _post_dm and the webhook branch take the open store or None.
def _new_run_id() -> str: ...                                     # uuid.uuid4().hex
def _now() -> datetime: ...                                       # datetime.now(UTC)
def _demo_selections(day: int, *,
                     previous_of: Callable[[str, date], date | None] | None = None
                     ) -> tuple[World, list[Selection]]: ...
    # previous_of(user_id, digest_day) gives previous_digest; None keeps today's rule
```

- `digest.daily.exposure` sits in `digest.daily`, which already imports `digest.store`.
  The import-linter contract needs no change. The implementer checks this with
  `uv run lint-imports`.
- `Delivery` validates the null rule of `channel` and `ts`, and `0 <= part < parts`.

## Stacking

- **Layout unit first.** `slack_message` is gone. `slack_digest(..., report=True)` adds
  `lines` to each message it returns (top message, thread reply). `parts` is 1 or 2. The
  webhook path sends the reply as a second plain message, so it is part 1 there too. A
  kept item's members are in `lines` with the kept line (Q8). Layout tests that pin the
  exact message dicts stay green, because `report` is off by default.
- **Bot unit first.** It defines `_new_run_id` and the I8 metadata. This unit reuses
  them. `--db` and its default follow the merge rule in Commands. The record write and
  the `posted_messages` write both follow the same accepted response.
- **This unit first.** It defines `--db` (no default), `_new_run_id` and the I7 refusal
  text. The bot unit rebases onto them and amends its Next units 2 line. This unit sends
  no message metadata: the metadata serves retries (send idempotency, stretch) and the
  bot's cross-check, and the record does not need it.

## Demo output

The unit does not change demo output. `digest demo` and `digest serve` do not change.
`digest post` without `--db` sends the same payloads and prints the same lines (I12).
Only `digest post --db PATH`, which no demo step uses, records rows and takes its window
from the record. Its first post into an empty store is a first digest (I10).

## Before tests

None. The DM path already reads the `chat.postMessage` response through `.get` in
`_slack_failure`, and the layout plan (11.1) and bot plan (I10) already rely on the
response `ts`. The webhook success test (status 200) is today's code.

## Acceptance

Each row is tested offline with `CliRunner`, a temporary store (`tmp_path`), and the
fakes of `tests/test_slack_post.py` (`FakeSlackWebClient`, `FakeWebhookClient`,
`FakeResponse`), unless it says otherwise. `FakeSlackWebClient.chat_postMessage` returns
None today. The test author gives it a response: `{"ok": True, "ts": <a ts per post>}`
by default, with a way to return a response without `ts`. Tests name the fakes by class,
not by line number: the layout branch rewrites that file.

- A1 (I4, `part_shown`). Hand-made lines: items in line order, each once; an absence
  line goes to `absences` with its key, source and last evidence, and not to `items`; a
  checklist key is `absence:<task id>`, an ask key is `absence:ask:<ask item id>`; a claim
  whose `channel_id` is not a channel of the line's evidence is left out; an empty
  sequence gives three empty tuples.
- A2 (I5, renderer). `slack_message(..., report=True)` holds `lines` in block order; with
  more lines than the 50-block limit, a cut line is not in `lines`; the dict without
  `lines` equals the `report=False` dict; a day with no lines gives `lines == ()`.
- A3 (I8, store). `record_delivery` returns True and writes one row; the same `(run_id,
  reader, part)` again returns False and writes nothing; another run with the same DM
  `(channel, ts)` returns False; two webhook rows with null `channel` and `ts` in two runs
  both write. `Delivery` refuses a webhook row with a `ts`, a DM row without one, and
  `part >= parts`.
- A4 (I9, read). A complete run for day D gives D for `before` after D; a run with part 1
  of 2 missing does not count; a run for day `before` or later does not count; another
  world or another reader does not count; two complete days give the later one; no rows
  give None; a complete run whose rows hold empty `item_ids`, `absences` and `claims` (a
  day with nothing new) counts.
- A5 (I3, I4, I10, DM). The store at `--db` holds a complete day-4 delivery for each of
  the three readers. `post --dm U012ABC --db <tmp> --day 5` with `_new_run_id` and `_now`
  patched: one new row per reader, in reader order, with the persona `reader`, the day,
  the run id, `kestrel`, `dm`, the recipient, the DM channel, the fake's `ts`, the
  patched time, and the `shown` of the lines that were sent. Both commands pass the same
  `previous_digest` (day 4), so the Slack calls and the output equal those of the same
  command without `--db`.
- A6 (I3, I7, partial). The second `chat.postMessage` raises: the first reader's row
  stays, no row for the others, exit 1, and the refusal text is today's text.
- A7 (I7, no ts). The fake returns `{"ok": True}` for the second post: the refusal of I7
  with its note, no row for that reader, the first row stays, and no third post.
- A8 (I7, write failure). `record_delivery` patched to raise `sqlite3.OperationalError`
  whose text holds a marker: the refusal names the type, says the message was sent, and
  the output does not hold the marker; nothing more is sent.
- A9 (I6, webhook). `post --db <tmp>` on the webhook path: one row per reader with `path`
  `webhook` and null `recipient`, `channel` and `ts`. A `FakeResponse(500, ...)` for the
  second reader: one row only, today's refusal text.
- A10 (Commands). `--db` pointing at a text file, and at a store of another schema
  version: exit 1 with the `_open_store` text, and no Slack call (no
  `conversations_open`, no `send`).
- A11 (I10, window). A store at `--db` holds a complete delivery for the ME reader on day
  4. `post --dm ... --db <tmp> --day 6` with `select_digest` wrapped: the ME reader gets
  `previous_digest` = the day-4 date; the other two readers get None. The same command
  without `--db` gives each reader the day-5 date (today's rule).
- A12 (I12). Without `--db`: no file appears in the working directory, and the existing
  tests in `tests/test_slack_post.py` and `tests/test_slack_links.py` pass unchanged.
- A13 (Data). `Store.open` on a version-2 store file without `deliveries` adds the table,
  keeps the rows of the other tables, and `schema_version` stays `"2"`.
- A14 (live, manual, recorded in the PR). In Scratch: `digest post --dm <member> --db
  <tmp>/d.sqlite --seed-map <map> --day 6`, then read the rows back with `uv run python -m
  sqlite3 <tmp>/d.sqlite "SELECT run_id, reader, part, parts, day, path, channel, ts FROM
  deliveries"`. Each `ts` is the `ts` of a message in the DM. Then post day 7 with the
  same store: each header says "Since your digest of" day 6.

Each new or changed test is RED-GREEN proven with `prove-test`.

## Test seams

- `part_shown`, `record_delivery` and `last_delivered` over `Store.open(tmp_path /
  "d.sqlite")` and hand-made `Line` objects, as `tests/test_daily_render.py` builds them.
- `slack_message(..., report=True)` with hand-made selections, as the Slack tests do.
- The CLI with `CliRunner`, the Slack clients patched (`_slack_web_client`,
  `_webhook_client`), `_extract_days` patched (the `prepare_post` and `prepare_dm`
  helpers), and `_new_run_id` and `_now` patched. No test touches the network.

## Next units

1. **Selection reads exposure** (the session that owns `daily/select.py`). The seen
   record (later.md, Interaction 1), `novelty` against the shown claims, and stale
   beliefs read `deliveries`. The read function and the record format are this unit's.
2. **Exposure on render** for the terminal, markdown, the web preview and replay (SPEC
   section 5 step 9). Same table, with a `path` value per surface.
3. **Send idempotency** (M5 stretch). The message metadata of bot plan I8, and a retry
   that reads the accepted parts of a run and posts only the missing ones.
4. **Recording on by default**, and the live scheduler (D7). This answers Q5 for
   production.
5. **The absence split** (later.md, Structure 2). It keeps the key strings of Q1.
6. **The `exposed` map** (SPEC section 4, `how = shown_in_digest`) derived from the claim
   snapshot when the ledger exists (D26), and the exposure weight of later.md Next steps
   11.

## Out of scope

- Any change to `daily/select.py`, to ranking or to the content of a digest.
- Message metadata, retries that send only the missing parts, and refusing a second
  send for the same reader and day (send idempotency, M5 stretch).
- Dead-credential handling, the admin alert, the "nothing today" line (M5 stretch).
- Exposure on render, the `exposed` and `seen` profile maps, real-time alerts.
- The `posted_messages` table and anything else of the bot unit.
- The absence split.

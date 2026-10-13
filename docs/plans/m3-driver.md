# Plan: M3 unit 7, the what-if driver (`digest say`, `digest diff`)

Status: v0.3, 2026-10-12. Process stage 1 (`docs/process.md`). Depends on units 5 and 6
(`digest/daily/select.py`, `digest/daily/render.py`, `digest run`, `_extract_days`),
which are in review on `m3-select` and `m3-render`. A stubs commit with the types and
signatures below lands after unit 6 merges; the test author works from those stubs only.

## Goal

Drive the digest from the command line the way a team would drive it from Slack: add a
message as a persona at a world time, then see which readers' digests change and why.

- Testing: a scenario is a short script of `say` lines plus one `diff`; a regression is a
  changed `diff` output.
- The interview demo: one message from the contract manufacturer changes three readers'
  digests in different ways, offline, from recorded cassettes.

Slack has no tool that posts as a simulated user, and the API posts only as the bot, so
the driver is ours. Posting into a real workspace is out of scope (the seeder, M5).

## The scratch store

`say` never writes to the base store. On first use it creates `--db` with
`Store.copy_to(path)`, a new store method that uses SQLite's online backup API from a
read-only connection (`sqlite3.Connection.backup`), so rows still in the base store's
write-ahead log are copied too. `--db` equal to the
base path, or resolving to the same file, is refused.

Review rulings (unit 7 code review, 2026-10-12):
- The base store is opened read-only (`Store.open_readonly`, a `mode=ro` connection with
  no schema script and no write); a file that is not a store of the current schema is
  refused with its reason. `copy_to` reads from that connection.
- `copy_to` removes the target file when the backup fails, so no half-made scratch
  store is left behind.
- A `say` that creates the scratch store refuses when `<db>.script.jsonl` already exists,
  naming that file, so a log never mixes two sessions.
- `--script` removes the scratch store (with its `-wal` and `-shm` files) on any
  failure after the copy, not only on a refused line, and prints the event ts values
  only after every line is stored.
- A script file is split into lines on `
` only, so a `--text` holding U+0085, U+2028
  or U+2029 replays.
- Every refusal is one line; a validation error is reduced to its first message.

## `digest say`

```
digest say --as <persona id> --channel <channel name> --at <YYYY-MM-DDTHH:MM[:SS]> \
           --text "<message>" [--thread <ts>] --db <scratch> [--base-db <store>]
```

Rules, each a refusal with exit 1, a one-line reason and no write when broken:

- S1. The persona exists and is not a bot. The channel exists, and its id is in the
  persona's `channels` list.
- S2. The store's day (the world-zone date of meta `until`, an ISO timestamp) is one of
  the world's working days (`world.date_of(1..days)`); extraction visits only those.
  `--at`, read in the world's time zone, falls on that day and is strictly later than
  every `event_ts` in the store. So every injected message is the
  newest event of its thread, and extraction always sees it as pending.
- S3. `--thread`, when given, is the `ts` of a top-level message in that channel. The
  command checks this against the store; `make_event` does not.

The event is built by `make_event(world, store_view, persona_id, channel_name, at, text,
thread)`, where `store_view` gives the store's day and latest `event_ts` (no other store
access in the function). `make_event` enforces S1 and S2. Fields: `source="replay"`, `channel_id` and `channel_name` from
the world channel, `user_id` the persona, `ts` = `--at` as Slack text (ten integer
digits, a dot, six fractional digits, `.000000` for whole seconds), `thread_ts` =
`--thread` or `ts`, `event_ts` = `ts`, `text`, empty `mentions`, `reactions` and
`files`, `permalink` from the world's `workspace_url`, `edited` and `deleted` false.
Because S2 makes `ts` greater than every stored `event_ts`, it is unique in the channel.

Then `say` writes the event with `store.upsert_events`, sets meta `extract_until` to the
day before the store's day when it is not already earlier (so the next `digest extract`
revisits that day; `thread_deltas` returns only the new or changed threads), appends the
`say` arguments as one JSON line to `<db>.script.jsonl`, and prints the event's `ts`.

`digest extract --db <scratch>` then triages and extracts the new thread: one triage
request and, when the thread is kept, one extraction request. Replay mode needs both
cassettes; a miss exits 1 with the cassette key. `DIGEST_MODE=record` with the gateway
key records them once.

`digest say --script <file> --db <scratch> [--base-db <store>]` runs every line of a
script file in order (each line as its own `say`, same rules) into a fresh scratch copy
(a `--db` that already exists is refused, so a script never overwrites a session),
and does not append to any script file. A script reproduces a session only when the
session ran one `digest extract` after its last `say`: extracting between lines changes
the extraction requests, so such a session is not reproducible from its script.

## `digest diff`

```
digest diff --as-of <YYYY-MM-DD> --db <store B> [--base-db <store A>] [--user <id>] [--json]
```

`diff_selections(before: Selection, after: Selection) -> ReaderDiff` compares two
selections of one reader by `item_id`. Types:

```python
class LineView(Frozen):
    item_id: str
    title: str
    section: Section
    lane: Literal["must_see", "ranked"]
    rank: int | None   # 1-based position among ranked lines in its section; None in the must-see lane
    reason: str

class Change(Frozen):
    item_id: str
    before: LineView
    after: LineView
    fields: tuple[Literal["section", "lane", "rank", "title", "reason"], ...]  # in this order

class ReaderDiff(Frozen):
    user_id: str
    added: tuple[LineView, ...]
    removed: tuple[LineView, ...]
    changed: tuple[Change, ...]
```

- An item in both selections whose `LineView` differs in any field is a `Change`; its
  `fields` lists every differing field. Equal views are not reported.
- Order inside each tuple: section order (as `Selection.lines`), then lane (must-see
  first), then rank, then `item_id`. A `Change` sorts by its `after` view.
- The command runs `select_digest` on both stores for every non-bot persona, or for
  `--user`, sorted by `user_id`. Text output lists each reader with changes (added,
  removed, changed, each with title and reason), then one line naming the readers with
  none. `--json` prints a JSON array of `ReaderDiff` in the same order.
- No change for any reader prints "no change" and exits 0. A refused store or an unknown
  user exits 1 with the reason, as `digest run` does.

`diff_selections` rulings (2026-10-13, before the tests):
- A `Line` maps to a `LineView` field by field: `item_id` is `line.item.item_id`, `title`
  is `line.item.title`, `section` is `line.section`, `lane` is `"must_see"` when
  `line.must_see` is true and `"ranked"` otherwise, and `reason` is `line.reason`.
- `rank` is the 1-based position of a ranked line among the ranked lines of its section,
  in the order of `Selection.lines`. A must-see line has `rank` None. `score` is not
  compared: a score change shows only through `rank` or `reason`.
- Section order is the order of `SECTION_CAPS` (`needs_you`, `not_happened`, `decisions`,
  `risks`, `fyi`).
- `user_id` is `after.profile.user_id`. Two selections of different readers raise
  `ValueError`. An `item_id` that occurs twice in one selection raises `ValueError`.
- Fields of `Selection` other than `profile` and `lines` (`day`, `hidden`, and any field
  added later) are not compared.
- The function is pure: same inputs, same output, no I/O.

## Code layout

- `digest/driver.py`: `make_event`, `diff_selections`, the types above, and the script
  line model. No model calls, no wall-clock time, no store writes.
- `digest/store.py`: `Store.copy_to(path)` (the store owns how it is opened and copied).
- `digest/cli.py`: `say` and `diff`, thin: the S3 check, the meta update, the script
  append and the output live in the commands.
- Import layers: `digest.driver` sits above `digest.daily` and below `digest.cli`; the
  import-linter contract changes in the stubs commit.

## Acceptance

- D1. `say` leaves the base store's logical contents unchanged: every table's rows and
  every meta key, compared before and after, with uncheckpointed write-ahead-log rows
  present in the base before the copy.
- D2. Demo scenario, pinned: base = the Kestrel store replayed and extracted through day
  6 (2026-10-19). Script: `U0CM01` (Luis Ortega, Sunrise) in `#ext-sunrise-cm` at
  2026-10-19T17:30, text "Blocker: we cannot ship LiDAR bracket PN-11051 Rev A this week;
  first articles move from 19 Oct to 2 Nov." Then `extract` (replay, both cassettes
  committed) and `diff --as-of 2026-10-19`. Expected, derived by hand before any code from
  SPEC section 5 step 6 and the world's ownership (`PN-11051` is owned by ME only; its
  parent `PN-90001` and subsystem `compute` have no owner): the extracted item has type
  `blocker` and names `PN-11051`; `U0ME01` gains it in the must-see lane; no other reader
  has it in the must-see lane. Ranked-lane changes for other readers are not asserted.
- D3. Visibility, pinned: on the same base, `U0EM01` in the private `#kestrel-leads`
  (members `U0EM01` and `U0PM01`) at 2026-10-19T17:40, text "Blocker: DVT-1 go/no-go
  G-DVT1 moves from 22 Oct to 29 Oct until the bracket lands." Expected: the extracted
  item is a `blocker` naming `G-DVT1` (owned by EM); `U0EM01` gains it in the must-see
  lane; `U0ME01`, `U0EE01`, `U0FW01`, `U0SC01` and `U0CM01` do not have it at all.
- D4. `diff` of a store against a backup copy of itself prints "no change" and exits 0,
  and its `--json` is byte-identical across two runs.
- D5. Each refusal S1 to S3 (including a store day that is not a working day) and the
  same-file guard exits 1 with its reason, and the scratch store and script file are
  unchanged.
- D6. Running the D2 script twice from fresh copies gives byte-identical `diff --json`.

## Test seams (for the test author)

- `diff_selections` on hand-built `Selection` values: added, removed, each `Change` field
  alone and combined, a lane switch, unchanged, and the ordering rules.
- `make_event` with a fake `store_view`: ts text format, the S2 boundaries (equal is
  refused, one second later is accepted, a non-working store day is refused), thread
  linkage, S1 refusals. S3 is tested through the command.
- `Store.copy_to` on a store with uncheckpointed write-ahead-log rows: the copy holds them.
- The commands through typer's `CliRunner` on fixture stores in `tmp_path`: D1, D4, D5.
  D2, D3 and D6 run in replay mode with the committed cassettes and script files under
  `data/world/kestrel/scripts/`.

## Out of scope

- Posting into a real Slack workspace: the seeder, M5.
- Feedback buttons and App Home actions from the CLI.
- `--json` on `digest run`: add when a caller needs it.
- Messages before the store's latest event (backdating): refused by S2; supporting them
  needs extraction state to be rebuilt, which this unit does not do.

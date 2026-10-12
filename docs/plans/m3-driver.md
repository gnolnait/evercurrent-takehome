# Plan: M3 unit 7, the what-if driver (`digest say`, `digest diff`)

Status: v0.1, 2026-10-12. Process stage 1 (`docs/process.md`). Depends on units 5 and 6
(`digest/daily/select.py`, `digest/daily/render.py`, `digest run`), which are in review on
`m3-select` and `m3-render`. Tests are written after unit 6 merges, against its merged
interface.

## Goal

Drive the digest from the command line the way a team would drive it from Slack: add one
message as a persona at a world time, then see which readers' digests change and why.
Two uses:

- Testing: a scenario is a short script of `say` lines plus one `diff`; a regression is a
  changed `diff` output.
- The interview demo: type one message as the contract manufacturer and show three
  readers' digests change in three different ways, live, from recorded cassettes.

Slack offers no tool that posts as a simulated user, and the API posts only as the bot,
so the driver is ours. A live variant that posts into the Scratch workspace is out of
scope here (it is the seeder's job, M5).

## Commands

### `digest say`

```
digest say --as <persona id> --channel <channel name> --at <YYYY-MM-DDTHH:MM> \
           --text "<message>" [--thread <ts>] --db <scratch store> [--base-db <store>]
```

- Copies `--base-db` (default: the store `digest run` uses) to `--db` when `--db` does
  not exist. It never writes to the base store. `--db` equal to the base path is refused.
- Builds one `Event`: `source="replay"`, `user_id` the persona, `channel_id` from the
  world's channel of that name, `ts` from `--at` in the world's time zone with a
  microsecond suffix that makes it unique in that channel, `thread_ts` = `--thread` or
  the event's own `ts`, `event_ts` = `ts`, permalink built the usual way.
- Writes it with `store.upsert_events` and appends the same line to
  `<db>.overlay.jsonl`, so the scenario can be replayed into a fresh copy.
- Prints the event's `ts` (the handle for `--thread` on a reply).
- Refuses, with exit 1 and a one-line reason: an unknown persona; an unknown channel; a
  channel whose id is not in the persona's `channels` list (the world's membership); `--at` outside the world's days; a
  `--thread` that is not a top-level message in that channel.

The message then goes through the normal pipeline: `digest extract --db <scratch>`
triages and extracts it. Replay mode needs a recorded cassette for that request; a miss
exits 1 with the cassette key, as `extract` does today. Record mode
(`DIGEST_MODE=record`, gateway key set) records the cassette once. Demo lines are
recorded ahead of time and committed with their overlay file.

### `digest diff`

```
digest diff --as-of <YYYY-MM-DD> --db <store B> [--base-db <store A>] [--user <id>] [--json]
```

- Calls `select_digest` for each reader on both stores: every non-bot persona, or the one
  `--user`. Compares the two `Selection` values by `item_id`.
- For each reader with a change, prints the added lines, the removed lines and the moved
  lines (section change, or rank change in the ranked lane), each with its `reason`
  (the must-see rule, or the score terms) and the item title. Readers with no change are
  listed on one line.
- `--json` prints one object per reader: `{user_id, added[], removed[], moved[]}`, each
  entry `{item_id, title, section, must_see, reason, rank_before, rank_after}`. Order is
  stable: readers by user id, entries by section order, then rank.
- Two identical stores print "no change" and exit 0. A refused store or an unknown user
  exits 1 with the reason, as `digest run` does.

## Code layout

- `digest/driver.py`: `make_event(world, persona, channel, at, text, thread)`,
  `diff_selections(before, after) -> ReaderDiff`, and their pydantic result types. No
  model calls, no wall-clock time.
- `digest/cli.py`: the two commands, thin; store copy and overlay append live in the
  command, the logic in `digest.driver`.
- Import layers: `digest.driver` sits above `digest.daily` and below `digest.cli`; update
  the import-linter contract in the same commit as the stubs.

## Acceptance

- D1. `say` leaves the base store byte-identical (file hash before and after).
- D2. On the Kestrel fixture at day 6: `say` as `U0CM01` (Luis Ortega, Sunrise) in
  `#ext-sunrise-cm` that the bracket slips two weeks, then `extract` (replay, recorded cassette),
  then `diff`: the slip appears for the readers whose must-see rules or rank admit it,
  and for no reader who cannot see the channel.
- D3. `diff` of a store against an identical copy prints "no change" and exits 0; its
  `--json` is byte-stable across runs.
- D4. Each refusal in "Commands" exits 1 with its reason and writes nothing.
- D5. Replaying the overlay file into a fresh copy of the base store and running
  `extract` gives the same `diff` as the original `say` session.

## Test seams (for the test author)

- `diff_selections` on hand-built `Selection` values: added, removed, moved by section,
  moved by rank, unchanged, and the JSON order.
- `make_event` on the Kestrel world: ts conversion in the world zone, the uniqueness
  suffix, thread linkage, each refusal.
- The two commands through typer's `CliRunner` on a fixture store in `tmp_path`: D1, D3,
  D4. D2 and D5 need one recorded cassette and run in replay mode.

## Out of scope

- Posting into a real Slack workspace (`--live`): the seeder, M5.
- Feedback buttons and App Home actions from the CLI.
- `--json` on `digest run`: add when a caller needs it.

## Open questions

- Q1. Should `diff` default to all readers or require `--user`? Plan: all readers, since
  the demo's point is that one message lands differently per role.
- Q2. Where do demo overlays live? Plan: `data/world/kestrel/overlays/<name>.jsonl`,
  committed with their cassettes.

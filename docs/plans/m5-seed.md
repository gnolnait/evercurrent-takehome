# Plan: M5 slice, the corpus seeder (`digest seed`)

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`). Depends on PR #52
(`digest post`): the seeder reuses its Slack token loading, its error text rules and its
`slack_sdk` client. The code branch starts from `main` after #52 merges. The link change
in `digest/daily/slack.py` that reads the seed map is a separate unit (see Out of scope).

## Goal

Put the Kestrel K2 corpus into a real Slack workspace, so that a digest can link to real
messages, channels and files that open in the Slack client.

SPEC section 9 (`digest seed --world <name>`) asks for persona display names, threads,
edits and deletes, a pace, and the world timestamp in each message's metadata, which
live ingest reads back as `ts`. Live ingest is not built. This slice writes a local
**seed map** from each world message to its Slack message instead. The renderer reads
the map to build links. Live ingest can replace the map later; the metadata is written
now, so that path stays open.

A spike on 2026-10-13 in the Scratch workspace proved the parts this plan relies on:
`chat:write.customize` posts under a persona name, `chat.getPermalink` returns a link
with `thread_ts` and `cid` for a thread root, and a link to a seeded message gets a
message preview (`is_msg_unfurl`) in a bot DM.

## Command

```
digest seed --world kestrel [--map PATH] [--until-day N] [--pace SECONDS]
            [--create-channels] [--dry-run]
```

- Reads `SLACK_BOT_TOKEN` as `digest post` does (environment, then `.env`).
- `--map`: the seed map file. Default `.slack-seed/<team_id>-<world>.json`. The folder
  is gitignored, because the map is specific to one workspace.
- `--until-day N`: seed only the events up to the end of world day N (default: all).
- `--pace SECONDS`: the wait between two posts (default 1.2). A rate-limited call waits
  for Slack's `Retry-After`, then tries again, as `digest post` does on its DM path.
- `--create-channels`: create a missing channel. Without it, a missing channel stops
  the command before any post, and the message lists the missing names.
- `--dry-run`: no write to Slack. Print the counts the run would post, by channel, and
  the missing channels and scopes.

Exit 0 when every selected event is in the map; exit 1 with the reason otherwise.

## Behaviours

B1. **Channels.** A world channel maps to the Slack channel with the same name. Public
channels are found with `conversations.list`; private ones need `groups:read`. The bot
joins a public channel it is not in (`conversations.join`). It cannot join a private
channel: a private channel must exist with the bot invited, or be created by the bot
with `--create-channels` (`groups:write`). The map records `world_channel -> slack_id`.

B2. **Order.** Events post in world order: by `ts`, then by input order. A reply posts
after its thread root, into the root's Slack thread (`thread_ts` from the map). A reply
whose root is not in the map stops the run with the event's world id; the input data is
broken, not Slack.

B3. **Persona.** Each post sets `username` to the persona's name and an `icon_emoji` per
role (one fixed table). A bot persona (`B0PLM01`, ...) posts under its persona name too.

B4. **Text.** The text is the event text with each `<@U...>` mention replaced by
`@<persona name>`: personas have no Slack accounts, so a real mention would point at
nobody. No other change: the extractor reads the same text from Slack as from the file.

B5. **Metadata.** Each post carries
`metadata = {event_type: "downstream_world_event", event_payload: {world: <name>,
channel: <world channel>, ts: <world ts>, thread_ts: <world thread_ts>}}`.

B6. **Files.** An event with `files` posts its text first (B2 to B5). Then each file is
uploaded into that message's thread with `files_upload_v2`, as a small placeholder PDF
whose page shows the file name and the line "Kestrel K2 test data". The map records each
file's Slack `permalink` by world file name. Without `files:write` the command posts the
text with one line per file (":paperclip: <name>"), records no file, and says so once.

B7. **Edits and deletes.** An event with `edited: true` updates its earlier post with
`chat.update` (the new text). `deleted: true` removes it with `chat.delete` and records
the delete in the map. The Kestrel corpus has none today; the behaviour is for other
worlds, and tests cover it with fake events.

B8. **Reactions.** Not seeded. Only the bot can react, once per emoji per message, so a
seeded reaction would not show the persona who reacted. The map records the count of
skipped reactions, and the summary line names it.

B9. **Resume.** The map is written after each successful post, atomically (write a
temporary file, then replace). A second run skips every event already in the map, so a
stopped run continues where it stopped and posts nothing twice. A map from another
workspace (`team_id`) or another world stops the run.

B10. **Secrets.** No output, log record or exception text holds the token, as in
`digest post` (its `_slack_failure` rules). The map holds ids, timestamps and
permalinks only.

## Seed map (JSON)

```json
{
  "version": 1,
  "team_id": "T...",
  "workspace_url": "https://<name>.slack.com",
  "world": "kestrel",
  "channels": {"C0ENGEF": "C0123ABCD"},
  "messages": {"C0ENGEF:1791993300.010026": {"channel": "C0123ABCD", "ts": "1791919171.279789",
               "thread_ts": "1791919171.279789", "deleted": false}},
  "files": {"PN-11020_FAI_photos.pdf": "https://<name>.slack.com/files/U.../F..."},
  "skipped_reactions": 72
}
```

`digest/seed.py` owns the schema (a pydantic model) and the only reader and writer of
the file.

## Code layout

- `digest/seed.py`: `SeedMap` (model, `load`, `save`), `plan_seed(world, events, map) ->
  list[Post]` (pure: order, text, metadata, skips), `seed(world, events, client, map,
  *, pace, sleep) -> SeedReport` (drives the client), `placeholder_pdf(name) -> bytes`.
- `client` is a small protocol with the eight calls the seeder uses (`auth_test`,
  `list_channels`, `join`, `create_channel`, `post`, `update`, `delete`, `upload_file`).
  The CLI passes an adapter over `slack_sdk.WebClient`; tests pass a fake. `sleep` is
  injected, so tests do not wait.
- `cli.py`: a `seed` command in its own block (the driver unit also edits `cli.py`).
  It reuses #52's token and error helpers.
- The import-linter layers contract gains `digest.seed` beside `digest.replay`.

## Slack app scopes

Present: `chat:write`, `chat:write.customize`, `chat:write.public`, `channels:read`,
`channels:join`. To add for this slice, all marked "seeding only" in the manifest:
`groups:read` (find private channels), `files:write` (B6), and, for
`--create-channels` only, `channels:manage` and `groups:write`. The user edits the
manifest and reinstalls the app; the seeder never changes app settings. SPEC already
says to remove the seeding scopes before a deployment outside the test workspace.

## Acceptance

A1. On a fake client, the Kestrel corpus seeds with 132 posts in world order; each
reply's `thread_ts` is its root's Slack ts; each post's metadata names its world ts.
A2. A second run on the same map makes no call that writes. A run stopped after post k
and run again posts the rest exactly once.
A3. A missing public channel without `--create-channels` stops before any post and
names it; with the flag, the channel is created first. A private channel the bot
cannot see stops the run with the same message.
A4. A rate-limited post waits for `Retry-After` (through the injected `sleep`) and
succeeds; no event posts twice.
A5. Mentions become `@<name>`; no `<@` remains in posted text.
A6. A file event posts its text, then uploads each file into its thread, and the map
holds each file's permalink; without `files:write` the fallback line is posted and the
summary says so.
A7. An edit event calls `chat.update` on the mapped message; a delete calls
`chat.delete` and marks the map entry.
A8. A map for another `team_id` or world stops the run before any call that writes.
A9. No output or exception text holds the token (the `digest post` test pattern).
A10. Live check (manual, recorded in the PR): seed day 1 into Scratch with `--pace 1.2`,
open one thread and one file link in Slack, and read one message's metadata back with
`conversations.history`.

## Test seams (for the test author)

- Tests call `plan_seed` and `seed` with a fake client that records calls and returns
  Slack-shaped responses, a tmp map path and a no-op `sleep`. Events come from small
  hand-made lists, plus one test over the real `data/world/kestrel/events.jsonl` for A1.
- The CLI tests use `CliRunner` with the fake client patched in, as
  `tests/test_slack_post.py` does for `digest post`. No test touches the network.

## Out of scope

- Links in the digest from the map (`<#C>`, permalinks, file links, previews): the
  next unit, on `digest/daily/slack.py`, after #52 and the layout rework.
- Live ingest reading the seeded messages back (M5).
- Pacing by world day (`one world day per N minutes`): `--pace` is per post; per-day
  pacing is SPEC M5 stretch.
- Real user accounts for personas, and reactions by persona.

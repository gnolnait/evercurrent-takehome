# Plan: M5 slice, the corpus seeder (`digest seed`)

Status: v0.4, 2026-10-13. Process stage 1 (`docs/process.md`). v0.3 applied the first
plan review (Codex, `-p review`) and the advisor review; v0.4 applies the second Codex
review (seven medium findings, none high). PR #52 (`digest post`) is
merged; the seeder reuses its token loading and its error text rules, not its client
retry setup (see B11).

## Goal

Put the Kestrel K2 corpus into a real Slack workspace, so that a later unit can link a
digest to real messages, channels and files that open in the Slack client.

SPEC section 9 (`digest seed --world <name>`) asks for persona display names, threads,
edits and deletes, a pace, and the world timestamp in each message's metadata, which
live ingest reads back as `ts`. This slice is a **partial** implementation of that line:

- It writes the metadata, so live ingest can use it later.
- Live ingest is not built. The slice writes a local **seed map** from each world message
  to its Slack message instead. A later renderer unit reads the map (see Next units).
- Edits and deletes are deferred (B7). Per-day pacing is SPEC M5 stretch and is deferred.

A spike on 2026-10-13 in the Scratch workspace showed: `chat:write.customize` posts
under a persona name; `chat.getPermalink` returns a link with `thread_ts` and `cid` for a
thread root; a link to a seeded message gets a message preview (`is_msg_unfurl`) in a
bot DM.

## Command

```
digest seed --world kestrel [--map PATH] [--until-day N] [--pace SECONDS] [--dry-run]
```

- Reads `SLACK_BOT_TOKEN` as `digest post` does (environment, then `.env`), with the
  same refusals for an unset or malformed token.
- `--map`: the seed map file. Default `.slack-seed/<team_id>-<world>.json`. The folder
  is added to `.gitignore`: the map is specific to one workspace.
- `--until-day N`: seed only events whose world `ts` falls on or before world day N in
  the world's time zone (`World.date_of(N)`, end of day inclusive). N must be 1 to
  `world.days`, else the command refuses. A later run with a larger N extends the map.
- `--pace SECONDS`: the wait between two posts (default 1.2, at least 0).
- `--dry-run`: makes no call that writes. It prints, in this order: one line per world
  channel with posts or uploads to make, `<channel name>: <P> posts, <F> files`, using the
  world channel's name; then one line per blocker (`missing channel: <name>`,
  `visibility mismatch: <name>`, `missing scope: <scope>`). Scopes come from the
  `x-oauth-scopes` header of `auth.test`. Exit 0 when nothing blocks a real run, else 1.

A real run exits 0 when every selected event and every one of its files is complete in
the map, else 1 with the reason. The last line is a summary (see B12).

## Behaviours

B1. **Channels.** `conversations.list` with `types=public_channel,private_channel`,
paginated until `next_cursor` is empty. A world channel maps to the Slack channel with
the same name **and** the same visibility (`private` in `world.yaml`). A name with the
other visibility stops the run before any post ("visibility mismatch"). A private world
channel the bot cannot see stops the run as "missing or not shared with the bot". The
bot joins a public channel it is not in (`conversations.join`) before its first post
there. The seeder never creates a channel. The map records `world_channel -> slack_id`.

B2. **Order.** Events post in world order: by `ts`, then by input order. A reply posts
into its root's Slack thread (`thread_ts` = the root's Slack `ts` from the map). `seed`
checks every reply before its first write: the reply's root must be in the saved map or
earlier in the same run's posts. If not, the run stops before any write and names the
reply's world key. (`plan_seed` cannot see the map, so it does not check this.)

B3. **Persona.** Each post sets `username` to the persona's name and `icon_emoji` from
this table: ME `:gear:`, EE `:zap:`, FW `:floppy_disk:`, supply_chain `:package:`, EM
`:clipboard:`, PM `:calendar:`, and `:robot_face:` for a bot persona (no role).

B4. **Text.** The event text, with each `<@U...>` mention of a persona replaced by
`@<persona name>` (personas have no Slack accounts). An unknown user id becomes
`@unknown`. No other change.

B5. **Metadata.** Each post carries
`metadata = {event_type: "downstream_world_event", event_payload: {world: <name>,
channel: <world channel>, ts: <world ts>, thread_ts: <world thread_ts>}}`.

B6. **Files.** An event with `files` posts its text first (B2 to B5). Then each file is
uploaded, one at a time, with `files_upload_v2(channel=<slack channel>,
thread_ts=<the event's Slack thread root ts>, filename=<world name>, content=<bytes>)`.
The bytes are a placeholder: a one-page PDF that shows the file name and the line
"Kestrel K2 test data" for a `.pdf` name, and a UTF-8 text file with the same line for any
other name (the name is kept, for example `BLD-DVT1_build_log_2026-10-23.xlsx`). The map
records the file's `permalink` from the response's `file` object; the adapter returns
only that value. Live spike, 2026-10-13, slack_sdk in the project's `slack` extra: the
response keys are `ok`, `file` and `files` (one entry); `file.permalink` is set at once
(`https://<ws>.slack.com/files/<bot user>/<file id>/<lower-case name>`), while
`file.shares` is empty and `file.filetype` is blank, because Slack processes the file
after the response. So the seeder needs no `files.info` call.

B7. **Edits and deletes: deferred.** The Kestrel corpus has none. An event with
`edited: true` or `deleted: true` in the selection stops the run before any write and
names the event. Reason: Slack documents that `chat.delete` cannot delete a message
posted under a custom name, which needs its own spike.

B8. **Reactions: not seeded.** Only the bot can react, once per emoji per message. The
summary states the number of reactions in the selected events that were not seeded; the
number comes from the selection, so a resumed run reports the same number.

B9. **Resume (checkpoint, not exactly once).** The map is saved after each acknowledged
post and after each acknowledged upload, atomically (write a temporary file in the same
folder, then replace). A message entry and each file entry have their own `done` state.
A second run skips every done operation and does the rest, so a missing upload is tried
again even when its message is done. **Limit:** when Slack accepts a call and the process
stops before the map is saved, the next run repeats that one operation. A run prints this
limit when it finds a map that a previous run did not finish (`complete: false`).
Read-back by metadata to close the gap is deferred.

B10. **Map identity.** A map whose `team_id` or `world` differs from the run stops the
run before any write.

B11. **Rate limits and transfer errors.** The adapter builds a `slack_sdk.WebClient`
with **no** retry handler. A Web API error with HTTP status 429 (a `SlackApiError`
whose response has a `Retry-After` header) becomes `RateLimited(retry_after)`. The
seeder catches it, calls the injected `sleep(retry_after)` and tries the same operation
again, up to 5 times per operation, for every call (`auth_test`, listing, join, post,
upload). After 5, the run stops. Any other error, including a `SlackRequestError` from
the byte-transfer step inside `files_upload_v2`, stops the run at once with no retry.
In both cases the map keeps what is done, and the message says to run the command again.

B13. **Pace.** Before each write call (post or upload) except the first write of the
run, the seeder calls `sleep(pace)`. It makes no such call when `pace` is 0. A retry wait
(B11) is a separate `sleep` call and does not replace the pace wait. Reads (`auth_test`,
listing) and joins are not paced.

B14. **Scopes.** A real run checks the granted scopes (the `x-oauth-scopes` value that
`auth_test` returns) before its first write. Required: `chat:write`,
`chat:write.customize`, `channels:read`, `channels:join`, `groups:read`, and
`files:write` when the selection holds a file. A missing scope stops the run before any
write and names it. The history scopes in "Slack app scopes" are for the manual check
only and are not checked. `--dry-run` reports the same missing scopes.

B12. **Output and secrets.** Progress lines name world events and counts only. The
summary line: "seeded P posts and F files in C channels; R reactions not seeded; map
<path>". P and F count the posts and uploads acknowledged **in this run**; C counts the
distinct Slack channels those writes went to; R counts the reactions on the selected
events (B8). So a rerun on a complete map reports 0, 0, 0 and the same R. No output, log record or exception text holds the token (the `digest post`
rules: a Slack error shows its error code only, any other error its type only).

## Seed map (JSON)

```json
{
  "version": 1,
  "team_id": "T...",
  "workspace_url": "https://<name>.slack.com",
  "world": "kestrel",
  "complete": false,
  "channels": {"C0ENGEF": "C0123ABCD"},
  "messages": {
    "C0ENGEF:1791993300.010026": {"channel": "C0123ABCD", "ts": "1791919171.279789",
                                   "thread_ts": "1791919171.279789"}
  },
  "files": {
    "C0EXTCM:1792164600.010061/PN-11020_FAI_photos.pdf": {"permalink": "https://..."}
  }
}
```

A message key is `<world channel>:<world ts>`; a file key adds `/<file name>`. An entry
exists only when its operation is done. `complete` is true after a run finishes every
selected operation.

## Interface (stubs land before tests)

```python
# digest/seed.py
class RateLimited(Exception):
    retry_after: float

class SlackSeedClient(Protocol):
    def auth_test(self) -> AuthInfo: ...                  # team_id, url, scopes
    def list_channels(self) -> list[SlackChannel]: ...    # all pages; id, name, is_private, is_member
    def join(self, channel_id: str) -> None: ...
    def post(self, channel_id: str, text: str, *, thread_ts: str | None, username: str,
             icon_emoji: str, metadata: dict[str, object]) -> str: ...   # returns Slack ts
    def upload(self, channel_id: str, thread_ts: str, filename: str, content: bytes) -> str: ...
                                                          # returns the file permalink

class SeedMap(BaseModel):  # the JSON above
    @classmethod
    def load(cls, path: Path) -> SeedMap | None: ...
    def save(self, path: Path) -> None: ...                # atomic

@dataclass(frozen=True)
class Post:            # one planned message
    key: str           # "<world channel>:<world ts>"
    world_channel: str
    root_key: str      # the key of its thread root (itself for a root)
    text: str          # after B4
    username: str
    icon_emoji: str
    metadata: dict[str, object]
    files: tuple[str, ...]
    reactions: int

@dataclass(frozen=True)
class SeedReport:
    posts: int
    files: int
    channels: int
    reactions_not_seeded: int
    complete: bool

def plan_seed(world: World, events: Sequence[Event], until_day: int | None) -> list[Post]: ...
    # pure: selection, order, text, metadata; raises ValueError for a B7 event or a bad day
def seed(world: World, posts: Sequence[Post], client: SlackSeedClient, map_path: Path,
         *, pace: float, sleep: Callable[[float], None], dry_run: bool) -> SeedReport: ...
    # owns all map reads and writes
def placeholder_file(name: str) -> bytes: ...
```

`cli.py` gets a `seed` command in its own block (the driver unit also edits `cli.py`).
It builds the adapter (`SlackWebSeedClient` in `digest/seed.py`, over `WebClient`),
calls `plan_seed`, then `seed` with `time.sleep`. The import-linter layers contract gains
`digest.seed` beside `digest.replay`.

## Slack app scopes

Present: `chat:write`, `chat:write.customize`, `chat:write.public`, `channels:read`,
`channels:join`, `channels:history`. To add for this slice, marked "seeding only" in the
manifest: `groups:read` (list private channels), `files:write` (B6), `groups:history`
(the live check reads metadata back from the private channel). The seeder never changes
app settings. Creating the eight channels needs `channels:manage` and `groups:write` for
a one-off setup script, or the user creates them by hand; the seeder itself needs neither.

## Before tests

1. Done 2026-10-13: scopes added and the app reinstalled; the eight channels exist in
   Scratch with their world visibility, and the bot and the user are in each (B1).
2. Done 2026-10-13: the upload spike; the response shape is under B6.
3. Done 2026-10-13: a second Codex review (fresh thread); v0.4 applies it.

## Acceptance

A1 (B2, B3, B5). On a fake client, the Kestrel corpus seeds 132 posts in world order;
each reply's `thread_ts` is its root's Slack ts; each post has the persona name, the
emoji from the table (`:robot_face:` for a bot persona) and metadata with its world ts.
A2 (B9). A second run on a complete map makes no write call. A run stopped after post k
posts the rest once; a run stopped between the two uploads of the FAI event uploads only
the missing file. A map with `complete: false` prints the resume limit line.
A3 (B1). A missing channel, an inaccessible private channel and a visibility mismatch
each stop the run before any write and name the channel. A public channel the bot is not
in is joined before its first post. Listing follows `next_cursor`.
A4 (B11). A `RateLimited(3)` on a post, an upload and the channel list each lead to
`sleep(3)` and one more try; the operation is done once. Six in a row stop the run with
the map saved.
A5 (B4). No posted text holds `<@`; a known persona id becomes `@<name>`.
A6 (B6). A file event posts its text, then uploads each file into the thread root with
the world file name; the map holds each permalink; the `.xlsx` name is kept.
A7 (B7). An edit or delete event in the selection stops `plan_seed` with the event's
world id, and no write happens.
A8 (B10). A map for another `team_id` or world stops the run before any write.
A9 (B12). No output or exception text holds the token.
A10 (Command). `--until-day 1` selects exactly the day-1 events in the world's time
zone; 0 and `days + 1` are refused; a later run with `--until-day 2` adds only day 2.
A11 (Command). `--dry-run` makes no write call, prints counts by channel and the missing
scopes, and exits 1 when a channel is missing.
A12 (B8). The summary states the reactions in the selection that were not seeded, and a
resumed run states the same number.
A13 (B11, adapter). `SlackWebSeedClient` over a fake `WebClient`: a `SlackApiError`
with status 429 and `Retry-After: 3` becomes `RateLimited(3)`; a `SlackRequestError`
from an upload is not retried and stops the run; a post sends `username`, `icon_emoji`,
`metadata` and `thread_ts`; an upload returns `file.permalink`.
A14 (B2). A reply whose root is in the saved map posts into the mapped thread; a reply
whose root is neither in the map nor earlier in the run stops `seed` before any write.
A15 (B12). Summary counts: a first full run on a fake client reports 132 posts, 5 files
and 8 channels; a rerun on the complete map reports 0, 0, 0; a run that resumes only the
missing FAI upload reports 0 posts, 1 file, 1 channel. R is the same in all three.
A16 (B13). With `pace=1.2`, the recording `sleep` gets 1.2 before every write but the
first, in order with the writes; with `pace=0` it gets no pace call; a retry adds its
own `sleep(retry_after)` call before the retried write.
A17 (B14). A missing `files:write` with a file in the selection, or a missing
`chat:write.customize`, stops a real run before any write and names the scope;
`--dry-run` lists it and exits 1.
A18 (live, manual, recorded in the PR). With the bot in every channel, seed through day
4 into Scratch with `--pace 1.2`. Open one thread and one file link in Slack. Read back
with `include_all_metadata=true` the metadata of one public root and of the first root in
`kestrel-leads` (world day 4).

## Test seams (for the test author)

- `plan_seed` and `seed` with a fake `SlackSeedClient` that records calls, returns
  Slack-shaped values, and can raise `RateLimited` or stop the run at call k; a tmp map
  path; a recording `sleep`.
- Events from small hand-made lists, plus one test over `data/world/kestrel/events.jsonl`
  for A1.
- The adapter with a fake `WebClient` for A13. The CLI with `CliRunner` and the client
  factory patched, as `tests/test_slack_post.py` does. No test touches the network.

## Next units

1. **Links from the map** (small, next): `digest post --seed-map PATH` swaps each
   evidence permalink for the mapped Slack message, writes a channel as `<#C...>`, and
   links a file name to its mapped permalink. The visibility filter still decides what
   a reader sees; the map only changes where a shown link points. With no map, the output
   stays as it is. This unit gives the native links.
2. **Layout rework** (larger, after): the v5 layout in `notes/research/slack-layout/`
   (top message, one thread reply per section), with the digest-window line.

## Out of scope

- Live ingest reading the seeded messages back (M5).
- Edits, deletes, reactions by persona, real user accounts for personas.
- Per-day pacing (SPEC M5 stretch); read-back recovery of the resume gap (B9).

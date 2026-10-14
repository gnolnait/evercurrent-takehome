# Plan: M5 slice, reseed edited messages in place (`digest seed --reference`)

Status: v0.2, 2026-10-13 (first plan review applied). Process stage 1 (`docs/process.md`). Builds on `digest seed`
(`docs/plans/m5-seed.md`, merged in #70). The demo room (the "M1" session's work) calls
this to push messages edited in the room into the seeded Slack workspace; the interface
below was agreed with that session.

## Goal

When a working copy of the corpus edits a message that `digest seed` already posted, a
second run updates that Slack message in place, posts the copy's new messages, and
reports the messages the copy removed, without deleting anything.

A spike on 2026-10-13 in Scratch showed that `chat.update` on a post made with a custom
`username` keeps that persona name; the API returned the message with an `edited` field.
Slack documents that a bot message may not show an "edited" label in the client, so the
plan promises the new text, not a visible label. `chat.update` keeps metadata when it is
omitted and takes new metadata when given; it is rate-limit Tier 3.

## Behaviours

R1. **Reference.** `seed` gains `reference: Sequence[Post] | None = None`: the posts that
Slack holds now, that is the corpus last seeded or pushed (`plan_seed` over the committed
`events.jsonl` before the first push; after a push, the corpus that push sent). With
`None`, the behaviour is exactly as today (every existing test stays green). The seeder
stores no text, so it compares against the reference only: a caller that passes the
committed corpus after a push cannot push a change back to the committed text.

R2. **Update.** With a reference, a post whose key is in the map, whose key is in the
reference, and whose `text` differs from the reference post's `text`, is updated:
`client.update(channel, ts, text, metadata=post.metadata)` with the map's `channel` and
`ts` for that key, for a root and for a reply alike (a reply edit updates the reply, not
its root). A post with the same text is skipped. A post whose key is in the map but not
in the reference is skipped (nothing to compare).

R2a. **Placement must match.** For every key in both `posts` and the reference, the two
posts must have the same `world_channel` and the same `root_key`. A mismatch raises
`SeedBlocked` naming the key, before any write, whatever the text. (A message moved to
another channel has another key, so it follows R3 and R4.)

R3. **Post.** A post whose key is not in the map is posted, as today (B2 to B6).

R3a. **Checks first.** Every check before the first write (B1, B2, B10, B14, B15, and
R2a) runs over `posts`, the mapped ones included, before any join, update, post or
upload. Reference posts do not satisfy a missing root and do not select channels.
`files:write` stays required when `posts` holds a file. So a later invalid reply stops the
run before an earlier update.

R4. **Removed.** A key that is in the reference and in the map, but not in `posts`, is
listed in `SeedReport.removed`, in reference order. It is never deleted; the summary
says so.

R5. **Files.** A file already in the map is never uploaded again. After an update, the
message's files that are missing from the map are uploaded, as today.

R6. **Pace and retry.** An update is a write: it is paced and retried as a post (B11,
B13), and counts toward "the first write of the run". Retries: up to 5 after the first
attempt; a sixth `RateLimited` in a row stops the run, five then a success pass.

R7. **Map.** The resume-limit line prints from the loaded map, before the map changes,
as today. After the checks and any join, the map is saved with `complete: false` just
before the run's first update, post or upload (B15). An update adds and changes no map
entry. The map is saved after each acknowledged post or upload, as today, and with
`complete: true` at the end of a finished run. Updates have no checkpoint: a stopped run
repeats them on the next run against the same reference.

R8. **Report and output.** `SeedReport` gains `updated: int = 0` (updates acknowledged in
this run) and `removed: tuple[str, ...] = ()`. Only when a reference is given (an empty
reference included), the summary line becomes
`seeded P posts, U updates and F files in C channels; R reactions not seeded; map <path>`
(C counts the channels of posts, updates and uploads), followed by one line
`not deleted: <key>` per removed key, and a dry run prints, after the channel lines and
before the blockers, `updates: <n>` and one `removed: <key>` line per removed key; it
makes no write. Without a reference, every output line is exactly as today.

R9. **CLI.** `digest seed` gains `--reference PATH`, an events.jsonl read as the world's
corpus is read, and `--events PATH`, the corpus to seed (default: the world's
`events.jsonl`). `--events` alone seeds that corpus as today (no reference, no updates).
With `--reference`, both files go through `plan_seed` with the same `--until-day`, and
the reference posts go to `seed(..., reference=...)`. Without either, the command is as
today.

R10. **Identity.** A message is the same message when its world key
`<world channel>:<world ts>` is the same. The edited copy keeps each message's channel
and ts; a new message has a new ts.

## Interface

```python
# digest/seed.py
class SlackSeedClient(Protocol):        # gains one method; SlackWebSeedClient implements it
    def update(self, channel_id: str, ts: str, text: str, *,
               metadata: dict[str, object]) -> None: ...   # chat.update; 429 -> RateLimited

@dataclass(frozen=True)
class SeedReport:
    ...                                 # existing fields unchanged
    updated: int = 0
    removed: tuple[str, ...] = ()

def seed(world, posts, client, map_path, *, pace, sleep, dry_run,
         reference: Sequence[Post] | None = None) -> SeedReport: ...
```

## Acceptance

- RS1 (R1). With `reference=None`, every existing seeder test passes unchanged.
- RS2 (R2). A changed text calls `update` once with the map's channel and ts, the new text
  and the post's metadata; an unchanged text calls nothing; a key not in the reference
  calls nothing.
- RS3 (R3). A new key in the copy is posted into the right thread, mixed with updates in
  world order.
- RS4 (R4). A removed key appears in `removed` and in a `not deleted:` line; no delete call
  exists in the protocol.
- RS5 (R5). An updated message whose files are all mapped uploads nothing; an updated
  message with a missing file uploads only that file.
- RS6 (R6). Pace before an update that is not the run's first write; a `RateLimited` on
  `update` waits and retries; five then a success pass; six in a row stop the run.
- RS7 (R7). A run that only updates leaves the map's messages and files unchanged, saves
  `complete: false` before the first update and `complete: true` at the end; a run stopped
  during updates leaves `complete: false`.
- RS7a (R2a, R3a). A key whose channel or root differs between posts and reference stops
  the run before any write; a later reply with no root stops the run before an earlier
  update.
- RS8 (R8). The summary and the dry-run lines, exactly as written; a dry run makes no
  write.
- RS9 (R9). The CLI with `--reference` and `--events` reads both files; without them, the
  output is as today. The adapter's `update` sends `channel`, `ts`, `text` and `metadata`
  to `chat_update`, and maps a 429 to `RateLimited`.
- RS10 (live, manual, recorded in the PR). Edit one seeded message's text in a copy of
  `events.jsonl`, run `digest seed --world kestrel --events <copy> --reference
  data/world/kestrel/events.jsonl`, and see the message edited in Scratch with its persona
  name.

## Test seams

The fake `SlackSeedClient` of `tests/test_seed.py` gains `update`; tests build posts with
`plan_seed` or by hand, and a saved map in `tmp_path`. No test touches the network.

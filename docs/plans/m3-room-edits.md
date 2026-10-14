# Plan: M3 unit 10, message edits and reseed in the demo room

Status: v0.2, 2026-10-13, after the Codex plan review. Process stage 1 (`docs/process.md`), the lighter
variant the user chose on 2026-10-13: Codex writes the tests, an Opus subagent writes the
code, every test is RED-GREEN proven, one review lane per commit, at most one fix round.
Builds on `docs/plans/m3-ui.md` (units 8 and 9, merged) and `docs/plans/m3-driver.md`
(unit 7, merged). The reseed step depends on a seed update function that the session
owning `src/digest/seed.py` adds; this plan names it `reseed` and fixes only what it
needs from it.

## Goal

The user asked on 2026-10-13 for the room to edit the messages themselves and to reseed
the Slack workspace from the page. Four steps, each one demoable without the next:

1. Scenario buttons: the two recorded unit 7 scripts (`d2-bracket-delay`,
   `d3-gate-delay`) post their message into the room. Offline, from committed cassettes.
2. Message what-ifs: edit a message's text or file names, or post a new message as a
   persona, and see the three digests and the diff change. A message change needs a
   model call unless its cassettes exist: `digest serve --record` allows live calls with
   the key; without it a change with no cassette is refused with a plain reason. Three
   or four curated edits are recorded tonight and committed, so they replay offline.
3. Save: write the room's message changes into a working copy of the corpus,
   `.room/kestrel/` (gitignored), which keeps the world name `kestrel`.
4. Reseed: from the page, a dry run shows what a reseed of the saved copy would do in
   the Slack workspace (messages to update, messages to post, messages removed and not
   deleted); a second button runs it. Never a delete.

Cut times: if step 2 is not merged by 23:00 PT on 2026-10-13, steps 3 and 4 move to after
the interview; if step 3 is not green by 01:00 PT, step 4 does. A live reseed during the
interview adds Slack and the network to the demo; the rehearsal decides whether to show it.

## The rule that changes: messages now reach extraction

Units 8 and 9 kept every edit away from extraction. A message change cannot be: the
extraction request carries the thread's messages, so a changed message is a new request.
The room therefore keeps a room store: a `Store.copy_to` of the base store, plus the
room's message changes, extracted again for day 6. World edits (units 8 and 9) still
apply only to the world that selection reads, and still never reach a request.

## Message changes

```python
class EditMessage(Frozen):   kind: Literal["edit_message"]; channel_id: str; ts: str; text: str; files: tuple[str, ...]
class PostMessage(Frozen):   kind: Literal["post_message"]; user_id: str; channel_id: str; text: str; thread_ts: str | None
class RunScenario(Frozen):   kind: Literal["run_scenario"]; name: Literal["d2-bracket-delay", "d3-gate-delay"]
```

- M1. A message change applies to the room store in log order after the last `Reset`.
  The k-th message change (1-based) since the reset is stamped at 2026-10-19 18:00 plus
  k minutes in the world's zone, so the same log gives the same requests and a curated
  edit replays offline when it is the first message change after a reset.
- M2. `EditMessage` writes the stored event again with the same `(channel_id, ts)`, the
  new `text` and `files`, `edited=True`, and `event_ts` = the stamp of M1. The message
  must exist in the base store and be on or before day 6. A bot's message may be edited.
- M3. `PostMessage` builds the event with `make_event` (unit 7) at the stamp of M1, against
  the recorded world, so the unit 7 rules apply: a human persona, a channel the persona is
  in, a thread root in that channel.
- M4. `RunScenario` drops every message change before it (it starts the room store again
  from the base store) and posts its script line at the script's own time (17:30 or
  17:40), as unit 7's `say --script` does; a later message change follows M1.
- M5. After each message change the room runs extraction for day 6 on the room store, as
  `digest say` then `digest extract` do (meta `extract_until` set to the day before).
  Replay mode reads cassettes; a miss refuses the change with "this message change is not
  recorded; start digest serve --record with OPENROUTER_API_KEY to try it live", and the
  state stays as it was (R4 of `m3-ui.md`). `--record` builds the gateway backend from
  `Settings` and records the miss into `data/cassettes/`.
- M6. Selection, diffs, reworded lines, `last_effect` and owners read the room store when
  message changes exist, else the base store, with the room world, as before.
- M7. File names: `EditMessage.files` replaces the message's file names. The extraction
  prompt carries file names (`src/digest/extract/prompts.py:81`, `:174-177`), so a file
  edit is a message change like a text edit: it needs its cassette or a live call, and it
  can change the digest. The reseed does not re-upload files, so in Slack a file edit
  shows only on a fresh seed; the page says so.
- M8. Known limit: an edited message keeps the claims extraction drew from its earlier
  text, because the merge keeps the claims of a message that is still cited
  (`src/digest/extract/verify.py:246`). Editing "Rev C" to "Rev D" in place therefore
  shows both values. A value change is shown cleanly by a new post that states the new
  value, which supersedes through the ledger rules; the page says this next to the edit
  form. Fixing the merge is extraction work (M2), after the interview.

Curated edits (`data/world/kestrel/room-edits.yaml`, committed with their cassettes): a
name, a one-line description and one message change each. Picked at recording time from
these candidates, each a new post (M8): the PLM bot's feed posts PN-11020 Rev D; Hamid
answers the anodize PO ask in its thread with the PO number; Tobias closes the thermal
review in its thread; the CM withdraws its bracket blocker in a reply. The PLM post is a
bot persona, so it is recorded through `EditMessage` of a reply or a scenario line rather
than `PostMessage`, whichever the M3 persona rule allows; the recording step settles it. Each is recorded as the first message change after a reset.

## Save and reseed

- S1. Save writes `.room/kestrel/`: a copy of `data/world/kestrel/` whose `events.jsonl`
  holds the committed events with every `EditMessage` applied (text and files) and every
  `PostMessage` and scenario line added, in `ts` order. `golden.jsonl` is copied
  unchanged and the page says it no longer describes the copy.
- S2. Reseed calls the seed owner's `reseed` with the copy's planned posts, the committed
  events as the reference, the seed map and a client, first with `dry_run=True` (the page
  shows the counts and lines it reports), then, on a second button, for real. It updates
  messages whose text or files differ from the reference, posts messages the map does not
  hold, and reports, never deletes, messages missing from the copy. It reads
  `SLACK_BOT_TOKEN` only when `digest serve` runs with `--slack`.
- S3. Reseed is refused while the room has unsaved message changes, so Slack shows only a
  saved copy.

## Interface additions

`digest/room.py`: the three message changes join `Edit`; `Room(base, *, record: bool =
False, cassettes: Path = Path("data/cassettes"))`; `Room.save(path: Path) -> Path`;
`RoomState` gains `messages: tuple[MessageView, ...]` (every message in the room store up
to day 6, after the changes, by channel, thread, ts) and `message_changes: int`.

`MessageView(Frozen)`: `channel_id`, `channel_name`, `thread_ts`, `ts`, `user_id`,
`author` (persona name), `text`, `files`, `edited`, `posted` (true for a message the room
added).

`digest/web/app.py`: `create_app(room, *, reseed: ReseedLike | None = None)`; routes `POST
/save`, `POST /reseed?dry_run=true|false`. The page gains a Messages panel (each message
with an edit form, a post form, the scenario and curated buttons) and, when `reseed` is
given, a Seed panel.

`digest serve [--port] [--record] [--slack]`.

## Behaviours

B10.1 Each message change kind updates the room store as M2 to M4 state, and the digests
change as extraction of the recorded cassettes says: the `d2-bracket-delay` scenario gives
unit 7's D2 result for the room's readers; `d3-gate-delay` gives D3's.
B10.2 M1 stamps: the same log gives byte-equal room stores; a change applied after a
reset gets the same stamp as the first change of a fresh room.
B10.3 A change with no cassette in replay mode is refused with the M5 reason, and the state
and the room store are unchanged; no backend is built.
B10.4 Every curated edit in `room-edits.yaml` replays with no cassette miss as the first
message change after a reset.
B10.5 World edits still never reach a request: after message changes, a world edit builds
no model request.
B10.6 Save writes the copy as S1; saving twice gives byte-equal files; the committed corpus
is unchanged.
B10.7 Reseed through a fake `ReseedLike`: the dry run is shown, the real run needs the
second button, a reseed with unsaved changes is refused, and no delete is ever requested.
B10.8 The page escapes message text and file names, and keeps no link, script or external
request.

## Test seams

The day-6 store fixture of units 8 and 9; the committed scenario scripts and their
cassettes; a temporary cassette directory for B10.3; a fake `ReseedLike`. No test uses a
real key, the network or Slack.

## Open for the user

- Which three or four curated edits to record (the candidates above by default).
- Whether to show a live reseed in the interview, decided at the rehearsal.

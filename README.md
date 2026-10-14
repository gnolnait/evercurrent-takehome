# Downstream: a role- and phase-aware daily digest for hardware teams on Slack

Thesis: a digest is the diff between what the project knows and what you were last told,
plus what should have happened but didn't.

Downstream reads a team's Slack threads once, extracts structured items (decisions,
blockers, risks, asks, schedule, spec and BOM changes) with claims such as
`(PN-31010, rev) = C`, finds the checklist tasks and asks that went silent, and then
selects and renders a different digest for each reader.

What runs today is the second half of the thesis and the per-reader selection: absences
("Not happened"), must-see rules, role-aware ranking and a source for every line. The
first half, a ledger of what each reader was last told, is designed but not built; see
[Scope](#scope) for why.

## Run the demo

No API key is needed. The demo replays recorded model calls (cassettes) for a synthetic
ten-day robotics program, Kestrel K2, and prints three digests for day 6: a mechanical
engineer, a supply chain engineer and an engineering manager.

```bash
uv sync --all-extras --dev
uv run digest demo
```

Part of the mechanical engineer's digest (summaries and sources trimmed):

```text
Daily digest for Nadia Brenner (ME) - 2026-10-19
Since your digest of 2026-10-16
Phase: EVT since 2026-08-03. Next gate: G-TR1 Top-cover tooling and texture release (PN-51010), in 1 working day.

Risks
- PN-11020 chassis base built to stale Rev B drawing pack (PN-11020, V-SUNRISE, DOC-11020-CM, chassis) [must see]
  FAI photos and CMM for PN-11020 show the Rev B hole pattern. Sunrise is building from a stale drawing pack. ...
  source: https://kestrel-k2.slack.com/archives/C0EXTCM/p1792164600010061
- Top-cover DFM review open and blocking G-DVT1 (G-TR1, PN-51010, G-DVT1) [must see]
  The top-cover DFM review is still open and waiting on snap detail feedback. ...
  source: https://kestrel-k2.slack.com/archives/C0KPROG/p1792423800010077
```

The run takes about 5 s on a laptop; a test holds it under 60 s. A digest shows the sections that have lines (Needs you,
Not happened, Decisions, Risks, FYI), with a source link for every line. The last line
gives the pairwise item overlap of the three digests. `--day N` picks another world day
(1 to 10).

On Windows, clone into a short path such as `C:\dc`. The `anthropic` package installs
`anthropic/types/cache_miss_previous_message_not_found.py`; under a deep folder its full
path passes the 260-character limit, the file is not written, and the import fails.

If Windows reports "An Application Control policy has blocked this file", Smart App
Control has blocked the `digest.exe` launcher in the new venv. Run the same command
through Python instead: `uv run python -m digest demo`.

## Send the digests to Slack

`digest post` builds the same three digests and sends them to Slack. It needs the
`slack` extra (installed by `uv sync --all-extras`).

- As direct messages from the app's bot: `uv run digest post --dm <member id>`. Put the
  bot token in `.env` as `SLACK_BOT_TOKEN=xoxb-...`; the app needs the bot scopes
  `chat:write` and `im:write`. All three readers' digests go to that one person, each
  headed by its reader.
- To one channel through an incoming webhook: `uv run digest post`, with
  `SLACK_WEBHOOK_URL=https://hooks.slack.com/...` in `.env`.

Add `--day N` for another world day. The command prints no token or URL, and it stops at
the first message Slack refuses. Without a seed map, the source links point to the
synthetic workspace `kestrel-k2.slack.com`, which does not exist.

To make the links open real messages, seed the corpus into the same workspace first:

- `uv run digest seed --world kestrel` posts the 132 corpus messages into channels named
  as in `data/world/kestrel/world.yaml`, under persona names, with their threads and
  placeholder files. The channels must exist, with `kestrel-leads` private and the bot
  invited to it. The run writes a seed map, `.slack-seed/<team id>-kestrel.json`, and a
  second run posts nothing that the map already holds.
- `uv run digest post --dm <member id> --seed-map .slack-seed/<team id>-kestrel.json`
  then links each source to its seeded message, names the channels as Slack channel
  links, and links the files.
- `uv run digest seed --world kestrel --events <edited copy> --reference <corpus Slack
  holds now>` updates the seeded messages whose text changed, in place, and deletes
  nothing.

Seeding needs the bot scopes `chat:write.customize`, `channels:read`, `channels:join`,
`groups:read` and `files:write` besides the ones above. The plans are `docs/plans/m5-seed.md`,
`docs/plans/m5-links.md` and `docs/plans/m5-reseed.md`.

## Change who reads what

`uv run python -m digest serve` builds the day-6 store from the cassettes, then serves a
page at http://127.0.0.1:8000; open that URL in a browser. The page shows the three digests side by side; the presenter
changes a reader's role, a channel's membership or private flag, an entity's owner role,
a gate date or a task's owner role, and the page shows each reader's new digest and what
changed, with the reason for each line. Reset returns to the recorded world. An edit
changes who reads what and what counts as late; it does not change what the model
extracted, and it does not show what a reader was last told. It needs the `web` extra
(`uv sync --all-extras`).

## What a reader sees, and why

- Visibility fails closed: a line reaches a reader only when its thread is in a public
  channel or one they are a member of.
- Must-see lines are chosen by rule: an ask made of the reader, an open blocker on an
  entity they own, a schedule change on a gate they own, a checklist task of their role
  (or any, for the engineering manager) that went silent, and an ask they made or
  received that has no reply.
- Other lines are ranked by importance (type and severity) times the role's interest in
  the item type times personal relevance (1.0 for an owned entity, 0.8 for the reader's
  role, else 0.4). An item at 0.4 that shares an entity with a current item whose
  visible evidence cites one of the reader's own messages from the last three working
  days gets 0.8 (both values are defaults in `config/priors.yaml`), so the digest follows
  what the reader works on now; the line's reason names the shared entities. A line needs a score of at least 0.15, and each section shows at most
  5 ranked lines. Not happened holds only absences, which are must-see for the readers
  they name and are not ranked.

## How well extraction works

`reports/eval-2026-10-23.md` scores the recorded run against golden labels. The file
name carries the world's last day. Every rate is given with its n.

| Metric | Value | n |
|---|---|---|
| Thread recall | 0.967 | 30 |
| Claim recall | 0.629 | 35 |
| Claim precision | 0.537 | 41 |
| Absence recall | 0.516 | 31 |
| Detected absences that are false | 0.304 | 23 |
| Citation validity | 1.0 | 73 |

Claim values match exactly on revision, gate date, quantity and owner; other attributes
match on entity and attribute, and an advisory row reports the share of those that also
agree on value. Only claims in golden threads are scored, and a claim the golden labels do
not list counts as wrong, even a true one. The corpus is small and synthetic: 132
messages in 35 threads.

The report calls 0.304 the absence false-alarm rate. It is the share of detected absences
that are false (7 of 23), counted once per day and source, by checklist task or by ask
thread, before the per-reader filter. SPEC A6 defines the false-alarm rate over simulated
silences that were not stalls, and the eval does not compute it yet. Silence is counted
from task links that the extractor writes, so a missed link makes an active task look
silent.

## Scope

The belief ledger and stale-belief lines (what each reader was last told about a key)
are not built yet; the demo slice took the kill-criterion path in `SPEC.md`. SPEC section
9 set a rule before the build: if claim keying precision is below 0.8, cut the ledger to
the ranker plus absences. The eval has no direct keying metric. Its claim precision uses
stricter matching on golden threads only, so it does not establish whether keying
precision meets 0.8. The cut is a conservative scope decision under that uncertainty.

A digest covers what is new since the reader's previous digest, plus must-see items and
open blockers of any age; the header names the window and the project phase with the
next gate. An absence goes only to the readers it names: an unanswered ask to the asker
and the person asked. Delivery is not
recorded yet, so the caller states the previous digest (the demo uses the previous world
day).

Built: an MCP server (stdio) with nine read tools over the store, so you can ask your
own assistant about the project; see `docs/mcp.md`.

Designed, not built: the claims ledger and linking, stale beliefs, delivery records,
phase re-weighting, feedback (and the MCP `feedback` tool), live ingest from Slack, and
the web preview's profile and open-items pages. See `docs/plans/m3-demo.md` for the slice and its known gaps, and `docs/later.md`
for the ordered next steps and other deferred work.

## Develop

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run lint-imports
```

Everything runs offline by default (`DIGEST_MODE=replay`); the tests and CI never call a
model. Read `SPEC.md` (the contract), `docs/decisions.md` and `docs/process.md` before a
change.

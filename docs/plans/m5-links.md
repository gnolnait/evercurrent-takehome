# Plan: M5 slice, native links from the seed map (`digest post --seed-map`)

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`). Depends on PR #70
(`digest seed`, merged): its seed map maps each world message to its Slack message, and
each world file to its Slack permalink. This unit is "Next units 1" of
`docs/plans/m5-seed.md`. The layout rework is a later unit and is out of scope.

## Goal

When the digest is posted to the workspace that holds the seeded corpus, its source
links open the real messages, show the channel as a Slack channel link, and link the
files that a source message carries. With no map, the output stays as it is.

## Command

```
digest post [--day N] [--dm USER_ID] [--seed-map PATH]
```

- `--seed-map PATH`: a seed map written by `digest seed` (`digest.seed.SeedMap`). The
  command loads it before it builds a digest. A missing or unreadable file, or a map for
  another world than the demo world, exits 1 with the reason and posts nothing.
- On the DM path (`--dm`), the command also checks that the map's `team_id` equals the
  bot token's workspace (`auth.test`), else it exits 1 and posts nothing. The webhook path
  cannot read its workspace, so it does not check.
- Without `--seed-map`, `digest post` does exactly what it does today.

## Behaviours

L1. **Mapped source link.** For each evidence entry of a line, the key
`<entry.channel_id>:<entry.message_ts>` is looked up in the map's `messages`. When found,
the link points at the Slack message:
`<workspace_url>/archives/<channel>/p<ts without the dot>`, plus
`?thread_ts=<thread_ts>&cid=<channel>` when the message is a reply (its `ts` differs from
its `thread_ts`). The link text stays `source`.

L2. **Channel link.** After a mapped source link, the line adds ` in <#<slack channel>>`,
once per distinct channel of the line's mapped sources, so Slack shows the channel name
as a link.

L3. **File links.** For each mapped source message, every map `files` entry whose key
starts with `<message key>/` adds a link `<permalink|<file name>>` after that source.
File names are escaped as other text (`&`, `<`, `>`).

L4. **Unmapped source.** An evidence entry not in the map keeps today's link (the world
permalink). The command prints one line before the first send:
`<n> source links are not in the seed map` when n > 0.

L5. **No new reach.** The map changes only where a shown link points. The visibility
filter, the selection and every line's text stay as they are without a map.

L6. **Limits and safety.** The 3000-character section cut and the 50-block limit hold as
today. A cut never leaves part of a link (the existing `_cut` rule). A reply link is
written as `chat.getPermalink` returns it, with a plain `&`: in the 2026-10-13 link spike,
a labelled reply link of that form resolved (Slack unfurled the reply it points to).

## Interface (stubs land before tests)

```python
# digest/daily/slack.py
def slack_message(world: World, selection: Selection,
                  links: SeedMap | None = None) -> dict[str, Any]: ...

# digest/seed.py
def message_permalink(seed_map: SeedMap, key: str) -> str | None: ...
    # L1: the Slack permalink of a mapped world message, or None
def channel_of(seed_map: SeedMap, key: str) -> str | None: ...
def files_of(seed_map: SeedMap, key: str) -> list[tuple[str, str]]: ...  # (name, permalink)
```

`cli.py`: `post` gains `--seed-map`, loads the map with `SeedMap.load`, runs the checks
in "Command", counts the unmapped sources over the three digests (L4) and passes the map
to `slack_message`. `digest.daily` already sits above `digest.seed` in the layers
contract, so no contract change.

## Acceptance

A1 (L1). A root source and a reply source in the map render as Slack permalinks of the
map's workspace; the root link has no query; the reply link ends with
`?thread_ts=<root ts>&cid=<channel>`.
A2 (L2). A line with two mapped sources in one channel shows one `<#C...>`; with sources
in two channels it shows both, in source order.
A3 (L3). A source whose message has two files in the map shows both file links, named;
a source with no files shows none.
A4 (L4). An unmapped source keeps the world permalink, and the command prints the count
line; with everything mapped it prints no count line.
A5 (L5). The same selection without a map renders byte for byte as today (the existing
`tests/test_slack_post.py` cases pass unchanged).
A6 (Command). A missing map file, a map for another world, and (DM path) a map for
another `team_id` each exit 1 before any send.
A7 (L6). A section near 3000 characters with mapped links is cut without a partial link.
A8 (live, manual, recorded in the PR). `digest post --dm <member> --seed-map
.slack-seed/<team_id>-kestrel.json` in Scratch: a source link opens the seeded message, a
channel link opens the channel, and a file link opens the file.

## Test seams (for the test author)

- `slack_message` with hand-made `Selection` values (as `tests/test_slack_post.py` builds
  them) and a hand-made `SeedMap`.
- The CLI with `CliRunner`, the Slack clients patched as in `tests/test_slack_post.py`,
  and a map file in `tmp_path`. No test touches the network.

## Out of scope

- The layout rework (v5: top message and one thread reply per section).
- Links in `render_digest` (terminal) and in the web preview.
- Reading the map by default without `--seed-map`.

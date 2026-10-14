# Plan: M5 slice, native links from the seed map (`digest post --seed-map`)

Status: v0.2, 2026-10-13 (first plan review applied). Process stage 1 (`docs/process.md`). Depends on PR #70
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
- On the DM path (`--dm`) with `--seed-map`, the command calls `auth.test` before it opens
  the DM and checks that the map's `team_id` equals the bot token's workspace, else it
  exits 1 and posts nothing. An `auth.test` failure exits 1 with the existing safe DM error
  text (`_dm_failure`: an error code or type, never the token, no exception chain), with
  no DM opened and nothing sent. The webhook path cannot read its workspace, so it does
  not check.
- Without `--seed-map`, `digest post` does exactly what it does today, and makes no
  `auth.test` call.

## Behaviours

L1. **Mapped source link.** For each evidence entry of a line, the key
`<entry.channel_id>:<entry.message_ts>` is looked up in the map's `messages`. When found,
the link points at the Slack message:
`<workspace_url>/archives/<channel>/p<ts without the dot>`, plus
`?thread_ts=<thread_ts>&cid=<channel>` when the message is a reply (its `ts` differs from
its `thread_ts`). The link text stays `source`.

L2. **Channel link.** After the line's last source (and its file links), the line adds
` in ` and then one `<#<slack channel>>` per distinct channel of the line's mapped
sources, in source order, separated by one space: ` in <#C1> <#C2>`. Slack shows each
channel name as a link. A line with no mapped source adds nothing.

L3. **File links.** For each mapped source message, every map `files` entry whose key
starts with `<message key>/` adds a link `<permalink|<file name>>` after that source, in
the map's insertion order (the seeder writes them in the world's file order). File names
are escaped as other text (`&`, `<`, `>`).

Example, a line with a root source that carries one file and a reply source, both in one
channel (the source part only):

```
<https://ws.slack.com/archives/C1/p100|source> <https://ws.slack.com/files/U1/F1/a.pdf|a.pdf> <https://ws.slack.com/archives/C1/p200?thread_ts=100&cid=C1|source> in <#C1>
```

L4. **Unmapped source.** An evidence entry not in the map keeps today's link (the world
permalink). The command prints one line before the first send:
`<n> source links are not in the seed map` when n > 0. n counts evidence entries on the
lines of the three selections, before any render cut, each occurrence once (a source on
two readers' lines counts twice); items hidden by the visibility filter add nothing.

L5. **No new reach.** The map changes only where a shown link points. The visibility
filter, the selection and every line's text stay as they are without a map.

L6. **Limits and safety.** With a map as without one: every text object stays mrkdwn
with `verbatim: true`; text from the model, Slack and the world (titles, summaries, file
names) is flattened to one line and escaped (`&`, `<`, `>`); the 3000-character section
cut and the 50-block limit hold; a cut never leaves part of a link or of an escaped
entity (the existing `_cut` rule). Only constructed URLs, channel ids and permalinks from
the map go inside `<...>` unescaped. Slack documents that explicit `<...>` links and
`<#C...>` work with automatic parsing off (verbatim). A reply link is
written as `chat.getPermalink` returns it, with a plain `&`: in the 2026-10-13 link spike,
a labelled reply link of that form resolved (Slack unfurled the reply it points to).

## Interface (stubs land before tests)

```python
# digest/daily/slack.py
def slack_message(world: World, selection: Selection,
                  links: SeedMap | None = None) -> dict[str, Any]: ...

# digest/seed.py
def message_permalink(seed_map: SeedMap, key: str) -> str | None: ...
    # L1: the Slack permalink of a mapped world message, or None when the key is absent
def channel_of(seed_map: SeedMap, key: str) -> str | None: ...
    # the Slack channel id of a mapped world message, or None when the key is absent
def files_of(seed_map: SeedMap, key: str) -> list[tuple[str, str]]: ...
    # (name, permalink) per file of a mapped world message, in map order; [] when none
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
`tests/test_slack_post.py` cases pass unchanged), and the CLI makes no `auth.test` call.
A6 (Command). A missing map file, a map for another world, (DM path) a map for another
`team_id`, and (DM path) an `auth.test` failure each exit 1 before any DM is opened or
any send; the auth failure output holds no token.
A7 (L6). With a map: every text object is mrkdwn with `verbatim: true`; a file name and
a summary holding `&`, `<` and `>` are escaped; a section near 3000 characters with
mapped links is cut without a partial link or entity.
A8 (L4). The count line counts each evidence occurrence on the selections' lines,
repeats included, and nothing from hidden items.
A9 (live, manual, recorded in the PR). The whole Kestrel corpus is seeded in Scratch
(done 2026-10-13, map `.slack-seed/T0C8CJQG92A-kestrel.json`). Run `digest post --dm
<member> --seed-map <map>` for the default day 6. In the posted digests, open one root
source link, one reply source link (it must open the reply in its thread), one channel
link and one file link (the PN-11020 Rev C release item carries one). Record the world
keys and what opened.

## Test seams (for the test author)

- `slack_message` with hand-made `Selection` values (as `tests/test_slack_post.py` builds
  them) and a hand-made `SeedMap`.
- The CLI with `CliRunner`, the Slack clients patched as in `tests/test_slack_post.py`,
  and a map file in `tmp_path`. No test touches the network.

## Out of scope

- The layout rework (v5: top message and one thread reply per section).
- Links in `render_digest` (terminal) and in the web preview.
- Reading the map by default without `--seed-map`.

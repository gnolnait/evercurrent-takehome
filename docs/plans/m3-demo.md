# Plan: M3 demo slice, a daily digest per reader

Status: v0.4, 2026-10-12. Process stage 1 (`docs/process.md`), lighter variant decided
with the user for the 2026-10-14 deadline: Codex writes the tests, an Opus subagent
writes the code, every test is RED-GREEN proven, one review lane per commit
(alternating Codex and the Claude lenses), at most one fix round per commit; findings
left over are recorded in the verdict. Amended 2026-10-12, 11:00: the user returned to
the full process. Both review lanes run on every commit, with fix rounds until the
findings are clean.

## Goal

SPEC A1: `just demo` runs offline in under 60 seconds and prints the digests of three
readers (ME, supply chain, EM) on the same day, from the bundled Kestrel corpus and its
recorded cassettes.

This slice takes SPEC section 9's day-3 kill-criterion path: no claims ledger and no
stale-belief section. A digest is the visibility filter, a must-see lane, a baseline
ranked lane, the absence lines and a plain-text renderer. The README says so. The
ledger, stale beliefs, feedback, sender affinity, decision exposure and delivery are M3
proper and follow after the interview.

## Units and branches

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 5 Selection | `m3-select` | `digest/daily/profile.py`, `digest/daily/select.py`, the `daily.role_priors` table in `config/priors.yaml` and `digest/priors.py` | `tests/test_select.py` |
| 6 Render and commands | `m3-render` | `digest/daily/render.py`, `digest run`, `digest demo`, the `demo` recipe in `justfile` | `tests/test_daily_render.py` |

A stubs commit on `m3-stubs` lands first, by pull request: the interfaces below raising
`NotImplementedError`, the priors table, the layer change, the CLI options. Units 5 and
6 then fork from `main` and run in parallel; unit 6 builds digests from hand-made
`Selection` values, so it does not wait for unit 5.

## Shared conventions

- Layers: `digest.daily` sits between `digest.eval` and the
  `digest.replay | digest.extract | digest.absence` layer; it reads items, events and
  absences from the store and the world, and calls no model.
- Determinism: same store, world, reader and day give the same bytes. No wall-clock
  time; ranked ties break as B5.4 says, so the order never depends on input order.
- The reader is a world persona. A persona with `kind: bot` is refused.
- Text from the model and from Slack is rendered as plain text; only permalinks built by
  code are links.

## Interface

`digest/daily/profile.py`

```python
class Profile(Frozen):
    user_id: str
    name: str
    role: Role
    team: str | None
    owned_entities: tuple[str, ...]  # the owned set of B5.1, sorted
    channels: tuple[str, ...]          # channels the reader is a member of

def profile_for(world: World, user_id: str) -> Profile   # KeyError for an unknown id, ValueError for a bot
def can_see(world: World, profile: Profile, channel_id: str) -> bool  # public, or private with the reader a member
```

`digest/daily/select.py`

```python
Section = Literal["needs_you", "not_happened", "decisions", "risks", "fyi"]
SECTION_CAPS: Mapping[Section, int]   # needs_you 5, not_happened 3, decisions 5, risks 5, fyi 5 (SPEC 5.7)

class Line(Frozen):
    item: Item
    section: Section
    must_see: bool
    reason: str          # why it is here: the must-see rule, or the score terms
    score: float         # 0 for a must-see line

class Selection(Frozen):
    profile: Profile
    day: date
    lines: tuple[Line, ...]           # section order; must-see by item_id, then ranked in the B5.4 order
    hidden: int                       # items dropped by the visibility filter

def select(world: World, profile: Profile, items: Sequence[Item], absences: Sequence[Item],
           priors: Priors, day: date) -> Selection
```

`digest/daily/render.py`

```python
def render_digest(world: World, selection: Selection) -> str      # plain text, see B6.1
def build_digest(store: Store, world: World, priors: Priors, user_id: str, day: date) -> str
```

CLI: `digest run --user ID --as-of YYYY-MM-DD [--world NAME] [--db PATH]` prints one
digest from a store that `replay` and `extract` filled up to that day.
`digest demo [--day N]` replays the bundled Kestrel world into a temporary store up to
world day N (default 6), runs `digest extract` in replay mode from the bundled
cassettes, and prints the digests of the ME (`U0ME01`), supply chain (`U0SC01`) and
EM (`U0EM01`) readers, separated by a rule line.

## Behaviours

B5.1 `profile_for` builds the profile from the persona. Its owned set follows SPEC
v0.4.3 section 3: the persona's `owned_entities`, every world entity whose `owner` is the
reader's role, and every entity whose `subsystem` or `parent` is in the set, repeated
until the set stops growing. Every rule below that says "owned" reads this set; `can_see` is true for a public
channel and for a private channel the reader belongs to, false otherwise (fail closed:
an unknown channel id is not visible).

B5.2 Visibility filter (SPEC 5.6, a v0.4 core rule): an item's channel is the channel of
its `thread_id` (`<channel>:<thread_ts>`); every evidence entry comes from that thread.
An item reaches the reader only when that channel is one the reader can see. An evidence
entry whose `channel_id` (SPEC 4, v0.4.3) is any other channel is removed from the line's item before
rendering (fail closed), so no quote or link from another channel is shown; an item left
with no evidence entry is dropped. An item with no
visible evidence is dropped and counted in `hidden`. An absence item follows the same
rule through its cited evidence.

B5.3 Must-see lane, the rules that need no ledger (SPEC 5.6): an open `ask` whose
`owner` is the reader; an open `blocker` whose entities include an entity the reader
owns; a `schedule_change` whose entities include a gate the reader owns; a checklist
absence whose `affected_roles` include the reader's role, or any checklist absence when
the reader is the EM, who runs the gate; an ask absence where the
reader is the asker or the person asked. A must-see line is never cut by a section cap.

B5.4 Ranked lane: every other visible open item gets
`score = importance * role_prior * personal_relevance`:
- `importance`: by type (blocker 1.0, decision 0.9, schedule_change 0.9, spec_change 0.8,
  bom_change 0.8, risk 0.7, ask 0.6, absence 0.9, fyi 0.3), times severity (high 1.0, medium 0.7,
  low 0.4).
- `role_prior`: `priors.daily.role_priors[role][type]` (each value above 0 and at most 1,
  checked when the priors load; SPEC 5.6 keeps every score term above zero), default 0.5 when the table has no entry (ruling: the table ships with the role-by-type values in `config/priors.yaml`;
  phase re-weighting is M3 proper).
- `personal_relevance`: 1.0 when an item entity is owned by the reader, 0.8 when the
  item's `affected_roles` include the reader's role, else 0.4. Amended 2026-10-13
  (`docs/plans/recent-activity.md`): an item at 0.4 that shares an entity with the
  reader's recent activity gets `daily.recent_activity.relevance` (default 0.8).
An item below the threshold `priors.daily.min_score` (default 0.15) is left out.
Ranked lines are ordered by score, highest first. Scores equal to six significant figures
break on the item's newest evidence, the greatest `message_ts` among its evidence
entries, newest first, then on `item_id` ascending (SPEC 5.6). Must-see lines are in
`item_id` order, above the ranked lines. `reason`
names the three terms with their values.

B5.5 Sections: an `ask` item goes to `needs_you` only when the reader is the person
asked (its `owner`); any other ask goes to `fyi`, so "Needs you" never lists a request
made of someone else. When a reader's digest shows an ask absence
line, after the caps and the threshold, the line of the ask item it cites is left out, so
one ask never shows twice. The absence names that item in its id
(`absence:ask:<ask item_id>`, as `digest.absence` builds it). Every other ask keeps its
line, in the same thread or another, and an ask whose absence line is not shown keeps
its line. The ask absence goes to
`needs_you` when the reader is the person asked and to `not_happened` when the reader is
the asker; no other reader gets it (amended 2026-10-13, `docs/plans/ask-audience.md`); `decision`, `schedule_change`, `spec_change`,
`bom_change` to `decisions`; `blocker` and `risk` to `risks`; `fyi` to `fyi`. Each
section holds its must-see lines plus up to its cap of ranked lines. A checklist absence reaches only the readers its must-see rule names (the task's owner
role and the EM); it never enters another reader's ranked lane (user ruling, 2026-10-12).
Resolved items are
left out. Items are those with `last_updated` at or before the end of `day` in the
world's zone; absences are `detect_absences` as of that day.

B5.6 Gaps to SPEC v0.4.3 that the slice keeps. None changes the bundled demo at its
default day 6:
- No digest window. Every demo digest is a first digest. SPEC's first window is five
  working days back; from day 6 (Monday 2026-10-19) that reaches day 1, the corpus
  start, so it holds every item. From day 7 on, the slice also shows older items that
  a first window leaves out. The window is M3 proper. Amended 2026-10-12: the window is
  built (`docs/plans/digest-window.md`), and the demo passes the previous world day.
- B5.1 leaves out entities whose `owner` is the reader's user id: `Entity.owner` in the
  world schema is a role only.
- `personal_relevance` is not the neutral 1 for a reader who owns nothing. Each of the
  three demo readers owns entities.
- A checklist absence also reaches the EM (user ruling, 2026-10-12). SPEC section 3
  sends it to the task's role only, and the golden labels keep that audience, so a
  golden-implied overlap target (M3 proper) must add the EM.
- The 0.5 default role prior is not the neutral 1 of SPEC 5.6. The `daily.role_priors`
  table in `config/priors.yaml` has a value for every role and every item type,
  so the default does not apply to the bundled config.
- Newest evidence is the greatest `message_ts`, not the greatest `event_ts` of the
  current message versions: an edit does not make an item newer. Items carry no
  `event_ts`. This changes only the order of tied lines.

B6.1 `render_digest` writes, in order: a header line with the reader's name, role and
the date; then each non-empty section with a title ("Needs you", "Not happened",
"Decisions", "Risks", "FYI"); each line as `- <title> (<entity ids>) - owner <name>`,
without the owner part when the item has no owner, and, indented below it, the summary, then `source: <permalink>` for each
visible evidence entry; an absence line reads `- Not happened: <task text> (<owner role>) for <gate> - <N>
working days silent, <consumed>/<buffer> buffer days used`, with the task text from the
world's checklist, or, for an ask, `- Not happened: <asker> asked <owner>, no reply for
<N> working days`; below an absence line come its summary (for an ask, what was asked)
and its source link; a must-see line is marked `[must see]`. A day with no line renders one
line saying there is nothing new for the reader. A footer line says how many items the
visibility filter hid, when that is above zero.

B6.2 `build_digest` reads items with `store.items(as_of)` and absences with
`detect_absences`, selects and renders. It refuses (ValueError naming the reason) a
store without `meta.until`, with pending extract deltas, or whose `meta.until` is not the
end of `day` in the world's zone: the store keeps only each item's latest version, so a
digest for an earlier day needs a store replayed exactly to that day. The refusal for a
store replayed past the day tells the operator to replay a new store (a fresh `--db`)
to that day and extract it, since replay does not move back.

B6.3 `digest run` prints `build_digest` and exits 0; it exits 1 with the reason on a
refused store, an unknown user or a bot. `digest demo` exits 0, prints three digests,
then one line with the realized pairwise item overlap of the three (SPEC A1): for each
reader pair, the Jaccard index of the item ids their digests show, to two decimals. It
reads no API key: it never constructs the settings object that loads keys from the
environment, and it builds the model client in replay mode only. Before any replay it checks
that the cassette directory exists and holds cassettes, else it exits 1 naming the
directory. It never writes outside
a temporary directory and finishes in under 60 seconds on the bundled data. A missing
cassette exits 1 with the `CassetteMiss` message; an extract run with a failed thread
exits 1 naming the failed-thread count, before any digest is built. The golden-implied
overlap target of SPEC A1 is M3 proper.

B6.4 `digest post` (added 2026-10-12 so the demo can be seen in Slack): it builds the
same three digests as `digest demo`, from the same day (`--day`, default 6), and posts one
Slack message per reader, in reader order, to the incoming webhook in the environment
variable `SLACK_WEBHOOK_URL` (also read from `.env`). It is the only command that sends
anything over the network in replay mode, and it reads no model API key.
- Message: Block Kit. A header block with the reader's name, role and date; a section
  block with the window line of the terminal digest (W9, `docs/plans/digest-window.md`);
  then for each non-empty section a section block with the bold section title, and one section block
  per line holding the same text as the terminal line in Slack mrkdwn (the title in
  bold, `[must see]` in bold, each source as `<permalink|source>`). Text from the model
  and from Slack is escaped for mrkdwn (`&`, `<`, `>`), so it cannot form a link or a
  mention. A section block's text stays under 3000 characters (cut with an ellipsis), and
  a message holds at most 50 blocks: lines past the limit are left out and a last block
  says how many. The plain-text `text` field holds the header line and the window line, for
  notifications.
- It posts nothing until all three digests are built; a refusal or cassette miss exits 1
  as `digest demo` does.
- No `SLACK_WEBHOOK_URL`: exit 1 naming the variable. A post that Slack refuses (status
  other than 200): exit 1 with the reader, the status and Slack's error code (the
  body only when the hardening rule below allows it), and no further posts. No output, log line or error ever contains the webhook URL.
- The source links point to the synthetic workspace `kestrel-k2.slack.com`, which does
  not exist; the README says so.
- Hardening (review round, 2026-10-12). Every mrkdwn text object sets `verbatim: true`,
  so Slack does not turn a bare URL, `@here` or `#channel` in model text into a link or
  a mention; the explicit `<permalink|source>` links still work. The URL must start
  with `https://hooks.slack.com/` and hold no whitespace, else exit 1 naming the
  variable, before any build.
  `.env` is read as UTF-8 with or without a byte-order mark, or as UTF-16 when it starts
  with a UTF-16 byte-order mark (the PowerShell 5.1 default); an unreadable file exits 1
  naming `.env`. An unquoted value ends at ` #` (an inline comment), as python-dotenv
  reads it. Any exception from a send exits 1 naming the reader and the exception type
  only, never its message. A refused post prints Slack's response body only when it is a plain
  error code (lower-case letters and underscores, such as `invalid_blocks`); any other
  body is withheld, since a proxy page can echo the URL path A code that also
  appears in the URL is withheld too. The client keeps the SDK's connection-error retry with its
  logger off.

B6.5 `digest post --dm USER_ID` (user ruling, 2026-10-12: the product delivers to each
reader directly). It builds the same three messages as B6.4 and sends each as a direct
message from the app's bot to the Slack user `USER_ID`, in reader order: it opens the DM
with `conversations.open` and posts with `chat.postMessage` (scopes `chat:write`,
`im:write`). The demo workspace has one real person, so all three readers' digests go to
that user; each message's header names its reader. The bot token comes from
`SLACK_BOT_TOKEN` (environment, then `.env`, read as in B6.4) and must start with
`xoxb-`; `USER_ID` must match `[UW][A-Z0-9]+`. Either check fails with exit 1 before any
build, naming the variable or the option and never the token. A Slack API error exits 1
with the reader and Slack's error code only (for example `missing_scope`,
`channel_not_found`); any other exception exits 1 with the reader and its type only. No
output, log line or exception text holds the token. Every refusal, in B6.4 and here, is raised after the
handler that caught the error, so no exception chained to it (`__context__` or
`__cause__`) holds the token or the webhook URL. Live-run rules (envelope review): the code
that turns a Slack error into text never raises, whatever the response holds; the DM
client retries a rate-limited call after Slack's `Retry-After`; `post` prints one line
before the first send naming how many digests it will send and where; a refusal after a
partial post says how many digests were already sent and that a rerun sends all of them
again; a missing token or URL names the directory whose `.env` was read; an
`invalid_auth` or `not_authed` refusal says whether the token came from the environment
or from `.env`. Without `--dm`, `post` uses the
webhook of B6.4.

## Tests

`tests/test_select.py`: B5.1 to B5.5 on a small synthetic world (one private channel,
two readers of different roles, items of each type, one absence of each source):
visibility drop and evidence trimming, each must-see rule, the score terms and the
threshold, section assignment and caps with must-see lines kept above the cap, ordering
and determinism. Null cases: a reader who owns nothing, a day with no items, an item
whose evidence is all in a channel the reader cannot see.

`tests/test_daily_render.py`: B6.1 on hand-made `Selection` values (every line kind,
the empty day, the hidden footer, plain text for quotes with markdown characters);
B6.2 and B6.3 with CliRunner on a tmp store and tmp cassettes (the same request-helper
pattern as `tests/test_extract.py`); `digest demo` on the bundled data once the data
pull request has recorded the cassettes (the test skips with a reason naming the
missing cassettes until then).

## Acceptance

- A1 on the bundled data after the data pull request.
- SPEC 5.6 visibility filter: no reader sees a quote or link from a private channel
  they are not in (LEADS-1 never appears for ME or supply chain).
- The README shows the three demo digests and states the deferred M3 scope.

## Open for the user

None. The deadline decisions are recorded in this plan's status line.

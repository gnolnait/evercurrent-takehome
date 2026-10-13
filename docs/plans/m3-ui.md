# Plan: M3 units 8 and 9, the shared demo room (a web UI over the world and the digests)

Status: v0.5, 2026-10-12. Process stage 1 (`docs/process.md`), the full process the user
set for M3 at 11:00 on 2026-10-12 (`docs/plans/m3-demo.md`): Codex writes the tests, an
Opus subagent writes the code, every test is RED-GREEN proven, both review lanes run on
every commit with fix rounds until clean, then the stage 4 adversarial pass and a stage 5
retro entry. Written with Codex (`gpt-6-astra`). Core needs nothing from M3 unit 7 except
`diff_selections`, which lands first in `digest/driver.py` (branch `m3-diff-stubs`);
unit 7 itself is built in parallel by the session that owns the driver plan, with its two
scenarios recorded, and the scenario buttons are stretch.

## Goal

One web page that the presenter and the interviewers open from their own browsers during
the interview on 2026-10-14. It shows the Kestrel K2 world and three readers' digests side
by side. Anyone in the room changes who reads what (a reader's role, channel membership, a
private flag, an entity's owner role, a gate date, a task's owner role), and every browser
shows the new digests and what changed for each reader, with the reason for each line. The
presenter resets the room to the recorded world. Core calls no model and needs no network
beyond the link itself.

## Cut list

Core, in build order:

1. World panel: personas (role, team, manager, channels, effective owned entities),
   channels (purpose, private, members), entities (type, owner role, parent, effective
   owners), gates and checklist tasks, and every stored message up to the day, by channel
   and thread, each with an in-page anchor.
2. Digest panel: three readers side by side (stacked below 720 px width), each line with
   its heading and summary in the same words as `digest run`, its reason (collapsed), and
   its sources as links to the message anchors in the world panel. The fictional Slack
   permalink is shown as text, not as a link.
3. Diff panel: each reader against the recorded world on the same day, from
   `diff_selections`.
4. Free edits (table below) through an edit log; reset by the presenter.
5. Sharing: one room per server, a participant name, everyone's edits visible within two
   seconds, the edit log with who did what.

Core reads one base store, built once at start and never written, and calls no model.

Stretch, only after core is green and reviewed, in this order: the two recorded scenarios
of unit 7 (the CM bracket blocker, the private leads message) as one-click buttons, after
unit 7 merges with its cassettes; free-text messages (refused on a cassette miss unless the
presenter approves recording for that message and `digest serve` ran with
`--allow-record`); a day slider (core fixes day 6); priors editing; snapshot export of a
room's edit log; a hosted container.

Out: editing message text in the recorded corpus, editing the world script or golden
labels, re-rendering, document files and links (documents are entities; messages carry no
files yet, D24 stretch), accounts, more than one room per server.

## The design rule: two views of the world

The extraction request carries a glossary of every entity (id, type, description,
aliases) and every person (user id, name, role, aliases), and the checklist tasks of the
gates on or after the day (task, owner role, gate and date)
(`src/digest/extract/prompts.py:104-124`). The cassette key is a hash of the whole request
(`src/digest/llm/cassette.py:22-29`). So an edit to any of those fields, applied to the
world that extraction reads, changes every extraction request after it and misses every
cassette.

The room therefore keeps two views:

- The recorded world: `load_world` of `data/world/kestrel/world.yaml`, never edited.
  Replay, triage, extraction and (stretch) `make_event` read only this view, so the base
  store equals the store `digest demo` builds and every cassette hits.
- The room world: the recorded world with the room's world edits applied. Only
  `select_digest(store, world, priors, user_id, day)` (`src/digest/daily/render.py:53`)
  and the panels read it, through `profile_for`, `can_see`, `detect_absences` and
  `select`. `select_digest` takes the world as an argument separate from the store, so no
  pipeline code changes.

The page states the consequence: an edit changes who reads what and what counts as late;
it does not change what the model extracted. "Make Hamid the EM" moves lines between
digests; it does not make the extractor treat Hamid as the EM.

| Edit | Room world field | Reruns | Model calls |
|---|---|---|---|
| Reader role | `Persona.role` | `profile_for` (role and owned set), `select` | none |
| Channel membership | `Persona.channels` | `can_see`, `select` | none |
| Channel private flag | `Channel.private` | `can_see`, `select` | none |
| Owner role of an entity | `Entity.owner`; the entity leaves every `Persona.owned_entities` | `profile_for` owned set, `select` | none |
| Gate date | `Gate.date` | `detect_absences`, `select` | none |
| Task owner role | `ChecklistTask.owner` | `detect_absences`, `select` | none |
| Recorded scenario (stretch) | a room store event | `make_event`, extract, `select` | replayed from cassettes |
| Free-text message (stretch) | a room store event | `make_event`, extract, `select` | one or two, only as above |

Not offered: names, aliases, entity descriptions and types (they would read as edits and
mean nothing to the extractor); team and manager (no ranking term reads them yet,
`_ranked_line`, `src/digest/daily/select.py:199`); gate buffers and task cadences (stretch
with priors).

The room world is built with `model_copy(update=...)`, not by validating a new `World`.
`World` validation enforces script consistency, for example that a poster stays a member
of every channel they posted in (`src/digest/world/schema.py:215`) and that an asked
person is in the ask's channel (`schema.py:257`). Those rules protect the script, which
the room never re-renders; applied to the room they would refuse "remove the ME from
#dvt1-build" because the ME posted there. The room runs its own checks instead (R3).

## State of the room

The room is a base and an ordered edit log. Nothing else is state.

- Base (`RoomBase`): the recorded world, the priors, the day (core: day 6, 2026-10-19),
  the base store replayed and extracted through that day, and the message view built
  from it. Built once by `digest serve`, on the worker thread.
- Edit log: `rooms/<room_id>/log.jsonl`, one `LogEntry` per line. Appending is the only
  write.
- Derived, rebuilt after each edit: the room world and the three selections and diffs.
  A room store (a `Store.copy_to` of the base store plus one scenario) exists only for
  the stretch scenarios.

Rules:

- R1. Same base and same log give byte-identical panels, on any machine. No wall-clock
  time enters a panel except the log's own timestamps.
- R2. Reset appends a `Reset` entry; the state is computed from the world edits after the
  last `Reset`. The log keeps the history. Only the presenter resets.
- R3. Room checks: every id in an edit exists in the recorded world; a persona edit names
  a human; a role is a `Role`; a gate date is a weekday on or after the world's
  `start_date` (it may fall after the world's last day). A refused edit returns its
  reason in plain words and is not appended.
- R4. Atomic publish: an edit builds a candidate state and replaces the published state
  only when every step succeeds. A refusal or an exception leaves the published state and
  the log unchanged.
- R5. Every edit sets an absolute value ("private = true", "role = EM"), never a toggle,
  so a repeated request changes nothing and two participants' edits in either order give
  the state the later one asked for. No operation ids.
- R6 (stretch). Scenarios are exclusive: a `RunScenario` rebuilds the room store from the
  base store and that one scenario, replacing any earlier one, so unit 7's rule S2 (each
  new message later than every stored event) holds and the requests are the recorded ones.
  The scenario's message is built against the recorded world, so a room edit cannot make
  it refuse.

## Sharing

- One room per server. `digest serve` creates it at start and prints two links: the
  participant link `/r/<room_id>` and the presenter link `/p/<presenter_token>`. Both ids
  are 128 random bits in URL-safe base64. There is no route that creates or lists rooms.
- Presenter. The presenter link sets an HttpOnly, SameSite=Lax presenter cookie and
  redirects to the participant link, so the token does not stay in the address bar on a
  shared screen. Reset and (stretch) recording need that cookie.
- Participants. A participant types a name once (at most 40 characters, shown as text).
  The server keeps a random participant id in an HttpOnly, SameSite=Lax cookie and maps it
  to the name; the name never enters a header. Every POST needs the participant cookie, so
  a cross-site form cannot post (SameSite=Lax withholds the cookie).
- Updates. Every page polls `GET /r/<room_id>/version` every 1.5 seconds and reloads its
  panels when the version changes. The page loads no script from a CDN: one inline script
  of at most 40 lines does the polling, the panel reloads and the form posts. Polling, not
  server-sent events: Cloudflare quick tunnels do not carry server-sent events.
- Concurrency. One uvicorn worker process. One worker thread applies every edit from a
  queue, in arrival order, and owns every SQLite connection, the base store's included:
  `Store.open` uses `sqlite3.connect` with the default `check_same_thread`
  (`src/digest/store.py:99`), and FastAPI runs sync routes on a thread pool. Pages read
  the published immutable `RoomState` only.
- Model calls. Core builds no `LLM`. `digest serve` reads no key unless it runs with
  `--allow-record` (stretch); without it, `Settings` is not built, as in `digest demo`.
  The stretch scenarios build `LLM` in replay mode with no backend, which cannot call a
  model (`src/digest/llm/client.py:37`). A tunnel makes every request arrive from
  localhost, so no check reads the client address.
- Limits. A log holds at most 500 world edits; `Reset` is always accepted. All text is
  rendered with Jinja autoescape; only links built by code are links (SPEC section 5
  step 7).

## Hosting, for the user to choose

| Option | Setup | Who can join | Risk |
|---|---|---|---|
| A. Presenter's laptop plus a quick tunnel (`cloudflared tunnel --url http://localhost:8000`, no account) | minutes | anyone with the participant link | the laptop and its network carry the demo; the URL changes per run; Cloudflare gives quick tunnels no uptime guarantee and no server-sent events |
| B. Hosted container, no key on the host | an account and a deploy | anyone with the link, before or after the call | a deploy the day before; free tiers sleep |
| C. Static snapshot as a claude.ai artifact | minutes | anyone with the link | read-only; no edits |

Recommended: A for the call, with C sent beforehand as a read-only preview. B only if the
interviewers should explore before the call. Publishing anything outside the laptop is
the user's decision.

## Units and branches

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 8 Room model and the selection diff | `m3-room` | `digest/room.py`; `diff_selections` in `digest/driver.py` | `tests/test_room.py`, `tests/test_driver.py` (the diff only) |
| 9 Web app and `digest serve` | `m3-web` | `digest/web/app.py`, `digest/web/templates/*.html`, `digest serve` in `digest/cli.py` | `tests/test_web.py` |

The diff types land first on `m3-diff-stubs`: `diff_selections`, `LineView`, `Change`,
`ReaderDiff` and `DiffField` in `digest/driver.py` as the unit 7 plan gives them, and
`digest.driver` beside `digest.eval` in the layers contract. Unit 7's stubs base on it;
unit 7 later adds `make_event` and `Store.copy_to` beside it without moving it.

A stubs commit on `m3-ui-stubs` follows: the types and signatures below raising
`NotImplementedError`; `line_parts` in `digest/daily/render.py` (below), with `_line`
rewritten to use it and no change to any rendered byte (unit 6's tests are the guard);
the templates directory; the layers contract with `digest.cli` above `digest.web` above
`digest.room` above `digest.eval | digest.driver`; and `rooms/` in `.gitignore`. Unit 9
builds pages from hand-made `RoomState` values, so units 8 and 9 run in parallel.

## Interface

`digest/daily/render.py` (stubs commit, no behaviour change)

```python
def line_parts(line: Line, names: Mapping[str, str],
               tasks: Mapping[tuple[str, str], ChecklistTask]) -> tuple[str, str]
    # (heading, summary): the two texts `_line` prints, without "- ", "[must see]" or sources
```

`digest/room.py`

```python
class SetRole(Frozen):       kind: Literal["set_role"]; user_id: str; role: Role
class SetMembership(Frozen): kind: Literal["set_membership"]; user_id: str; channel_id: str; member: bool
class SetPrivate(Frozen):    kind: Literal["set_private"]; channel_id: str; private: bool
class SetOwner(Frozen):      kind: Literal["set_owner"]; entity_id: str; role: Role | None
class SetGateDate(Frozen):   kind: Literal["set_gate_date"]; gate_id: str; date: dt.date
class SetTaskOwner(Frozen):  kind: Literal["set_task_owner"]; task_id: str; role: Role
WorldEdit = Annotated[SetRole | SetMembership | SetPrivate | SetOwner | SetGateDate
                      | SetTaskOwner, Field(discriminator="kind")]

class Reset(Frozen):         kind: Literal["reset"]
class RunScenario(Frozen):   kind: Literal["run_scenario"]; name: Literal["cm_bracket", "leads_gate"]  # stretch
Edit = Annotated[SetRole | SetMembership | SetPrivate | SetOwner | SetGateDate
                 | SetTaskOwner | Reset | RunScenario, Field(discriminator="kind")]

class LogEntry(Frozen):
    seq: int            # 1-based, dense
    author: str         # the participant's name
    at: str             # ISO time of the server clock; display only, never read by state
    edit: Edit

class MessageView(Frozen):           # one stored message, for the world panel
    channel_id: str
    thread_ts: str
    ts: str                          # also the anchor id
    author: str                      # persona name
    text: str
    permalink: str                   # the fictional Slack URL, shown as text

class RoomBase(Frozen):              # built once by `digest serve` on the worker thread
    world: World                     # the recorded world
    priors: Priors
    day: date
    store_path: Path                 # the base store, replayed and extracted through `day`
    messages: tuple[MessageView, ...]  # every stored message up to the end of `day`, by channel, thread, ts
    readers: tuple[str, ...]         # U0ME01, U0SC01, U0EM01
    recorded: tuple[Selection, ...]  # each reader's selection in the recorded world, the diff baseline

class RoomState(Frozen):
    version: int                     # the log length; the page polls it
    world: World                     # the room world
    selections: tuple[Selection, ...]  # same order as RoomBase.readers
    diffs: tuple[ReaderDiff, ...]    # each reader against RoomBase.recorded
    owners: dict[str, tuple[str, ...]]  # entity id -> user ids whose owned set holds it
    log: tuple[LogEntry, ...]

def apply_world_edits(world: World, edits: Sequence[WorldEdit]) -> World  # ValueError naming the edit on an R3 refusal
def build_state(base: RoomBase, store: Store, log: Sequence[LogEntry]) -> RoomState
class Room:                          # owns the log file, the worker queue and the published state
    def append(self, author: str, edit: Edit) -> RoomState   # validates, appends, rebuilds; ValueError on a refusal
    def state(self) -> RoomState
```

`SetOwner(entity_id, role)` sets `Entity.owner` to the role (or `None`) and removes the
entity from every `Persona.owned_entities`, so the role is its only direct owner. The
owned set (`_owned_set`, `src/digest/daily/profile.py:33-50`) also adds the children of an
owned entity, so an entity whose `parent` or `subsystem` is owned stays owned by that
parent's owner. `RoomState.owners` comes from `_owned_set` for every human persona, and
the world panel shows it, so the page never claims a transfer the selection does not see.

`digest/web/app.py`: `create_app(room: Room, room_id: str, presenter_token: str) -> FastAPI`.

Routes: `GET /p/{presenter_token}` (sets the presenter cookie, redirects), `GET
/r/{room_id}` (the room page), `GET /r/{room_id}/version`, `GET /r/{room_id}/panel/{name}`
(HTML fragments: `world`, `digests`, `diff`, `log`), `POST /r/{room_id}/edit` (one form per
edit kind), `POST /r/{room_id}/name`, `POST /r/{room_id}/reset` (presenter cookie).

`digest serve [--host 127.0.0.1] [--port 8000] [--day 6]` builds the base store for the
day once, as `digest demo` does and with the same helper, prints the participant and
presenter links, and serves. The world is `kestrel`.

## Behaviours

B8.1 `apply_world_edits` on an empty list returns a world equal to the input. Each edit
kind changes exactly its field; an edit that breaks an R3 check raises `ValueError` naming
the edit and the id. Removing a persona from a channel they posted in is accepted.

B8.2 The recorded world and the base store are never changed: after any sequence of core
edits, the base store file is byte-identical to before, `load_world` of the world file
equals the recorded world, and no core edit builds a model request.

B8.3 Determinism: two `Room`s built from the same log give equal `RoomState` values.

B8.4 Reset: the world edits after the last `Reset` decide the state; a log ending in
`Reset` gives the recorded selections and empty diffs.

B8.5 Effects on day 6 (2026-10-19), each computed on the day-6 store with `model_copy`
worlds on 2026-10-12 and derived again by the test author from `world.yaml` and plan B5.3:
- `U0SC01` to role `EM`: the reader's role prior and owned set change (entities whose
  owner is `EM` join, those owned by `supply_chain` leave). The G-DVT1 EMC and thermal
  checklist absences and the top-cover DFM risk become must-see lines, the thermal review
  risk becomes must-see, and the stale drawing pack risk and the ECO-0142 decision leave.
  The test pins the whole `ReaderDiff`.
- `#ext-sunrise-cm` (`C0EXTCM`) private: its members are `U0ME01`, `U0SC01`, `U0PM01` and
  `U0CM01`. `U0EM01` loses the decision "PN-51010 T1 gate date moved to 2026-10-21" and
  `hidden` goes from 0 to 4; `U0ME01` and `U0SC01` are unchanged.
- Then `U0SC01` out of `#ext-sunrise-cm`: `U0SC01` loses the must-see ask "Confirm anodize
  PO for DVT-1 brackets", the risk "PN-11020 chassis base built to stale Rev B drawing
  pack" and the T1 decision, gains two ranked lines that the freed caps admit, and
  `hidden` goes from 0 to 4.
- A persona added to or removed from a public channel: no change (`profile.py:52`).
- `U0ME01` or `U0SC01` added to the private `#kestrel-leads`: no change, because no
  leads line clears a cap on day 6. The page says so when it happens; the stretch leads
  scenario is what puts a leads line in play.
- G-DVT1 from 2026-10-22 to 2026-10-29 (buffer 7 working days): day 6 falls before the
  buffer (`src/digest/absence.py:41-43`), so the G-DVT1 checklist absences leave every
  digest. A date of 2026-10-28 or earlier changes nothing on day 6.

B8.6 (stretch) `RunScenario("cm_bracket")` and `RunScenario("leads_gate")` each give the
unit 7 acceptance results D2 and D3 in the room's diffs, from committed cassettes, with
no key in the process.

B8.7 Concurrency: two `append` calls started together give a log with dense sequence
numbers and a state equal to applying the two edits in log order.

B8.8 Atomicity: an edit whose build fails (a forced exception in `select`) leaves
`state()` and the log file byte-identical to before.

B9.1 The room page shows the four panels. All persona, channel, entity and message text
is escaped: a name or message containing `<script>` renders as text. Each digest line's
source links point at message anchors on the page; no link points at the Slack
workspace.

B9.2 An edit posted by one client is visible in a second client's next `version` and
panel response; the log line names the first client's participant name.

B9.3 A refused edit returns the reason in the edit panel and does not change `version`.

B9.4 Reset without the presenter cookie is refused, whatever the client address or
forwarded headers say. A POST without the participant cookie is refused. A name longer
than 40 characters is refused.

B9.5 An unknown room id returns 404.

B9.6 The digest panel's heading and summary of every line equal `line_parts` for that
line, so the page and `digest run` use the same words.

## Acceptance

- U1. From a fresh clone with no key and no network: `digest serve`, open the
  participant link in two browsers, set `U0SC01` to EM in one, see both browsers show the
  new digests and the diff within two seconds; reset from the presenter browser; the
  digests and diffs equal the first load.
- U2. B8.2 holds after every core edit kind.
- U3. At 375 px width the page has no horizontal scroll and the digests stack.
- U4. `just ci` is green, the layers contract places `digest.web` above `digest.room`,
  and every unit 6 render test passes unchanged after the `line_parts` change.

## Test seams

- `apply_world_edits` and `build_state` on the Kestrel world and the day-6 base store
  built once per test session from the committed cassettes.
- `diff_selections` on hand-built `Selection` values, as the unit 7 plan lists them.
- The web app through FastAPI's `TestClient`, two clients with different cookies for
  B9.2; no real browser in tests. U1 and U3 are checked by hand with the browser-verify
  skill.

## Premise ledger

| # | Premise | Check | Status |
|---|---|---|---|
| P1 | The extraction request carries the glossary and checklist tasks, so edits to them would miss cassettes | read `src/digest/extract/prompts.py:104-124` | verified |
| P2 | `select_digest` takes the world separately from the store | read `src/digest/daily/render.py:53-100` | verified |
| P3 | `detect_absences` reads gates from the project passed to it | `render.py:99`; `src/digest/absence.py:205-222` | verified |
| P4 | Building the day-6 base store from cassettes takes under 20 seconds | `digest demo` from a fresh clone: 9 seconds, 2026-10-12 | verified |
| P5 | A rebuild after a free edit takes under 200 ms | three `build_digest` calls on the day-6 store: 10 to 12 ms; `load_world` 61 ms | verified |
| P6 | A quick tunnel serves 1.5-second polling for five clients | `cloudflared` against `digest serve`, five tabs | TBD, before the user picks hosting |
| P7 | Core needs no unit 7 code beyond `diff_selections` | the cut list; unit 7 has a plan only on 2026-10-12 | verified |
| P8 | The room world can skip the `World` script checks | `model_copy` worlds ran through `select_digest` with no error, 2026-10-12 | verified, pinned by B8.1 |
| P9 | A gate moved past its buffer drops its checklist absences | `src/digest/absence.py:41-43` | verified |
| P10 | The B8.5 effects | computed on the day-6 store, 2026-10-12, by this session and by an independent reviewer | verified |

## Open for the user

1. Hosting: A, B or C, or A with C beforehand (recommended). Anything published outside
   the laptop needs your yes.
2. Free-text messages during the call: keep them stretch and refused on a cassette miss
   (recommended), or allow recording from the presenter browser with `--allow-record` and
   the key on the laptop.
3. The schedule under the full process, proposed: the diff stub PR and the UI stubs PR
   merged by 10:00 PT on 2026-10-13; units 8 and 9 merged by 18:00 PT; that evening a
   check through the real tunnel with two browsers outside the laptop's network;
   rehearsal on the morning of 2026-10-14. Unit 7 records its two scenarios as early as
   it can, since recording is the one step that needs the key and the network. Past
   18:00 PT on 2026-10-13 with unit 9 not merged, the demo is `just demo` in a terminal.

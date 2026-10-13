# Plan: M3 units 8 and 9, the shared demo room (a web UI over the world and the digests)

Status: v0.3, 2026-10-12. Process stage 1 (`docs/process.md`), the lighter variant of
the M3 demo plan: Codex writes the tests, an Opus subagent writes the code, every test is
RED-GREEN proven, one review lane per commit, at most one fix round per commit. Written
with Codex (`gpt-6-astra`). Core needs nothing from M3 unit 7 except `diff_selections`,
which unit 8 builds at its unit 7 home (`digest/driver.py`); the recorded scenarios and
free-text messages need the rest of unit 7 and are stretch.

## Goal

One web page that the presenter and the interviewers open from their own browsers during
the interview on 2026-10-14. It shows the whole Kestrel K2 world and three readers'
digests side by side. Anyone in the room changes the world (a reader's role, who owns a
part, who is in a private channel, a gate date), and every browser shows the new digests
and what changed for each reader. The presenter resets the room to the recorded world.
Core calls no model and needs no network beyond the link itself.

## Cut list

Core, in build order:

1. A read-only view of the world: personas (role, team, manager, channels, owned
   entities), channels (purpose, private, members), entities (type, owner, parent,
   description, aliases, documents included), gates and checklist tasks, messages by
   channel and thread, golden items and absences for the day.
2. Three digests side by side for the room's day and readers, rendered by
   `render_digest`, with the diff of each reader against the recorded world.
3. Free edits (the "none" rows of the table below) applied through an edit log, with
   reset.
4. Sharing: a room link, a name per participant, everyone's edits visible within two
   seconds, the edit log with who did what.

Core reads one base store, built once at start and never written. No core step needs a
scratch store, a cassette or unit 7 beyond `diff_selections`.

Stretch, only after core is green, in this order: the recorded scenarios of unit 7 (the
CM bracket blocker, the private leads message) as one-click buttons, after unit 7 merges
and its cassettes are committed; free-text messages (a cassette miss is refused unless
the presenter approves recording for that message); a day slider over all ten days (core
fixes day 6); priors editing; snapshot export of a room's edit log; a hosted container.

Out: editing message text in the recorded corpus, editing the world script and golden
labels, re-rendering, document files and links (documents are entities; messages carry no
files yet, SPEC D24 stretch), accounts, persistence across server restarts beyond the
edit log file.

## The design rule: two views of the world

The extraction request carries a glossary of every entity (id, type, description,
aliases) and every person (user id, name, role, aliases), and the checklist tasks of the
gates on or after the day (task, owner role, gate and date)
(`src/digest/extract/prompts.py:104-124`). The cassette key is a hash of the whole request
(`src/digest/llm/cassette.py:24-29`). So an edit to any of those fields, applied to the
world that extraction reads, changes every extraction request after it and misses every
cassette.

The room therefore keeps two views:

- The recorded world: `load_world` of `data/world/kestrel`, never edited. Replay,
  triage and extraction read only this view, so the store a room reads is byte-identical
  to the store `digest demo` builds, and every cassette hits.
- The room world: the recorded world with the room's edit log applied. Only
  `select_digest(store, world, priors, user_id, day)` (`src/digest/daily/render.py:53`)
  and `render_digest` read it, through `profile_for`, `can_see`, `detect_absences` and
  `select`. `select_digest` already takes the world as an argument separate from the
  store, so no pipeline code changes.

Consequence the UI states on the page: an edit changes who reads what and what counts as
late; it does not change what the model extracted. "Make Hamid the EM" moves lines
between digests; it does not make the extractor treat Hamid as the EM.

| Edit | Room world field | Reruns | Model calls |
|---|---|---|---|
| Reader role | `Persona.role` | `profile_for` (role and owned set), `select`, render | none |
| Channel membership | `Persona.channels` | `can_see`, `select`, render | none |
| Channel private flag | `Channel.private` | `can_see`, `select`, render | none |
| Ownership | `Persona.owned_entities`, `Entity.owner` | `profile_for` owned set, `select`, render | none |
| Gate date, buffer, task cadence, task owner role | `ProjectConfig.gates` | `detect_absences`, `select`, render | none |
| Priors (stretch) | `Priors` | `detect_absences`, `select`, render | none |
| Recorded scenario (stretch) | scratch store event | `make_event`, extract, `select`, render | replayed from cassettes |
| Free-text message (stretch) | scratch store event | `make_event`, extract, `select`, render | one or two; refused on a cassette miss unless the presenter approves recording |

Edits to names, aliases, entity descriptions and types are refused: they would read as
free edits and silently mean nothing to the extractor. Team and manager edits are not
offered: no ranking term reads them yet (`_ranked_line`,
`src/digest/daily/select.py:199`), so the edit would change nothing on the page.

The room world is built with `model_copy(update=...)`, not by validating a new `World`.
`World` validation enforces script consistency, for example that a poster stays a member
of every channel they posted in (`src/digest/world/schema.py:215`) and that an asked
person is in the ask's channel (`schema.py:257`). Those rules protect the script, which
the room never re-renders; applied to the room they would refuse "remove the ME from
#dvt1-build" because the ME posted there. The room runs its own checks instead (R3).

## State of one room

A room is a base and an ordered edit log. Nothing else is state.

- Base: the world directory, the priors file and a day (core: day 6, 2026-10-19), all
  fixed when the room is created, plus the recorded base store for that day.
- Edit log: `rooms/<room_id>/log.jsonl`, one `Edit` per line, each with the
  participant's name, a sequence number and the edit. Appending is the only write.
- Derived, cached, rebuilt on demand: the room world (base world plus every world edit
  in order) and the three selections. Core rooms read the base store directly. A room
  store (a `Store.copy_to` of the base store plus its message edits through
  `make_event` and `extract`) exists only for the stretch message edits.

Rules:

- R1. Same base and same log give byte-identical pages, on any machine. No wall-clock
  time enters a page except the edit log's own timestamps.
- R2. Reset appends a `Reset` entry; the room state is computed from the entries after
  the last `Reset`. The log keeps the history, so "undo reset" is possible later. Only
  the presenter may reset.
- R3. Room checks: every id in an edit exists in the recorded world; a persona edit
  names a human; a role is a `Role`; a gate date is a weekday on or after the world's
  `start_date` (it may fall after the world's last day); a field on the refused list is
  refused. A refused edit returns its reason and is not appended.
- R4. Atomic publish: an edit builds a candidate state (world, store, selections) and
    replaces the published state only when every step succeeds. A refusal, an exception
  or (stretch) a cassette miss leaves the published state and the log unchanged.
- R5. Each edit carries an operation id chosen by the browser; a repeated id (a double
  click, a retried request) is applied once. Every edit sets an absolute value
  ("private = true", "role = EM"), never a toggle, so two participants' edits applied in
  either order give the state the later one asked for.
- R6 (stretch). Message edits keep unit 7's rule S2: each new message is later than every event in
  the room store. Each `RunScenario` appends its messages and runs one extract before the
  next edit, so the extraction requests depend only on the scenario and the base. The
  offered scenarios touch distinct threads; the acceptance runs each alone and both in
  each order with no cassette miss.

## Sharing

- Rooms. `POST /rooms` creates a room and redirects to `/r/<room_id>`, where `room_id`
  is 128 random bits in URL-safe base64. The link is the access control: anyone with it
  joins; the server lists no rooms.
- Identity. A participant types a name once; it is kept in an HttpOnly, SameSite=Lax
  cookie scoped to the room and written into each edit. No accounts. Every form carries
  a per-room form token that the server checks on each POST.
- Updates. Every page in a room polls `GET /r/<room_id>/version` every 1.5 seconds and
  reloads its panels when the version changes. The page loads no script from a CDN: one
  inline script of at most 40 lines does the polling, the panel reloads and the form
  posts, so the page works with no network beyond the link. Polling, not server-sent
  events: Cloudflare quick tunnels do not carry server-sent events.
- Concurrency. One uvicorn worker process. One worker thread per process applies every
  edit of every room from a queue, in arrival order, and owns every SQLite connection,
  the base store's included:
  `Store.open` uses `sqlite3.connect` with the default `check_same_thread`
  (`src/digest/store.py:99`), so a connection must not cross threads, and FastAPI runs
  sync routes on a thread pool. Pages read the published immutable `RoomState` only.
- Model calls. Participant operations always build `LLM` in replay mode with no
  backend, which cannot call a model (`src/digest/llm/client.py:34`). Reset and
  recording need the presenter token: `digest serve` prints a presenter link carrying a
  second random token. Recording is never a room-wide switch: the presenter approves one
  message at one room version, and that single extract runs in record mode, only when
  the process has a key. A tunnel makes every request arrive from localhost, so no
  check reads the client address.
- Limits. A message is at most 1,000 characters; a room log at most 500 entries; at most
  20 rooms per process. All text is rendered with Jinja autoescape; only permalinks built
  by code are links (SPEC section 5 step 7).

## Hosting, for the user to choose

| Option | Setup | Who can join | Risk |
|---|---|---|---|
| A. Presenter's laptop plus a quick tunnel (`cloudflared tunnel --url http://localhost:8000`, no account) | minutes | anyone with the room link | the laptop and its network carry the demo; the URL changes per run; Cloudflare gives quick tunnels no uptime guarantee and no server-sent events |
| B. Hosted container in replay-only mode (no key on the host) | an account and a deploy | anyone with the link, any time before or after | a deploy the day before; free tiers sleep |
| C. Static snapshot as a claude.ai artifact | minutes | anyone with the link | read-only; no edits |

Recommended: A for the call, with C sent beforehand as a read-only preview. B only if the
interviewers should explore before the call. Publishing anything outside the laptop is
the user's decision.

## Units and branches

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 8 Room model and the selection diff | `m3-room` | `digest/room.py`, `diff_selections` and its types in `digest/driver.py` (their unit 7 home) | `tests/test_room.py`, `tests/test_driver.py` (the diff only) |
| 9 Web app and `digest serve` | `m3-web` | `digest/web/app.py`, `digest/web/templates/*.html`, `digest serve` in `digest/cli.py` | `tests/test_web.py` |

A stubs commit on `m3-ui-stubs` lands first: the types and signatures below raising
`NotImplementedError`, `diff_selections` and its types in `digest/driver.py` as the unit 7
plan gives them, the templates directory, the import-linter change (`digest.web` above
`digest.room`, above `digest.driver`, above `digest.daily`), and `rooms/` in
`.gitignore`. Unit 9 builds pages from hand-made `RoomState` values, so units 8 and 9 run
in parallel. Unit 7 later adds `make_event` and `Store.copy_to` beside `diff_selections`
without moving it; the session that owns unit 7 is told before the stubs land.

## Interface

`digest/room.py`

```python
class SetRole(Frozen):       kind: Literal["set_role"]; user_id: str; role: Role
class SetMembership(Frozen): kind: Literal["set_membership"]; user_id: str; channel_id: str; member: bool
class SetPrivate(Frozen):    kind: Literal["set_private"]; channel_id: str; private: bool
class SetOwner(Frozen):      kind: Literal["set_owner"]; entity_id: str; user_id: str | None
class SetGateDate(Frozen):   kind: Literal["set_gate_date"]; gate_id: str; date: dt.date
class SetTaskOwner(Frozen):  kind: Literal["set_task_owner"]; task_id: str; role: Role
class RunScenario(Frozen):   kind: Literal["run_scenario"]; name: str   # stretch; a file under data/world/kestrel/scripts/
class Reset(Frozen):         kind: Literal["reset"]

Edit = Annotated[SetRole | SetMembership | SetPrivate | SetOwner | SetGateDate
                 | SetTaskOwner | RunScenario | Reset, Field(discriminator="kind")]

class LogEntry(Frozen):
    seq: int            # 1-based, dense
    op_id: str          # chosen by the browser; a repeat is applied once (R5)
    author: str         # the participant's name
    at: str             # ISO time of the server clock; display only, never read by state
    edit: Edit

class RoomState(Frozen):
    room_id: str
    day: date
    world: World                     # the room world
    readers: tuple[str, ...]         # default U0ME01, U0SC01, U0EM01
    selections: tuple[Selection, ...]
    diffs: tuple[ReaderDiff, ...]    # each reader against the recorded world, same day
    log: tuple[LogEntry, ...]
    stale: bool                      # true when message edits exist, so the eval report no longer describes the room

def apply_world_edits(world: World, edits: Sequence[Edit]) -> World   # ValueError naming the edit on a refusal
def build_state(base: RoomBase, log: Sequence[LogEntry]) -> RoomState
class Room:                                # owns the log file, the lock and the caches
    def append(self, author: str, edit: Edit) -> RoomState   # validates, appends, rebuilds
    def state(self) -> RoomState
```

`SetOwner` with `user_id` sets `Persona.owned_entities` of that persona and removes the
entity from every other persona's list; `None` removes it from all. `Entity.owner` (a
role) is left unchanged in core; the owned set of SPEC v0.4.3 reads both.

`digest/web/app.py`: `create_app(base: RoomBase, presenter_token: str) -> FastAPI`.

Routes: `GET /` (create-room form), `POST /rooms`, `GET /r/{room_id}` (the room page),
`GET /r/{room_id}/version`, `GET /r/{room_id}/panel/{name}` (HTML fragments: `world`,
`digests`, `diff`, `log`), `POST /r/{room_id}/edit` (one form per edit kind),
`POST /r/{room_id}/name`.

`digest serve [--host 127.0.0.1] [--port 8000] [--world kestrel] [--day 6]` builds the base
store for the day once (as `digest demo` does), prints the participant URL and the
presenter URL, and serves.

## Behaviours

B8.1 `apply_world_edits` on an empty list returns a world equal to the input. Each edit
kind changes exactly its field; an edit that breaks an R3 room check raises `ValueError`
naming the edit and the id. Removing a persona from a channel they posted in is
accepted (the script checks of `World` do not run on the room world).

B8.2 The recorded world and the base store are never changed: after any sequence of
core edits, the base store file is byte-identical to before, `load_world` of the world
file equals the recorded world, and no core edit builds a model request.

B8.3 Determinism: two `Room`s built from the same log produce equal `RoomState` values and
byte-identical rendered digests.

B8.4 Reset: entries after the last `Reset` decide the state; a log ending in `Reset`
gives the recorded state, and the diffs are empty.

B8.5 Effects, pinned on day 6 (2026-10-19) with expectations derived by hand from
`world.yaml` and plan B5.3 before any code:
- Setting `U0SC01` to role `EM` changes the reader's role prior and owned set (entities
  whose `owner` is `EM` join, those owned by `supply_chain` leave, `profile.py:33`) and
  makes the G-DVT1 checklist absences must-see ("you run the gate", `select.py:190`).
  The test pins the whole `ReaderDiff` for `U0SC01`, with its derivation written in the
  test commit.
- Setting `#ext-sunrise-cm` (`C0EXTCM`) private: its members are `U0ME01`, `U0SC01`,
  `U0PM01` and `U0CM01`, so `U0EM01` loses every line whose only visible evidence is in
  that channel (the Sunrise stale drawing pack risk among them) and `hidden` rises;
  `U0ME01` and `U0SC01` are unchanged.
- Removing `U0ME01` from `#dvt1-build` changes nothing while the channel is public
  (`can_see`, `profile.py:52`); after `#dvt1-build` is set private as well, `U0ME01`
  loses the lines whose only evidence is there.
- Moving G-DVT1 from 2026-10-22 to 2026-10-29 (buffer 7 working days) puts day 6 before
  the buffer, so the G-DVT1 checklist absences leave every digest.

B8.6 (stretch) `RunScenario` of the unit 7 scripts gives the unit 7 acceptance results D2
and D3 in the room's diffs, from committed cassettes, with recording off.

B8.7 Concurrency: two `append` calls started together on one `Room` produce a log with
dense sequence numbers and a state equal to applying the two edits in log order. A
repeated `op_id` is applied once.

B8.8 Atomicity: an edit whose build fails (a forced exception in `select`) leaves
`state()` and the log file byte-identical to before, and the base store unchanged.

B9.1 The room page shows the world panels, three digests and their diffs, and the log.
All user and model text is escaped: a persona name or a message containing
`<script>` renders as text.

B9.2 An edit posted by one client is visible to a second client's next `version` poll
and panel reload; the log line names the first client's participant name.

B9.3 A refused edit returns the reason in the edit panel and does not change `version`.

B9.4 Without the presenter token, reset and any request that would record are refused,
whatever the request's client address or forwarded headers say. With the token and no
key in the process, recording is refused naming the missing key. A free-text message
whose cassettes are missing is refused with the cassette key unless the presenter
approves recording for that one message. A POST without the room's form token is
refused.

B9.5 An unknown room id returns 404 and does not create a room; the room count and log
limits refuse with a reason.

## Acceptance

- U1. From a fresh clone with no key and no network: `digest serve`, open the
  participant link in two browsers, change `U0SC01` to EM in one, see both browsers show
  the new digests and the diff within two seconds; reset from the presenter link; the
  page equals the first load.
- U2. B8.2 holds after every core edit kind.
- U3. The page is usable at phone width with no horizontal scroll, since an interviewer
  may join from a phone.
- U4. `just ci` is green; the import-linter contract places `digest.web` above
  `digest.room`.

## Test seams

- `apply_world_edits` and `build_state` on the Kestrel world and the committed day-6 base
  store built once per session in a fixture; B8.5 expectations derived by hand before
  any code from the world file and SPEC section 5 step 6.
- The web app through FastAPI's `TestClient`, two clients with different cookies for
  B9.2; no real browser in tests. U1 is checked by hand with the browser-verify skill.

## Premise ledger

| # | Premise | Check | Status |
|---|---|---|---|
| P1 | The extraction request carries the glossary and checklist tasks, so edits to them would miss cassettes | read `src/digest/extract/prompts.py:104-124` | verified |
| P2 | `select_digest` takes the world separately from the store | read `src/digest/daily/render.py:53-100` | verified |
| P3 | `detect_absences` reads gates from the world passed to it, not from the store | `render.py:99`; read `src/digest/absence.py:205` | verified for the call site; the function body is checked in the stubs commit |
| P4 | Building the day-6 base store from cassettes takes under 20 seconds | `digest demo` from a fresh clone ran in 9 seconds on 2026-10-12 | verified |
| P5 | A room rebuild after a free edit (re-select and render three readers) takes under 200 ms | `build_digest` for the three readers on the day-6 store: 10 to 12 ms; `load_world` 61 ms (2026-10-12) | verified |
| P8 | The room world can skip the `World` script checks without breaking selection | `select` and `profile_for` read personas, channels, entities and gates only; checked again by B8.1 | verified by reading, pinned by B8.1 |
| P9 | Free edits never change an extraction request | the store is built from the recorded world only; B8.2 compares cassette keys | pinned by B8.2 |
| P6 | A quick tunnel serves the 1.5-second polling without throttling for five clients | run `cloudflared` against `digest serve` and open five tabs | TBD, before the user picks hosting |
| P7 | Core needs no unit 7 code beyond `diff_selections` | the cut list; unit 7 has a plan only (`m3-driver-plan`, no code branch on 2026-10-12) | verified; scenarios wait for unit 7 |
| P10 | A gate moved past its buffer drops its checklist absences | `checklist_absences` fires only when `buffer_start <= as_of <= gate.date` (`src/digest/absence.py:41-43`) | verified |

## Open for the user

1. Hosting: A, B or C, or A with C beforehand (recommended). Anything published outside
   the laptop needs your yes.
2. Free-text messages during the call: keep them stretch and refused on a cassette miss
   (recommended), or allow recording from the presenter link with the key on the laptop.
3. The schedule, proposed: stubs and unit 8 merged by 12:00 PT on 2026-10-13; unit 9 by
   18:00 PT; that evening a check through the real tunnel with two browsers outside the
   laptop's network; rehearsal on the morning of 2026-10-14. Past 18:00 PT on 2026-10-13
   with unit 9 not merged, the demo is `just demo` in a terminal.

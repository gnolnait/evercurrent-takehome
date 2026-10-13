# Plan: M3 units 8 and 9, the demo room (a web UI over the world and the digests)

Status: v0.7, 2026-10-13. Process stage 1 (`docs/process.md`), the full process the user
set for M3 at 11:00 on 2026-10-12 (`docs/plans/m3-demo.md`): Codex writes the tests, an
Opus subagent writes the code, every test is RED-GREEN proven, both review lanes run on
every commit with fix rounds until clean, then the stage 4 adversarial pass and a stage 5
retro entry. Written with Codex (`gpt-6-astra`). Scope: the presenter-operated room the
user chose on 2026-10-13; sharing with interviewers' own browsers is stretch. Core needs
nothing from M3 unit 7 except `diff_selections`, which lands first in `digest/driver.py`
(PR 54, stacked on the pre-push fix in PR 53); unit 7 itself is built in parallel by the
session that owns the driver plan, and its scenario buttons are stretch.

## Goal

One web page on the presenter's laptop, shown to the interviewers over screen share
during the interview on 2026-10-14. It shows the Kestrel K2 world and three readers'
digests side by side. The interviewers ask for a change to who reads what (a reader's
role, channel membership, a private flag, an entity's owner role, a gate date, a task's
owner role); the presenter makes it, and the page shows the new digests, what changed for
each reader, and the reason for each line. Reset returns to the recorded world. Core
calls no model and needs no network.

## Cut list

Core, in build order:

1. World panel: personas (role, team, manager, channels, effective owned entities),
   channels (purpose, private, members), entities (type, owner role, parent, effective
   owners), gates and checklist tasks.
2. Digest panel: three readers side by side, each line with its heading and summary in
   the same words as `digest run`, its reason (collapsed), and its sources as text. The
   fictional Slack permalinks are not links: the workspace does not exist.
3. Diff panel: each reader against the recorded world on the same day, from
   `diff_selections`, plus the lines whose words changed while their `LineView` did not
   (an absence's buffer count after a gate move), and a note when the latest edit
   changed no digest.
4. Edit forms for the six edits in the table below, the list of edits made, and reset.

Core reads one base store, built once at start and never written. Building it uses only
the replay client with no backend that `digest demo` uses, which cannot call a model
(`src/digest/cli.py:95`, `src/digest/llm/client.py:37`).

Stretch, only after core is green and reviewed, in this order: the two recorded scenarios
of unit 7 (the CM bracket blocker, the private leads message) as one-click buttons, after
unit 7 merges with its cassettes; the message panel with in-page anchors that the digest
sources link to; sharing with interviewers' own browsers (participant cookies and names,
1.5-second polling, a presenter link, edit attribution, a quick tunnel; designed in v0.6
of this plan, commit 8a0490f); a layout for phone width; a day slider (core fixes day 6);
priors editing.

Out: free-text messages (refused unless recorded); editing message
text in the recorded corpus, the world script or golden labels; re-rendering; document
files and links (documents are entities; messages carry no files yet, D24 stretch); a
hosted copy; accounts.

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
- The room world: the recorded world with the room's edits applied. Only
  `select_digest(store, world, priors, user_id, day)` (`src/digest/daily/render.py:53`)
  and the panels read it, through `profile_for`, `can_see`, `detect_absences` and
  `select`. `select_digest` takes the world as an argument separate from the store, so no
  pipeline code changes.

The page states the consequence: an edit changes who reads what and what counts as late;
it does not change what the model extracted, and it does not show what a reader was last
told. "Make Hamid the EM" moves lines between digests; it does not make the extractor
treat Hamid as the EM.

| Edit | Room world field | Reruns | Model calls |
|---|---|---|---|
| Reader role | `Persona.role` | `profile_for` (role and owned set), `select` | none |
| Channel membership | `Persona.channels` | `can_see`, `select` | none |
| Channel private flag | `Channel.private` | `can_see`, `select` | none |
| Owner role of an entity | `Entity.owner`; the entity leaves every `Persona.owned_entities` | `profile_for` owned set, `select` | none |
| Gate date | `Gate.date` | `detect_absences`, `select` | none |
| Task owner role | `ChecklistTask.owner` | `detect_absences`, `select` | none |
| Recorded scenario (stretch) | a room store event | `make_event`, extract, `select` | replayed from cassettes |

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

The room is a base and an ordered edit list, held in memory. Nothing else is state.

- Base (`RoomBase`): the recorded world, the priors, day 6 (2026-10-19), the base store
  replayed and extracted through that day, and each reader's recorded selection. Built
  once by `digest serve` at start.
- Edits: the ordered list of edits since start. A server restart starts from the
  recorded world.
- Derived, rebuilt after each edit: the room world, the three selections, diffs,
  reworded lines and the effect of the latest edit.

Rules:

- R1. Same base and same edits give byte-identical panels, on any machine.
- R2. Reset empties the edit list; the state equals the first load.
- R3. Room checks: every id in an edit exists in the recorded world; a persona edit names
  a human; a role is a `Role`; a gate date is a weekday on or after the world's
  `start_date` (it may fall after the world's last day). A refused edit returns its
  reason in plain words and is not added.
- R4. Atomic update: an edit builds a candidate state and replaces the current state only
  when every step succeeds. A refusal or an exception leaves the state unchanged.
- R5. Every edit sets an absolute value ("private = true", "role = EM"), never a toggle,
  so a repeated form post changes nothing.
- R6 (stretch). Scenarios are exclusive: a `RunScenario` rebuilds a room store from the
  base store and that one scenario, replacing any earlier one, so unit 7's rule S2 (each
  new message later than every stored event) holds and the requests are the recorded ones.
  The scenario's message is built against the recorded world, so a room edit cannot make
  it refuse.

## Serving

- One user, one process. `digest serve [--port 8000]` binds 127.0.0.1 only and prints the
  URL. The page is a plain HTML form page with no script: each edit is a form POST that
  redirects back to the page (post, redirect, get), so a browser refresh never repeats an
  edit.
- Threads. Every route is an `async def`, so all of them run on the event loop's one
  thread, and the base store is opened on that thread at start: `Store.open` uses
  `sqlite3.connect` with the default `check_same_thread` (`src/digest/store.py:99`), so a
  connection must not cross threads. Edits are therefore applied one at a time. A rebuild
  takes about 10 ms (P5), so the loop never waits long.
- Model calls. `digest serve` reads no key and does not build `Settings`, as in
  `digest demo`.
- Text. All text is rendered with Jinja autoescape.

## Units and branches

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 8 Room model and the selection diff | `m3-room` | `digest/room.py`; `diff_selections` in `digest/driver.py` | `tests/test_room.py`, `tests/test_driver.py` (the diff only) |
| 9 Web page and `digest serve` | `m3-web` | `digest/web/app.py`, `digest/web/templates/room.html`, `digest serve` in `digest/cli.py` | `tests/test_web.py` |

The diff types land first (PR 54): `diff_selections`, `LineView`, `Change` and
`ReaderDiff` in `digest/driver.py` as the unit 7 plan gives them, with `DiffField` as the
name of the field Literal that the plan writes inline, and `digest.driver` beside
`digest.eval` in the layers contract. Unit 7's stubs base on it; unit 7 adds
`make_event` and `Store.copy_to` beside it without moving it. The UI stubs commit adds
one line to `docs/plans/m3-driver.md` saying unit 8 of this plan builds and tests
`diff_selections`, which the session that owns unit 7 agreed on 2026-10-12.

A stubs commit on `m3-ui-stubs` follows: the types and signatures below raising
`NotImplementedError`; `line_parts` in `digest/daily/render.py` (below), with `_line`
rewritten to use it and no change to any rendered byte (unit 6's tests are the guard);
the template file; and the layers contract with `digest.cli` above `digest.web` above
`digest.room` above `digest.eval | digest.driver`. Unit 9 builds pages from hand-made
`RoomState` values, so units 8 and 9 run in parallel.

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
Edit = Annotated[SetRole | SetMembership | SetPrivate | SetOwner | SetGateDate
                 | SetTaskOwner, Field(discriminator="kind")]

class RoomBase(Frozen):              # built once by `digest serve`
    world: World                     # the recorded world
    priors: Priors
    day: date                        # 2026-10-19, world day 6
    store_path: Path                 # the base store, replayed and extracted through `day`
    readers: tuple[str, ...]         # U0ME01, U0SC01, U0EM01
    recorded: tuple[Selection, ...]  # each reader's selection in the recorded world, the diff baseline

class RoomState(Frozen):
    world: World                     # the room world
    edits: tuple[Edit, ...]          # since start or the last reset
    selections: tuple[Selection, ...]  # same order as RoomBase.readers
    diffs: tuple[ReaderDiff, ...]    # each reader against RoomBase.recorded
    reworded: tuple[tuple[str, ...], ...]  # per reader: item ids in both selections whose LineView is equal
                                           # but whose `line_parts` differ from the recorded ones
    last_effect: tuple[int, ...]     # per reader: lines added, removed, changed or reworded by the latest edit
    owners: dict[str, tuple[str, ...]]  # entity id -> user ids whose owned set holds it

def apply_world_edits(world: World, edits: Sequence[Edit]) -> World  # ValueError naming the edit on an R3 refusal
def build_state(base: RoomBase, store: Store, edits: Sequence[Edit]) -> RoomState
class Room:                          # owns the base store connection and the current state
    def apply(self, edit: Edit) -> RoomState   # validates, rebuilds; ValueError on a refusal, state unchanged
    def reset(self) -> RoomState
    def state(self) -> RoomState
```

`SetOwner(entity_id, role)` sets `Entity.owner` to the role (or `None`) and removes the
entity from every `Persona.owned_entities`, so the role is its only direct owner. The
owned set (`_owned_set`, `src/digest/daily/profile.py:33-50`) also adds the children of an
owned entity, so an entity whose `parent` or `subsystem` is owned stays owned by that
parent's owner. `RoomState.owners` comes from `_owned_set` for every human persona, and
the world panel shows it, so the page never claims a transfer the selection does not see.

`digest/web/app.py`: `create_app(room: Room) -> FastAPI`. Routes: `GET /` (the page),
`POST /edit` (one form per edit kind; redirects to `/`, or shows the page with the
refusal), `POST /reset` (redirects to `/`).

## Behaviours

B8.1 `apply_world_edits` on an empty list returns a world equal to the input. Each edit
kind changes exactly its field; an edit that breaks an R3 check raises `ValueError` naming
the edit and the id. Removing a persona from a channel they posted in is accepted.

B8.2 The recorded world and the base store are never changed: after any sequence of
edits, the base store file is byte-identical to before, `load_world` of the world file
equals the recorded world, and no edit builds a model request.

B8.3 Determinism: two `Room`s given the same edits give equal `RoomState` values.

B8.4 Reset: after `reset`, the selections equal `RoomBase.recorded` and every diff,
`reworded` entry and `last_effect` is empty or zero.

B8.5 Effects on day 6 (2026-10-19), each computed on the day-6 store with `model_copy`
worlds on 2026-10-12 and derived again by the test author from `world.yaml` and plan B5.3:
- `U0SC01` to role `EM`: the reader's role prior changes and the entities whose owner is
  `EM` join the owned set; `PO-45002211` stays, because `U0SC01` owns it directly
  (`world.yaml:89`, `profile.py:39`). The G-DVT1 EMC and thermal checklist absences and
  the top-cover DFM blocker become must-see lines, the thermal review blocker becomes
  must-see, and the stale drawing pack blocker and the ECO-0142 decision leave. The test
  pins the whole `ReaderDiff`.
- `#ext-sunrise-cm` (`C0EXTCM`) private: its members are `U0ME01`, `U0SC01`, `U0PM01` and
  `U0CM01`. `U0EM01` loses the decision "PN-51010 T1 gate date moved to 2026-10-21" and
  `hidden` goes from 0 to 4; `U0ME01` and `U0SC01` are unchanged.
- Then `U0SC01` out of `#ext-sunrise-cm`: `U0SC01` loses the must-see ask "Confirm anodize
  PO for DVT-1 brackets", the risk "PN-11020 chassis base built to stale Rev B drawing
  pack" and the T1 decision, gains two ranked lines that the freed caps admit, and
  `hidden` goes from 0 to 4.
- A persona added to or removed from a public channel: no change (`profile.py:52`).
- `U0ME01` or `U0SC01` added to the private `#kestrel-leads`: no line and no `hidden`
  count changes (measured: `hidden` 0 before and after), because no leads line clears a
  cap on day 6. `last_effect` is all zero and the page says the edit changed no digest.
- G-DVT1 moved (buffer 7 working days; an absence fires only when
  `buffer_start <= day <= gate date`, `src/digest/absence.py:41-43`), measured for
  `U0EM01`: to 2026-10-16 (before day 6) or 2026-10-29 (day 6 before the buffer) the EMC
  and thermal absences leave; to 2026-10-23 they stay with 3 of 7 buffer days used; to
  2026-10-28 they stay with 0 of 7. The last two leave every `LineView` equal and show in
  `reworded`.

B8.6 (stretch) `RunScenario("cm_bracket")` and `RunScenario("leads_gate")` each give, for
the room's three readers, the parts of the unit 7 acceptance results D2 and D3 that name
them, from committed cassettes, with no key in the process.

B8.7 `reworded` and `last_effect`: after G-DVT1 moves to 2026-10-23, `U0EM01`'s
`reworded` holds the two absence ids and its `ReaderDiff` is empty; after the leads
membership edit above, `last_effect` is zero for every reader.

B8.8 Atomicity: an edit whose build fails (a forced exception in `select`) leaves
`state()` equal to before.

B9.1 The page shows the world, digest and diff panels, the edit forms and the edit list.
All persona, channel, entity and item text is escaped: a name containing `<script>`
renders as text. No link on the page points at the Slack workspace.

B9.2 A valid edit POST redirects to `/`, and the next `GET /` shows the new digests and
diffs. A browser refresh after the redirect repeats nothing.

B9.3 A refused edit shows its reason next to the edit forms and leaves the digests
unchanged.

B9.4 The digest panel's heading and summary of every line equal `line_parts` for that
line, so the page and `digest run` use the same words.

B9.5 `digest serve` binds 127.0.0.1 and reads no key: it starts and serves with no
`OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` in the environment.

## Acceptance

- U1. From a fresh clone with no key and no network: `digest serve`, open the page, set
  `U0SC01` to EM, see the new digests and the diff; reset; the digests and diffs equal
  the first load.
- U2. B8.2 holds after every edit kind.
- U3. `just ci` is green, the layers contract places `digest.web` above `digest.room`,
  and every unit 6 render test passes unchanged after the `line_parts` change.

## Test seams

- `apply_world_edits` and `build_state` on the Kestrel world and the day-6 base store
  built once per test session from the committed cassettes.
- `diff_selections` on hand-built `Selection` values, as the unit 7 plan lists them.
- The web page through FastAPI's `TestClient`; no real browser in tests. U1 is checked by
  hand with the browser-verify skill.

## Premise ledger

| # | Premise | Check | Status |
|---|---|---|---|
| P1 | The extraction request carries the glossary and checklist tasks, so edits to them would miss cassettes | read `src/digest/extract/prompts.py:104-124` | verified |
| P2 | `select_digest` takes the world separately from the store | read `src/digest/daily/render.py:53-100` | verified |
| P3 | `detect_absences` reads gates from the project passed to it | `render.py:98`; `src/digest/absence.py:205-222` | verified |
| P4 | Building the day-6 base store from cassettes takes under 20 seconds | `digest demo` from a fresh clone: 9 seconds, 2026-10-12 | verified |
| P5 | A rebuild after an edit takes under 200 ms | three `build_digest` calls on the day-6 store: 10 to 12 ms; `load_world` 61 ms | verified |
| P7 | Core needs no unit 7 code beyond `diff_selections` | the cut list; unit 7 has stubs only on 2026-10-13 | verified |
| P8 | The room world can skip the `World` script checks | `model_copy` worlds ran through `select_digest` with no error, 2026-10-12 | verified, pinned by B8.1 |
| P9 | A gate moved past its buffer drops its checklist absences | `src/digest/absence.py:41-43` | verified |
| P10 | The B8.5 effects | computed on the day-6 store, 2026-10-12, by this session and by an independent reviewer | verified |

## Decided

- Scope: the presenter-operated room on the laptop, shown over screen share (the user,
  2026-10-13). Sharing with interviewers' own browsers is stretch.
- Hosting: none; the laptop serves 127.0.0.1 and the call shares the screen.

Defaults the user has not overruled (both reviewers and the advisor recommended them):

- Free-text messages: refused unless recorded; not in this plan.
- Schedule: the diff stub PR and the UI stubs PR merged by 10:00 PT on 2026-10-13; units 8
  and 9 merged by 18:00 PT; rehearsal on the morning of 2026-10-14. Past 18:00 PT on
  2026-10-13 with unit 9 not merged, the demo is `just demo` in a terminal.

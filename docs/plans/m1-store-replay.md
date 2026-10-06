# Plan: M1 store, replay, cassette and world schema

Status: draft v1, 2026-10-06. Process stage 1 (`docs/process.md`). Review by Codex
(`-p review`) and the Claude lens panel before any test is written.

M1 from `SPEC.md` section 9: skeleton, models, SQLite store, replay ingest, cassette
wrapper, world schema and generator scaffold, gate checklist config. Tests for store and
replay. Nothing in M1 calls a model. Everything in M1 runs offline.

## Units

M1 splits into three units. Units 2 and 3 do not depend on unit 1, so their tests and
code can be dispatched in parallel. Each unit is one test commit plus one code commit
on its own branch, reviewed in one sitting.

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 1 Store and replay | `m1-store-replay` | `digest/models.py`, `digest/store.py`, `digest/replay.py`, `digest/settings.py` | `tests/test_store.py`, `tests/test_replay.py` |
| 2 LLM client and cassette | `m1-llm-cassette` | `digest/llm/__init__.py`, `digest/llm/cassette.py`, `digest/llm/anthropic_api.py` | `tests/test_llm.py` |
| 3 World schema and project config | `m1-world-schema` | `digest/world/schema.py`, `digest/world/load.py`, `config/project.yaml`, `data/world/kestrel/world.yaml` | `tests/test_world.py` |

`digest/cli.py` gains `replay` (unit 1) and `world check` (unit 3). The `demo`, `run`
and `eval` stubs stay as they are.

## Shared conventions

- Time. The store holds UTC. Slack `ts` is the identity of a message and is a string
  with six fractional digits (`1760000000.000100`). It is never a float. The store
  keeps `ts` as TEXT and a derived `ts_us` INTEGER (microseconds since the epoch) for
  ordering and range queries.
- Clock. There is no module-level "now". Every read that depends on time takes an
  explicit `as_of: datetime` (UTC, timezone-aware). `digest replay --until DATE` takes a
  calendar date, which it converts to the end of that day in the world's timezone. The
  world's timezone and name are stored in the `meta` table at ingest.
- Thread identity. Every event has `thread_ts`. A top-level message has
  `thread_ts == ts`. The thread id is `f"{channel_id}:{thread_ts}"`. There is no
  threads table in M1; nothing reads one.
- Settings. `digest/settings.py` holds one pydantic-settings class `Settings` with
  prefix `DIGEST_`: `mode` (`replay` | `record`, default `replay`), `db` (path, default
  `data/digest.sqlite`), `cassettes` (path, default `data/cassettes`),
  `anthropic_model` (default read from `config/models.yaml` at implementation time,
  see D2). `*.sqlite` is gitignored; `data/cassettes/` and `data/world/` are committed.
- Logging. Stdlib `logging`, logger per module, event token plus `extra={...}`
  (`.claude/rules/logging.md`). No new logging dependency.
- Dependencies added: `pyyaml>=6` (runtime), `types-pyyaml` (dev). Nothing else.
- Errors. Corpus and config files are ours. A malformed line or an unknown reference
  raises with the file and line; nothing is skipped silently.

## Unit 1: store and replay

### Interface

`digest/models.py`

```python
class Reaction(BaseModel, frozen=True):
    name: str
    count: int

class Event(BaseModel, frozen=True):
    source: Literal["replay", "slack"]
    channel_id: str
    channel_name: str
    thread_ts: str          # equals ts for a top-level message
    ts: str                 # Slack ts, r"^\d{10}\.\d{6}$"
    user_id: str
    text: str
    mentions: tuple[str, ...] = ()
    reactions: tuple[Reaction, ...] = ()
    files: tuple[str, ...] = ()     # file titles; content is out of scope
    permalink: str
    edited: bool = False
    deleted: bool = False

    @property
    def thread_id(self) -> str
    @property
    def ts_us(self) -> int
    @property
    def at(self) -> datetime            # UTC

def permalink_for(workspace_url: str, channel_id: str, ts: str, thread_ts: str) -> str
```

`permalink_for` builds `{workspace_url}/archives/{channel_id}/p{ts without the dot}` and
appends `?thread_ts={thread_ts}&cid={channel_id}` for a reply. Links are constructed,
never model-written (D5).

`digest/store.py`

```python
class StageRun(BaseModel, frozen=True):
    stage: str                 # "extract", "link", ...
    thread_id: str
    last_ts: str               # ts of the newest non-deleted event the run saw
    prompt_version: str
    model: str
    run_at: datetime
    status: Literal["ok", "failed"]

class ThreadDelta(BaseModel, frozen=True):
    thread_id: str
    channel_id: str
    thread_ts: str
    last_ts: str
    since_ts: str | None       # last_ts of the previous ok run for this key, or None
    events: tuple[Event, ...]  # non-deleted events with since_ts < ts <= last_ts

class Store:
    @classmethod
    def open(cls, path: Path | Literal[":memory:"]) -> Store
    def close(self) -> None
    def __enter__ / __exit__

    def set_meta(self, key: str, value: str) -> None
    def get_meta(self, key: str) -> str | None

    def upsert_events(self, events: Iterable[Event]) -> int        # rows written
    def events_in_thread(self, thread_id: str, as_of: datetime) -> list[Event]
    def search_events(self, query: str, as_of: datetime, limit: int = 50) -> list[Event]

    def record_stage_run(self, run: StageRun) -> None
    def thread_deltas(self, stage: str, prompt_version: str, model: str, as_of: datetime) -> list[ThreadDelta]
```

`digest/replay.py`

```python
class IngestReport(BaseModel, frozen=True):
    read: int
    written: int
    after_until: int

def read_events(path: Path, workspace_url: str) -> Iterator[Event]
def ingest(store: Store, path: Path, until: datetime, workspace_url: str) -> IngestReport
```

CLI: `digest replay --world NAME --until YYYY-MM-DD [--db PATH]`. Resolves
`data/world/NAME/world.yaml` for `workspace_url` and `timezone` (unit 3 loader; until
unit 3 lands the command reads those two keys with `yaml.safe_load` directly), ingests
`data/world/NAME/events.jsonl`, writes `meta` (`world`, `timezone`, `workspace_url`,
`until`) and prints the report.

### Schema

One `digest/schema.sql`, applied with `executescript` on open; every statement is
`CREATE ... IF NOT EXISTS`. `meta` holds `schema_version = 1`. Opening a store with a
different version raises; there is no migration in v1.

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
  channel_id   TEXT NOT NULL,
  ts           TEXT NOT NULL,
  ts_us        INTEGER NOT NULL,
  thread_ts    TEXT NOT NULL,
  source       TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  text         TEXT NOT NULL,
  mentions     TEXT NOT NULL,   -- JSON array
  reactions    TEXT NOT NULL,   -- JSON array of {name, count}
  files        TEXT NOT NULL,   -- JSON array
  permalink    TEXT NOT NULL,
  edited       INTEGER NOT NULL DEFAULT 0,
  deleted      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_id, ts)
);
CREATE INDEX IF NOT EXISTS events_thread ON events (channel_id, thread_ts, ts_us);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts_us);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text, content='events', content_rowid='rowid',
  tokenize = "unicode61 tokenchars '-_'"
);
-- The three standard external-content triggers (after insert, after delete, after
-- update of text) keep events_fts in sync. `events` keeps its implicit rowid for
-- this reason; it is not WITHOUT ROWID.

CREATE TABLE IF NOT EXISTS stage_runs (
  stage          TEXT NOT NULL,
  thread_id      TEXT NOT NULL,
  last_ts        TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  run_at         TEXT NOT NULL,
  status         TEXT NOT NULL,
  PRIMARY KEY (stage, thread_id, last_ts, prompt_version, model)
) WITHOUT ROWID;
```

FTS note. A `WITHOUT ROWID` table cannot back an external-content FTS table, so
`events` keeps its rowid. The upsert must be `INSERT ... ON CONFLICT DO UPDATE` (one
statement) so the update trigger fires once; `INSERT OR REPLACE` would delete and
re-insert with a new rowid and the FTS delete trigger would see the old row.

Tokenizer. `tokenchars '-_'` keeps `PN-31010`, `ECO-0142`, `G-DVT1` and `TST-THM-02`
as single tokens, so a search for `PN-31010` does not match a bare `31010`. Entity-key
blocking in M3 is exact anyway; FTS is the second net.

Items, claims, profiles, impressions and feedback tables land with the stage that
first writes them (M2, M3). Their shape depends on the extraction schema, which M2
fixes. D4 lists them as the target, not as M1 scope.

### Behaviours

B1.1 Upsert is idempotent on `(channel_id, ts)`. Writing the same event twice leaves one
row. Writing an event with the same key and different `text`, `edited`, `deleted`,
`reactions`, `mentions` or `files` replaces those columns. `source`, `channel_name`,
`user_id`, `thread_ts` are also replaced (last write wins; the corpus is append-only
and later lines are corrections).

B1.2 Edits and deletes arrive as later JSONL lines with the same key. An edit carries
the new `text` and `edited: true`. A delete carries `deleted: true` and keeps the last
text. `events_in_thread` and `search_events` return deleted events with `deleted=True`
so callers can filter; `thread_deltas` excludes them.

B1.3 `ts` must match `^\d{10}\.\d{6}$`; `ts_us = seconds * 1_000_000 + micros`.
Ordering by `ts_us` equals ordering by the Slack timeline. `Event.at` is the UTC
datetime.

B1.4 Reading is bounded by `as_of`: `events_in_thread`, `search_events` and
`thread_deltas` never return an event with `at > as_of`.

B1.5 FTS: `search_events("PN-31010", as_of)` returns events whose text contains that
token and no event whose text contains only `31010`. The FTS index follows upserts:
after an edit, the old text no longer matches and the new text does.

B1.6 `thread_deltas(stage, pv, model, as_of)` returns one `ThreadDelta` per thread
whose `last_ts` (newest non-deleted event at or before `as_of`) has no `ok` row in
`stage_runs` for `(stage, thread_id, last_ts, pv, model)`. `since_ts` is the `last_ts`
of the newest `ok` run for `(stage, thread_id, pv, model)` with `last_ts < last_ts`,
else None. `events` are the non-deleted events with `since_ts < ts <= last_ts`, in
`ts_us` order. A thread whose only change since the last run is a deletion has the
same `last_ts` and is not returned. A thread with no events at or before `as_of` is
not returned. Changing `prompt_version` or `model` makes every thread pending again.

B1.7 `record_stage_run` upserts on the five-column key. A `failed` row does not
satisfy B1.6; the thread stays pending.

B1.8 `read_events` yields one `Event` per JSONL line. The line is the `Event` JSON
with these allowances: `thread_ts` may be absent (defaults to `ts`), `permalink` may
be absent (built with `permalink_for`), `source` may be absent (defaults to
`"replay"`), `mentions`, `reactions`, `files`, `edited`, `deleted` may be absent
(defaults). Any other missing field, an unknown field, or a bad `ts` raises
`ReplayFormatError` naming the file and the 1-based line number.

B1.9 `ingest` writes only events with `at <= until` and counts the rest in
`after_until`. Ingesting to D3 and then to D5 produces the same rows as ingesting to
D5 once (prefix property). Ingesting the same file twice to the same `until` changes
nothing.

B1.10 `Store.open(":memory:")` works for tests. `Store.open(path)` creates parent
directories, sets `journal_mode=WAL` and `foreign_keys=ON`, and is safe to call on an
existing file.

B1.11 `digest replay --world kestrel --until 2026-10-14` on the bundled minimal corpus
exits 0 and prints the report. A missing world raises a clear error, exit code 1.

### Tests

`tests/test_store.py`: B1.1 to B1.7, B1.10. Property tests with Hypothesis: upsert
idempotence over random event lists (B1.1), `ts` round trip through `ts_us` and back,
`thread_deltas` consistency after recording runs (every returned delta, once recorded
`ok`, disappears; no other delta changes).

`tests/test_replay.py`: B1.8, B1.9, B1.11 (CliRunner, tmp world dir). Property test:
prefix property over random `until` splits.

Each test file lists its RED break target in the dispatch brief. Required null cases
(`.claude/rules/testing.md`): an event with empty `mentions`/`reactions`/`files`; a
`ThreadDelta` with `since_ts=None`; `get_meta` on a missing key.

### Acceptance

- A8 (CI green in replay mode): unit tests touch no network.
- A4 groundwork: every stored event carries a permalink built from channel and ts.
- A1 groundwork: `digest replay` runs offline on the bundled corpus.

## Unit 2: LLM client and cassette

### Interface

`digest/llm/__init__.py`

```python
class Message(BaseModel, frozen=True):
    role: Literal["user", "assistant"]
    content: str

class LLMRequest(BaseModel, frozen=True):
    model: str
    system: str
    messages: tuple[Message, ...]
    schema: dict[str, Any] | None = None     # JSON schema for structured output
    temperature: float = 0.0
    max_tokens: int = 4096                   # not part of the cassette key

class LLMResponse(BaseModel, frozen=True):
    text: str                                # raw text or JSON text
    json: dict[str, Any] | None              # parsed when schema was given
    backend: str                             # "anthropic_api" | "claude_cli" | "codex_cli" | "fake"
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None

class Backend(Protocol):
    name: str
    def complete(self, request: LLMRequest) -> LLMResponse: ...

class LLM:
    def __init__(self, backend: Backend | None, cassettes: Cassettes, mode: Literal["replay", "record"])
    def complete(self, request: LLMRequest) -> LLMResponse
    def extract(self, request: LLMRequest, model_cls: type[T]) -> T
    def judge(self, request: LLMRequest, choices: type[E]) -> E       # E: Enum

def from_settings(settings: Settings) -> LLM
```

`digest/llm/cassette.py`

```python
def request_key(request: LLMRequest) -> str      # sha256 hex of canonical JSON
class Cassettes:
    def __init__(self, root: Path)
    def get(self, key: str) -> LLMResponse | None
    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> Path
class CassetteMiss(LookupError): ...
```

`digest/llm/anthropic_api.py`

```python
class AnthropicBackend:
    name = "anthropic_api"
    def __init__(self, client: anthropic.Anthropic)
    def complete(self, request: LLMRequest) -> LLMResponse
```

### Behaviours

B2.1 `request_key` hashes the canonical JSON of exactly `(model, system, messages,
schema, temperature)` (SPEC section 6): `json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=False)`. Two requests that differ only in
`max_tokens` share a key. Two schemas with the same content and different key order
share a key. Changing one character of `system` changes the key.

B2.2 Cassette file `data/cassettes/<key>.json` holds `{"key", "request", "response",
"recorded_at"}`. `request` is the `LLMRequest` dump (including `max_tokens`, for the
reader). `response` is the `LLMResponse` dump. The payload is backend-neutral: it is
what the backend returned after parsing, not the provider's wire message. This is what
makes replay backend-agnostic (D15). `put` writes atomically (temp file then rename).

B2.3 Mode `replay`: hit returns the stored response; miss raises `CassetteMiss` with
the key, the model and the first 80 characters of the last user message. No backend
is needed in replay mode (`backend=None` is allowed). Tests and CI run in this mode.

B2.4 Mode `record`: hit returns the stored response without calling the backend; miss
calls `backend.complete`, writes the cassette, returns the response. A second
identical call in the same process reads the file, not the backend. Record mode with
`backend=None` raises at construction.

B2.5 `extract` sends `request` with `schema = model_cls.model_json_schema()` when
`request.schema` is None, validates `response.json` with `model_cls.model_validate`,
and raises `LLMOutputError` (carrying the raw text) when validation fails. No retry in
M1: the first recording in M2 shows whether retries are needed. `judge` builds the
schema `{"type": "object", "properties": {"choice": {"enum": [...]}}, "required":
["choice"]}` and returns the enum member.

B2.6 `AnthropicBackend.complete` calls `client.messages.create` with `model`,
`system`, `messages`, `max_tokens`, `temperature` and, when `schema` is given,
`output_config={"format": {"type": "json_schema", "schema": schema}}` (verified
present in anthropic 1.11.0). It reads the first text block, parses JSON when a
schema was given, and fills token counts from `usage`. A response with
`stop_reason == "max_tokens"` or `"refusal"` raises `LLMOutputError`. The test injects
`anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=
httpx2.MockTransport(handler)))` and asserts the request body and the parsed
response. No `respx`, no `vcrpy` (D13).

B2.7 `from_settings` builds `LLM` from `Settings`: replay mode gets no backend; record
mode gets `AnthropicBackend` with a client from `ANTHROPIC_API_KEY`.

B2.8 Logging: one `llm_call` record per `complete` with `key`, `mode`, `hit: bool`,
`backend`, `model`, token counts. Never the prompt text.

### Tests

`tests/test_llm.py`: B2.1 (plus a Hypothesis test: the key is invariant under schema
key reordering and `max_tokens` changes), B2.2 to B2.8. A `FakeBackend` recording the
requests it saw is the seam for B2.3 and B2.4; it is a test-local class, not a
production module.

### Acceptance

- SPEC section 6: one client wrapper, cassettes keyed by the canonical hash, strict
  replay raises on a miss. A8: CI in replay mode.

## Unit 3: world schema and project config

### Interface

`digest/world/schema.py`

```python
Role = Literal["ME", "EE", "FW", "supply_chain", "EM", "PM"]
Phase = Literal["concept", "EVT", "DVT", "PVT", "ramp"]
EntityType = Literal["part", "assembly", "pcba", "harness", "molded", "battery",
                     "purchased", "component", "eco", "ecr", "deviation", "fa", "po",
                     "gate", "build", "test", "release", "vendor"]
Attribute = Literal["rev", "lead_time", "gate_date", "owner", "status", "qty", "spec_param"]

class ChecklistTask(BaseModel, frozen=True):
    task: str
    owner: Role
    expected_cadence_days: int

class Gate(BaseModel, frozen=True):
    id: str
    name: str
    date: date
    buffer_days: int
    checklist: tuple[ChecklistTask, ...] = ()

class PhaseWindow(BaseModel, frozen=True):
    phase: Phase
    from_date: date

class ProjectConfig(BaseModel, frozen=True):
    project_id: str
    name: str
    phases: tuple[PhaseWindow, ...]      # ascending from_date
    gates: tuple[Gate, ...]
    def phase_on(self, day: date) -> Phase

class Entity(BaseModel, frozen=True):
    id: str
    type: EntityType
    description: str
    owner: Role | None = None
    vendor: str | None = None
    state: dict[Attribute, str] = {}     # initial attribute values, e.g. {"rev": "B"}

class Persona(BaseModel, frozen=True):
    user_id: str                         # Slack-style, e.g. "U0ME01"
    name: str
    role: Role
    channels: tuple[str, ...]
    owned_entities: tuple[str, ...] = ()

class Channel(BaseModel, frozen=True):
    id: str
    name: str
    purpose: str

class WorldClaim(BaseModel, frozen=True):
    entity: str
    attribute: Attribute
    value: str
    authority: Literal["owner_stated", "plm_bot", "cm_stated", "hearsay"]

class WorldEvent(BaseModel, frozen=True):
    id: str
    day: int                             # 1-based index from world.start_date
    channel: str
    poster: str                          # persona user_id
    kind: Literal["decision", "blocker", "risk", "schedule_change", "spec_change",
                  "bom_change", "ask", "fyi", "distractor"]
    summary: str                         # ground truth, one sentence
    scenario: str | None = None          # "S1".."S15", "N1".."N5"
    claims: tuple[WorldClaim, ...] = ()
    affected_roles: tuple[Role, ...] = ()
    must_see_for: tuple[str, ...] = ()   # persona user_ids
    known_by: dict[str, int] = {}        # persona user_id -> day they learn it
    noise: dict[str, bool] = {}          # renderer knobs: indirection, wrong_claim, typo, delayed_crosspost

class World(BaseModel, frozen=True):
    name: str
    program: str
    workspace_url: str
    timezone: str                        # IANA name
    start_date: date
    days: int
    project: ProjectConfig
    channels: tuple[Channel, ...]
    personas: tuple[Persona, ...]
    entities: tuple[Entity, ...]
    events: tuple[WorldEvent, ...]
```

`digest/world/load.py`

```python
def load_world(path: Path) -> World
def load_project(path: Path) -> ProjectConfig
```

CLI: `digest world check --world NAME` loads and validates, prints counts per day and
per channel, exit 0; validation errors print the path and exit 1.

### Behaviours

B3.1 Cross-references validate in a `model_validator` on `World`: every
`event.channel` is a channel id; every `event.poster`, `must_see_for` entry and
`known_by` key is a persona user_id; every `claim.entity` and `persona.owned_entities`
entry is an entity id; every `persona.channels` entry is a channel id; every
`event.day` and `known_by` value is in `1..days`; a poster is a member of the channel
they post in; event ids are unique; entity ids are unique. Each failure names the
event or persona id.

B3.2 `ProjectConfig.phases` must be ascending by `from_date` with distinct dates; the
first `from_date` must be at or before `world.start_date` when embedded in a world.
`phase_on(day)` returns the phase of the latest window with `from_date <= day`.

B3.3 Gate ids are unique; `buffer_days >= 0`; `expected_cadence_days >= 1`; every
gate `date` is within the world's day range when embedded in a world.

B3.4 `load_world` reads YAML with `yaml.safe_load` and validates. An unknown top-level
key is an error (`extra="forbid"` on every model). `load_project` does the same for
`config/project.yaml`.

B3.5 The bundled fixture `data/world/kestrel/world.yaml` validates and contains: the
program "Kestrel K2"; the six roles as personas; channels `#kestrel-program`,
`#eng-mech`, `#eng-elec-fw`, `#supply-chain`, `#dvt1-build`, `#ext-sunrise-cm`; the
entity table from the domain research (at least PN-11051, PN-31010, PN-21030,
PN-51010, PN-61001, ECO-0142, DEV-0007, PO-45002211, G-DVT1, G-TR1, TST-THM-02,
BLD-DVT1); gates G-EVT2-EXIT (day 1), G-TR1 (day 7), G-DVT1 (day 9, buffer 7) with
checklists; phase windows EVT then DVT; scenario events S1 to S15 and N1 to N5 with
`known_by` and `must_see_for`. Day 1 is Monday 2026-10-12, `days = 10`, timezone
`America/Los_Angeles`. `digest world check --world kestrel` exits 0. The rendered
`events.jsonl` for this world is M2 work; M1 ships a 10-line human-authored
`events.jsonl` covering S1 and N1 so `digest replay` has input.

B3.6 `config/project.yaml` is the same `ProjectConfig` shape as `world.project`,
written for the live workspace. It validates with `load_project`.

### Tests

`tests/test_world.py`: B3.1 (parametrized over each broken reference), B3.2, B3.3,
B3.4, B3.5 (loads the bundled file and asserts the named counts), B3.6, the `world
check` CLI on a tmp copy with one broken reference. Null cases: a persona with no
owned entities; an event with no claims and no `known_by`; a gate with an empty
checklist.

### Acceptance

- SPEC section 6 (world.yaml shape) and section 5 step 4 (gates and checklists from
  config). A2 and A6 groundwork.

## Decisions recorded in this plan

| Fork | Chosen | Why |
|---|---|---|
| One unit or three | Three | Units 2 and 3 are independent of unit 1; parallel dispatch halves the wall clock and keeps each review under an hour. |
| Which tables in M1 | `meta`, `events`, `events_fts`, `stage_runs` | Only tables with an M1 reader. `stage_runs` has one: `thread_deltas`, which M2 extraction consumes. |
| `ts` storage | TEXT plus derived INTEGER micros | Float loses the sixth digit. TEXT keeps the Slack identity; INTEGER orders and ranges. |
| Threads table | None | Derived from `thread_ts`; no reader in M1. |
| Where the clock lives | Explicit `as_of` on every read; `--until` filters ingest | No global state; `run --as-of` in M3 reuses the same bound. |
| Day boundary for `--until` | End of day in the world's timezone, stored in `meta` | A US team's day ends at midnight Pacific; the store stays UTC. |
| Reactions timing | Carried on the message line, not simulated | Simulating reaction time adds an event kind nobody reads in v1. Recorded in `docs/later.md` if M3 ranking wants reaction recency. |
| Cassette payload | Parsed `LLMResponse`, not the provider message | Backend-neutral replay (D15). The `anthropic.types.Message` form from the research note would tie cassettes to one backend. |
| Cassette key fields | Exactly SPEC section 6's five | `max_tokens` excluded so a limit change does not invalidate recordings. |
| Replay strictness | Replay always raises on a miss; record reads before calling | Two modes are enough; a lenient replay would hide a missing recording until CI. |
| Backends in M1 | Protocol, cassette, `anthropic_api`; CLI backends later | The Claude Code CLI is not installed on this machine (premise P7), so `claude_cli` cannot be verified in M1. `codex_cli` lands with the renderer. See open fork F1. |
| Retry on invalid output | None in M1 | No evidence yet that retries help; the M2 recording run is the evidence. |
| YAML library | pyyaml | SPEC names `.yaml` files; `tomllib` reads only TOML. Adds one dependency plus stubs. |
| FTS table shape | External-content with triggers; `events` keeps a rowid | Documented pattern; no sync code in `store.py`. |

## Open forks for the user

F1. Backends in M1. Recommended: cassette plus `anthropic_api` now; `claude_cli` as a
small follow-up unit once `npm install -g @anthropic-ai/claude-code` is done and
`claude -p --output-format json` flags are verified. Alternative: install the CLI
first and include `claude_cli` in unit 2 (adds about 40 lines and a fake-binary test).

F2. Timezone for `--until`. Recommended: the world's timezone (`America/Los_Angeles`
for Kestrel). Alternative: UTC everywhere, simpler but the demo's "Tuesday" ends at
17:00 Pacific.

F3. Dispatch shape. Recommended: Codex writes all three test files in parallel (three
`codex exec` sessions, one per unit, fresh context each), then three Opus subagents
implement in parallel in separate worktrees. Alternative: sequential, one unit at a
time, slower but one review at a time.

## Premise ledger

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| P1 | SQLite in uv's Python 3.12 has FTS5 | `uv run python -c "create virtual table t using fts5(x)"` ok; sqlite 3.49.1 | verified |
| P2 | anthropic 1.11.0 exposes `output_config.format` with `type: json_schema` | `inspect.signature(Messages.create)` has `output_config`; `JSONOutputFormatParam` has `schema`, `type` | verified |
| P3 | anthropic 1.x runs on httpx2 2.13.1; httpx mocks would miss | `import httpx2` 2.13.1; D13 | verified |
| P4 | `data/cassettes/` and `data/world/` are not gitignored | `.gitignore` ignores `data/live/`, `*.sqlite`, `*.db` only | verified |
| P5 | `hypothesis`, `time-machine`, `pytest` are dev deps | `pyproject.toml` `[dependency-groups] dev` | verified |
| P6 | pyyaml is not installed | `uv run python -c "import yaml"` ModuleNotFoundError | verified; plan adds it |
| P7 | The Claude Code CLI is installed | `Get-Command claude` not found; no `claude` under npm global or AppData | refuted; drives F1 |
| P8 | `prove-test` is callable from Git Bash as `prove-test` | `command not found`; works as `python ~/.local/bin/prove-test.py` | refuted; mechanics below use the python form |
| P9 | `prove-test` records a proof with `--break blank:<file>:<a>-<b>` and `--expect-error` | proof written for `tests/test_cli.py` on 2026-10-06 | verified |
| P10 | Codex `exec` has `-p`, `-C`, `-o`, `--output-schema` | `codex exec --help`; handoff verified `--output-schema` on 2026-10-06 | verified |
| P11 | Slack permalink shape `archives/<C>/p<ts no dot>` plus `?thread_ts=&cid=` for replies | Slack docs, not re-fetched; replay links are synthetic | accepted-assumption |
| P12 | Slack `ts` seconds part is 10 digits through 2286 | arithmetic | verified |
| P13 | Reaction timing can be ignored in v1 | no v1 consumer of reaction recency in SPEC sections 5 to 8 | accepted-assumption |

## Mechanics for this milestone

- Prove a test: `python ~/.local/bin/prove-test.py --test tests/test_store.py --break
  blank:src/digest/store.py:N-M --run "uv run pytest tests/test_store.py" --format
  "uv run ruff format tests/test_store.py" --expect-error "<specific line>"`. The
  pattern must be specific; generic words (`assert`, `failed`, `Error`) are refused.
- Test author brief (Codex, execution lane, fresh session, `-C` the worktree): this
  plan, `SPEC.md` sections 4 to 6, the interface stubs committed with `raise
  NotImplementedError`, and `.claude/rules/testing.md`. It writes only
  `tests/test_<unit>.py` and hands back unstaged.
- Implementer brief (Opus subagent, own worktree): this plan, the stubs, the tests,
  the rules under `.claude/rules/`. It may not edit `tests/`. Done when `uv run pytest
  tests/test_<unit>.py`, `uv run ruff check .`, `uv run ruff format --check .` and
  `uv run mypy` pass. Before touching `digest/llm/anthropic_api.py` it loads the
  `claude-api` skill and records the chosen model ids in `config/models.yaml`.
- Reviews: Codex `timeout 600 codex exec -p review --output-schema
  ~/Documents/dev-kit/codex/schemas/review.json -o <file> "..." </dev/null` and
  `/review-local --base main` on each branch after the test commit and after the code
  commit. Neither sees the other's findings.

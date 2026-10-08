# Plan: M1 store, replay, cassette and world schema

Status: v3.5, 2026-10-08, after four review rounds (Codex `-p review` and Claude lenses)
and the user's design review (SPEC v0.3). Process stage 1 (`docs/process.md`).
Converges before any test is written.

M1 from `SPEC.md` section 9: skeleton, models, SQLite store, replay ingest, cassette
wrapper, world schema and generator scaffold, gate checklist config. Tests for store and
replay. Nothing in M1 calls a model. Everything in M1 runs offline.

## Units and branches

M1 splits into three units, each its own pull request into `main`. First a `m1-stubs`
branch lands by pull request: interfaces that raise `NotImplementedError`,
`digest/settings.py`, `digest/models.py`, the `load_world_header` function, the
import-linter contract, the dependency and `.gitignore` changes. Each unit branch forks
from `main` after that merge, lands one test commit and one code commit, is reviewed,
and is merged by the user. Beyond the stubs, units share one file, `digest/cli.py`:
the stubs commit adds the `replay` command and the `world check` command with their
full option declarations and bodies that call the file's `_not_yet` helper, and units 1
and 3 each fill one body. Every other
file belongs to one unit, so tests and code run in parallel and the three pull requests
merge in any order; a unit branch rebases on `main` if `digest/cli.py` conflicts.

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 1 Store and replay | `m1-store-replay` | `digest/store.py`, `digest/schema.sql`, `digest/replay.py` | `tests/test_store.py`, `tests/test_replay.py` |
| 2 LLM client and cassette | `m1-llm-cassette` | `digest/llm/__init__.py`, `digest/llm/cassette.py`, `digest/llm/client.py`, `digest/llm/anthropic_api.py`, `digest/llm/openrouter.py`, `config/models.yaml` | `tests/test_llm.py` |
| 3 World schema and project config | `m1-world-schema` | `digest/project.py`, `digest/world/schema.py`, `digest/world/load.py`, `config/project.yaml`, `data/world/kestrel/world.yaml`, `data/world/kestrel/events.jsonl` | `tests/test_world.py`, `tests/test_project.py` |

`digest/cli.py` gains `replay` (unit 1) and `world check` (unit 3). The `demo`, `run`
and `eval` stubs stay as they are.

## Shared conventions

- Time. The store holds UTC. Slack `ts` is the identity of a message and is a string
  with ten integer and six fractional digits (`1760000000.000100`). It is never a
  float. The store keeps `ts` as TEXT and a derived `ts_us` INTEGER (microseconds since
  the epoch) for ordering and range queries.
- Two times per event. `ts` is when the message was posted. `event_ts` is when this
  version of the message was observed: equal to `ts` for the original, later for an
  edit or a delete. Replay ingest filters on `event_ts`.
- The store is a snapshot. A store holds the world as observed up to `meta.until`, one
  version per message. Replay only moves forward: `--until` earlier than `meta.until`
  is refused; delete the file to start over. A digest for day D (M3 `run --as-of D`) is
  computed on a store whose `meta.until` is day D, and `run` refuses otherwise. The
  eval (M4) replays forward one day at a time and runs each day in turn. This is what
  makes dated playback deterministic without version history.
- Clock. There is no module-level "now". Every read that depends on time takes an
  explicit `as_of: datetime` (UTC, timezone-aware). `digest replay --until DATE` takes
  a calendar date and converts it to the end of that day in the world's timezone.
  `meta` records `world`, `timezone`, `workspace_url` and `until`.
- Thread identity. Every event has `thread_ts`. A top-level message has
  `thread_ts == ts`. The thread id is `f"{channel_id}:{thread_ts}"`. There is no
  threads table; nothing reads one.
- Shared types. `digest/models.py` holds `Role`, `Phase`, `Attribute`, `Event` and
  `Reaction`. `digest/project.py` holds `ProjectConfig`, `Gate`, `ChecklistTask`,
  `PhaseWindow` and `load_project`, because the absence detector (M2) and the live
  configuration read them, not only the simulator. `digest/world/` imports from both.
- Settings. `digest/settings.py` holds one pydantic-settings class `Settings` with
  prefix `DIGEST_`: `mode` (`replay` | `record` | `live`, default `replay`), `backend`
  (`anthropic_api` | `openrouter`, default `openrouter`), `world` (default `kestrel`),
  `db` (path or None; None means `data/<world>.sqlite`), `cassettes` (path, default
  `data/cassettes`). Keys come from `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY`.
  Model ids live in `config/models.yaml` (unit 2). `.env.example` gains
  `DIGEST_BACKEND`, `DIGEST_WORLD` and `OPENROUTER_API_KEY` lines in the stubs commit.
- Project configuration source. In replay the project configuration is
  `world.project` from the selected world; in live it is `config/project.yaml`. The
  absence detector (M2) takes a `ProjectConfig` and does not know which.
- Logging. Stdlib `logging`, logger per module, event token plus `extra={...}`
  (`.claude/rules/logging.md`). No new logging dependency.
- Dependencies added in the stubs commit: `pyyaml>=6` (runtime), `types-pyyaml` and
  `import-linter` (dev). The `corpus` extra (`openai`) is removed: the renderer calls
  the gateway (D12 as amended, D15). `.gitignore` gains `*.sqlite-wal` and `*.sqlite-shm` (WAL side files; only
  `*.sqlite-journal` is ignored today).
- Errors. Corpus and config files are ours. A malformed line or an unknown reference
  raises with the file and line; nothing is skipped silently.
- Import contract. `import-linter` (dev group; `uv run lint-imports` in CI and the
  justfile) holds one layers contract, top to bottom: `digest.cli`; `digest.replay`;
  `digest.store`, `digest.llm` and `digest.world` as independent siblings;
  `digest.project`; `digest.models` and `digest.settings`. A module imports only from
  its own layer's dependencies or a lower layer. A forbidden contract with
  `include_external_packages` keeps `anthropic` and `httpx2` importable only inside
  `digest.llm`: the SDK and the gateway's HTTP client are the two ways a model gets
  called, so a call from any other module fails the gate (the rule AGENTS.md states).
  The stubs commit adds both contracts and the three unit branches stay inside them.

## Unit 1: store and replay

### Interface

`digest/models.py` (stubs commit)

```python
Role = Literal["ME", "EE", "FW", "supply_chain", "EM", "PM"]
Phase = Literal["concept", "EVT", "DVT", "PVT", "ramp"]
Attribute = Literal["rev", "lead_time", "gate_date", "owner", "status", "qty", "spec_param", "priority"]
Priority = Literal["critical", "high", "normal", "low"]
Authority = Literal["owner_stated", "plm_bot", "cm_stated", "hearsay"]

class Reaction(BaseModel, frozen=True):
    name: str
    user_id: str
    ts: str                 # when this user reacted; Slack ts format

class Event(BaseModel, frozen=True):
    source: Literal["replay", "slack"]
    channel_id: str
    channel_name: str
    thread_ts: str          # equals ts for a top-level message
    ts: str                 # r"^\d{10}\.\d{6}$"
    event_ts: str           # same format; >= ts; equals ts for the original post
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
    def at(self) -> datetime            # from ts, UTC
    @property
    def observed_at(self) -> datetime   # from event_ts, UTC

def permalink_for(workspace_url: str, channel_id: str, ts: str, thread_ts: str) -> str
```

`permalink_for` builds `{workspace_url}/archives/{channel_id}/p{ts without the dot}` and
appends `?thread_ts={thread_ts}&cid={channel_id}` for a reply. Links are constructed,
never model-written (D5).

`digest/world/load.py` (stubs commit, implemented, not a stub)

```python
class WorldHeader(BaseModel, frozen=True):
    name: str
    workspace_url: str
    timezone: str

def load_world_header(path: Path) -> WorldHeader   # yaml.safe_load, reads three keys, ignores the rest
```

`digest/store.py`

```python
class StageRun(BaseModel, frozen=True):
    stage: str                 # "extract", "link", ...
    thread_id: str
    last_ts: str               # thread revision: newest event_ts in the thread (any row, deleted included)
    prompt_version: str
    model: str
    run_at: datetime
    status: Literal["ok", "failed"]

class ThreadDelta(BaseModel, frozen=True):
    thread_id: str
    channel_id: str
    thread_ts: str
    last_ts: str               # newest event_ts in the thread at as_of
    since_ts: str | None       # last_ts of the previous ok run for (stage, thread, pv, model), or None
    root: Event | None         # the top-level message; None when the store has no row with ts == thread_ts
    changed: tuple[Event, ...] # rows with since_ts < event_ts <= last_ts, ts order; deleted rows included

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

class ReplayFormatError(ValueError): ...

def read_events(path: Path, workspace_url: str) -> Iterator[Event]
def ingest(store: Store, path: Path, until: datetime, workspace_url: str) -> IngestReport
```

CLI: `digest replay --until YYYY-MM-DD [--world NAME] [--db PATH]`. `--world` defaults
to `Settings.world`. Reads `load_world_header(data/world/NAME/world.yaml)`, opens the
store at `--db` or `data/NAME.sqlite`, refuses a store whose `meta.world` is a
different world or whose `meta.until` is later than the requested day, ingests
`data/world/NAME/events.jsonl`, writes `meta` and prints the report.

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
  event_ts     TEXT NOT NULL,
  event_ts_us  INTEGER NOT NULL,
  thread_ts    TEXT NOT NULL,
  source       TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  text         TEXT NOT NULL,
  mentions     TEXT NOT NULL,   -- JSON array of user ids
  reactions    TEXT NOT NULL,   -- JSON array of {name, user_id, ts}
  files        TEXT NOT NULL,   -- JSON array of titles
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
-- three external-content triggers: after insert, after delete, after update of text

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

FTS note. `events` keeps its rowid because an external-content FTS table needs one.
The upsert is one `INSERT ... ON CONFLICT (channel_id, ts) DO UPDATE` statement so the
update trigger fires. `INSERT OR REPLACE` must not be used: with the default
`recursive_triggers=OFF` the implicit delete does not fire the delete trigger, and the
old text stays searchable (verified on sqlite 3.49.1, premise P14).

Query note. An FTS5 bareword cannot contain `-`; `MATCH 'PN-31010'` is a syntax error
(verified, P15). `search_events` splits the query on whitespace and quotes every term
as a phrase: `"PN-31010" "Rev"`. Callers never pass raw FTS syntax.

Tokenizer. `tokenchars '-_'` keeps `PN-31010`, `ECO-0142`, `G-DVT1` and `TST-THM-02`
as single tokens, so a search for `PN-31010` does not match a bare `31010` (verified,
P15). Entity-key blocking in M3 is exact anyway; FTS is the second net.

Items, claims, entities, profiles, impressions and feedback tables land with the
stage that first writes them (M2, M3). Their shape depends on the extraction schema,
which M2 fixes. D4 lists them as the target, not as M1 scope.

### Behaviours

B1.1 Upsert is idempotent on `(channel_id, ts)`. Writing the same event twice leaves one
row. Writing an event with the same key replaces every other column (last write wins;
the corpus is append-only and later lines are corrections).

B1.2 Edits and deletes arrive as later JSONL lines with the same `(channel_id, ts)`,
a later `event_ts`, and `edited: true` or `deleted: true`. An edit carries the new
`text`; a delete keeps the last text. `events_in_thread` and `search_events` return
deleted rows with `deleted=True` so callers can filter.

B1.3 `ts` and `event_ts` must match `^\d{10}\.\d{6}$` and `event_ts >= ts`;
`ts_us = seconds * 1_000_000 + micros`. Ordering by `ts_us` equals ordering by the
Slack timeline. `Event.at` and `Event.observed_at` are UTC datetimes.

B1.4 Reads are bounded by `as_of` on `ts`: `events_in_thread`, `search_events` and
`thread_deltas` never return a row with `at > as_of`. The store holds one version per
message (see "The store is a snapshot").

B1.5 FTS: `search_events("PN-31010", as_of)` returns rows whose text contains that
token and no row whose text contains only `31010`. After an edit, the old text no
longer matches and the new text does.

B1.6 `thread_deltas(stage, pv, model, as_of)` returns one `ThreadDelta` per thread
that has at least one row with `at <= as_of` and whose `last_ts` (newest `event_ts`
over all its rows with `at <= as_of`, deleted rows included) has no `ok` row in
`stage_runs` for `(stage, thread_id, last_ts, pv, model)`. `since_ts` is the
`last_ts` of the newest `ok` run for `(stage, thread_id, pv, model)` with a smaller
`last_ts`, else None. `changed` holds the rows with `since_ts < event_ts <= last_ts`
(all rows when `since_ts` is None), in `ts_us` order, deleted rows included. `root` is
the row with `ts == thread_ts`, or None when the store has no such row (a reply whose
parent was never ingested; legal in replay and expected in a live backfill window).
Reactions are not part of `last_ts`: a reaction changes exposure, which M3 reads from
the row under `as_of`, not what the thread says, which is what extraction reads. An
edit or a delete of an older message bumps `last_ts` and makes the thread pending
again. A thread whose every row is deleted is still returned once (its `changed` rows
carry `deleted=True`) so the consumer can retract. Changing `prompt_version` or
`model` makes every thread pending again.

B1.7 `record_stage_run` upserts on the five-column key. A `failed` row does not
satisfy B1.6; the thread stays pending.

B1.8 `read_events` yields one `Event` per JSONL line. The line is the `Event` JSON
with these allowances: `thread_ts` absent means `ts`; `event_ts` absent means `ts`;
`permalink` absent means `permalink_for(...)`; `source` absent means `"replay"`;
`mentions`, `reactions`, `files`, `edited`, `deleted` absent mean their defaults. Any
other missing field, an unknown field, a bad `ts`, or an `event_ts` lower than the
previous line's `event_ts` raises `ReplayFormatError` naming the file and the 1-based
line. Lines are therefore sorted by `event_ts`; the renderer (M2) writes them so.

B1.9 `ingest` writes only lines with `observed_at <= until` and counts the rest in
`after_until`. Within a written line, reactions with `ts` later than `until` are
dropped before the upsert. Ingesting to D3 and then to D5 produces the same rows as
ingesting to D5 once (prefix property; it holds because lines are applied in
`event_ts` order and every line with `observed_at <= until` is re-applied on each
ingest). Ingesting the same file twice to the same `until` changes nothing.

B1.10 `Store.open(":memory:")` works for tests. `Store.open(path)` creates parent
directories, sets `journal_mode=WAL` and `foreign_keys=ON`, and is safe to call on an
existing file.

B1.11 `digest replay --until 2026-10-13` with the default world on the bundled corpus
exits 0 and prints the report. A missing world, a store whose `meta.world` differs,
or an `--until` earlier than `meta.until` prints the reason and exits 1.

B1.12 `load_world_header` reads a world file that has more keys than the three it
returns and ignores them; a missing key raises.

### Tests

`tests/test_store.py`: B1.1 to B1.7, B1.10. Property tests with Hypothesis: upsert
idempotence over random event lists (B1.1), `ts` round trip through `ts_us` and back,
`thread_deltas` consistency after recording runs (every returned delta, once recorded
`ok`, disappears; no other delta changes).

`tests/test_replay.py`: B1.8, B1.9, B1.11, B1.12 (CliRunner, tmp world dir). Property
test: prefix property over random `until` splits of a generated, `event_ts`-sorted
file.

Required null cases (`.claude/rules/testing.md`): an event with empty `mentions`,
`reactions` and `files`; a `ThreadDelta` with `since_ts=None`; a `ThreadDelta` with
`root=None`; `get_meta` on a missing key; a thread whose root is deleted.

### Acceptance

- A8 (CI green in replay mode): unit tests touch no network.
- A4 groundwork: every stored event carries a permalink built from channel and ts.
- A1 groundwork: `digest replay` runs offline on the bundled corpus.
- A5 groundwork: reactions keep reactor and time, so `exposed.how = reacted` (SPEC
  section 4) can be dated in M3.

## Unit 2: LLM client and cassette

### Interface

`digest/llm/__init__.py` holds the types; `digest/llm/client.py` holds the wrapper,
because `cassette.py` imports the types and the wrapper imports `cassette.py`, so one
module would be an import cycle.

```python
LogicalModel = Literal["extract", "adjudicate", "render", "judge", "classify"]

class Message(BaseModel, frozen=True):
    role: Literal["user", "assistant"]
    content: str

class LLMRequest(BaseModel, frozen=True):
    model: LogicalModel
    system: str
    messages: tuple[Message, ...]
    output_schema: dict[str, Any] | None = None  # JSON schema for structured output; not `schema`, which pydantic's BaseModel owns
    temperature: float = 0.0
    max_tokens: int = 4096                   # not part of the cassette key

class LLMResponse(BaseModel, frozen=True):
    text: str                                # raw text or JSON text
    data: dict[str, Any] | None              # parsed when output_schema was given; not `json`, which BaseModel owns
    backend: str                             # "anthropic_api" | "openrouter" | "fake"
    model: str                               # the backend's concrete model id
    input_tokens: int | None = None
    output_tokens: int | None = None

class LLMOutputError(ValueError): ...        # carries .raw: str

class Backend(Protocol):
    name: str
    def complete(self, request: LLMRequest) -> LLMResponse: ...

class ModelIds(BaseModel, frozen=True):       # config/models.yaml: logical name -> {backend: concrete id}
    extract: dict[str, str]
    adjudicate: dict[str, str]
    render: dict[str, str]
    judge: dict[str, str]
    classify: dict[str, str]
    def resolve(self, logical: LogicalModel, backend: str) -> str

def load_model_ids(path: Path = DEFAULT_MODEL_IDS_PATH) -> ModelIds   # config/models.yaml
```

`digest/llm/client.py`

```python
class LLM:
    def __init__(self, backend: Backend | None, cassettes: Cassettes, mode: Literal["replay", "record", "live"])
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
    def __init__(self, client: anthropic.Anthropic, models: ModelIds)
    def complete(self, request: LLMRequest) -> LLMResponse
```

`digest/llm/openrouter.py`

```python
class OpenRouterBackend:
    name = "openrouter"
    def __init__(self, api_key: str, models: ModelIds, client: httpx2.Client | None = None, timeout_s: float = 120)
    def complete(self, request: LLMRequest) -> LLMResponse
```

### Behaviours

B2.1 `request_key` hashes the canonical JSON of `request.model_dump(exclude={"max_tokens"})`:
the keys `model`, `system`, `messages` (a list of `{"role", "content"}` objects),
`output_schema` (`null` when absent) and `temperature`. SPEC section 6 names the same
five fields; `schema` there is `output_schema` in code. The JSON is `json.dumps(...,
sort_keys=True, separators=(",", ":"), ensure_ascii=False)` and the key is its sha256
hex digest. Two requests that differ only in
`max_tokens` share a key. Two schemas with the same content and different key order
share a key. Changing one character of `system` changes the key.

B2.2 Cassette file `data/cassettes/<key>.json` holds `{"key", "request", "response",
"recorded_at"}`. `request` is the `LLMRequest` dump. `response` is the `LLMResponse`
dump. The payload is backend-neutral: it is what the backend returned after parsing,
not the provider's wire message, which is what makes replay backend-agnostic (D15).
`put` writes atomically (temp file in the same directory, then `os.replace`) and
overwrites an existing key; git shows the change, and the path is logged.

B2.3 Mode `replay`: hit returns the stored response; miss raises `CassetteMiss` with
the key, the model and the first 80 characters of the last user message. No backend
is needed in replay mode (`backend=None` is allowed). Tests and CI run in this mode.

B2.4 Mode `record`: hit returns the stored response without calling the backend; miss
calls `backend.complete`, writes the cassette, returns the response. Mode `live`:
always calls the backend, never reads or writes a cassette. `record` or `live` with
`backend=None` raises at construction.

B2.5 `extract` sends `request` with `output_schema = model_cls.model_json_schema()`
when `request.output_schema` is None and validates `response.data` with
`model_cls.model_validate`. On a validation failure in `record` or `live` mode it
calls `backend.complete` directly once more, bypassing the cassette read, validates
again, and in `record` mode overwrites the cassette with the response that
validated; a second failure raises `LLMOutputError` carrying the raw text. In
`replay` mode there is no retry: a cassette that fails validation raises at once,
with no network. This is the single retry D15 describes, placed in the wrapper so
every backend gets it. `judge` builds the schema `{"type": "object", "properties":
{"choice": {"enum": [...]}}, "required": ["choice"]}` and returns the enum member,
with the same retry.

B2.6 `AnthropicBackend.complete` calls `client.messages.create` with `model =
models.resolve(request.model, "anthropic_api")`, `system`, `messages`, `max_tokens`,
`temperature` and, when `output_schema` is given,
`output_config={"format": {"type": "json_schema", "schema": output_schema}}` (present in
anthropic 1.11.0, P2). It reads the first text block, parses JSON when a schema was
given, and fills token counts from `usage`. `stop_reason` of `max_tokens` or
`refusal` raises `LLMOutputError`. The test boundary is the SDK client (D13): the test
passes a fake client object whose `messages.create` records its kwargs and returns an
`anthropic.types.Message` built with `Message.model_validate({...})`. No HTTP
transport mock of any kind.

B2.7 `OpenRouterBackend.complete` posts to
`https://openrouter.ai/api/v1/chat/completions` with the `Authorization: Bearer`
header, `model = models.resolve(request.model, "openrouter")`, the system prompt as
the first message, `temperature`, `max_tokens`, and, when `output_schema` is given,
`response_format = {"type": "json_schema", "json_schema": {"name": "out", "strict":
true, "schema": output_schema}}` plus `provider = {"require_parameters": true}` so the
request is routed only to an endpoint that honours the schema. It reads
`choices[0].message.content`, parses JSON when a schema was given, fills token counts
from `usage`, and raises `LLMOutputError` on a non-2xx status (carrying the body) or
a `finish_reason` of `length` or `content_filter`. The test injects
`httpx2.Client(transport=httpx2.MockTransport(handler))` and asserts the request
body and the parsed response; this is our own HTTP client, not the Anthropic SDK, so
D13 does not apply. The `openai` package is not used.

B2.8 `load_model_ids` reads `config/models.yaml`, which maps each logical name
(`extract`, `adjudicate`, `render`, `judge`, `classify`) to a concrete id per backend.
The implementer loads the `claude-api` skill for the Anthropic ids and reads the
OpenRouter catalogue (`GET /api/v1/models`, no key) for the gateway ids, and writes
both with a dated comment (D2). The cassette key carries the logical name, never the
concrete id, so a gateway recording replays on the API backend. `from_settings`
builds `LLM`: replay mode gets no backend; `record` gets the backend named by
`settings.backend` (`OpenRouterBackend` from `OPENROUTER_API_KEY`, or
`AnthropicBackend` from `ANTHROPIC_API_KEY`); `live` requires `backend ==
"anthropic_api"` and raises otherwise, because the gateway is development tooling
(D15).

B2.9 Logging: one `llm_call` record per `complete` with `key`, `mode`, `hit: bool`,
`backend`, `model`, token counts. Never the prompt text.

### Tests

`tests/test_llm.py`: B2.1 (plus a Hypothesis test: the key is invariant under schema
key reordering and `max_tokens` changes), B2.2 to B2.9. A `FakeBackend` that records
the requests it saw and returns scripted responses is the seam for B2.3, B2.4 and
B2.5; it is a test-local class, not a production module. Null cases: a request with
`output_schema=None` through `complete`; a gateway response with no `usage`.

### Acceptance

- SPEC section 6: one client wrapper, cassettes keyed by the canonical hash, strict
  replay raises on a miss. A8: CI in replay mode. D15: two backends behind one protocol.

## Unit 3: world schema and project config

### Interface

`digest/project.py`

```python
class ChecklistTask(BaseModel, frozen=True):
    id: str                              # unique within the project, e.g. "G-DVT1.thermal"
    task: str
    owner: Role
    expected_cadence_days: int           # working days between evidence
    priority: Priority = "normal"

class Gate(BaseModel, frozen=True):
    id: str
    name: str
    date: date
    buffer_days: int
    priority: Priority = "normal"
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
    def task(self, task_id: str) -> ChecklistTask

def load_project(path: Path) -> ProjectConfig
```

`digest/world/schema.py`

```python
EntityType = Literal["part", "assembly", "pcba", "harness", "molded", "battery",
                     "purchased", "component", "subsystem", "document",
                     "eco", "ecr", "deviation", "fa", "po",
                     "gate", "build", "test", "release", "vendor"]

class Entity(BaseModel, frozen=True):
    id: str
    type: EntityType
    description: str
    owner: Role | None = None
    vendor: str | None = None
    subsystem: str | None = None         # id of an entity of type "subsystem"
    parent: str | None = None            # id of the containing entity (BOM or document set)
    aliases: tuple[str, ...] = ()        # names people use in chat; the extractor's glossary
    state: dict[Attribute, str] = {}     # initial attribute values, e.g. {"rev": "B"}

class Persona(BaseModel, frozen=True):
    user_id: str                         # Slack-style, e.g. "U0ME01" or "B0PLM01" for a bot
    name: str
    kind: Literal["human", "bot"] = "human"
    role: Role | None = None             # required for a human, None for a bot
    team: str | None = None              # free text, e.g. "drive"
    manager: str | None = None           # persona user_id
    channels: tuple[str, ...]
    owned_entities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()        # "Alice", "@alice"
    authority: Authority = "owner_stated"  # what this persona's claims carry; "plm_bot" for a system of record

class Channel(BaseModel, frozen=True):
    id: str
    name: str
    purpose: str

class WorldClaim(BaseModel, frozen=True):
    entity: str
    attribute: Attribute
    value: str
    authority: Authority | None = None   # None means the poster's authority
    supersedes: str | None = None        # event id whose claim on the same key this replaces
    contradicts: tuple[str, ...] = ()    # event ids whose claims on the same key this disagrees with

class TaskTouch(BaseModel, frozen=True):
    task: str                            # ChecklistTask.id
    effect: Literal["evidence", "done"]  # evidence of progress, or completion

class WorldEvent(BaseModel, frozen=True):
    id: str
    day: int                             # 1-based working-day index from start_date
    channel: str
    poster: str                          # persona user_id
    kind: Literal["decision", "blocker", "risk", "schedule_change", "spec_change",
                  "bom_change", "ask", "fyi", "distractor"]
    summary: str                         # ground truth, one sentence
    scenario: str | None = None          # "S1".."S15", "N1".."N5"
    claims: tuple[WorldClaim, ...] = ()
    tasks: tuple[TaskTouch, ...] = ()    # checklist tasks this event is evidence for or completes
    affected_roles: tuple[Role, ...] = ()
    must_see_for: tuple[str, ...] = ()   # persona user_ids
    known_by: dict[str, int] = {}        # persona user_id -> working day they learn it
    noise: dict[str, bool] = {}          # renderer knobs: indirection, wrong_claim, typo, delayed_crosspost

class World(BaseModel, frozen=True):
    name: str
    program: str
    workspace_url: str
    timezone: str                        # IANA name
    start_date: date                     # a Monday
    days: int                            # working days
    project: ProjectConfig
    channels: tuple[Channel, ...]
    personas: tuple[Persona, ...]
    entities: tuple[Entity, ...]
    events: tuple[WorldEvent, ...]
    def date_of(self, day: int) -> date  # skips Saturday and Sunday
```

`digest/world/load.py`

```python
def load_world(path: Path) -> World      # beside load_world_header from the stubs commit
```

CLI: `digest world check [--world NAME]` loads and validates, prints counts per day and
per channel, exit 0; validation errors print the path and exit 1.

### Behaviours

B3.1 Cross-references validate in a `model_validator` on `World`: every
`event.channel` is a channel id; every `event.poster`, `must_see_for` entry and
`known_by` key is a persona user_id; every `claim.entity` and `persona.owned_entities`
entry is an entity id; every `entity.subsystem` names an entity of type `subsystem`;
every `entity.parent` names an entity; every `tasks[].task` is a checklist task id in
`project`; every `claim.supersedes` and `claim.contradicts` entry is an earlier or
same-day event id that carries a claim on the same `(entity, attribute)`; every
`persona.channels` entry is a channel id; every `persona.manager` is another persona's
user_id; a human persona has a role and a bot persona has none; every `event.day` and
`known_by` value is in `1..days`; a poster is a member of the channel they post in;
`must_see_for` and `known_by` name human personas only; event ids, entity ids and
persona user_ids are unique; the union of all `aliases` (entities and personas) has
no duplicate, so the glossary is unambiguous. Each failure names the event, entity or
persona id.

B3.2 `ProjectConfig.phases` must be ascending by `from_date` with distinct dates;
`phase_on(day)` returns the phase of the latest window with `from_date <= day` and
raises for a day before the first window. Gate ids and checklist task ids are unique
across the project; `buffer_days >= 0`; `expected_cadence_days >= 1`.

B3.3 When embedded in a world: `start_date` is a Monday, the first phase window starts
at or before `start_date`, and every gate `date` is one of `date_of(1..days)`.
`date_of(1) == start_date`; `date_of(6)` is the next Monday.

B3.4 `load_world` and `load_project` read YAML with `yaml.safe_load` and validate.
Unknown keys are errors (`extra="forbid"` on every model).

B3.5 The bundled fixture `data/world/kestrel/world.yaml` validates and contains: the
program "Kestrel K2"; the six roles as human personas with `team` and `manager` set
(the EM manages the engineers, the PM manages supply chain), plus one CM persona for
the external channel with `cm_stated` authority, plus three bot personas (`plm_bot`,
`schedule_bot`, `doc_control_bot`) with `plm_bot` authority posting only in the feed
channel; channels `kestrel-program`, `eng-mech`, `eng-elec-fw`, `supply-chain`,
`dvt1-build`, `ext-sunrise-cm`, `plm-feed`; subsystems `drive`, `compute`, `safety`, `power`,
`chassis`, `enclosure`; the entity table from the domain research (at least PN-11051,
PN-31010, PN-21030, PN-51010, PN-61001, ECO-0142, DEV-0007, PO-45002211, G-DVT1,
G-TR1, TST-THM-02, BLD-DVT1) with `subsystem` set; three documents (the CM drawing
pack for PN-11020, the PRD, the program schedule) with `parent` or `subsystem` set
and a `rev` or `status` in `state`; gates G-EVT2-EXIT (day 1), G-TR1 (day 7), G-DVT1
(day 9, buffer 7 working days) with checklists whose task ids the events reference;
phase windows EVT then DVT; scenario events S1 to S15 and N1 to N5 with `known_by`,
`must_see_for`, `tasks`, and `supersedes` or `contradicts` where the scenario table
says a claim replaces or disputes another (S2 and S6 on PN-31010 rev; N4 on the
harness count; S12 where the CM's slip contradicts the schedule bot's last post; S1
where no PLM bot post ever appears). Gate G-DVT1 has `priority: critical`.
`start_date` 2026-10-12, `days` 10, timezone `America/Los_Angeles`.
`digest world check` exits 0. The rendered `events.jsonl` for the full world is M2
work; M1 ships a human-authored `events.jsonl` of about ten lines covering S1 and N1
(part of the human-authored anchor slice, SPEC section 6) so `digest replay` has
input.

B3.6 `config/project.yaml` is the same `ProjectConfig` shape as `world.project`,
written for the live workspace. It validates with `load_project`.

### Tests

`tests/test_project.py`: B3.2, B3.4 (`load_project`), B3.6. `tests/test_world.py`:
B3.1 (parametrized over each broken reference, including a duplicate alias and a
`supersedes` that points at a later day), B3.3, B3.4, B3.5 (loads the bundled file and
asserts the named counts), the `world check` CLI on a tmp copy with one broken
reference. Null cases: a persona with no owned entities and no aliases; an entity with
no subsystem, parent or aliases; an event with no claims, no tasks and no `known_by`;
a gate with an empty checklist.

### Acceptance

- SPEC section 6 (world.yaml shape) and section 5 step 4 (gates and checklists from
  config). A2 groundwork (phase windows). A6 groundwork: `TaskTouch` lets M2 derive
  golden absences from the script. Ledger groundwork: `supersedes` and `contradicts`
  on scripted claims give M3 its supersession and contradiction labels without a rules
  engine. Documents as entities let S4, S8 and S12 be stated as stale-document claims.

## Decisions recorded in this plan

| Fork | Chosen | Why |
|---|---|---|
| One unit or three | Three, on an integration branch | Units 2 and 3 share only the stubs commit with unit 1, so test writing and implementation can run in parallel; each review stays small. |
| Which tables in M1 | `meta`, `events`, `events_fts`, `stage_runs` | Only tables with an M1 reader. `stage_runs` has one: `thread_deltas`, which M2 extraction consumes. |
| `ts` storage | TEXT plus derived INTEGER micros | TEXT keeps the Slack identity exactly (trailing zeros included) and makes equality safe; INTEGER orders and ranges. |
| Corrections | Separate `event_ts` per row version | A day-5 edit must not be visible in a day-3 replay; one column fixes ingest filtering, delta detection and the deletion contract at once. |
| Historical reads | Snapshot store; forward-only replay; `run` requires `meta.until` to be the digest day | Version history would add a table and a second time axis to every query for a demo that replays forward anyway. |
| Reactions and the thread revision | Reactions do not bump `last_ts`; dropped at ingest when later than `until` | Extraction reads what the thread says; exposure is read by M3 from the row. Coupling the two would re-extract a thread for every thumbs-up. |
| Threads table | None | Derived from `thread_ts`; no reader in M1. |
| Where the clock lives | Explicit `as_of` on every read; `--until` filters ingest; `meta.until` is the snapshot bound | No global state; `run --as-of` in M3 reuses the same bound. |
| Day boundary for `--until` | End of day in the world's timezone, stored in `meta` | A US team's day ends at midnight Pacific; the store stays UTC. |
| Reactions | One `Reaction` per reactor with `user_id` and `ts` | SPEC section 4 dates `exposed.how = reacted`; a count cannot say who saw what. |
| Default DB path | `data/<world>.sqlite`; the store refuses a different world | One shared file would mix corpora silently. |
| Backend selection | Explicit `DIGEST_BACKEND`, default `openrouter`; `live` requires `anthropic_api` | D15 asks for one config value; key presence as a selector could bill the wrong provider by accident during recording. |
| Cassette payload | Parsed `LLMResponse`, not the provider message | Backend-neutral replay (D15). |
| Cassette key fields | SPEC section 6's five, including `temperature` | D9 listed four; SPEC is the contract and D9 was amended. `max_tokens` is excluded so a limit change does not invalidate recordings. |
| Cassette overwrite | `put` overwrites | A never-overwrite rule pins a bad first response forever; git shows the diff. |
| Replay strictness | Replay raises on a miss; record reads before calling; live bypasses cassettes | Matches `.env.example`; a lenient replay would hide a missing recording until CI. |
| Backends in M1 | `anthropic_api` and `openrouter` | Two implementations justify the protocol; one gateway covers Claude, GPT-class and the classifier for development (D15); the API path is the product. The cassette key uses logical model names so recordings replay across backends. |
| Anthropic test boundary | Fake SDK client returning `anthropic.types.Message` | D13: never HTTP mocks. |
| Retry on invalid output | One retry in `extract` and `judge`, every backend, bypassing the cassette; none in replay | D15 promised one retry for the CLI; the wrapper is the one place that validates, so it owns the retry. A retry that read the cassette back would never reach the backend. |
| YAML library | pyyaml | SPEC names `.yaml` files; `tomllib` reads only TOML. One dependency plus stubs. |
| FTS table shape | External-content with triggers; `events` keeps a rowid | Documented pattern; no sync code in `store.py`. |
| World day index | Working days, Monday to Friday | The domain timeline is "D1 Monday 2026-10-12 to D10 Friday 2026-10-23": ten working days; calendar days would put day 7 on a Sunday. |
| Shared type placement | `Role`, `Phase`, `Attribute`, `Event` in `digest/models.py`; project config in `digest/project.py` | The absence detector and live config read them; the simulator is one consumer, not the owner. |
| Richer world model | Two entity types (`subsystem`, `document`) and three fields (`subsystem`, `parent`, `aliases`); no relations table, no teams, no reporting lines, no person entity | Covers the stale-document scenarios, subsystem ownership for ranking and the extractor's glossary without a graph. Personas stay the only shape for people. |
| Scripted supersession | Explicit `supersedes` and `contradicts` on world claims | Scripted truth should be scripted; an ordering-and-authority rules engine would be a second ledger to validate. |
| Systems of record | Bot personas with `plm_bot` authority posting into a feed channel | That is how PLM, schedule and document-control tools appear in Slack; no new source type or API. |
| Relationships | `team` and `manager` on personas; interaction frequency derived in M3 | Two fields give manager, reports and team for the sender-affinity term; no org-chart connector. |
| Priority | `priority` attribute; `priority` on gates and tasks | A PM edits it in the configuration; a tracker bot posts it as a claim; the ranker reads one input. |
| Classifier seam | `judge` is the port; the Haiku-class model through the gateway is the core adapter; Jev and Laya are stretch (D17) | M1 needs no classifier; the enum-only request shape is fixed now so later adapters are additive. |
| Type gate | `uv run ty check` (D16) | Measured; same three planted errors caught as mypy in less than half the time (under 0.5 s against 1.1 s). |

## Open items for the user

U1. Put `OPENROUTER_API_KEY` in `.env` (never in chat). The first real recording (M2)
needs it; M1 does not. The first call in M2 is a spike: one extraction request through
the gateway with a JSON schema against a Claude model, to confirm the schema is
honoured before any cassette is recorded.

U2. Decided 2026-10-08: the world's timezone (`America/Los_Angeles`) for `--until`.

U3. Decided 2026-10-08: one pull request per unit, plus one for the stubs.

## Premise ledger

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| P1 | SQLite in uv's Python 3.12 has FTS5 | `uv run python -c "create virtual table t using fts5(x)"` ok; sqlite 3.49.1 | verified |
| P2 | anthropic 1.11.0 exposes `output_config.format` with `type: json_schema` | `inspect.signature(Messages.create)` has `output_config`; `JSONOutputFormatParam` has `schema`, `type` | verified |
| P3 | anthropic 1.11.0 imports httpx2 2.13.1, not httpx | `import httpx2` 2.13.1; `uv.lock` | verified; the "mocks miss" half is D13's claim, not re-measured |
| P4 | `data/cassettes/` and `data/world/` are not gitignored | `.gitignore` ignores `data/live/`, `*.sqlite`, `*.sqlite-journal`, `*.db` | verified; WAL side files are not covered, added in the stubs commit |
| P5 | `hypothesis`, `time-machine`, `pytest` are dev deps | `pyproject.toml` `[dependency-groups] dev` | verified |
| P6 | pyyaml is not installed | `uv run python -c "import yaml"` ModuleNotFoundError | verified; plan adds it |
| P7 | OpenRouter serves Claude Haiku, Sonnet, Opus and Fable and the GPT-5 family with `response_format` and `structured_outputs` in `supported_parameters` | `GET https://openrouter.ai/api/v1/models` on 2026-10-08, no key | verified for the catalogue; the first schema-bearing call is the M2 spike (U1) |
| P8 | `prove-test` is callable from Git Bash as `prove-test` | `command not found`; works as `python ~/.local/bin/prove-test.py` | refuted; mechanics below use the python form |
| P9 | `prove-test` records a proof with `--break blank:<file>:<a>-<b>` and `--expect-error` | proof written for `tests/test_cli.py` on 2026-10-06 | verified |
| P10 | Codex `exec` has `-p`, `-C`, `-o`, `--output-schema` | `codex exec --help`; handoff verified `--output-schema` on 2026-10-06 | verified |
| P11 | Slack permalink shape `archives/<C>/p<ts no dot>` plus `?thread_ts=&cid=` for replies | Slack docs, not re-fetched; replay links are synthetic | accepted-assumption |
| P12 | Slack `ts` seconds part is 10 digits through 2286 | arithmetic | verified |
| P13 | The domain timeline numbers working days | `notes/research/domain-npi-corpus.md`: "D1 = Mon 2026-10-12 to D10 = Fri 2026-10-23" | verified |
| P14 | `INSERT OR REPLACE` leaves a stale external-content FTS row; `ON CONFLICT DO UPDATE` does not | sqlite 3.49.1 probe: after REPLACE the old term still matched; after upsert it did not | verified |
| P15 | FTS5 bareword with `-` is a syntax error; quoted phrase matches; `tokenchars '-_'` keeps `PN-31010` one token | sqlite 3.49.1 probe with three queries | verified |
| P16 | ty 0.0.84 type-checks frozen pydantic models with `Literal`, tuple defaults, properties and a `model_validator` without false positives, and catches planted errors | scratch module probe, D16 | verified |

## Mechanics for this milestone

- Gates: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run ty check`, `uv run lint-imports`.
- Prove a test: `python ~/.local/bin/prove-test.py --test tests/test_store.py --break
  blank:src/digest/store.py:N-M --run "uv run pytest tests/test_store.py" --format
  "uv run ruff format tests/test_store.py" --expect-error "<specific line>"`. The
  pattern must be specific; generic words (`assert`, `failed`, `Error`) are refused.
- Test author brief (Codex, execution lane, fresh session, `-C` the worktree, PATH
  including uv): this plan, `SPEC.md` sections 4 to 6, the stubs, and
  `.claude/rules/testing.md`. It writes only its `tests/test_<unit>.py` files and hands
  back unstaged.
- Implementer brief (Opus subagent, own worktree): this plan, the stubs, the tests,
  the rules under `.claude/rules/`. It may not edit `tests/`. Done when the five gates
  pass. Before touching `digest/llm/anthropic_api.py` it loads the `claude-api` skill
  and writes `config/models.yaml`.
- Reviews: Codex `timeout 900 codex exec -p review --output-schema
  ~/Documents/dev-kit/codex/schemas/review.json -o <file> "..." </dev/null` and the
  Claude lens panel (`review-lens-*` agent types) on each branch after the test commit
  and after the code commit. Neither sees the other's findings.

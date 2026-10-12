# Plan: M2 triage, extraction, absences, golden labels and the corpus

Status: v1.11, 2026-10-11. Process stage 1 (`docs/process.md`) for M2. Amended as the
units are built; stages 2 to 4 run in the unit pull requests.

M2 from `SPEC.md` section 9: a triage classifier, the extraction prompt and schema with
evidence verified in code, the absence detector with its two core sources, golden labels
derived from the world script, the rendered corpus and the extraction eval. v0.4 core for
M2 is the channel `private` field and the triage threshold derived from the cost of a miss
(part of the section 5 step 2 probability shape). Every other v0.4 addition listed for M2
is stretch and is not in this plan; SPEC section 9 holds the list.

Everything in M2 runs offline in tests and CI. Two model families call the gateway once,
in one recording session (the data pull request): the Haiku-class model triages and
extracts, the GPT-class model renders the corpus (D2, D12, D15).

## Units and branches

M2 splits into four units, each its own pull request into `main`. First a `m2-stubs`
branch lands by pull request: shared types, the store tables and method stubs, the
world schema fields and fixture content M2 reads, `config/priors.yaml`, the module
skeletons that raise `NotImplementedError`, the CLI commands with bodies that call
`_not_yet`, the import-linter layers, and the tracked negative-name list. Each unit
branch forks from `main` after that merge, lands one test commit and one code commit,
is reviewed, and is merged by the user.

Units 1, 2 and 3 share only the stubs and `digest/cli.py` (each fills its own command
bodies), so their tests and code run in parallel and the three pull requests merge in
any order; a unit branch rebases on `main` if `digest/cli.py` conflicts. The stubs pull request
implements, not stubs, the shared pieces more than one unit reads (the store's item and
triage methods, the priors loader, the manifest reader and writer, `World.glossary()`).
They are about a hundred lines of real code, so they go through the wall inside that
pull request: a Codex test author writes `tests/test_priors.py` and appends to `tests/test_store.py`
and `tests/test_world.py`, and an Opus implementer makes them pass; the orchestrator writes only the interfaces, the fixture content and the
configuration. The stubs branch forks from `m2-plan` (it needs the plan and the SPEC
edit) and rebases onto `main` after the plan pull request merges; its pull request opens
as a draft until then. Commit order: interfaces, fixture and configuration first; then
the shared-piece tests (proofs recorded against the implementation, as M1 did); then the
implementation; the `tests/test_world.py` count edit rides with the shared-piece tests. Unit 4 runs the
other three, so its tests are written in parallel against the stubs and its code lands
after units 1 to 3 have merged. The data pull request comes last and holds no code.

| Unit | Branch | Modules | Tests |
|---|---|---|---|
| 0 Stubs and shared pieces | `m2-stubs` | interfaces and fixture content (orchestrator); `digest/store.py` item and triage methods, `digest/priors.py`, `digest/world/manifest.py`, `World.glossary()` (Opus implementer) | `tests/test_priors.py`, `tests/test_store.py` (appended), `tests/test_world.py` (appended, with the channel-count and corpus-bound edits) |
| 1 Triage and extraction | `m2-extract` | `digest/extract/__init__.py`, `digest/extract/schema.py`, `digest/extract/prompts.py`, `digest/extract/normalize.py`, `digest/extract/verify.py`, `digest/extract/triage.py`, `digest/extract/stage.py`, `digest extract` | `tests/test_extract.py` |
| 2 Absences and golden labels | `m2-absence-golden` | `digest/absence.py`, `digest/world/golden.py`, the ask validation rules in `digest/world/schema.py`, the working-day helpers in `digest/project.py`, `digest absences`, `digest world golden` | `tests/test_absence.py`, `tests/test_golden.py`, `tests/test_world.py` (new tests appended) |
| 3 Corpus renderer | `m2-corpus` | `digest/corpus/__init__.py`, `digest/corpus/render.py`, `digest/corpus/prompts.py`, `digest/corpus/avoid.py`, `data/world/kestrel/anchor.jsonl`, `data/world/kestrel/manifest.json` (anchor entries), `digest world render` | `tests/test_corpus.py` |
| 4 Extraction eval | `m2-eval` | `digest/eval/__init__.py`, `digest/eval/extraction.py`, `Cassettes.recorded_models` in `digest/llm/cassette.py`, `digest eval` | `tests/test_eval.py`, `tests/test_llm.py` (one test appended) |
| Data | `m2-corpus-data` | `data/cassettes/*.json`, `data/world/kestrel/events.jsonl`, `data/world/kestrel/manifest.json` (rendered entries added), `data/world/kestrel/golden.jsonl`, `reports/eval-<date>.md`, README | `tests/test_corpus_data.py` (B5.1) |

## Shared conventions

- Layers. The import-linter layers contract becomes, top to bottom: `digest.cli`;
  `digest.eval`; `digest.corpus`; `digest.replay | digest.extract | digest.absence`;
  `digest.store | digest.llm | digest.world`; `digest.project | digest.priors`;
  `digest.models | digest.settings`. `digest.corpus` sits above `digest.replay` so the
  renderer reads the anchor file with `read_events` instead of a second parser. A module imports only from its own layer's
  dependencies or a lower layer. The renderer lives in `digest/corpus/`, not in
  `digest/world/`, because `digest.world` and `digest.llm` are siblings and the
  renderer needs both. The forbidden contract on `anthropic` and `httpx2` is unchanged.
  The stubs commit changes the contract; every unit stays inside it.
- Shared types. `digest/models.py` gains `Item`, `Evidence`, `Claim`, `TaskLink`,
  `AbsenceDetail`, `TriageDecision`, `Glossary` and the enums they use (interface
  below). `TriageDecision` is a shared type because the store persists it and the
  extract layer produces it. The store
  persists `Item`; extraction writes it; the absence detector reads and produces it;
  the eval compares it. `Glossary` is built by `World.glossary()` and consumed by
  extraction, so neither package imports the other.
- Prompt versions. Each prompt module exports one version constant
  (`EXTRACT_PROMPT_VERSION` for the triage and extraction prompts together,
  `RENDER_PROMPT_VERSION`), a short string such as `"m2.1"`. A change to either prompt's
  text bumps it. `stage_runs` and the `triage` table key on `EXTRACT_PROMPT_VERSION`, and
  a store holds one such version, so a bump runs on a fresh store (B1.6). The cassette key already
  carries the prompt text (M1 B2.1), so a bump also records fresh cassettes.
- Stage-run model column. `stage_runs.model` and `triage.model` hold the logical model
  name (`extract`, `classify`), not the concrete id, so a store built from gateway
  recordings is valid for the API backend (D15). The concrete id is in the log record.
- Output models. Every model that is sent as an `output_schema` has every field
  required; an optional field is `X | None` with no default. No `dict` field, no
  `date` or `datetime` field (strings, parsed in code), no `minItems` above 1, no
  numeric bounds in the schema (a `field_validator` checks them so the client's single
  retry fires). This is the intersection of Anthropic structured outputs and the
  gateway's strict mode (premise P8). Unit 1's tests assert this for `Extraction` and
  `TriageOutput`, and unit 3's for a `thread_model` class: the schema survives
  `strict_schema` with every property in `required` and no `minItems` above 1.
- Working days. `digest/project.py` gains `add_working_days(day, n)` and
  `working_days_between(start, end)` (Monday to Friday, no holidays). Every cadence,
  buffer and age in M2 is in working days; the world's `day` index already is.
- Priors. `config/priors.yaml` holds the constants SPEC section 5 names and
  `digest/priors.py` loads it as `Priors`. M2 adds `triage.miss_cost_ratio` (default
  10) and `absence.ask_reply_cadence_days` (default 2). M3 adds the role-by-phase table
  to the same file.
- Store schema. Two tables are added, `items` and `triage`, both `CREATE TABLE IF NOT
  EXISTS`, and `SCHEMA_VERSION` stays `"1"`: the change is additive and no existing
  column moves. Claims stay JSON on the item row; the ledger table lands with its
  writer in M3 (M1's rule: a table lands with the stage that first writes it).
- Deleted messages. A deleted row is never put in a prompt and cannot be cited; an
  existing item whose evidence message is later deleted stays as it is (retraction is
  M3). The Kestrel corpus has no deletes in M2.
- Logging. As M1: event token plus `extra={...}`; never prompt text or message text.
- Errors. Corpus, world and config files are ours: a malformed file raises naming the
  file and the line. Model output is not ours: it is verified and dropped field by
  field with counts, never trusted and never repaired.

## Stubs pull request

Three commits on `m2-stubs`, forked from `m2-plan`: interfaces, shared types, fixture
content and configuration, by the orchestrator; the tests below, by the Codex test author,
RED against the stubs; the implementation of the shared pieces, by an Opus implementer,
with the proofs recorded before it. The pull request opens as a draft and rebases onto
`main` after the plan pull request merges.

- `digest/models.py`: the shared types below.
- `digest/schema.sql`: the `items` and `triage` tables. `digest/store.py`: the six new
  methods (B0.2), implemented in this pull request; units 1 and 2 read them.
- `digest/project.py`: `add_working_days` and `working_days_between` as stubs.
- `digest/priors.py` and `config/priors.yaml`, implemented in this pull request (B0.1);
  units 1 and 2 read them.
- `digest/world/schema.py`: `Channel.private`, `Ask`, `WorldEvent.ask` (fields only; the
  validation rules of B2.8 are unit 2 code) and `World.glossary()`, implemented in this
  pull request (B0.3).
- `data/world/kestrel/world.yaml`: the private channel `kestrel-leads` with its one
  event, and two ask events (B2.9). `config/project.yaml` is unchanged.
- `digest/world/manifest.py`, implemented in this pull request (B0.4); units 2 and 3
  read it.
- `digest/world/golden.py`, `digest/absence.py`, `digest/extract/*`, `digest/corpus/*`,
  `digest/eval/*`: interfaces that raise `NotImplementedError`; `prompts.py` modules hold
  the version constants and empty prompt strings.
- `data/world/avoid-names.yaml`: the complete negative-name list (B3.6).
- `digest/cli.py`: `extract`, `absences`, `world render`, `world golden` with their
  option declarations and `_not_yet` bodies; `eval` gains its `--world` and `--out`
  options and keeps its `_not_yet` body, so unit 4's RED run fails on the body, not on
  an unknown option.
- `pyproject.toml`: the layers contract above. No new dependency.
- `tests/test_world.py`: the bundled-world test that pins seven channels and their
  names gains the eighth, and the corpus test's `8 <= len(events) <= 14` becomes a lower
  bound of 8 (the data pull request regenerates `events.jsonl` with every scripted
  thread, and B5.1 then asserts the manifest covers every event). These edits ride in the
  test commit with the B0 tests and get their own proofs.

### Behaviours

B0.1 `load_priors` reads `config/priors.yaml` and validates: `miss_cost_ratio > 0`,
`ask_reply_cadence_days >= 1`, unknown keys are errors. `keep_threshold` is
`1 / (1 + miss_cost_ratio)`; with the default 10 it is `1/11`.

B0.2 Store: `upsert_items` is idempotent on `item_id` and replaces every other column,
`prompt_version` and `model` included; it raises `ValueError` for an item of type
`absence` (the table has no column for `absence`, and absences are not stored).
`items_for_thread` returns the thread's items in `first_seen` order. `items(as_of)`
returns items whose `last_updated` is at or before `as_of`, in `first_seen` order.
`record_triage` upserts on the four-column key; `triage_decision` returns the row or
None; `triage_for_thread` returns the thread's decisions in `last_ts` order. Column round trip: `Item` in, equal `Item` out (JSON columns, date, enums).

B0.3 `World.glossary()` returns every entity (id, type, description, aliases) and every
persona (user id, name, role or None for a bot, aliases, authority), in file order.

B0.4 `load_manifest` reads `{"threads": {<event id>: {"channel_id", "thread_ts",
"authored"}}}`; `write_manifest` writes it with keys sorted, two-space indent and a
trailing newline, so a round trip is byte-stable. A missing file raises
`FileNotFoundError`; an unknown key, an `authored` value outside `human | render` or a
`thread_ts` that does not match `TS_PATTERN` raises `ValidationError`. An empty
`threads` mapping is valid.

### Tests

`tests/test_priors.py` (new): B0.1 on a file under `tmp_path` and on the bundled
`config/priors.yaml`; null case: a file with the `absence` block missing (error).
`tests/test_store.py` (appended): B0.2, including the null columns (`owner`, `due`,
`resolved_by` None), `absence` None, and the refused absence item. `tests/test_world.py`
(appended): B0.3 on the bundled world (a bot with `role=None`, an entity with no aliases),
B0.4 (round trip, missing file, bad `authored`, empty mapping), and the channel-count and
corpus-bound edits. No consolidation of existing tests.

## Shared types (`digest/models.py`, stubs commit)

```python
ItemType = Literal[
    "decision",
    "blocker",
    "risk",
    "schedule_change",
    "spec_change",
    "bom_change",
    "ask",
    "fyi",
    "absence",
]
ItemStatus = Literal["open", "resolved", "superseded"]
Severity = Literal["low", "medium", "high"]
Confidence = Literal["low", "medium", "high"]
TaskEffect = Literal["evidence", "done"]


class Evidence(Frozen):
    message_ts: str  # TS_PATTERN
    quote: str  # verbatim span of the cited message
    permalink: str  # the stored event's permalink (constructed, never model-written)


class Claim(Frozen):
    entity: str  # entity id after normalization
    attribute: Attribute
    value: str
    message_ts: (
        str  # the evidence message that states the value; its ts is the claim's asserted time
    )
    authority: Authority  # that message's author, from the glossary; "hearsay" when unknown


class TaskLink(Frozen):
    task: str  # ChecklistTask.id
    effect: TaskEffect
    message_ts: str  # the evidence message that shows the progress; its ts is the evidence time


class AbsenceDetail(Frozen):
    source: Literal["checklist", "ask"]
    gate: str | None  # checklist source
    task: str | None  # checklist source
    asker: str | None  # ask source: user id of the person who asked
    days_silent: int  # working days since the cited evidence
    buffer_consumed: int | None  # checklist source: working days since the buffer opened
    buffer_days: int | None


class Item(Frozen):
    item_id: str
    thread_id: str
    type: ItemType
    title: str
    summary: str
    entities: tuple[str, ...]
    owner: str | None  # user id; for an ask, the person asked
    due: date | None
    affected_roles: tuple[Role, ...]
    severity: Severity
    confidence: Confidence
    evidence: tuple[Evidence, ...]  # at least one (min_length=1)
    first_seen: str  # ts of the earliest evidence
    last_updated: str  # ts of the latest evidence
    claims: tuple[Claim, ...] = ()
    tasks: tuple[TaskLink, ...] = ()
    status: ItemStatus = "open"
    resolved_by: str | None = None  # message ts or item id that closed it
    absence: AbsenceDetail | None = None  # set only when type == "absence"


class TriageDecision(Frozen):
    thread_id: str
    last_ts: str
    prompt_version: str
    model: str  # "classify"
    probability: float
    threshold: float
    kept: bool
    rationale: str
    decided_at: datetime


class GlossaryEntity(Frozen):
    id: str
    type: str
    description: str
    aliases: tuple[str, ...]


class GlossaryPerson(Frozen):
    user_id: str
    name: str
    role: Role | None
    aliases: tuple[str, ...]
    authority: Authority


class Glossary(Frozen):
    entities: tuple[GlossaryEntity, ...]
    people: tuple[GlossaryPerson, ...]
```

`Item` is the SPEC section 4 item without `phase_tags` (no M2 reader) and `links`
(M3). An `Item` with `type == "absence"` has `absence` set and vice versa (a
`model_validator`).

## Unit 1: triage and extraction

### Interface

`digest/priors.py`

```python
class TriagePriors(Frozen):
    miss_cost_ratio: float            # > 0; a miss costs this many false keeps
    @property
    def keep_threshold(self) -> float # 1 / (1 + miss_cost_ratio)

class AbsencePriors(Frozen):
    ask_reply_cadence_days: int       # >= 1

class Priors(Frozen):
    triage: TriagePriors
    absence: AbsencePriors

DEFAULT_PRIORS_PATH = Path("config/priors.yaml")
def load_priors(path: Path = DEFAULT_PRIORS_PATH) -> Priors
```

`digest/extract/schema.py` (what the model returns; verified into `Item` by `verify.py`)

```python
class ExtractedEvidence(Frozen):
    message_ts: str
    quote: str


class ExtractedClaim(Frozen):
    entity: str  # id or alias
    attribute: Attribute
    value: str
    message_ts: str  # one of the item's evidence message_ts


class ExtractedTask(Frozen):
    task: str  # ChecklistTask.id from the prompt
    effect: TaskEffect
    message_ts: str  # one of the item's evidence message_ts


class ExtractedItem(Frozen):
    rationale: str  # first, before every other field
    item_id: str | None  # an existing item of this thread to update, else null
    type: Literal[
        "decision", "blocker", "risk", "schedule_change", "spec_change", "bom_change", "ask", "fyi"
    ]
    title: str
    summary: str
    entities: list[str]  # ids or aliases
    owner: str | None  # user id, mention or alias; for an ask, the person asked
    due: str | None  # YYYY-MM-DD
    affected_roles: list[Role]
    severity: Severity
    confidence: Confidence
    status: Literal["open", "resolved"]
    evidence: list[ExtractedEvidence] = Field(min_length=1)  # minItems 1 in the schema
    claims: list[ExtractedClaim]
    tasks: list[ExtractedTask]


class Extraction(Frozen):
    rationale: str
    items: list[ExtractedItem]


class TriageOutput(Frozen):
    rationale: str
    probability: float  # field_validator: 0 <= p <= 1, else ValidationError
```

`digest/extract/prompts.py`

```python
EXTRACT_PROMPT_VERSION = "m2.1"  # covers the triage and the extraction prompt
EXTRACT_SYSTEM: str                # static; rules, output instructions, abstention rule
TRIAGE_SYSTEM: str
def extract_request(delta: ThreadDelta, glossary: Glossary, project: ProjectConfig, as_of: date, existing: Sequence[Item], citable: Sequence[Event]) -> LLMRequest
def triage_request(delta: ThreadDelta, channel_purpose: str | None, citable: Sequence[Event]) -> LLMRequest
```

`digest/extract/normalize.py` (pure functions)

```python
def normalize_space(text: str) -> str                                   # collapse runs of whitespace, strip
def resolve_entity(text: str, glossary: Glossary) -> str | None         # entity id; a revision suffix is split off, not matched (D24)
def resolve_person(text: str, glossary: Glossary) -> str | None         # user id
```

`digest/extract/verify.py` (pure)

```python
DROP_FIELDS = ("evidence.unknown_message", "evidence.quote", "item_id.unknown",
               "entities.unresolved", "claims.entity", "claims.message",
               "owner.unresolved", "due.format", "tasks.unknown", "tasks.message",
               "evidence.stale", "claims.stale", "tasks.stale")

class Verified(Frozen):
    items: tuple[Item, ...]
    drops: dict[str, int]       # every DROP_FIELDS key present, zero when nothing dropped

def verify(extraction: Extraction, delta: ThreadDelta, citable: Mapping[str, Event],
           glossary: Glossary, tasks: Mapping[str, ChecklistTask],
           existing: Mapping[str, Item]) -> Verified
```

`digest/extract/triage.py`

```python
def triage(llm: LLM, delta: ThreadDelta, citable: Sequence[Event], channel_purpose: str | None,
           priors: Priors, now: datetime) -> TriageDecision
```

`digest/extract/stage.py`

```python
class ExtractReport(Frozen):
    threads: int                # pending thread deltas seen
    kept: int
    dropped: int                # by triage
    failed: int                 # threads where triage or extraction raised LLMOutputError (after the client's retry where one runs)
    items_written: int
    drops: dict[str, int]       # summed verify drops

def run_extract(store: Store, llm: LLM, glossary: Glossary, project: ProjectConfig,
                priors: Priors, as_of: datetime, zone: ZoneInfo,
                channel_purposes: Mapping[str, str]) -> ExtractReport   # as_of in zone gives the date the prompt's gate filter uses
```

`digest/store.py` (additions)

```python
def upsert_items(self, items: Iterable[Item], *, prompt_version: str, model: str) -> int   # provenance columns, not on Item
def items_for_thread(self, thread_id: str) -> list[Item]
def items(self, as_of: datetime) -> list[Item]           # last_updated at or before as_of, first_seen order
def record_triage(self, decision: TriageDecision) -> None
def triage_decision(self, thread_id: str, last_ts: str, prompt_version: str, model: str) -> TriageDecision | None
def triage_for_thread(self, thread_id: str) -> list[TriageDecision]      # every revision, last_ts order
```

`digest/world/schema.py` (stubs commit, implemented there as M1 implemented `load_world_header`)

```python
class World(Frozen):
    ...
    def glossary(self) -> Glossary    # entities with aliases; human and bot personas with authority
```

CLI: `digest extract [--world NAME] [--db PATH]`. Opens the store as `replay` does,
refuses a store without `meta.until` or `meta.world`, loads the world named in `meta`,
builds the glossary and the project from it, loads priors, builds the client from
`Settings`, and runs `run_extract` once per working day from the world's first day to
`meta.until` (each `as_of` the end of that day in `meta.timezone`), so its requests are
the one-day deltas the eval records and replay finds the same cassettes; it prints the
summed report as JSON. A `--world` that differs from `meta.world` is refused. Exit 1 with the reason on a refused store, a cassette miss in replay mode or a
missing key in record mode.

### Schema

```sql
CREATE TABLE IF NOT EXISTS items (
  item_id        TEXT PRIMARY KEY,
  thread_id      TEXT NOT NULL,
  type           TEXT NOT NULL,
  title          TEXT NOT NULL,
  summary        TEXT NOT NULL,
  entities       TEXT NOT NULL,   -- JSON array of entity ids
  owner          TEXT,
  due            TEXT,            -- ISO date
  affected_roles TEXT NOT NULL,   -- JSON array
  severity       TEXT NOT NULL,
  confidence     TEXT NOT NULL,
  evidence       TEXT NOT NULL,   -- JSON array of {message_ts, quote, permalink}
  claims         TEXT NOT NULL,   -- JSON array of {entity, attribute, value, message_ts, authority}
  tasks          TEXT NOT NULL,   -- JSON array of {task, effect, message_ts}
  status         TEXT NOT NULL,
  resolved_by    TEXT,
  first_seen     TEXT NOT NULL,
  first_seen_us  INTEGER NOT NULL,
  last_updated   TEXT NOT NULL,
  last_updated_us INTEGER NOT NULL,
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_thread ON items (thread_id);

CREATE TABLE IF NOT EXISTS triage (
  thread_id      TEXT NOT NULL,
  last_ts        TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  probability    REAL NOT NULL,
  threshold      REAL NOT NULL,
  kept           INTEGER NOT NULL,
  rationale      TEXT NOT NULL,
  decided_at     TEXT NOT NULL,
  PRIMARY KEY (thread_id, last_ts, prompt_version, model)
) WITHOUT ROWID;
```

Absence items are not stored in M2: they are a function of the store and the day, and
M3's `run` decides what it persists per impression.

### Behaviours

B1.1 Moved to B0.1: the priors loader lands in the stubs pull request.

B1.2 `triage` sends `triage_request` with `model="classify"` through `llm.extract(...,
TriageOutput)`. The request carries the channel purpose when known, the thread root
and the changed messages (ts, author user id, text), and asks for a probability that the delta carries project information (a decision,
blocker, risk, schedule, spec or BOM change, or an ask about the program). `kept` is
`probability >= priors.triage.keep_threshold`. A probability outside `[0, 1]` fails
validation; in record or live mode the client retries once and a second failure raises
`LLMOutputError`, in replay mode it raises at once (M1 B2.5, tested there; unit 1 tests
the replay case). The decision carries `EXTRACT_PROMPT_VERSION`, `model="classify"`,
the threshold used and `decided_at = now`.

B1.3 `extract_request` has a static system prompt (`EXTRACT_SYSTEM`) and one user
message with, in this order: the glossary (entity id, type, description, aliases; people
with user id, name, role, aliases), the checklist tasks of every gate whose date is on or after `as_of` (task id, task
text, owner role, gate id and gate date), the existing items of the thread (item_id, type, title, status,
owner, due, entities, claims, tasks), the thread root (ts, author, text, file names) when it
exists, then the changed messages in `ts` order (ts, author, text, file names). Deleted
rows are omitted. The system prompt states: return items for new information only;
name an existing `item_id` to update or close an item; quote evidence verbatim from one
listed message; use null, never a guess, for `owner`, `due` and `item_id`; `owner` of an
ask is the person asked and its first evidence entry is the message that asks; `tasks` names, from the list only, the checklist tasks a message reports progress on
(a run, a result, a review held, a sign-off, a part received), with `effect: done` only
when a message states the task is complete, each link naming the message that shows it; a question about a task or a status
roll-up that lists it as open is not evidence (the world's `TaskTouch` has the same
meaning, M1 plan unit 3); the rationale comes first. The prompt never contains the words "golden", "scenario" or an event id.

B1.4 `normalize_space` collapses every run of whitespace to one space and strips the
ends. `resolve_entity` returns the id of the entity whose id equals the text
(case-insensitive) or whose alias equals the text (case-insensitive, whole string); a
text of the form `<id>-<L>`, `<id> Rev <L>`, `<id> rev <L>` or `<id> Rev. <L>` where
`<id>` resolves and `<L>` is one uppercase letter returns that id, the suffix split off
and discarded (a mention at a revision is not a claim about the current revision;
revisions enter as `rev` claim values). Any other text returns None. `resolve_person` accepts `<@U...>`, a bare user id, a persona
alias or a persona name (case-insensitive) and returns the user id, else None.
Property tests: every entity id and alias resolves to its id; every `id-L` form resolves
to the id; a string that is not an id, an alias or one of the suffixed forms resolves to
None.

B1.5 `verify` turns an `Extraction` into `Item`s and counts what it drops, per field:
- An evidence entry whose `message_ts` is not a key of `citable` drops the item
  (`evidence.unknown_message`). An entry whose `normalize_space(quote)` is empty or is not a
  substring of `normalize_space(event.text)` drops the item (`evidence.quote`); the
  empty case matters because an empty string is a substring of every text. An item
  with no evidence entry fails schema validation before `verify` (`minItems: 1`).
  `permalink` is the cited event's `permalink`.
- An `item_id` that is not a key of `existing` drops the item (`item_id.unknown`). A
  null `item_id` makes a new item with id `f"{thread_id}#{last_ts}#{n}"`, `n` the
  item's index in the output (unique because a store holds one extraction prompt
  version, B1.6). An existing id keeps `item_id`; its `evidence` is the
  old entries followed by the new ones for messages not already cited (output order
  kept, so `evidence[0]` stays the first message the item cited); a new entry for an
  already cited `message_ts` replaces the old one in place, because it was verified
  against the current text; an old entry whose message is in `citable` (the delta
  carries the edit) is re-checked against the current text and dropped when its quote
  no longer matches (`evidence.stale`), and every old claim or task link whose
  `message_ts` is no longer in the merged evidence is dropped with it (`claims.stale`,
  `tasks.stale`), so a link never cites an entry the item does not hold; the merged
  evidence is never empty, because the incoming item passed the schema's `minItems: 1`
  and its entries were verified before the merge, and its
  `claims` the union deduplicated by `(entity, attribute, value, message_ts)`; its
  `tasks` the union deduplicated by `(task, effect, message_ts)`, so a `done` link is
  never erased by a later update that omits it; every scalar field, `entities` and `affected_roles`
  are replaced by the new values. The old quotes stay, so a reader shown the item while open can later be shown
  the closing evidence beside it (SPEC section 4).
- An entity string that `resolve_entity` cannot resolve is dropped from `entities`
  (`entities.unresolved`); the item survives. Resolved entities are the ids,
  deduplicated, in output order.
- A claim whose entity does not resolve is dropped (`claims.entity`). A claim whose
  `message_ts` is not one of the item's verified evidence messages is dropped
  (`claims.message`). A claim's `authority` is the glossary authority of that message's
  author, `hearsay` when the author is not in the glossary; the message's ts is the
  claim's asserted time (D19), so M3 reads it without a schema change.
- An `owner` that `resolve_person` cannot resolve becomes None (`owner.unresolved`).
- A `due` that `date.fromisoformat` rejects becomes None (`due.format`).
- A task id not in `tasks` is dropped from the item (`tasks.unknown`); a task link whose
  `message_ts` is not one of the item's verified evidence messages is dropped
  (`tasks.message`).
- `first_seen` is the smallest evidence ts and `last_updated` the largest, over the
  merged evidence on an update. `status == "resolved"` sets `resolved_by` to the
  largest evidence ts.
`drops` has every `DROP_FIELDS` key, zero when nothing was dropped.

B1.6 `run_extract` first reads the meta key `extract_prompt_version`: absent, it is set
to `EXTRACT_PROMPT_VERSION` with the first write; different, the run is refused with a
`ValueError` naming both versions and the store (a bump starts from a fresh store, as
`replay` does for an earlier day; the eval always uses one). It then iterates
`store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", as_of)`. For each delta it builds `citable` (the root plus the changed rows,
deleted rows excluded, keyed by ts) and looks up
`store.triage_decision(thread_id, last_ts, EXTRACT_PROMPT_VERSION, "classify")`; when
none exists it calls `triage` and records the decision. A dropped thread records an
`ok` stage run for `extract` at `last_ts` and writes nothing, so it is not revisited
until the thread changes. A kept thread calls `llm.extract(extract_request(...),
Extraction)`, verifies, upserts the items with `prompt_version=EXTRACT_PROMPT_VERSION` and
`model="extract"`, and records an `ok` run. An `LLMOutputError` from either call
records a `failed` run, logs `extract_failed` with the thread id and the error class,
counts `failed`, and continues with the next thread. A `CassetteMiss` is logged as `extract_cassette_miss`
with the thread id and re-raised unchanged: strict replay is the contract (SPEC section 6). Running twice on an unchanged store writes
nothing the second time, and the second report's `threads` equals the first run's
`failed` (a failed run leaves the thread pending, M1 B1.7), zero when none failed. One version constant covers both
prompts, so a triage prompt edit re-runs the stage like an extraction prompt edit.

B1.7 The thread delta a kept thread extracts is the one `thread_deltas("extract", ...)`
returned: since the last `ok` extract run, root included. A thread dropped at revision
1 and kept at revision 2 therefore sees only revision 2's new messages plus the root;
revision 1 was judged to carry no project information.

B1.8 Moved to B0.2: the store methods land in the stubs pull request.

B1.9 `digest extract` on a store replayed to a day, in replay mode with the matching
cassettes, exits 0 and prints the report. Without `meta.until` it prints the reason and
exits 1. In replay mode with no cassette for a pending thread it exits 1 with the
`CassetteMiss` message (key and model); the thread id is in the `extract_cassette_miss`
log record.

B1.10 Logging: `triage_decided` (thread id, probability, threshold, kept),
`extract_thread` (thread id, items written, drops), `extract_failed`. Never text.

B1.11 Moved to B0.3: `World.glossary()` lands in the stubs pull request.

### Tests

`tests/test_extract.py`: B1.2 to B1.7 and B1.9 (CliRunner, tmp store, cassette dir under
`tmp_path` written with `Cassettes.put` from `LLMResponse`s the test builds; the LLM
runs in replay mode so no backend is needed). B1.4 has Hypothesis property tests over a
generated glossary. B1.5 is parametrized over each drop field. Null cases: an item with
`owner=None`, `due=None`, empty `claims` and empty `tasks`; a delta with `root=None`; a
glossary with a person who has no role (a bot); `verify` on an `Extraction` with no
items; a probability of exactly the threshold (kept).

### Acceptance

- A4 groundwork: every stored item carries evidence whose quote was found in the cited
  message and whose permalink was constructed by code.
- A8: replay mode, no network.
- SPEC section 5 step 2: triage probability through `extract`, threshold from
  `config/priors.yaml`, decision stored per thread revision.

## Unit 2: absences and golden labels

### Interface

`digest/project.py` (additions)

```python
def add_working_days(day: date, n: int) -> date            # n may be negative; Monday to Friday
def working_days_between(start: date, end: date) -> int    # working days strictly after start up to and including end; 0 when end <= start
```

`digest/absence.py`

```python
def checklist_absences(project: ProjectConfig, items: Sequence[Item], as_of: date, zone: ZoneInfo) -> list[Item]
def ask_absences(items: Sequence[Item], thread_events: Callable[[Item], Sequence[Event]], as_of: date, cadence_days: int, zone: ZoneInfo) -> list[Item]
def detect_absences(store: Store, project: ProjectConfig, priors: Priors, as_of: datetime, zone: ZoneInfo) -> list[Item]
```

`digest/world/schema.py` (stubs commit)

```python
class Channel(Frozen):
    id: str
    name: str
    purpose: str
    private: bool = False


class Ask(Frozen):
    of: str  # persona user_id the question is addressed to
    answered_day: int | None  # working day of the reply, None when never answered


class WorldEvent(Frozen):
    ...
    ask: Ask | None = None  # present exactly when kind == "ask"
```

`digest/world/manifest.py`

```python
class ThreadRef(Frozen):
    channel_id: str
    thread_ts: str
    authored: Literal["human", "render"]

class Manifest(Frozen):
    threads: dict[str, ThreadRef]      # world event id -> thread

def load_manifest(path: Path) -> Manifest
def write_manifest(manifest: Manifest, path: Path) -> None
```

`digest/world/golden.py`

```python
class GoldenClaim(Frozen):
    entity: str
    attribute: Attribute
    value: str
    authority: Authority
    supersedes: str | None     # event id
    contradicts: tuple[str, ...]

class GoldenItem(Frozen):
    event_id: str
    scenario: str | None
    day: int
    date: date
    channel_id: str
    thread_ts: str
    type: ItemType             # the event kind; distractors are not items
    summary: str
    entities: tuple[str, ...]  # claim entities, deduplicated
    claims: tuple[GoldenClaim, ...]
    tasks: tuple[TaskTouch, ...]   # the world's touches (task, effect); the day is the event's
    owner: str | None          # ask: the persona asked
    affected_roles: tuple[Role, ...]
    must_see_for: tuple[str, ...]
    known_by: dict[str, int]

class GoldenAbsence(Frozen):
    day: int
    date: date
    source: Literal["checklist", "ask"]
    gate: str | None
    task: str | None
    cites: str                 # event id of the last evidence or the ask
    audience: tuple[str, ...]  # roles for a checklist absence; user ids for an ask
    days_silent: int

class GoldenDistractor(Frozen):
    event_id: str
    day: int
    channel_id: str
    thread_ts: str

class Golden(Frozen):
    ask_reply_cadence_days: int     # the prior the absences were derived with
    items: tuple[GoldenItem, ...]
    absences: tuple[GoldenAbsence, ...]
    distractors: tuple[GoldenDistractor, ...]

def derive_golden(world: World, manifest: Manifest, priors: Priors) -> Golden
def write_golden(golden: Golden, path: Path) -> None     # JSONL: one meta line, then one line per item, absence and distractor, each with a "kind" field
def read_golden(path: Path) -> Golden
```

CLI: `digest absences [--world NAME] [--db PATH]` opens the store as `extract` does,
refuses (exit 1, "run digest extract first") while `thread_deltas("extract", ...)` as of
`meta.until` is non-empty, and prints the absence items as of `meta.until` as JSON
lines. `digest world golden [--world
NAME]` reads `world.yaml`, `manifest.json` and `config/priors.yaml`, writes
`data/world/<name>/golden.jsonl`, and prints the counts; it exits 1 when the manifest
lacks an event that is not a distractor.

### Behaviours

B2.1 `add_working_days(Thu 2026-10-22, -7)` is `Tue 2026-10-13`; `add_working_days(Fri,
1)` is the next Monday. `working_days_between(Fri, Mon)` is 1; `working_days_between(d,
d)` is 0; a weekend `end` counts the working days up to the Friday before it. Property:
`working_days_between(d, add_working_days(d, n)) == n` for `n >= 0` and weekday `d`.

B2.2 Checklist absences (SPEC section 5 step 5 source a). A gate is "inside its buffer"
on `as_of` when `add_working_days(gate.date, -gate.buffer_days) <= as_of <=
gate.date`. For each such gate and each task of its checklist: the task is done when an
item carries a `TaskLink` to the task with `effect="done"` and `message_ts` at or
before `as_of`; a done
task yields nothing. Otherwise the last evidence is the `TaskLink` to the task (either effect)
with the latest `message_ts` at or before `as_of` over every item; with none the task
yields nothing (SPEC: never an absence without a citation; open item U2). `days_silent =
working_days_between(evidence date, as_of)` where the evidence date is that
`message_ts` converted to a date in `zone`; the link's own message, not the item's
`last_updated`, so later chatter merged into the item does not reset the clock, which is
also how the golden derivation counts (B2.5). When `days_silent > task.expected_cadence_days`
the detector emits an `Item` with `type="absence"`, `item_id=f"absence:{task.id}"`,
`thread_id` of the cited item, `title` naming the gate and the task, `entities` the
gate id plus the cited item's entities, `owner=None`, `affected_roles=(task.owner,)`,
`severity` from the task's priority (`critical` and `high` map to `high`, else
`medium`), `confidence="high"`, `evidence` the cited item's evidence entry for the
link's `message_ts`, `first_seen` and `last_updated` that ts, and `absence` with
`source="checklist"`, the gate and task ids, `days_silent`, `buffer_consumed =
working_days_between(buffer start, as_of)` and `buffer_days`.

B2.3 Ask absences (source b). For each item with `type="ask"`, `status="open"` and
`owner` set: `thread_events(item)` returns the thread's events at or before `as_of`
(`store.events_in_thread`). The ask message is the event cited by the first evidence entry whose author is not
`owner` (B1.3 and B1.5 keep the asking message first, and this rule holds when the model
cites the reply first); its `user_id` is the asker. A non-deleted event by `owner` with `ts`
greater than the ask message's `ts` is a reply and closes the question (nothing
emitted); the comparison is against the ask message, not `last_updated`, so a reply the
extractor folded into the item as evidence still counts. Otherwise `days_silent =
working_days_between(ask date, as_of)`, the ask date being the ask message's `ts` in
`zone`; when it exceeds `cadence_days` the detector emits an absence item with
`item_id=f"absence:ask:{ask item_id}"`, `thread_id` the ask's, `title` naming the asker
and the person asked, `summary` the ask's, `entities` the ask's, `owner` the person
asked, `affected_roles` the ask's, `severity="medium"`, `confidence="high"`, `evidence`
the ask message's entry, `first_seen` and `last_updated` the ask message's ts, and
`absence` with `source="ask"`, `asker` the asker and `days_silent`. An ask
with no such evidence entry is skipped and logged (`absence_ask_without_message`). `detect_absences` reads `store.items(as_of)` and
`store.events_in_thread` and runs both sources with `as_of` converted to a date in the
world's zone.

B2.4 The checklist source emits nothing for a gate whose date has passed and nothing
before its buffer opens; the ask source has no gate and runs on every day. Output is checklist absences in `(gate id, task id)` order, then ask absences
in ask item id order, so a run is deterministic. On the Kestrel fixture with the
scripted touches applied as items, the checklist absences by day are: thermal days 5 to
9, EMC days 6 to 9, FAI days 8 and 9, clear-to-build days 6 to 9, battery day 9, and none for the
safety and texture tasks, which have no evidence (premise P5; S3-BUFFER's roll-up and
ASK-SAF's question are not progress); the ask absence for ASK-SAF fires on days 6 to 10
and none fires for ASK-PO. Unit 2's tests pin these from a hand-built item list, and
the eval measures them from extracted items.

B2.5 `derive_golden`: one `GoldenItem` per event whose kind is not `distractor`, with
`type` the kind, `claims` resolved (authority the claim's own or the poster's),
`entities` the claim entities in order without repeats, `owner` the `ask.of` persona
for an ask, and the thread from the manifest; an event that is not a distractor and has
no manifest entry raises `KeyError` naming it. One `GoldenDistractor` per distractor
event with a manifest entry. Golden absences follow B2.2 and B2.3 on the script: for
each day `d` in `1..days`, each gate with `gate day - buffer_days <= d <= gate day`
(day indices), each task not marked done by a `TaskTouch` on or before `d`, the latest
`TaskTouch` on or before `d` is the evidence; `d - evidence day > cadence` emits an
absence citing that event with `audience=(task.owner,)`. For each ask event on day `a`
with `answered_day` None or greater than `d`, `d - a > priors.absence.ask_reply_cadence_days`
emits an absence citing the ask with `audience=(poster, ask.of)`. Day `d` is converted
to a date with `World.date_of`.

B2.6 `write_golden` writes one JSON object per line with `"kind"` set to `meta`, `item`,
`absence` or `distractor`: one `meta` line first (`ask_reply_cadence_days`), then items in
`(day, event_id)` order, then absences in
`(day, source, gate or "", task or "", cites)` order (`gate` and `task` are None for
the ask source), then distractors; `read_golden` reads it back equal.
Dates are ISO strings.

B2.7 `digest world golden` on the Kestrel world with a complete manifest writes the file
and exits 0; with a manifest missing a non-distractor event it exits 1 naming the
event. `digest absences` on a replayed store prints one JSON line per absence item and
exits 0; on a store with no `meta.until`, or with pending extract deltas, it exits 1.

B2.8 World validation (unit 2 code, on fields the stubs commit adds): an event has `ask` exactly when
its kind is `ask`; `ask.of` is a human persona who is a member of the event's channel and
is not the poster; `ask.answered_day` is between the event's day and `days`. A world
check also records nothing about `private`: it is a flag the M3 filter reads.

B2.9 Fixture content (stubs commit, pinned by unit 2's tests on the bundled world):
channel `C0LEADS` `kestrel-leads` with `private: true`, members `U0EM01` and `U0PM01`,
one event `LEADS-1` (day 4, PM to EM, kind `risk`: the pilot customer presses for 600 kg
and the two agree to keep the PRD at 500 kg until after DVT-1; no claim, so the scripted
claim pair on `PN-90001 spec_param` (S8-PAYLOAD) and its labels are unchanged;
`must_see_for` `U0EM01`; `known_by` PM and EM on day 4);
two ask events: `ASK-SAF` (day 3, `C0ENGEF`, EM asks FW when TST-SAF-01 reruns on the
DVT firmware, `answered_day` None, no task touch (a question is not progress, B1.3),
`must_see_for` `U0FW01`, `known_by` EM and FW on day 3) and `ASK-PO` (day 4, `C0SUPCH`, EE asks SC whether PO-45002211 was amended to Rev C,
`answered_day` 5, `known_by` EE and SC on day 4). Every other channel stays public.
The private channel has at least one human persona outside it (five of seven). No
existing event changes.

### Tests

`tests/test_absence.py`: B2.1 (with a Hypothesis property), B2.2 to B2.4 from a
hand-built item list on the bundled project configuration and on a two-gate synthetic
one; B2.7 for `digest absences` (CliRunner, tmp store). Null cases: a task with no
evidence; an item with `tasks=()`; an ask with `owner=None` (skipped); a reply by
someone other than the owner (still silent); `as_of` on a weekend.

`tests/test_golden.py`: B2.5 to B2.7 on a small synthetic world and on the bundled
world with a hand-written manifest under `tmp_path`. `tests/test_world.py` (appended):
B2.8 (parametrized over each broken ask reference) and B2.9 on the bundled world. Null cases: an event with no claims (no entities); a world
with no ask events (no ask absences); a manifest entry for a distractor only.

### Acceptance

- A6 groundwork: every absence item cites the last evidence and names the gate, the
  buffer consumed and the task.
- SPEC section 6: `golden.jsonl` derived from the script, not hand-written.
- SPEC section 9 v0.4 core: channel `private`, with a persona outside the private
  channel so the M3 filter has a test input.

## Unit 3: corpus renderer

### Interface

`digest/corpus/avoid.py`

```python
class AvoidNames(Frozen):
    names: tuple[str, ...]          # people and programs: whole word, case-insensitive
    identifiers: tuple[str, ...]    # part numbers, codes: whole token, case-insensitive
    phrases: tuple[str, ...]        # multi-word terms and channel names: whole-word bounded, case-insensitive

DEFAULT_AVOID_PATH = Path("data/world/avoid-names.yaml")
def load_avoid_names(path: Path = DEFAULT_AVOID_PATH) -> AvoidNames
def find_avoided(text: str, avoid: AvoidNames) -> list[str]     # the entries found, in order, no repeats
```

`digest/corpus/prompts.py`

```python
RENDER_PROMPT_VERSION = "m2.1"
RENDER_SYSTEM: str
def render_request(world: World, event: WorldEvent, earlier: Sequence[WorldEvent], avoid: AvoidNames, output_schema: dict[str, Any]) -> LLMRequest
```

`digest/corpus/render.py`

```python
class RenderedReaction(Frozen):
    name: str
    user_id: str                    # Literal of the channel's human members, built per request

class RenderedMessage(Frozen):
    poster: str                     # Literal of the personas whose channels include the event's channel
    day_offset: int                 # 0 = the event's day; an answered ask puts the reply on answered_day
    minutes_after_start: int
    text: str
    files: list[str]
    reactions: list[RenderedReaction]

class RenderedThread(Frozen):
    messages: list[RenderedMessage]  # the first message is the thread root

def thread_model(world: World, event: WorldEvent) -> type[RenderedThread]   # create_model with the per-event Literals

class RenderReport(Frozen):
    rendered: int
    reused: int                     # anchor threads
    messages: int
    messages_per_thread: dict[str, int]
    chars_per_message: tuple[int, ...]

class RenderError(ValueError): ...  # names the event and the rule it broke

def render_world(world: World, llm: LLM, anchor: Path, manifest_in: Manifest, avoid: AvoidNames,
                 events_out: Path, manifest_out: Path) -> RenderReport
```

CLI: `digest world render [--world NAME]` loads the world, `anchor.jsonl`,
`manifest.json` (anchor entries), `avoid-names.yaml` and the client from `Settings`,
renders, and writes `events.jsonl` and `manifest.json`. Exit 1 with the reason on a
`RenderError`, a cassette miss in replay mode or a missing key in record mode.

### Behaviours

B3.1 Every event in `world.events` that has no `authored: human` manifest entry is
rendered once, in `(day, position in the file)` order, through `llm.extract(request,
thread_model(world, event))` with `model="render"`. The request's user message carries:
the program and its one-line description, the channel (name, purpose, members with user
id, name, role and aliases), the poster, the event's kind and summary, each claim as
entity id, the entity's aliases and the value, the `noise` knobs, for an ask the
persona asked (`ask.of`) and the day of the reply or that it is never answered, the
summaries of earlier events with the same scenario id, the day of week, and the target message
count by kind (fyi and bom_change 1 to 3; decision, risk, schedule_change and
spec_change 3 to 8; blocker 3 to 6; ask 1 plus the reply when `answered_day` is set;
distractor as the summary says, else 2 to 6). The system prompt states the style guide
(short, informal engineering chat; `<@U...>` mentions; part numbers and revisions in
the forms the glossary lists; no narration of the scenario; a claim is stated by the
poster or by a member who would know it; `indirection` means the value is implied
rather than stated; `delayed_crosspost` means the thread restates an earlier post;
never name the event id, the scenario or the word "scenario"), the negative-name list
("never use these names, ids or phrases"), and the output rules.

B3.2 Timestamps are assigned by code. Day `d` maps to `world.date_of(d)`; the event's
base time is 08:30 local plus 25 minutes per earlier event on the same day; message
`ts` is the base plus `minutes_after_start`, on `date_of(d + day_offset)`, with the
fractional part a running six-digit counter starting at `010000` (the anchor lines use
values below that; the nine M1 lines use `000100` to `000900`). The first message is the root (`thread_ts == ts`); every other
message is a reply. `event_ts == ts`. `mentions` are the `<@U...>` ids found in the
text. Reactions get `ts` one second after their message. `channel_id`, `channel_name`,
`user_id` and `permalink` (via `permalink_for`) come from the world. A message's
`day_offset` must be 0 unless the event is an answered ask, where exactly the last
message has `day_offset = answered_day - day`; any other value is a `RenderError`.

B3.3 Code checks each rendered thread: every claim entity's id or one of its aliases
appears in the thread text (case-insensitive) unless the event sets `noise.indirection`
(the knob asks for the value to be implied, and the extractor missing it under noise is
what the degradation measurement reads), the thread text contains no entry of the
negative-name list, the message count is at least one, the root is by the event's poster with
`day_offset` 0, a reply never precedes the root, every computed `ts` falls on `date_of(d
+ day_offset)` in the world's zone, and for an ask no message other than the last is by
`ask.of`; when `answered_day` is set the thread has at least two messages and the last is
by `ask.of` on that day, and when it is None no message is by `ask.of`. A failed check raises `RenderError` naming the event, the rule and the cassette key of
the request (`request_key`), so the operator can delete that cassette and re-render;
nothing is written. The schema enforces posters and reactors through the per-event Literals, so
an unknown poster cannot occur.

B3.4 `render_world` reads `anchor.jsonl` (human-authored lines in the `read_events`
line format, M1 plan B1.8), merges
them with the rendered lines, sorts by `event_ts`, writes `events.jsonl`, and writes
`manifest.json` with the anchor entries kept and one `authored: render` entry per
rendered event. Rendering the same world twice in replay mode writes identical files.

B3.5 `find_avoided`: a `names` entry matches a whole word (letters on neither side),
case-insensitive, so `Alex` does not match `Alexander` and `Jun` does not match
`June`; an `identifiers` entry matches a whole token bounded by whitespace or
punctuation; a `phrases` entry matches with word boundaries at both ends,
case-insensitive. The role labels ME, EE, SC, EM and PM are not in the list: the brief
mandates them.

B3.6 `data/world/avoid-names.yaml` holds every name, identifier and phrase listed under
"Names and terms we must NOT reuse" and "Names to add" in
`notes/research/competing-submissions.md` (the private note; the file is the tracked
copy). A test asserts the three sections of the list are non-empty, runs `find_avoided` over
`world.yaml`, `anchor.jsonl` and, when they exist, `events.jsonl` and `golden.jsonl`,
and asserts nothing is found; B5.1 asserts the two generated files exist once the data
pull request lands.

B3.7 `anchor.jsonl` holds the human-authored anchor slice: the nine lines M1 shipped in
`events.jsonl` (N1-TORQUE, S1-AGREE and S1-CUT) plus the lines that complete the
40-message slice (open item U3), each thread listed in `manifest.json` with `authored:
human`; unit 3 creates `manifest.json` with those entries and the data pull request
adds the rendered ones. `events.jsonl` keeps the nine lines until the data pull request regenerates it.

B3.8 `RenderReport` carries message counts per rendered thread (anchor threads excluded,
as in B4.6) and the character length of every rendered message for the command's
printed summary; the eval computes the same figures from the
files (B4.6), so the report has one source.

### Tests

`tests/test_corpus.py`: B3.1 to B3.4 on a two-event synthetic world with scripted
`LLMResponse`s in a `tmp_path` cassette dir (replay mode), including an answered ask;
B3.2 pins the timestamp arithmetic; B3.3 is parametrized over each rule (a missing
entity mention, an avoided name, a bad `day_offset`); B3.5 is parametrized with the
`Alexander` and `June` cases; B3.6 on the bundled files; the CLI with CliRunner. Null
cases: an event with no claims (no entity check); a message with no files and no
reactions; a world with every event human-authored (nothing rendered, files rewritten
unchanged).

### Acceptance

- SPEC section 6: a different model family renders; noise knobs from the world; the
  negative list in the prompt and checked in CI; a human-authored anchor slice.
- D12: the eval report states which model rendered (from the cassettes' `model`).

## Unit 4: extraction eval

### Interface

`digest/eval/extraction.py`

```python
class TypeScores(Frozen):
    golden: int                  # golden items of this golden type
    golden_matched: int          # of those, matched (B4.2)
    extracted: int               # extracted items of this extracted type in golden or distractor threads
    extracted_matched: int       # of those, paired with a golden item
    precision: float | None      # extracted_matched / extracted; None when extracted == 0
    recall: float | None         # golden_matched / golden; None when golden == 0

class ExtractionReport(Frozen):
    world: str
    days: int
    models: dict[str, str]       # logical name -> concrete id seen in the cassettes
    by_type: dict[str, TypeScores]
    thread_recall: float | None  # golden items whose thread yielded any item
    type_agreement: float | None # among matched golden items, share whose item has the same type (advisory)
    objective_misses: dict[str, int]   # thread-matched golden items with a wrong or missing objective field, per field
    claims_golden: int
    claims_found: int            # golden claims found on an item in the thread with equal normalized value
    claims_extracted: int        # extracted claims in golden threads
    claims_extracted_matched: int
    claim_recall: float | None   # claims_found / claims_golden
    claim_precision: float | None   # claims_extracted_matched / claims_extracted
    evidence_entries: int        # delivered evidence entries, the n of citation_validity
    entity_jaccard: float | None # mean over matched items with golden entities; n reported
    entity_jaccard_n: int
    entities_golden: int         # golden claim entities over the matched items with at least one
    entities_found: int          # of those, present in the matched item's entities
    entity_recall: float | None  # entities_found / entities_golden
    citation_validity: float     # verified evidence entries over delivered entries; 1.0 by construction, reported
    drops: dict[str, int]
    triage_golden_kept: int
    triage_golden_total: int
    triage_by_event: dict[str, float]  # max probability over the thread's revisions, per golden and distractor event
    triage_dropped_golden: tuple[str, ...]
    triage_distractors_kept: tuple[str, ...]
    missed: tuple[str, ...]      # golden event ids with no matched item
    unmatched: tuple[str, ...]   # extracted item ids matched to no golden item, with the thread kind
    absence_tp: int
    absence_fp: int
    absence_fn: int
    absence_false_alarm_rate: float | None
    absence_recall: float | None
    uncitable_tasks: tuple[str, ...]   # tasks inside a buffer with no evidence on any day
    realism: dict[str, float]    # messages per thread and chars per message: mean, median, p90
    not_run: dict[str, str]      # metric -> reason

def run_extraction_eval(world_dir: Path, llm: LLM, priors: Priors, store_path: Path) -> ExtractionReport
def write_report(report: ExtractionReport, path: Path) -> None     # markdown
```

CLI: `digest eval [--world NAME] [--out PATH]` builds the client from `Settings`,
runs the eval in a fresh store at `data/eval-<world>.sqlite` (deleted first), writes `reports/eval-<date>.md` (or `--out`), prints the path and the
headline rows. Exit 1 on a cassette miss in replay mode.

### Behaviours

B4.1 The eval replays the world one day at a time: for `d` in `1..days`, `replay_world`
to `date_of(d)`, then `run_extract` and `detect_absences` both as of the end of that day
in the world's zone (the same `as_of`); it collects the absence items per day and the drop counts. It reads `golden.jsonl`
and `manifest.json` from the world directory, re-derives the golden set from `world.yaml`,
the manifest and the priors in force, and raises `ValueError` ("run digest world golden")
when the two differ, so a stale label file never scores a run; the file's meta line makes
a cadence change the visible cause.

B4.2 Matching: an extracted item belongs to the golden item whose manifest thread equals
`item.thread_id`. A golden item is thread-matched when its thread has at least one
item. It is matched (objective fields, SPEC section 7) when one item in its thread
carries every golden claim (same entity and attribute, `normalize_space(value).lower()`
equal) and, when the golden item has an `owner`, the same `owner`; type is not part of
the match. SPEC's other objective fields, `due` and `status`, have no golden value in M2
(world events carry neither), so they are not scored and `not_run` says so. Pairing is deterministic: golden items in `(day, event_id)` order each take the
first qualifying item in `first_seen` order that no earlier golden item took, so one
extracted item pairs with at most one golden item. `by_type` groups golden items by
golden type for recall and extracted items by extracted type for precision;
`type_agreement` is the share of pairs whose types agree (advisory, SPEC section 7). A golden item whose thread's triage decisions
(`store.triage_for_thread`) were `kept=False` at every revision counts in
`triage_dropped_golden` and in `missed`; a golden thread with no triage row at all is a
data error and the eval raises `ValueError` naming it (every thread in the store was
extracted). Items in a distractor thread, and items in a
golden thread matched to no golden item, are `unmatched`.

B4.3 Claims: a golden claim is found when an item in its thread carries a claim with
the same entity and attribute and `normalize_space(value).lower()` equal;
`claim_recall` is found golden claims over golden claims and `claim_precision` is
extracted claims in golden threads equal to some golden claim over those extracted
claims. A thread-matched golden item that is not matched is listed in `objective_misses`
per field (`claims.value`, `owner`). Entity Jaccard is computed for matched items whose
golden item has at least one entity; `entity_jaccard_n` is that count. Golden entity
lists are the claim entities, so an item that also names context entities scores below
1; `entity_recall` (`entities_found` over `entities_golden`, counted over the same matched
items) is reported beside it.

B4.4 Absences: detected and golden absences are keyed `(day, task)` for the checklist
source and `(day, ask event id)` for the ask source (the ask item's thread maps to the
event through the manifest). `absence_false_alarm_rate = fp / (tp + fp)` and
`absence_recall = tp / (tp + fn)`, None when the denominator is zero. `uncitable_tasks` lists the
tasks that were inside a buffer on some day with no evidence on any day.

B4.5 `models` comes from `Cassettes.recorded_models()`, a new method that reads every
cassette under the root and maps each request's logical model to the set of concrete
`response.model` ids seen; the report prints them joined, so it names which family
rendered and which extracted (D12). The render cassettes are in the same directory, so
the eval reports the renderer without running it.

B4.6 Realism: from the rendered threads (`manifest.json` entries with `authored:
render` and the events in `events.jsonl`): messages per thread and characters per
message, mean, median and p90. The comparison to DISCO statistics is a `not_run` row
("no reference statistics in the repo") unless `data/world/reference-stats.yaml`
exists with published figures and their source.

B4.7 `write_report` writes markdown with one table per section (extraction by type,
claims, triage, absences, drops, realism, models, not run) and `n` beside every rate,
read from the report's own counts (`golden`, `extracted`, `claims_golden`,
`claims_extracted`, `evidence_entries`, `entity_jaccard_n`, `entities_golden`, the
absence counts).
The report is deterministic for a given store and cassettes: it carries no wall-clock
time, its only dates are world dates, and the date in the default file name comes from
`--out` or the world's last day, never from today.

B4.8 `digest eval` in replay mode with complete cassettes exits 0 and writes the file;
with a missing cassette it exits 1 with the `CassetteMiss` message.

B4.9 `Cassettes.recorded_models()` on an empty or missing root returns an empty
mapping; on a root with two cassettes for `extract` with different concrete ids it
returns both ids under `extract`.

### Tests

`tests/test_eval.py`: B4.1 to B4.4, B4.6 and B4.7 on a small synthetic world (two
golden events, one distractor, one ask) with hand-written `events.jsonl`,
`manifest.json`, `golden.jsonl` and scripted cassettes for triage, extraction and
nothing else (the renderer is not run by the eval); the test checks the counts against
hand-computed values. B4.8 with CliRunner; B4.9 in `tests/test_llm.py`. Null cases: a type with no golden items
(`recall=None`); no distractor threads; no absences detected and none golden (rates
None); a golden item with no entities (excluded from Jaccard, `n` unchanged).

### Acceptance

- A4: citation validity reported (100 percent by construction of `verify`); A6:
  absence false-alarm rate reported; SPEC section 7: precision and recall by type, the
  two mirror counts, triage recall against golden items, drop count per verified field,
  entity Jaccard beside exact match, the advisory type split, models named.

## Data pull request: the first recording

Runs after units 1 to 4 have merged and the user has put `OPENROUTER_API_KEY` in
`.env`. Steps, in order, from a clean worktree:

1. Spike (premise P7): one `extract` call with the `Extraction` schema (it carries a
   `$ref` list with `minItems: 1`, nullable fields and enums, the constructs P8 asserts)
   and one `render` call with a one-message `RenderedThread` schema, through
   `OpenRouterBackend.complete` directly (no cassette written; `live` mode is for the
   API backend only, M1 B2.8), printed and discarded. Both must
   return schema-conforming JSON. If the Claude call does not, extraction cassettes are
   recorded through `anthropic_api` (D15 "would change it") and the plan records it.
2. `DIGEST_MODE=record uv run digest world render` writes `events.jsonl`,
   `manifest.json` and the render cassettes. A `RenderError` names the cassette to
   delete; re-running then records a fresh response (record mode returns a stored
   response otherwise).
3. `uv run digest world golden` writes `golden.jsonl`.
4. `DIGEST_MODE=record uv run digest eval` records the triage and extraction cassettes
   and writes `reports/eval-<date>.md`.
5. `DIGEST_MODE=replay uv run digest eval` reproduces the report byte for byte, and
   the data pull request's test (B5.1) keeps that check in CI. The
   report's `triage_by_event` table is the input SPEC section 5 step 2 names for
   revisiting `miss_cost_ratio` after the first twenty golden threads; a change to the
   ratio is its own commit with the before and after in the report.
6. README: which backend and models recorded each cassette set, the report link, the
   corpus sources (B3.1 style guide sources: public hardware engineering forums and
   open-source hardware issue threads, paraphrased, never copied; HERB's artifact shape
   as a schema reference only, no content, CC BY-NC).

The pull request holds the data, the README change and one test, written by the Codex
test author with its own proof (B5.1). Both reviewers see it.

B5.1 `tests/test_corpus_data.py`, written by the Codex test author after the recording
and proven with `--break blank:<one cassette file>:1-3` (replay then misses and the test
fails), runs `run_extraction_eval` on the bundled world in replay mode against the
committed cassettes and asserts: the written report equals
`reports/eval-<date>.md` byte for byte; `citation_validity == 1.0`; every event of the
world has a manifest entry and every manifest thread has at least one line in
`events.jsonl`; `models` names two model families (D12). This is the check CI runs on
the recordings; without it a missing cassette or an empty render would pass green.

## Decisions recorded in this plan

| Fork | Chosen | Why |
|---|---|---|
| Triage threshold core or stretch | Core, as `1 / (1 + miss_cost_ratio)` from `config/priors.yaml` | SPEC section 5 step 2 states it as the core triage shape while the section 9 cut list named it stretch; a probability needs a threshold and the formula is one line. This plan's pull request removes the two section 9 mentions so the contract agrees with itself. |
| Where the renderer lives | `digest/corpus/`, a new layer above `store | llm | world` | `digest.world` and `digest.llm` are siblings in the import contract; a renderer inside `world` could not import the client. |
| Claims storage | JSON on the item row; no claims table in M2 | The ledger's bi-temporal shape is M3's design; a table now would be altered then. M3 builds its table from `items.claims`. |
| Schema version | Stays 1; new tables under `IF NOT EXISTS` | Additive change; a bump would refuse every existing development store for no reader's benefit. |
| Triage record | Its own `triage` table, not `stage_runs` | SPEC asks for the decision per thread revision with its rationale; `stage_runs` has no payload column and its status means something else. |
| Dropped thread and the extract stage | Records an `ok` extract run at `last_ts` | Otherwise the thread is pending every day and re-triaged daily; B1.7 states the cost (the next delta omits the dropped revision's messages). |
| Model column in `stage_runs` and `triage` | Logical name | Stores built from gateway recordings stay valid for the API backend (D15); the concrete id is logged. |
| Task linkage | The model picks `tasks[{task, effect}]` from the checklist in the prompt; code verifies the id | SPEC gives the detector no other way to find a task's last evidence in M2 core (task `entities` is stretch). Known gap: a hallucinated `done` closes a task; the eval reports absence recall beside the false-alarm rate so over-linking shows. |
| A task with no evidence at all | No absence (SPEC: never without a citation) | Open item U2 for the user; the eval lists such tasks. |
| Ask fields on world events | `ask: {of, answered_day}`, present only on `ask` events | Unanswered asks are a v0.3 core absence source; the user's "only `private`" decision was about v0.4 fields. These two fields are the minimum the renderer and the golden derivation need. |
| Which channel is private | A new `kestrel-leads` (EM, PM) with one event | Making any existing channel private blocks a scripted must-see for a persona outside it (verified: every channel but `dvt1-build` and `plm-feed`; `dvt1-build` has nobody outside and a private PLM feed would hide the system of record from EM, PM and FW). A new channel adds the M3 test input without changing a golden label. |
| Rendering the private channel now | Yes, in M2 | The corpus is rendered once; adding the channel in M3 would re-record cassettes. |
| Anchor slice storage | `anchor.jsonl`; `events.jsonl` is generated | The renderer owns `events.jsonl`; the human lines must survive regeneration. |
| Fractional ts ranges | Anchor below `010000`; rendered from `010000` | Keeps every ts unique when the two are merged, with room for a 40-line anchor. |
| Posters and reactors in the render schema | Per-event `Literal` through `create_model` | The schema enforces membership; a wrong poster cannot occur, so no post-check and no retry logic is needed. |
| Entity mentions in rendered text | Checked in code, `RenderError` | Cannot be expressed in a schema; failing the recording run is the honest outcome. |
| Negative-name check | Whole-word for names, whole-token for ids, bounded phrases; scans the four data files | A substring check flags `June` for `Jun`; the rule must be stated or two implementers diverge. |
| Eval placement | Its own unit after units 1 to 3 | It runs the other three; folding it into the data pull request would mix code and recordings. |
| Cost and latency rows | M4 | Section 7 lists them; M4 owns the report; M2 adds no usage counter to the client. |
| DISCO realism comparison | `not_run` unless reference figures with a source are in the repo | No number is reconstructed from memory. |
| HERB | Schema reference only | CC BY-NC; its generator is not released; the unanswerable-question idea is covered by distractor threads that look like project chatter. |
| Deleted messages | Omitted from prompts and the citable set | The corpus has no deletes in M2; retraction is M3's rule. |
| Claim provenance | Each claim names its evidence `message_ts`; authority and asserted time derive from that message | One item can carry claims by different authors (a CM root, an EE reply); M3's bi-temporal ledger needs the asserted time per claim, and adding it later is a schema change. |
| Item update | Evidence and claims merge; scalars replace | Dropping the earlier quotes would lose the evidence a reader was shown while the item was open. |
| Priority-scaled absence window and rank growth with age | Not in M2; the window is `buffer_days` for every task | Both sentences entered SPEC in the v0.4 commit (4737852) and section 9 does not name them core, so section 10's rule makes them stretch. |
| Shared code in the stubs commit | Store item and triage methods, priors loader, manifest reader and writer, `World.glossary()` are implemented there | Units 2 and 3 read them; as stubs they would force one merge order. M1's stubs commit implemented `load_world_header` on the same reasoning. |
| Extraction match | Objective fields (claims, owner); type advisory | SPEC section 7 splits objective from advisory fields; a same-type item with the wrong revision is a miss, not a match. |
| Ask reply detection | A reply is an event by the person asked after the ask message, not after `last_updated` | An update that folds the reply into the item as evidence moves `last_updated` past the reply. |
| Revision suffix on an entity mention | Split off and discarded; the id is kept | A mention at a revision is not a claim about the current revision; revisions enter as `rev` claim values, and the stale-action audit that reads mentioned revisions is M3 stretch (D24). |
| Extraction prompt version per store | One; a different version refuses the run | New item ids are `thread#last_ts#n`, so two versions in one store would overwrite each other's items; a fresh store is how a bump is run, as replay restarts for an earlier day. |
| Which evidence entry is the ask message | The first entry whose author is not the person asked; B1.3 and B1.5 keep the asking message first | `first_seen` moves when an update cites an earlier message, such as an unrelated root; the first-cited message does not, and the author rule covers a model that cites the reply first. |
| Task evidence | Progress or completion, not any mention | The world's `TaskTouch` carries that meaning and the fixture's roll-up (S3-BUFFER) and question (ASK-SAF) have no touch; a mention rule would make the EM's own status post silence the detector. U2 offers the mention rule as an alternative. |
| Task link provenance | Each task link names the message that shows the progress | The silence clock counts from that message, as the golden counts from the scripted day; counting from the item's `last_updated` would let merged chatter reset it. |
| Entity Jaccard | Reported as SPEC asks, with `entity_recall` beside it | Golden entity lists are claim entities only, so Jaccard penalizes correct context entities. |
| One prompt version for the extract stage | `EXTRACT_PROMPT_VERSION` covers the triage and extraction prompts | A separate triage constant re-ran nothing on its own; one constant gives a prompt edit one visible effect. |
| `digest extract` steps one day at a time | The CLI runs the stage per working day up to `meta.until` | A multi-day delta is a different request from the one-day deltas the eval records, so replay would miss its cassettes. |
| Golden freshness | The eval re-derives the golden set and refuses a difference | A cadence check alone misses a world or manifest edit. |
| Edited evidence | A re-cited message replaces its entry; an entry whose message changed in the delta is re-checked and dropped when stale, with the claims and task links that cited it | The store holds one version per message (M1), so a kept quote must match the current text and a link must cite an entry the item holds; the corpus has no edits in M2. |
| Entity mention check and `indirection` | Events with the knob are exempt | The knob asks the renderer to imply the value; forcing the id into the text would cancel the noise the degradation curve measures. |
| `due` and dates in output schemas | Strings parsed in code | `format: date` support differs across backends; parsing in code drops and counts. |

## Open items for the user

U1. Put `OPENROUTER_API_KEY` in `.env` (never in chat). Needed for the spike and the
data pull request only; stubs, tests and code need no key. The `.env` line exists and is
blank (checked 2026-10-11).

U2. Checklist tasks with no evidence at all. Evidence means progress (B1.3, the world's
`TaskTouch`), and SPEC-literal this plan emits no absence for a task with none, so on
the Kestrel world the safety validation and texture release tasks are silent and A6's
demo shows neither. Two alternatives: (a) cite the latest message that names the gate
(the schedule bot's post or the gate review) as the last evidence; (b) let any message
about a task count as evidence, which makes ASK-SAF cite the safety task from day 3 but
also lets the EM's day-6 roll-up (S3-BUFFER) reset the thermal clock, so the thermal
absence fires on days 5 and 9 only. Default if no answer: SPEC-literal, and the eval
lists the tasks.

U3. The anchor slice. SPEC section 6 and D12 say one 40-message slice is human-authored.
Nine lines exist. Who writes the remaining 31 lines: the user; or the orchestrator model
drafts them and the user edits and approves, with the README saying "author-reviewed";
or the slice stays at nine lines, which changes SPEC section 6 and D12. No default: the
data pull request waits for the answer, and options two and three are contract changes
that land in SPEC and D12 first.

U4. Confirm the two ask events and the private channel event (B2.9) as world content.

## Premise ledger

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| P1 | Pull requests 5 to 9 are merged; `main` is 2ee5a48 | `gh pr list --state all`, `git log origin/main` on 2026-10-11 | verified |
| P2 | `SPEC.md` on `main` is v0.4 with the at-a-glance cut list | `git show origin/main:SPEC.md` | verified |
| P3 | `OPENROUTER_API_KEY=` is present and blank in the checkout's `.env` | `grep` with a length check, value never printed | verified |
| P4 | The Kestrel world has no `ask` events and no private channel | `yaml.safe_load` over `world.yaml`: kinds present, no `private` key | verified |
| P5 | Only `G-DVT1.safety` and `G-TR1.texture` never receive a `TaskTouch` | the same script over `events[].tasks` | verified |
| P6 | Making any existing channel private blocks a scripted `must_see_for` entry, except `dvt1-build` (no one outside) and `plm-feed` | membership script over personas and events | verified |
| P7 | The gateway honours a JSON schema for the `extract` (Claude Haiku) and `render` (GPT) models | needs the key; step 1 of the data pull request | TBD |
| P8 | Anthropic structured outputs reject `minimum`/`maximum` and `minItems > 1`; the gateway's strict mode requires every property in `required` | `notes/research/retrieval-dedup-eval.md` section 3 (from the Anthropic structured-outputs documentation) and M1 plan U1; the spike shows the constructs the plan uses are accepted and cannot show the rejections | accepted |
| P9 | Pydantic models with no defaults produce schemas with every property required, nullable fields as `anyOf` with `null`, nested models under `$defs`, and `create_model` with a `Literal` gives an `enum` | probe on pydantic 2.13.5 | verified |
| P10 | `add_working_days(2026-10-22, -7)` is 2026-10-13, so G-DVT1's buffer opens on world day 2 and S3-BUFFER's "4 days into the 7-day buffer" on day 6 is consistent | arithmetic probe | verified |
| P11 | The nine anchor lines fall on world days 2 and 4 with fractional ts `000100` to `000900` | timestamp conversion in `America/Los_Angeles` | verified |
| P12 | The import contract rejects `digest.world` importing `digest.llm` | `pyproject.toml` layers: siblings in one layer | verified by reading the contract |
| P13 | anthropic 1.11.0, pydantic 2.13.5, hypothesis 6.168.5 installed | `uv run python -c` | verified |
| P14 | ruff 0.16.10 (the resolved `ruff>=0.8`) formats Python code fences inside Markdown, so this plan failed `ruff format --check` in CI while the pre-commit ruff hook (0.14.0, Python files only) passed it | CI run 38188665234 on f275dbe; local `uv run ruff format --check .` | verified; `*.md` is excluded from `ruff format` in `pyproject.toml` so the code blocks stay illustrative and the hook and CI agree |

## Mechanics for this milestone

- Gates: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run ty check`, `uv run lint-imports`.
- Prove a test: `python ~/.local/bin/prove-test.py --test tests/test_extract.py --break
  blank:src/digest/extract/verify.py:N-M --run "uv run pytest tests/test_extract.py"
  --format "uv run ruff format tests/test_extract.py" --expect-error "<specific
  line>"`. Proofs cannot run against stubs: the test commit records the RED run
  (`NotImplementedError`) in its message and under `notes/proofs/`, and the proofs are
  recorded against the implementation before the code commit, as M1 did.
- Test author brief (Codex, execution lane, fresh session, `-C` its worktree under
  `C:\Users\disim\Documents\evercurrent-wt\`, stdin from `/dev/null`): this plan,
  `SPEC.md` sections 4 to 7, the stubs, `.claude/rules/testing.md`. It writes only the test
  files the unit table names for its unit (new files, and the named appended files)
  and hands back unstaged. No network.
- Implementer brief (Opus subagent, own worktree): this plan, the stubs, the tests, the
  rules under `.claude/rules/`. It may not edit `tests/`. Done when the five gates pass.
  Unit 1's and unit 3's implementers write prompt text; the briefs name B1.3 and B3.1 as
  the content contract.
- Reviews: Codex `timeout 900 codex exec -p review -m gpt-6-astra --output-schema
  ~/Documents/dev-kit/codex/schemas/review.json -o <file> "..." </dev/null` and the
  Claude lens panel (`review-lens-*` agent types, `/review-local --base main`) on each
  branch after the test commit and after the code commit; verdicts under
  `.review/<sha>/verdict.json`. Neither sees the other's findings.
- The recording session (data pull request) runs from a worktree with `.env` present;
  cassettes are committed; `data/eval-kestrel.sqlite` is gitignored by the existing
  `*.sqlite` rule.

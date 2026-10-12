# Architecture decisions

Short ADR-style records. Each has a decision, the reason, and what would change it.
Status: proposed 2026-10-06, to be reviewed by a second model before M1 starts.

## D1. LLM provider: Anthropic primary, provider boundary kept thin

Decision. All LLM calls go through one small module, `digest/llm/`, with a `Provider`
protocol of two operations: `extract(messages, schema) -> validated model` and
`judge(prompt) -> enum`. Anthropic is the default implementation. An OpenAI implementation
is optional and used for one thing only: the faithfulness judge in evaluation.

Amended 2026-10-08 by D15: the second implementation is one OpenAI-compatible gateway
adapter, used for every development call, not an OpenAI SDK client for the judge alone.
The protocol is named `Backend` and has one operation, `complete`; `extract` and
`judge` are methods of the client wrapper around it.

Why. Structured outputs on the Claude API are GA with JSON schema enforcement, prompt
caching and batch pricing stack, and the extraction workload is exactly the shape those
features serve. The judge should come from a different model family than the generator
because self-preference bias is documented, and a reviewer who knows evals will look for
that. A thin boundary keeps the choice reversible and shows restraint: no multi-provider
abstraction layer beyond what the eval actually needs.

Would change it. If EverCurrent's stack turns out to be OpenAI-first and they say so, the
default flips with one config value and the judge becomes Claude.

## D2. Models by job

- Extraction (bulk, nightly): Haiku-class. Cheap, fast, good enough for schema-filling
  when the schema is flat and the rationale field comes first.
- Dedup adjudication, watch-prompt matching, low-confidence re-extraction: Sonnet-class.
- Faithfulness judge: a different family (GPT) when a key is present, else Opus-class with
  the known-bias caveat printed in the eval report.
Exact model ids and prices are read from the Anthropic docs at implementation time, not
from memory, and recorded in `config/models.yaml`.

## D3. No agent framework

Decision. Plain Python. Pydantic models, the Anthropic SDK, typer, SQLite. No LangChain,
LangGraph, LlamaIndex, or agent orchestration library.

Why. The pipeline is a deterministic DAG of idempotent stages, not an agent loop. Every
stage is a function from typed records to typed records with its run keyed by
(thread_id, last_ts, prompt_version, model). A reviewer can read the whole thing in an
hour. Frameworks would hide the parts that are the actual design (state, idempotency,
citations) behind abstractions we would then have to explain.

Would change it. An interactive agent in Slack (later milestone) may justify the Claude
Agent SDK for tool use and compaction. That is a separate component, not this pipeline.

## D4. Storage: SQLite with FTS5, no vector database in v1

Decision. One SQLite file per corpus. Tables: events, threads, items, item_sources,
item_links (supersedes, update_of, related), project_state, profiles, impressions,
feedback, stage_runs, cassettes index. FTS5 over events and items for lexical search.

Why. Two weeks of a team's Slack is a few thousand messages. Dedup is entity-key blocking
plus field diff plus LLM adjudication; embeddings are only a recall booster and are not
needed to hit the acceptance criteria. Adding a vector store would be the kind of
resume-driven complexity a strong reviewer penalizes. Embeddings can be added behind the
blocking step later (sqlite-vec or Voyage) without touching the schema.

## D5. Extraction unit and citation discipline

Decision. The unit of work is the thread delta. The model receives the new messages, the
thread root, existing items for the thread, the project glossary, and returns items with
`evidence[{message_ts, quote}]`. Code verifies each quote is a whitespace-normalized
substring of the cited message and builds the permalink from channel and ts. Items that
fail verification are dropped and counted; the count is part of the eval report.

Why. The Citations API cannot be combined with structured outputs, so grounding has to
be verified by us. Verification in code is also the only faithfulness guarantee that does
not depend on another model's judgment.

Amended 2026-10-11 (SPEC v0.4): the extraction prompt also carries the current ledger
values for the entities the thread names; verification also covers `owner`, `due` and
claim values against the cited text, with unit-aware numbers and user ids for people; the
digest renders quoted and model text as plain text and links only constructed permalinks.

Amended 2026-10-12 (SPEC v0.4.3): evidence entries and task links carry `channel_id`
beside `message_ts`, because a Slack `ts` is unique only within a channel and merged
items cite more than one channel.

## D6. Ranking: must-see lane plus ranked lane, explainable by construction

Decision. Must-see is rule-based and never ranked away: asks addressed to the user,
blockers on owned entities, schedule changes to owned gates. The ranked lane uses a
scoring function whose terms are stored per impression (prior, interest, watch, urgency,
novelty) so "why am I seeing this" is a lookup, not a generated explanation. The
role-by-phase prior is a YAML table a human can read and edit.

Why. The prompt asks for adaptation over time. Adaptation you can inspect and correct is
worth more than a learned ranker nobody can explain, and at this data volume a learned
ranker would be fiction anyway. Logging terms per impression also makes offline
evaluation of ranking changes possible from day one.

Amended 2026-10-11 (SPEC v0.4): the must-see lane also carries a change on an entity an
owned entity depends on, an open contradiction on an owned key, and a stale value on an
entity the reader's pending checklist task touches; a visibility check (public channels,
or private with membership) runs before both lanes and fails closed; the ranked score
gains `decision_exposure` and a one-hop `dependency_distance`; learned interest is clamped.

Amended 2026-10-12 (SPEC v0.4.2): `novelty` is defined against the reader's exposure
from any source, not the age of a message, and is held at 1 for items that stay true
until someone acts (absences, open blockers, unanswered asks, stale beliefs, open
contradictions, pending claims). A stretch `window_recency` term orders items inside a
window longer than 36 hours and never applies to those items. Ranked-lane ties break on
the newest evidence, then on item id. Plain age decay is rejected: it pushes down the
items that matter more as they age.

Amended 2026-10-12 (SPEC v0.4.3): every score term is 1 when its input is missing and
stays in a band above zero. "Off" in the ablation means the term is 1; a term stays when
it moves person-days or must-see recall by a configured minimum, and before person-days
exists nDCG at 5 may also keep it (D21). Items merge only within one audience, and each
reader reads a ledger view built by the same rules from the claims they can see. Open
blockers, unanswered asks, absences, stale beliefs, contradictions, pending claims and
undelivered closing lines stay candidates when their evidence is older than the window.

## D7. Slack integration: Bolt sync, Socket Mode, internal app, scheduler in-process

Decision. `slack-bolt` sync app with `SocketModeHandler`, `RateLimitErrorRetryHandler`
and `ServerErrorRetryHandler` installed, a per-method token bucket, resumable backfill
with a per-channel watermark, event dedup on event_id, a nightly 26-hour reconciliation
read, and delivery idempotent on (user, digest_date). The app is installed to one
workspace with distribution off. Scheduling runs in-process with APScheduler, sending at
each user's local time from `users.info`.

Why. No public URL during development, Tier 3 history limits for an internal app, and
the simplest thing that is honest about failure modes. Detailed facts in
`notes/research/slack-platform.md`.

Amended 2026-10-11 (SPEC v0.4): exposure is recorded when the digest is delivered, on
Slack's acceptance for Slack and on render for the other surfaces and replay; each sent
part carries the run id in message metadata so retries post only missing parts; revoked or
invalid tokens stop retries and mark the install inactive; auth failures, and a run with
scheduled recipients that delivered nothing, raise an admin alert.

## D8. Web preview: server-rendered, no frontend build

Decision. FastAPI plus Jinja2 plus htmx. Pages: digest for a user and date, item detail
with rationale and sources, profile editor (role, watch prompts, interest weights as an
editable natural-language profile), eval report. Feedback buttons post to the same
endpoints the Slack buttons use.

Why. The preview exists for screenshots and for exercising the feedback loop without
Slack. A React build adds a toolchain, a lockfile and a CI job for no reviewer-visible
gain. The editable profile page is a product idea (an inspectable reader model), not a
settings form, so it earns the page.

## D9. Playback and cassettes are the test substrate

Decision. All tests and CI run with `DIGEST_MODE=replay`. The synthetic corpus is
generated once by a scripted timeline (not free-form LLM generation) so entities, gates
and scenarios are deterministic and labelled at generation time. LLM calls are recorded
keyed by a hash of (model, system, messages, schema, temperature) and replayed from
`data/cassettes`.

Why. Deterministic tests are the difference between a prototype and a project. The
scripted timeline doubles as the golden labels, which removes a day of hand-labelling.

## D10. Evaluation reports, not scores

Decision. `digest eval` writes `reports/eval-<date>.md` with: extraction precision and
recall by type, citation validity (gate at 100%), faithfulness (binary judge, with judge
agreement on a hand-checked sample), must-see recall at k and nDCG per persona, role
separation, phase sensitivity, duplicate rate, and the two mirror counts (missed golden
items, hallucinated items). The README links the latest report.

Why. A single accuracy number invites the question "compared to what". A report with
named failure modes and mirror metrics shows judgment and makes the honest story easy.

## D11. Repo hygiene as signal

Decision. One CI workflow (ruff, format, ty, import-linter, pytest, hook tests).
Devcontainer.
`just` recipes mirrored by plain `uv run` commands in the README. Conventional commits.
Test and implementation in separate commits per the walled TDD process. No coverage
badge, no release automation.

Why. A tidy, runnable repo is the first thing a technical founder sees. Beyond that,
every extra mechanism is cost without signal for a one-week take-home.

## D12. The corpus renderer is a different model family from the extractor

Decision. The world-first simulator renders Slack text with a GPT-class model, while
Claude does extraction and linking. Amended 2026-10-08 by D15: the renderer calls the
model through the gateway backend with the logical name `render`; the `openai` package
is not used and the `corpus` extra leaves `pyproject.toml` in the M1 stubs commit. One 40-message
slice is human-authored. The eval report states which models rendered and which
extracted.

Why. If the same family writes the chat and reads it back, extraction scores measure
the generator's legibility, not the system. A researcher reviewer will look for this.

## D13. Testing the two external boundaries without network

Decision. The Anthropic boundary is tested through the SDK-level cassette (D9), never
through HTTP mocks: anthropic 1.x runs on httpx2 and respx or vcrpy patches of httpx
fail silently. The Slack boundary is tested by injecting fake retry handlers into
slack_sdk's `retry_handlers` and with the bolt-python mock web API server pattern.
`httpx2.alias_httpx()` is loaded as an early pytest plugin so any stray httpx import
resolves to the same package.

## D14. Naming

Decision. The Slack app, package and repo do not carry EverCurrent's brand. Working
name for the bot: "Downstream" (what changed, what it impacts). The program in the
synthetic world is "Kestrel K2", a warehouse AMR; personas, channels and conflicts are
chosen to differ from the public submissions catalogued in
`notes/research/competing-submissions.md`.

## D15. LLM backends: Anthropic API in production, one gateway for development

Decision. The provider protocol (D1; `Backend` in code) has two implementations behind
one config value:
- `anthropic_api`: the production path, schema-enforced structured outputs, needs an
  Anthropic key.
- `openrouter`: one OpenAI-compatible chat-completions adapter with `response_format`
  of type `json_schema`, used for every development call: Claude for extraction and
  adjudication, GPT-class for the corpus renderer and the different-family judge, and
  the Haiku-class triage classifier. OpenRouter's public catalogue lists the Claude
  and GPT models with `structured_outputs` support (checked 2026-10-08); the first
  call is a five-line spike at the start of M2.
Requests name a logical model (`extract`, `adjudicate`, `render`, `judge`,
`classify`) and `config/models.yaml` maps each logical name to a model id per backend.
The cassette key uses the logical name, so a cassette recorded through the gateway
replays for the API backend. The client wrapper's `extract` and `judge` validate the
response with pydantic and retry once, whichever backend answered; `complete` returns
the parsed JSON unvalidated. The README names which
backend recorded each cassette set.

Why. One adapter replaces the three that v0.2 planned (two CLI shells and a second
SDK): the renderer, the judge and the classifier all go through the same request
shape, and the user already pays for the gateway. Reviewers still need no keys at
all. The API path is the product.

Would change it. OpenRouter not honouring a JSON schema for a Claude model in the
spike: the extraction cassettes are recorded through the Anthropic API and the gateway
stays for the renderer and the judge. If structured-output enforcement matters for
extraction quality, the extraction cassettes are re-recorded through the API before
submission and the report says so.

## D16. Type checker: ty, with warnings as errors

Decision. `ty` (Astral) replaces mypy as the type gate: `uv run ty check` over `src`
and `tests`, `error-on-warning = true`, version pinned in the dev group. No pydantic
plugin is needed. One check from `mypy --strict` has no ty equivalent: ty's 148 rules
(0.0.84, `ty explain rule`) include none that requires a definition to be annotated.
ruff's `ANN` rules (flake8-annotations) take that over, with `ANN401` ignored so
`Any` stays allowed where it is explicit. Bare generics are covered by ty's
`missing-type-argument`.

Why. Measured on 2026-10-06 against a scratch module with the patterns M1 is built
from (frozen pydantic models, `Literal` fields, tuple defaults, properties, a
`model_validator`) plus three planted errors (wrong `Literal`, unguarded `None`, wrong
return type): ty 0.0.84 and pyrefly 1.3.2 each caught all three with no false positive
in under 0.5 s; mypy caught the same three in 1.1 s. Strict-only checks were not part
of that measurement; the annotation requirement is restored through ruff as stated
above. ty was chosen over pyrefly for one vendor across uv, ruff and ty. It is
pre-1.0, hence the pin.

Would change it. A ty release that misses a planted error or flags pydantic models;
pyrefly is the fallback. The swap touches the dependency line, the two `[tool.ty.*]`
tables, the `uv run ty check` call sites in the pre-push hook, CI and the justfile,
the gate-command list in AGENTS.md and CLAUDE.md, SPEC A8, and D11.

## D17. Classifier models behind the same client: Jev, Laya, and the LLM baseline

Decision. The LLM client's `judge` operation (enum in, enum out) is the classifier
port. Core: the Haiku-class model through the gateway handles triage before
extraction (M2) and watch-prompt matching (M3). Stretch, behind the same port:
`jev` (TypeSafe's native System One API, `state` plus typed `questions`, needs a
TypeSafe key; OpenRouter's "jev-router" is a model router that returns generated
text, not a classifier, checked 2026-10-08) and `laya_local` (open-weight
ModernBERT-based decision model, Apache 2.0, run in the Linux devcontainer in v1; torch
2.9.1 ships Windows ARM64 wheels, checked 2026-10-08, but that path is untested here). The M4 evaluation compares whatever adapters exist on triage recall,
faithfulness-judge agreement and latency. Extraction and the linker's adjudicator stay
on structured outputs, because they must return fields and quoted evidence that a
classifier cannot produce.

Why. Triage and matching are yes/no questions at volume; a non-generative classifier
answers them in tens of milliseconds, cannot hallucinate text, and as a judge has no
self-preference bias. The comparison is a measured finding for the README either way.
Making the LLM the core adapter keeps M2 free of a third provider.

Would change it. A TypeSafe key appearing before M2: Jev becomes the core triage
adapter and the LLM the baseline. Jev dropping golden items in triage above the miss
budget: the LLM stays.

Amended 2026-10-11 (SPEC v0.4): the triage classifier returns a probability, through the
structured-output path (`extract` with a two-field schema) for the LLM adapter and
natively for Jev; `judge` stays enum-only and serves adjudication. The keep threshold is
derived from a stated cost of a miss.

## D18. Profile bootstrap and onboarding

Decision. Onboarding is in scope for v1. The first-run profile is a draft inferred
from Slack (job title and timezone from the user record; team, collaborators and owned
subsystems from channel membership, replies and mentioned entities over 30 days),
shown with its evidence for confirmation. A user may also paste a role description,
which the extraction client turns into the same fields plus watch prompts. Every field
records its source (configured, inferred, confirmed, pasted). The web preview's
profile page is the single settings surface; the onboarding DM links to it; delivery
time defaults to the Slack timezone. Slack App Home settings and slash commands are
stretch (SPEC section 9). No org-chart, HR or LinkedIn connectors.

Amended 2026-10-11 (SPEC v0.4): an HRIS CSV may fill profile fields through
`digest import people` (M5 stretch), recorded with source `imported`, replacing only `inferred` or `imported` fields. The profile page stays the single settings surface; connectors stay in
later.md.

Why. The data the bootstrap needs is already ingested; a confirmed draft beats a blank
form; a source per field keeps inferred values from being mistaken for stated ones.

Would change it. A second real workspace with an HR directory: add the connector
listed in `docs/later.md`.

## D19. The ledger is bi-temporal

Decision. Each claim carries `asserted_ts` (when it was said), `observed_ts` (when the
pipeline saw it) and `invalid_ts` (when a later claim superseded it, null while
current). Supersession sets `invalid_ts`; nothing is deleted.

Why. This is the temporal-knowledge-graph model (Zep's Graphiti) applied in SQLite:
stale-belief lines can say when a value stopped being true, supersession recall can be
measured on time rather than only on links, and a tracker connector later writes the
same rows.

Amended 2026-10-11 (SPEC v0.4): claims carry `certainty`, `kind` (stated, target or measured) and `conditions`; a hedged
or question claim never supersedes, a target never supersedes a stated or measured value
(the two coexist), and measurements under different conditions never contradict; a lower-authority claim that disagrees with a recorded value is `pending`, and the record
resolves it: confirmation supersedes, restatement makes a contradiction routed to the key's
owner, and silence past a cadence makes an absence.

Amended 2026-10-12 (SPEC v0.4.3): a `spec_param` key also holds the parameter name, taken
from the words before the value's first numeric token. Without it, two parameters on one
part share a key and supersede each other. The world file already writes values in that
form, so it needs no change; the ledger code derives the name.

## D20. Interfaces: one function layer, three thin adapters, MCP for assistants

Decision. Queries and feedback over the store and the ranker are plain functions in
one module. The CLI, the web preview's HTTP endpoints and an MCP server (stdio) call
those functions and contain no logic of their own. MCP is the adapter for the user's
own assistant because Claude, ChatGPT and Cursor speak it without a per-host tool
definition. One read-only `query` tool over SQL, with a row cap and a timeout and the
schema published as a resource, is the escape hatch for questions the fixed tools do
not cover. The tool list holds at most ten entries. The pipeline's model calls use direct
structured-output requests and never MCP.
Enforced by import-linter (`uv run lint-imports` in CI): a layers contract (cli; replay;
store, llm and world as siblings; project; models and settings) and a forbidden
contract keeping `anthropic` and `httpx2` inside `digest.llm`.

Why. A REST API alone needs a tool definition per assistant host; per-host function
definitions drift; letting the assistant run SQL alone depends on it knowing the
schema. One function layer keeps the three adapters from diverging. The cap of ten
tools (line above) is a design choice to keep the list readable, not a measured limit. Keeping the pipeline's
own calls direct preserves determinism, batching and cassette replay. This is also the
concrete answer to the take-home's question on function calling versus MCP.

Would change it. A hosted deployment: the MCP server gains an HTTP transport with
per-user tokens (docs/later.md).

Amended 2026-10-11: the server targets protocol revision 2026-07-28 (stateless requests,
no handshake, stdio still a standard transport) through the Python SDK 2.x. The design
uses none of the features that revision deprecates (roots, sampling, logging) and no
server-initiated request, so no migration path is needed.

Amended 2026-10-12 (SPEC v0.4.3): the four write actions are one `feedback` tool with an
action field, so nine read tools and one write tool fit the cap of ten. The v1 server is
an administrative interface for the operator of the store: it applies no reader
visibility check, and `query` reads every row. A reader-scoped server waits for a
hosted deployment (docs/later.md).

## D21. The objective is stale-belief person-days

Decision. The digest minimises the working days readers hold a value the ledger has
superseded, summed over readers and facts, under a per-digest length budget. A ranking term earns its place by moving that sum, or must-see recall for the facts the
sum does not count, in the ablation; the evaluation reports the sum directly (digest on versus off; the
measure is M4 stretch, and until it is built the ablation reads must-see recall and nDCG), with time-to-told per role and the novelty rate
beside it. The formulas read only
world fields (`supersedes`, `must_see_for`, `known_by`, event days) and the store.

Why. One scalar that ranking and evaluation share stops the two from drifting apart,
and it is the quantity the thesis names. It needs per-reader exposure and a bi-temporal
ledger, which the public submissions to the same brief do not have, so it is also the
line a reviewer has not read fifteen times.

Would change it. A measured case where person-days and reader judgement disagree on
what mattered; then the weight by decision exposure becomes the headline instead.

## D22. A light entity graph: edges, decision exposure, routed contradictions

Decision. Entities carry `depends_on` edges; a change on an entity an owned entity
depends on reaches the owner through the must-see lane with the edge in the reason.
A stale value on an entity the reader's pending checklist task touches is must-see
(SPEC section 5 step 6). Decision exposure is the ranked-lane term read from the same
checklist data (a task's `entities`) that orders everything below the must-see lane. An open contradiction is routed to the owner of the key as a `needs_you`
line with both claims and their authority; code never picks a winner. No graph
database: edges are a list on the entity, read by the ranker and the golden-label
derivation.

Why. Four public submissions model cross-team reach; the one that measured it derived its
gold from the same fan-out rules, so its 12 of 12 is construction, not evidence, but edges are what reach the owners of dependent entities who were never in the thread. The
checklist already states what each reader is about to do, so decision exposure costs
one lookup. Contradictions routed to a person stay honest states in the ledger.

Would change it. More than two edge kinds, or queries that walk more than one hop:
then the `relations` table in later.md.

## D23. Evaluation gates

Decision. Golden labels and the ranking configuration are fingerprinted; `digest eval
--gate` refuses a mismatch, while a plain `digest eval` runs and reports it, so ablations
and deliberate ranking changes still run. A deliberate change re-freezes the baseline in
its own commit with the before and after in the report. Every ranking rate is reported beside two baselines, role-only
and send-everything, even when a baseline wins. Quiet world days give an abstention
precision. `digest eval --gate` (M4 stretch) compares the report to a frozen baseline and CI fails on
a drop beyond a stated tolerance per metric; citation validity stays a hard 100 percent.
Prose ablations (renderer, prompt version) use a pairwise judge with position swap.

Why. A single run on self-written labels is the common failure of the field. A frozen
baseline, published baselines and a gate make the report a regression instrument, and
pairwise judging is more reliable than a 1 to 10 score for text.

Would change it. A metric that gates noisily under cassette replay: raise its tolerance
or move it to report-only, and say so in the report.

## D24. Documents and actions are claims too

Decision. A revision token in a file name or a pinned-version URL is a `referenced` claim
on the document entity, read by a deterministic pass before the model runs; posting, replying to or reacting on the message is `linked` exposure, so a reader who
handled the Rev B pack holds Rev B. A reference older than the ledger's value as of the message's time is a stale-reference
item (suppressed when the thread also cites the current value); into a channel shared
with a contract manufacturer it is must-see for the document's owner. A claim may carry a
`scope` (a build id or expiry), so a deviation for one build neither supersedes nor
contradicts the record. An action claim (PO placed, tooling PO placed, release sent, build started) is audited against the ledger and the actor's exposure as of its time; acting
on a superseded value is a blocker for the actor and the key's owner.

Why. PLM tools flag a pinned document link that is out of date inside their own walls;
none of the chat digest tools or submissions reviewed does it in chat, where the CM
receives the file. The audit needs a record of what each reader was told, which none of the memory systems
reviewed (Graphiti and Zep, GraphRAG, Mem0, LlamaIndex graphs) keeps; Graphiti's validity
intervals are the nearest design and D19 follows them. Both reuse D19 and the
exposure record and add no infrastructure. Fuzzy matching stays out: a revision suffix
is split by a normalizer, not matched (PN-31010-C versus PN-31010).

Would change it. A corpus where PO and release messages never name a revision: the audit
then fires only on attachments, and the stretch items wait for a PLM connector.

## D25. Tickets are labels and claims

Decision. An issue tracker is one more system of record: a bot persona whose `status`,
`owner` and `priority` claims land on `issue` entities. A `ticket` reaction by a human on
a message creates an issue item owned by the reactor, cites the message, and records a
human judgement that the thread carries project information. The triage evaluation
reports recall against ticketed threads beside recall against golden items. Live mode
gets a mock tracker (create, assign, status) behind the function layer until a connector
exists; the connector stays in later.md.

Why. The public submissions reviewed evaluate on labels their authors wrote. Tickets
people open from chat are the one label a real workspace produces without the digest's
own buttons, and the reaction is the smallest possible labelling interface. The tracker as a bot persona
reuses the authority model; nothing new enters the ledger.

Would change it. A workspace that tickets nothing from chat: the metric reports n = 0
and the golden labels carry the eval alone.

## Validation against EverCurrent's published experience

Each decision above is checked against what EverCurrent has written publicly, so the
README can argue from their experience rather than ours.
- Multi-source validation ("determine what is actually happening"): D5 evidence
  verification and the ledger's `contradicts` link treat channels as sources.
- "Where decisions fail to land": the landing map (SPEC section 4) with the strict rule
  that only contradicting evidence marks a cell.
- Schedule Safeguard's buffer rule ("flag if required reviews are still open inside the
  7-day buffer"): the absence detector reads the same gate checklist shape and adds the
  chat-side evidence their card does not show.
- "Human status routers" and "two weeks too late": stale-belief lines target the exact
  failure (parts ordered to the old rev) their decision-traceability post describes.
- Zero Trust posture: read-only scopes, extraction model runs with no tools, links are
  constructed never generated, minimal retention.

## Open questions for the second-model review

1. Is Haiku-class extraction accurate enough on hardware vocabulary, or should the
   first pass be Sonnet-class with batch pricing? Decide after the first 20 golden threads.
2. Should the web preview be dropped if M3 slips? Default: yes, screenshots can come
   from the terminal renderer and Slack.
3. Should watch prompts be matched by embeddings or by a cheap LLM yes/no over the top
   30 candidates? Default: LLM yes/no, cached, since there are few candidates per day.

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

## D18. Profile bootstrap and onboarding

Decision. Onboarding is in scope for v1. The first-run profile is a draft inferred
from Slack (job title and timezone from the user record; team, collaborators and owned
subsystems from channel membership, replies and mentioned entities over 30 days),
shown with its evidence for confirmation. A user may also paste a role description,
which the extraction client turns into the same fields plus watch prompts. Every field
records its source (configured, inferred, confirmed, pasted). The web preview's
profile page is the single settings surface; the onboarding DM links to it; delivery
time defaults to the Slack timezone. Slack App Home settings and slash commands are
stretch (SPEC section 9). No org-chart, HR or LinkedIn sources.

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

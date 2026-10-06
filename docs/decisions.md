# Architecture decisions

Short ADR-style records. Each has a decision, the reason, and what would change it.
Status: proposed 2026-10-06, to be reviewed by a second model before M1 starts.

## D1. LLM provider: Anthropic primary, provider boundary kept thin

Decision. All LLM calls go through one small module, `digest/llm/`, with a `Provider`
protocol of two operations: `extract(messages, schema) -> validated model` and
`judge(prompt) -> enum`. Anthropic is the default implementation. An OpenAI implementation
is optional and used for one thing only: the faithfulness judge in evaluation.

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
keyed by a hash of (model, system, messages, schema) and replayed from `data/cassettes`.

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

Decision. One CI workflow (ruff, format, ty, pytest, hook tests). Devcontainer.
`just` recipes mirrored by plain `uv run` commands in the README. Conventional commits.
Test and implementation in separate commits per the walled TDD process. No coverage
badge, no release automation.

Why. A tidy, runnable repo is the first thing a technical founder sees. Beyond that,
every extra mechanism is cost without signal for a one-week take-home.

## D12. The corpus renderer is a different model family from the extractor

Decision. The world-first simulator renders Slack text with GPT through the OpenAI API
(optional extra `corpus`), while Claude does extraction and linking. One 40-message
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

## D15. LLM backends: API in production, subscription CLIs for recording

Decision. The `Provider` protocol (D1) has three implementations behind one config value:
- `anthropic_api`: the production path, schema-enforced structured outputs, needs a key.
- `claude_cli`: shells out to `claude -p --output-format json` with the same system
  prompt and schema in the prompt; validates with pydantic and retries once. Uses the
  Claude subscription. Used to record cassettes during development.
- `codex_cli`: shells out to `codex exec` with the mechanical or review profile. Uses
  the ChatGPT subscription. Used for the corpus renderer and the different-family judge.
Cassettes are keyed by request content, not backend, so replay is backend-agnostic.
The README names which backend recorded each cassette set.

Why. Development should cost nothing beyond subscriptions already paid for, and
reviewers must need no keys at all. The CLI backends are development tooling; the
API path is the product. The one rule: use the CLI binaries as-is, never reuse their
OAuth tokens inside an SDK.

Would change it. If structured-output enforcement matters for extraction quality, the
cassettes for extraction are re-recorded through the API before submission and the
report says so.

## D16. Type checker: ty, with warnings as errors

Decision. `ty` (Astral) replaces mypy as the type gate: `uv run ty check` over `src`
and `tests`, `error-on-warning = true`, version pinned in the dev group. No pydantic
plugin is needed.

Why. Measured on 2026-10-06 against a scratch module with the patterns M1 is built
from (frozen pydantic models, `Literal` fields, tuple defaults, properties, a
`model_validator`) plus three planted errors (wrong `Literal`, unguarded `None`, wrong
return type): ty 0.0.84 and pyrefly 1.3.2 each caught all three with no false positive
in under 0.5 s; mypy caught the same three in 1.1 s. ty was chosen over pyrefly for
one vendor across uv, ruff and ty. It is pre-1.0, hence the pin.

Would change it. A ty release that misses a planted error or flags pydantic models;
pyrefly is the fallback and the swap is the dependency line, the `[tool.ty]` table and
the three `uv run ty check` call sites.

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

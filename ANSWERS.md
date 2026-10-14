# Technical questions

Draft v0.2, 2026-10-12. Each answer is stated in general terms first, then shown as it
appears in this repo, so the code is the evidence for the claim. Each "In this repo"
paragraph says what is built and what is designed.

## LLM behaviour: how do you handle LLMs losing context in long-running or multi-step workflows?

Treat context as a cache, not as state. The state lives outside the model in typed
records, and every model call is stateless and purpose-built.

- Externalize state. The item store and the per-user profile are the memory, and a
  claim ledger is designed to join them. A model never has to remember what it extracted yesterday; it is handed the
  existing items for the thread it is looking at.
- Keep each call small and scoped. The extraction unit is a thread delta: new messages,
  the thread root, the items already known for that thread, and a glossary. Nothing
  else. Long threads are processed incrementally, not re-read whole.
- Make stages idempotent and resumable. Each stage run is keyed by
  (thread_id, last_ts, prompt_version, model) and recorded. A crash re-runs only the
  unfinished runs, and nothing is lost because nothing lived only in a conversation. A
  new prompt version starts from a fresh store, so results from two prompt versions
  never mix.
- When a conversation is unavoidable (an interactive agent), compact with explicit
  retention rules: preserve item ids, part number and revision, decisions, open
  questions and permalinks; drop tool output once it has been turned into a record.
  Use sub-agents with clean contexts that return condensed, structured summaries rather
  than one long agent accumulating everything.
- Verify against the source of truth, not against memory. Every digest line cites a
  message, and code checks its quote against that message's text; the model's
  recollection is never trusted on its own.

In this repo (built): `src/digest/extract/stage.py` runs extraction per thread delta and
passes the thread's existing items, the glossary and the checklist tasks of gates on or
after the day; the `stage_runs` table in `src/digest/schema.sql` is the checkpoint; there
is no long-lived conversation anywhere. Designed, not built: the current ledger values in the extraction
prompt (D5).

## Retrieval: how do you address cases where vector similarity returns many nearly identical results with small but critical differences?

Do not let embeddings make the decision. They are a recall tool; precision comes from
structure.

- Extract structure before you compare. "PN-31010 Rev B" and "PN-31010 Rev C" embed
  almost identically, and published results show embedding models largely fail to
  separate texts that differ only in a number. So first normalize
  identifiers (part numbers, revisions, ECO ids, PO numbers, dates, quantities) with
  deterministic parsers.
- Block, then diff. Candidate pairs come from shared entity keys, lexical hits on
  identifiers, and only then embedding similarity. Each candidate pair is diffed on a
  declared set of discriminating fields per item type (rev, qty, date, supplier,
  status, owner).
- Encode the rule that a small difference is a different fact. Same key and same
  discriminators merge. Same key and a different discriminator never merge: it becomes
  a `supersedes` link (later, authoritative) or a `contradicts` link (concurrent,
  different sources). That is exactly the case that matters in hardware: the old
  revision and the new one must stay distinct, with the relationship recorded.
- Use an LLM only for the residual ambiguity, and force it to name the differing field
  in its verdict so the decision is auditable.
- At presentation time, apply diversity (MMR) but exempt items that differ in a
  discriminating field, so the digest never hides "Rev C" behind "Rev B".

In this repo (built): `src/digest/extract/normalize.py` maps glossary aliases and splits a
single-letter revision suffix, and `src/digest/extract/verify.py` normalizes revision and
quantity forms and drops a claim whose entity does not resolve. Designed, not built: the
linker (blocking, field diff and the adjudicator), ECO and PO id parsers, and checks of
values against the cited text (SPEC section 5). The ledger and the linker are cut from the
demo under the SPEC day-3 rule, as a conservative scope decision: the eval's claim
precision is 0.537 (n = 41), but it uses stricter matching on golden threads only, so it
does not establish whether keying precision meets 0.8. No vector database is used.

## Evaluation: how do you evaluate LLM-generated content and summaries?

Separate the questions "is it true", "is it complete", and "is it the right thing for
this reader", and measure each with the cheapest method that is actually reliable.

- Truth (faithfulness). Enforce grounding structurally: every digest line carries an
  evidence quote that code verifies against the cited message, and ungrounded lines are
  dropped and counted. On top of that, a claim-level binary judge ("is this sentence
  entailed by its sources, yes or no") from a different model family than the
  generator, to avoid self-preference bias. The judge is calibrated on a hand-checked
  sample and its agreement rate is reported next to its scores.
- Completeness (coverage). Golden items act as key facts; coverage is the share of
  golden items the digest surfaced. Both mirror metrics are reported: missed golden
  items and hallucinated items.
- Relevance per reader. Must-see recall at k and nDCG per persona per day against
  golden must-see sets; role separation (different roles should get different
  digests); phase sensitivity (the same role before and after a phase change should
  differ, for stated reasons).
- Ground truth before text. The synthetic corpus is generated from a scripted world
  with per-persona knowledge over time, so labels are derived rather than hand-written,
  and the renderer is a different model family than the extractor so the eval does not
  measure the generator's own legibility. Noise knobs produce a degradation curve, not
  a single number.
- Binary checks over Likert scales; capability suite separate from the regression
  suite; a human-authored slice as an anchor; online signals (thumbs, mutes,
  time-to-action) logged from day one with impressions so ranking changes can be
  evaluated offline later.

In this repo (built): `digest eval` writes `reports/eval-<date>.md` with precision and
recall per item type, claim precision and recall, citation validity, triage recall and
the absence false-alarm rate, each with its n, and a list of metrics not run. Citation
validity is a release gate at 100%. The current report gives citation validity 1.0
(n = 73), claim precision 0.537 (n = 41), and 7 false among 23 detected absences
(0.304). That last number is the share of detected absences that are false, not yet the
SPEC A6 rate over silences that were not stalls. Designed, not run: the faithfulness judge,
must-see recall and nDCG, the baselines and the online signals.

## System design: when would you use function calling versus MCP in production?

Function calling is for tools you own, inside a workflow you control. MCP is for tools
other people own, that many clients need to discover and call with their own
authorization.

- Use function calling (or structured outputs) when the call is part of a deterministic
  pipeline, latency and cost are yours to optimize, the tool set is small (under about
  twenty) and stable, and auth is your own. The extraction and adjudication steps here
  are this case: no discovery is needed, the schema is fixed, and the model should run
  with no tools at all so untrusted Slack text can never trigger an action.
- Use MCP when the tool surface is a product boundary: tools owned by other teams or
  vendors, reused across several clients (Claude, ChatGPT, Cursor, an internal agent),
  with per-user OAuth, discovery and cacheable tool lists. The right MCP use here is to
  expose the digest itself as a read-only server (search items, explain a ranking, set
  a watch prompt) so engineers can query it from whatever assistant they use.
- Production cautions: tool count degrades accuracy, so defer-load tool definitions or
  front a large API with a code-execution tool; treat any MCP server that mixes private
  data, untrusted content and an outbound channel as an exfiltration risk and scope it
  read-only by default; pin spec versions and validate tool results like any other
  untrusted input.

In this repo (built): pipeline calls go through one wrapper, `src/digest/llm/client.py`,
with schema-enforced output and no tools, and an import contract keeps the SDK inside
`digest.llm`. Also built: a stdio MCP server with nine read tools over the store
(`digest mcp`, `docs/mcp.md`). Designed, not built: its tenth tool, `feedback` (D20).

## System design: how would you handle Slack API failures?

Classify the failure, then respond in the way that failure class deserves, and design
so that every side effect is safe to repeat.

- Classify. Transient (network, 5xx, 429): retry with backoff. Permission or
  configuration (`missing_scope`, `not_in_channel`, `invalid_auth`, `token_revoked`):
  fix automatically where possible (join the channel), otherwise fail loudly naming the
  scope; never retry blindly. Bad payload (`invalid_blocks`, `msg_too_long`): degrade
  to a simpler message rather than send nothing.
- Respect rate limits on purpose. Honour `Retry-After` on 429; pace with a per-method
  token bucket because limits are per method, per workspace, per app; know the special
  cases (about one message per second per channel for posting; Tier 3 for history on
  an internal app, but 1 request per minute with 15 messages if the app is ever
  treated as a distributed non-Marketplace app, so detect that throttle and fall back
  to a slow resumable crawl).
- Make side effects idempotent. Posting has no idempotency key, so record the
  (user, digest date) to message ts mapping before and after each send and check it
  first; dedupe inbound events on event id, since retries and multiple Socket Mode
  connections deliver duplicates; make feedback writes upserts.
- Acknowledge fast, work later. Events and button clicks must be acknowledged within
  three seconds or Slack retries; slow work (LLM calls, extraction) runs after the ack.
  Write the raw event to the store before the ack: by default Bolt acknowledges before
  the listener runs, so a crash after the ack loses the event.
- Assume live events will be missed. Socket Mode reconnects on its own; a
  reconciliation read before each digest build fills gaps, and it also walks thread
  replies, because channel history does not show a new reply to an old thread; backfill resumes from a stored
  per-channel watermark so a crash never restarts from zero.
- Isolate and observe. One user's or channel's failure does not stop the batch; a
  degraded digest beats none; structured logs per call (method, error code, retries,
  latency), counters for 429s and errors, alerts on auth errors and on a run that
  delivered nothing. Keep stored Slack data minimal and short-lived, and never train on
  it, per the API terms.

In this repo (built): ingest is an upsert on (channel_id, ts) in `src/digest/store.py`,
so a repeated event is harmless, and stage runs resume after a crash. `digest post`
sends through the Slack SDK with its retry handlers: a connection error is retried at
most once, and for bot DMs a 429 is retried at most once after Slack's `Retry-After`
time. Any failure stops the run; for bot DMs, `invalid_auth` or `not_authed` also says
whether the token came from the environment or `.env`. A message that would break Slack's
limits is cut, not dropped (a section at 3000 characters, at most 50 blocks with a count
of the lines left out). `digest seed` keeps a map of what it posted, so a rerun resumes,
and it retries a 429 up to five times after `Retry-After`. Designed, not built: the
delivery ledger, so that a rerun sends only the missing digests; live ingest with each
event stored before the ack; and the tests for both (D7, D13).

# Later: deferred scope

Everything here is out of v1 (SPEC.md section 3). Each entry names the trigger that
would promote it.

- Asks back. One button-answerable question per person per day when a gate-relevant
  belief has low confidence and exactly one plausible answerer exists. Trigger: ledger
  precision holds through M3 and days 6 to 7 are free.
- Interactive agent in Slack. Thread equals session keyed on (channel, thread_ts); emoji
  acknowledgement under 3 seconds; streaming replies; Stop and mute; mention required in
  channels; other people's messages treated as information, not instructions. Trigger:
  the take-home is submitted.
- Agentic feature set beyond the v1 MCP server (design sketch, so v1 data structures
  do not block it). The v1 MCP server already lets a user's own assistant read the
  store and act through feedback events; that assistant supplies the agent loop, the
  ask-user step and scheduling. What remains for a product-owned agent, built on the
  Claude Agent SDK as a separate component from the pipeline (D3):
  - `update_profile_field` as an action of the MCP `feedback` tool, so the tool list
    stays at ten, with `source = agent_proposed` until the user confirms.
  - A product-owned ask-user step in Slack: one button-answerable question in the
    user's DM, one open question per user, used for "asks back" and low-confidence
    profile edits. Never free-text prompts.
  - An always-on event triage loop that scores each new thread delta as it arrives
    and decides digest, real-time or ignore; the same triage classifier as v1, run per
    event instead of per day.
  - Calendar integration (Google Calendar, M365): three uses. A gate review or DFM
    review that is on no calendar inside the buffer is an absence signal of the same
    kind as a missing ECO post. Meeting titles and attendees are evidence of task
    progress ("thermal review held Tuesday"). Delivery timing avoids the user's
    meetings and respects out-of-office. Calendar events enter through the event
    envelope with their own source and `plm_bot`-level authority for dates. Meeting
    notes or transcripts are a later source and need the same evidence discipline as
    chat.
  Trigger: the interactive agent exists and a real workspace has used the digest for a
  month, so the ask-user rate and the triage thresholds can be set from data.
- Full entity model beyond the v1 minimum. v1 has entity types with `subsystem`,
  `parent` and `aliases`, personas with `team` and `manager`, claims and `depends_on` as the
  only edges (v0.4 added `depends_on`). The full model adds: a `relations` table with
  typed edges (`part_of`, `owned_by`, `member_of`, `reports_to`, `governs`) each with a
  validity window; `team` and `person` as entity types so ownership can be held by a team;
  documents with a content reference (PLM or Drive id, version) rather than a title;
  `deliverable` as a grouping of tasks, builds and tests that a gate consumes; a
  glossary table with alias provenance. Each is additive to the v1 schema. Trigger:
  a ranking or landing-map need that the v1 fields cannot express, or a connector that
  supplies the edges.
- Org-chart and HR connectors to fill team and manager. First: Google Workspace
  Directory API, where each user record carries a `relations` entry of type manager and
  dynamic group queries list a manager's reports; Microsoft Graph has the same shape.
  Vendor PLM APIs (Arena, Duro, Onshape) stay deferred until a customer names the tool;
  until then PLM enters as feed posts (SPEC section 4). The Slack-inferred profile draft
  covers the demo; `digest import people` (SPEC section 9, M5 stretch) is the fallback.
  Trigger: a second real workspace on Google Workspace or Microsoft 365.
- Point-in-time history of edited messages (row versioning in the store). The snapshot
  rule (a digest for day D runs on a store replayed to day D) covers the demo and the
  eval. Trigger: a live deployment that must answer "what did the digest say last
  Tuesday" after edits.
- Formal model of the ledger in Quint with the three invariants (no superseded value
  is current; every stale-belief line has a superseding permalink; no absence without a
  citation), model-checked and linked from the README. The stateful Hypothesis test in
  M3 covers the same invariants empirically. Trigger: days 6 to 7 are free.
- Linear connector. Webhooks into the event store (issue state, assignee, priority, due
  date, cycle changes; project update health; SLA breaches), read-only MCP for ad hoc
  questions. Trigger: the event envelope is stable and a Linear workspace exists.
- Subscription CLI backends (`claude -p`, `codex exec`) for recording cassettes at
  zero marginal cost. Replaced in v0.3 by the single gateway backend (D15). Trigger:
  gateway spend becomes material, or the gateway stops honouring JSON schemas.
- MCP server over HTTP with per-user tokens for hosted assistants. Each tool applies the
  reader's visibility check, and `query` reads per-reader views (an SQLite authorizer
  denies the base tables). The v1 server is stdio for local assistants and is an
  administrative interface with no reader check. Trigger: a hosted deployment, or a
  second person using one store.
- Learned ranking. Impressions and feedback are logged from day one; a learning-to-rank
  step or Thompson-sampled exploration slots come after thousands of labelled
  impressions exist.
- Phase-transition detection from chat (change-point on item-type mix and vocabulary),
  proposing a phase change for a human to confirm. Trigger: real multi-phase history.
- Time-travel onboarding digest for a new hire built from the ledger with superseded
  chains. Trigger: ledger shipped.
- Embedding-based recall in the linker (voyage-4 or sqlite-vec) behind the existing
  blocking step. Trigger: lexical blocking misses measured on the eval.
- Temporal-style durable execution for the stage DAG. Trigger: production deployment.

## Deferred from the v0.4 review of public submissions (2026-10-11)

- Reaction kind (alarm versus acknowledgement emoji) as an urgency signal; only if the
  ablation shows engagement moves the numbers.
- Implicit thread continuation for top-level follow-ups that point at a thread.
- Phase-change proposal from checklist state, confirmed by a human (a non-chat variant
  of the change-point entry above).
- An "acknowledged" button feeding exposure and the landing map.
- A shared section of 0 to 3 items every reader gets (phase change, major decision);
  take it if the landing map shows decisions not landing.
- A holdout world written by a fresh agent from a brief, scored on aggregates only.
- A repair prompt that re-asks with the validator's reason after a verification failure.
- A per-stage trace page for one item in the web preview.
- `cost` as an attribute in the claim vocabulary; entity criticality beyond `priority`;
  a per-reader attention budget.
- The author's digest showing where their own decision did not land (landing map read
  from the author's side).
- Transient backoff for 429 and 5xx on the gateway during record runs.
- Anchor slice to 40 messages. SPEC section 6 asked for a 40-message human-authored
  slice; M2 ships nine. Add 31 human-written lines in two or three threads to
  `data/world/kestrel/anchor.jsonl` with their `authored: human` manifest entries, then
  re-render. Trigger: after the 2026-10-14 interview.

## Next steps from the eval and research review (2026-10-12)

In order. Each item names its trigger. Measured numbers are from
`reports/eval-2026-10-23.md`.

1. Absence false alarms. Per-scenario tests on task-link extraction, because silence is
   counted from links the extractor writes; ask absences go only to the asker and the
   owner. Trigger: SPEC A6 fails (0.304, n = 23, against 0.20).
2. Claim value against the cited text (D5). Check each claim value in the cited message
   after Slack-aware normalization (mentions, links, markup, curly quotes); on a miss,
   search the message for a supporting span and check again before the drop. A verbatim
   quote is not support for a claim (arXiv 2609.15964). Trigger: now.
3. Claim key checks in code. A table of the attributes each entity type may carry, a
   value form per attribute, and an attachment rule: the entity is named in the quote,
   the thread root or an earlier cited message. The measured key errors put a true fact
   on the wrong entity or attribute (a PO revision stored as a part revision, status
   stored as lead time); a synonym merge does not fix that. Trigger: before the ledger.
4. Claim scoring. Report each extracted claim as matched, true but unlabeled, wrong key,
   wrong value or unsupported. Label every claim in the golden threads from the message
   text, by a labeler who does not see the extractor output. The ledger gate becomes the
   key-error rate, with a recall guard and key stability across two recordings.
   Trigger: before the ledger.
5. Digest window per reader and a seen record, so a reader is not told the same thing
   twice. Trigger: every demo digest is a first digest (13 to 16 lines).
6. Stage key records the concrete model id, and a run refuses a mismatch. Today the key
   holds the logical name, so a model change in `config/models.yaml` reuses old runs.
7. Extraction payload limit: open items, plus items that share an entity with the new
   messages, with the new messages last. Trigger: threads longer than the corpus p90
   (5 messages).
8. Absence sources (f) suspect evidence and (g) ended scope (SPEC section 5 step 5) as
   candidates for core. A drawing revision change triggers a partial first article
   inspection (AS9102), and a deviation carries an end. Add a last-time-buy
   window absence: a discontinuation notice opens a fixed buy window (JEDEC JESD48).
   Trigger: item 1 passes.
9. A Sonnet-class extraction run on the same 36 thread revisions, and one re-record
   through the Anthropic API backend, each with its report difference.
10. Gate tolerances (D23) from paired differences over three recordings, clustered by
    world day.
11. Exposure weight: `shown_in_digest` counts less than authored, replied or reacted.
12. A test that every repo path named in the root Markdown files exists. Trigger: five
    such paths were found in `ANSWERS.md` on 2026-10-12.
13. More names in `data/world/avoid-names.yaml` from the 2026-10-12 search of public
    submissions (Robot Arm, Mobile Base, Alice Chen, Sam). The render prompt reads the
    list, so the change re-renders the corpus. Trigger: the next re-render.
14. Bounded delay for a real-time alert tier: hold an urgent line for a short window
    instead of an instant interruption. Trigger: a real-time tier.
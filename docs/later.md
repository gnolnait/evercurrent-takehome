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
  - `update_profile_field` as an MCP write tool, with `source = agent_proposed` until
    the user confirms.
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
  `parent` and `aliases`, personas with `team` and `manager`, and claims as the only
  edges. The full model adds: a `relations` table with typed edges (`part_of`,
  `owned_by`, `member_of`, `reports_to`, `governs`, `depends_on`) each with a validity
  window; `team` and `person` as entity types so ownership can be held by a team;
  documents with a content reference (PLM or Drive id, version) rather than a title;
  `deliverable` as a grouping of tasks, builds and tests that a gate consumes; a
  glossary table with alias provenance. Each is additive to the v1 schema. Trigger:
  a ranking or landing-map need that the v1 fields cannot express, or a connector that
  supplies the edges.
- Org-chart and HR connectors (Google Directory, Workday, BambooHR) to fill team and
  manager. The Slack-inferred profile draft approximates them. Trigger: a second real
  workspace.
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
- MCP server over HTTP with per-user tokens for hosted assistants. The v1 server is
  stdio for local assistants. Trigger: a hosted deployment.
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
- "You are the last to know": the gap between the reader's exposure and their peers'
  as a ranking term (needs no new data; stretch in M3).
- The author's digest showing where their own decision did not land (landing map read
  from the author's side).
- Transient backoff for 429 and 5xx on the gateway during record runs.

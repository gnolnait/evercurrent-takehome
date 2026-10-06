# SPEC: role- and phase-aware daily Slack digest

Status: v0.2, 2026-10-06. This is the contract the tests are written against. Change it by
editing this file first, then the tests, then the code. v0.2 adds the belief ledger,
absence detection, decision landing map and world-first simulator on top of the v0.1
baseline pipeline; scope D (live Slack, web preview) is kept in full.

## 1. Problem

A robotics hardware team (ME, EE, firmware, supply chain, engineering manager, PM) runs on
Slack. Important updates get buried in threads and every role has different priorities.
Build a daily digest that gives each person what matters to them, given their role, the
project's current phase, and how their focus has shifted recently.

## 2. Thesis

A digest is the diff between what the project knows and what you were last told, plus
what should have happened but didn't.

Four consequences:

1. Extract once, personalize many. Threads become structured items with entities,
   owners, dates, affected roles and source permalinks. Personalization is a scoring
   pass per reader over shared items.
2. Some items carry claims: keyed beliefs such as `(PN-31010, rev) = C` from a closed
   hardware vocabulary. Claims form a ledger with `supersedes` and `contradicts` links.
   A reader's state is the last value they were exposed to per key. The gap between
   the ledger and the reader's exposure is the most valuable thing a digest can say.
3. Expected-but-absent events are items too. Gate checklists define what should have
   happened; the digest reports what hasn't, citing the last evidence.
4. Role and phase are priors, behaviour is the posterior. The role-by-phase prior is a
   human-readable table; decayed interest, watch prompts and explicit feedback update it.
   A phase change re-weights which keys and item types matter.

## 3. Scope (v1, about one week)

In scope
- Ingest Slack-shaped events from a JSONL corpus (replay) or a live workspace (Socket Mode).
- Extract items and claims from thread deltas with an LLM into a fixed schema, with
  evidence quotes verified in code.
- Dedup, merge and link items; maintain the claim ledger with supersession.
- Maintain project state (phase, gates, open gate tasks) and per-user profiles with
  exposure tracking.
- Rank per user; render a digest (terminal, markdown, Slack Block Kit) with sections for
  stale beliefs and absences.
- Decision landing map across roles.
- Feedback loop: thumbs up/down, "more like this", mute topic; profile updates.
- World-first simulator: scripted ground truth rendered into noisy Slack by a different
  model family; golden labels derived from the script.
- Evaluation harness with golden labels, claim-level faithfulness, and simulated
  stale-belief metrics.
- Web preview of digests, landing map and rationale with feedback buttons.
- One-command demo on the bundled corpus with no API key (LLM cassettes).

Out of scope for v1 (tracked in `docs/later.md`)
- Interactive Q&A agent in Slack, onboarding flows, "asks back" questions, Linear or
  other connectors beyond the event envelope, multi-workspace.

## 4. Domain model

Event: one normalized message from any source.
`{source, channel_id, channel_name, thread_ts, ts, user_id, text, mentions[], reactions[],
files[], permalink, edited, deleted}`

Item: one extracted unit of information.
`{item_id, type, title, summary, entities[], owner, due, affected_roles[], phase_tags[],
severity, confidence, evidence[{message_ts, quote}], claims[], first_seen, last_updated,
status, links[{kind, item_id}]}`
- type: decision | blocker | risk | schedule_change | spec_change | bom_change | ask | fyi | absence
- entities: part numbers with rev, ECO/ECR/DEV ids, gates, tests, vendors, POs, people
- status: open | resolved | superseded
- links.kind: duplicate | update_of | supersedes | contradicts | related

Claim: `{key, value, asserted_ts, author, channel, authority}` where key is
`(entity_id, attribute)` and attribute is from the closed vocabulary: rev, lead_time,
gate_date, owner, status, qty, spec_param. authority is a small enum (owner_stated,
plm_bot, cm_stated, hearsay).

Ledger: current value per key with history; `supersedes` when a later authoritative
claim replaces a value; `contradicts` when concurrent claims from different channels
disagree and neither is superseded.

ProjectState: `{project_id, phase, phase_entered, gates[{id, name, date, buffer_days,
checklist[{task, owner, expected_cadence}]}]}`
- phase: concept | EVT | DVT | PVT | ramp

UserProfile: `{user_id, role, projects[], owned_entities[], watch_prompts[],
interest{entity_or_topic: weight}, exposed{key: (value, ts, how)}, seen{item_id: ts},
feedback[]}`
- role: ME | EE | FW | supply_chain | EM | PM
- exposed.how: authored | replied | reacted | shown_in_digest

Digest: `{user_id, as_of, sections{needs_you[], stale_beliefs[], not_happened[],
decisions[], risks[], fyi[]}, items[], rationale{item_id: score_breakdown}}`

LandingMap: per decision item, per role: landed | contradicted | unknown, each with
evidence item ids. Only contradicting evidence may mark a cell contradicted; silence is
unknown and is never coloured as bad.

## 5. Pipeline

Deterministic DAG of idempotent stages. Each stage run is keyed by
(thread_id, last_ts, prompt_version, model) and recorded in a stage_runs table.

1. Ingest: events into SQLite, idempotent on (channel_id, ts). Replay feeds the JSONL
   corpus under a simulated clock. Live uses slack_sdk with resumable backfill, Socket
   Mode events, event_id dedup and a nightly 26-hour reconciliation read.
2. Extract: per thread delta, the LLM receives new messages, the thread root, existing
   items for the thread and the project glossary, and returns items with evidence and
   claims. Code verifies each evidence quote is a whitespace-normalized substring of the
   cited message and builds the permalink from channel and ts. Entity ids must match
   the normalizers. Items failing verification are dropped and counted.
3. Link: candidate pairs by shared entity key, then lexical similarity (FTS5), then an
   LLM adjudicator returning one of {duplicate, update_of, supersedes, contradicts,
   related, distinct} with quoted evidence. Claims update the ledger.
4. Project state: gates and checklists from `config/project.yaml`; phase may be
   overridden by a human and is otherwise the configured phase for the date.
5. Absence: for each open checklist task on a gate inside its buffer, find the last
   evidence item; if the age exceeds the task's expected cadence (weekend-aware), emit
   an absence item citing that last evidence. Never emit an absence without a citation.
6. Rank per user: must-see lane (rule-based, never ranked away: asks addressed to the
   user, blockers on owned entities, schedule changes to owned gates, stale beliefs on
   owned entities). Ranked lane: `score = importance * role_phase_prior * personal_relevance
   * novelty * urgency`, each term stored per impression. Stale-belief items are keys where
   the user's exposed value differs from the ledger value, weighted by the prior.
7. Render: sections capped (needs_you 5, stale_beliefs 3, not_happened 3, decisions 5,
   risks 5, fyi 5). Each line: fact, specific id and rev, buffer or deadline math,
   consequence, owner, source link. Rationale on demand.
8. Feedback: button or CLI feedback updates the profile, records exposure, and is logged
   with run_id and item_id for evaluation.

## 6. World-first simulator and playback harness

- `data/world/<name>/world.yaml`: scripted ground truth: entities, gates, checklists,
  personas with channel membership, and dated events (ECOs, slips, quotes, decisions,
  failures), each with `known_by` per persona over time.
- `data/world/<name>/events.jsonl`: Slack-shaped messages rendered from the world by a
  different model family than the extractor (GPT renders, Claude extracts), with noise
  knobs: indirection, wrong claims by agents, revision typos, delayed cross-posting,
  distractor threads. One 40-message slice is human-authored as an anchor.
- `data/world/<name>/golden.jsonl`: labels derived from the script, not hand-written:
  items per day, claims and supersession, must-see per persona per day, absences.
- `digest replay --world <name> --until <date>` and `digest run --user <id> --as-of <date>`
  are deterministic. LLM calls go through one client wrapper that records to
  `data/cassettes/<hash>.json` keyed by a canonical hash of (model, system, messages,
  schema, temperature) and replays when `DIGEST_MODE=replay`; strict replay raises on a
  miss. Tests and CI run in replay mode.

## 7. Evaluation

Offline, on the simulated world, reported in `reports/eval-<date>.md`:
- Extraction precision and recall by item type, with exact match on discriminating
  fields (rev, date, qty, owner).
- Citation validity: 100% of delivered items carry a verified evidence quote. Release gate.
- Faithfulness: binary judge per claim from a different model family where a key is
  present, with judge agreement on a hand-checked sample of 50.
- Ranking: must-see recall at 5 and nDCG at 5 per persona per day; role separation;
  phase sensitivity.
- Ledger: supersession recall; contradiction precision; stale-belief agent-days with the
  digest on versus off (simulated; labelled as such).
- Absence: false-alarm rate on simulated silences that were not stalls.
- Duplicate rate among delivered items.
- Mirror metrics: missed golden items and hallucinated items, both counted.
- Degradation curve: extraction and supersession recall as the renderer's noise knobs rise.

Online (design only in v1): open rate, thumbs, mute rate, time-to-first-action.

## 8. Acceptance criteria

- A1 `just demo` runs offline in under 60 seconds and prints digests for three users
  (ME, supply chain, EM) on the same day. Role separation target: pairwise item overlap
  at or below the value implied by golden must-see sets; the realized value is reported.
- A2 The same user's digest on the day after the EVT to DVT transition differs from the
  day before; the rationale names the phase and the re-weighted keys. Target difference
  is calibrated from the golden sets and reported, not asserted.
- A3 After a thumbs-down on a topic, the next day's digest ranks that topic lower; the
  rationale shows the feedback term.
- A4 Every delivered item carries a verified evidence quote and permalink; faithfulness
  at or above 0.9 on the golden corpus.
- A5 A stale-belief line shows the user's last-exposed value with how and when they were
  exposed, the current value, and the superseding item's permalink.
- A6 A "Not happened" line names the gate, buffer consumed, the open task, and cites the
  last evidence; absence false-alarm rate on the simulator is reported and below 20%.
- A7 The decision landing map marks a cell contradicted only with a citing item; the
  README shows one such cell with evidence on both sides.
- A8 `just ci` is green: ruff, mypy strict, pytest, hook guard tests, all in replay mode.
- A9 Live mode, with a bot token, backfills a channel resumably, delivers a DM, and
  handles a 429 by honouring Retry-After. Tested by injecting a fake retry handler into
  slack_sdk and with the bolt-python mock web API server pattern; no network in tests.
- A10 Web preview renders a digest, the landing map and rationale, and posts feedback
  that changes the stored profile.

## 9. Milestones and build order

- M1 (day 1): skeleton, models, SQLite store, replay ingest, cassette wrapper, world
  schema and generator scaffold, gate checklist config. Tests for store and replay.
- M2 (day 2): extraction prompt and schema with evidence verification; absence detector
  ("Not happened" ships first, it does not depend on the ledger); golden labels from
  the world script; extraction eval.
- M3 (day 3): claims, linking and ledger; stale-belief computation; ranking with priors,
  profiles, feedback; renderer. Day-3 kill criterion: if claim keying precision on the
  noisy corpus is below 0.8, the ledger is cut to the baseline ranker plus absences and
  the README says so.
- M4 (day 4): landing map; eval harness and report; faithfulness judge; degradation
  curve; web preview. A1 to A8, A10.
- M5 (day 5): live Slack mode, Block Kit, failure handling. A9. Docker, devcontainer
  check, README design doc, ANSWERS.md, demo screenshots.
- Days 6 to 7: adversarial validation, second-model review, retro, polish. No cuts were
  taken to fund the thesis; if M4 slips, the web preview is the first thing deferred.

## 10. Risks and how we handle them

- Ledger is the load-bearing bet: kill criterion above; absences ship first.
- Self-fulfilling eval: different model families render and extract; adversarial noise
  knobs; a human-authored slice; agents in the world sometimes say wrong things so chat
  and world diverge.
- LLM nondeterminism breaks tests: cassettes with strict replay; golden tests assert on
  structure and thresholds.
- Absence detector nags: expectations come from the gate template (authoritative) and
  learned cadence (advisory, weekend-aware); every absence cites a last presence.
- Synthetic corpus looks derivative: program, personas, channels and conflicts differ
  from the public submissions listed in `notes/research/competing-submissions.md`.
- Slack API limits: internal app keeps Tier 3; throttle detection and resumable backfill
  regardless; replay makes the demo independent of Slack.

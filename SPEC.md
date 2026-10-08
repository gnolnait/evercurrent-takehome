# SPEC: role- and phase-aware daily Slack digest

Status: v0.3, 2026-10-07. This is the contract the tests are written against. Change it by
editing this file first, then the tests, then the code. v0.2 added the belief ledger,
absence detection, decision landing map and world-first simulator on top of the v0.1
baseline pipeline. v0.3 adds, after the design review: bot personas for systems of
record, a bi-temporal ledger, the `priority` attribute, sender affinity and engagement
in ranking, re-surfacing by age, diversity at render, actions on `needs_you` lines,
unanswered asks as absences, a classifier port (Jev, Laya) for triage and judging,
profile bootstrap and onboarding, a two-tier delivery policy, and a live replay seeder.

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
- Feedback loop: thumbs up/down, "more like this", mute topic, and the `needs_you`
  actions done, snooze, not mine; profile updates.
- World-first simulator: scripted ground truth, including bot personas for the systems
  of record, rendered into noisy Slack by a different model family; golden labels
  derived from the script.
- Evaluation harness with golden labels, claim-level faithfulness, simulated
  stale-belief metrics, a ranking ablation table, a profile scrutability check and a
  classifier comparison.
- Web preview of digests, landing map, rationale, the editable profile page and the
  open-items page, with feedback buttons.
- Onboarding and configuration: a profile draft inferred from Slack and confirmed by
  the user, a settings surface (role, team, manager, owned subsystems, watch prompts,
  delivery time, weekdays, quiet hours, pause) in Slack App Home and in the web
  preview, and `/digest` slash commands.
- Live replay: the rendered corpus seeded into a real Slack workspace with pacing, so
  live mode can be demonstrated end to end.
- An MCP server over the store, so engineers can query the digest from the assistant
  they already use (Claude, ChatGPT, Cursor). Read tools: `search_items`, `get_item`,
  `get_entity_history`, `list_open_blockers`, `list_absences`, `why_ranked`,
  `get_digest`, `get_project_state`. Write tools, each an idempotent feedback or
  profile event: `set_watch`, `mute_topic`, `mark_done`, `snooze`. stdio transport
  for local assistants; a short usage skill ships with it. The product ships no agent
  loop of its own: the user's assistant supplies the loop, the ask-user step and any
  scheduling.
- One-command demo on the bundled corpus with no API key (LLM cassettes).

Out of scope for v1 (tracked in `docs/later.md`)
- Interactive Q&A agent in Slack, "asks back" questions, Linear, PLM or org-chart
  connectors beyond the event envelope, multi-workspace, point-in-time history of
  edited messages, formal verification of the ledger.

## 4. Domain model

Event: one normalized message from any source.
`{source, channel_id, channel_name, thread_ts, ts, event_ts, user_id, text, mentions[],
reactions[{name, user_id, ts}], files[], permalink, edited, deleted}`
- `ts` is the message time and the identity; `event_ts` is when this version was
  observed (equal to `ts` for the original post, later for an edit or a delete).
- A system of record (PLM, schedule tool, document control) appears as a bot user
  posting into a feed channel; its claims carry `plm_bot` authority.

Item: one extracted unit of information.
`{item_id, type, title, summary, entities[], owner, due, affected_roles[], phase_tags[],
severity, confidence, evidence[{message_ts, quote}], claims[], first_seen, last_updated,
status, links[{kind, item_id}]}`
- type: decision | blocker | risk | schedule_change | spec_change | bom_change | ask | fyi | absence
- entities: part numbers with rev, ECO/ECR/DEV ids, gates, tests, vendors, POs, people
- status: open | resolved | superseded
- links.kind: duplicate | update_of | supersedes | contradicts | related

Claim: `{key, value, asserted_ts, observed_ts, invalid_ts, author, channel, authority}`
where key is `(entity_id, attribute)` and attribute is from the closed vocabulary:
rev, lead_time, gate_date, owner, status, qty, spec_param, priority. authority is a
small enum (owner_stated, plm_bot, cm_stated, hearsay).
- The ledger is bi-temporal: `asserted_ts` is when the claim was made, `observed_ts`
  when the pipeline saw it, `invalid_ts` when a later claim superseded it (null while
  current).

Ledger: current value per key with history; `supersedes` when a later authoritative
claim replaces a value; `contradicts` when concurrent claims from different channels
disagree and neither is superseded. A `plm_bot` claim is the system of record: a
lower-authority claim never supersedes it; a disagreement is a contradiction.

Entity: `{id, type, description, owner, vendor, subsystem, parent, aliases[], state}`
- type: part | assembly | pcba | harness | molded | battery | purchased | component |
  subsystem | document | eco | ecr | deviation | fa | po | gate | build | test |
  release | vendor
- `aliases` is the extractor's glossary; `subsystem` and `parent` give the ranker
  ownership and containment.

ProjectState: `{project_id, phase, phase_entered, gates[{id, name, date, buffer_days,
priority, checklist[{id, task, owner, expected_cadence, priority}]}]}`
- phase: concept | EVT | DVT | PVT | ramp
- priority: critical | high | normal | low (default normal)

UserProfile: `{user_id, role, team, manager, projects[], owned_entities[],
watch_prompts[], interest{entity_or_topic: weight}, exposed{key: (value, ts, how)},
seen{item_id: ts}, feedback[], delivery{time, weekdays[], quiet_hours, paused},
style{description, length, section_order[], sections_off[], language},
sources{field: configured | inferred | confirmed | pasted}}`
- role: ME | EE | FW | supply_chain | EM | PM
- exposed.how: authored | replied | reacted | shown_in_digest
- `sources` records where each field came from, so an inferred draft is
  distinguishable from a confirmed one.
- `style.description` is the user's own words ("short, risks first, skip fyi, in
  German"); the extraction client parses it once into the structured fields beside it,
  and the renderer reads only those fields. Rendering stays template-based and
  deterministic, so evidence quotes and permalinks are never rewritten by a model.
  Slash commands: `/digest now`, `/digest pause <duration>`, `/digest style <text>`,
  `/digest watch <prompt>`, `/digest mute <topic>`, `/digest why <item>`.

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
2. Extract: a classifier first decides whether the thread delta contains project
   information (triage; a dropped golden item counts as a miss in the eval). Then,
   per thread delta, the LLM receives new messages, the thread root, existing items
   for the thread and the project glossary, and returns items with evidence and
   claims. Code verifies each evidence quote is a whitespace-normalized substring of the
   cited message and builds the permalink from channel and ts. Entity ids must match
   the normalizers. Items failing verification are dropped and counted.
3. Link: candidate pairs by shared entity key, then lexical similarity (FTS5), then an
   LLM adjudicator returning one of {duplicate, update_of, supersedes, contradicts,
   related, distinct} with quoted evidence. Claims update the ledger.
4. Project state: gates and checklists from `config/project.yaml`; phase may be
   overridden by a human and is otherwise the configured phase for the date.
5. Absence: two sources. (a) For each open checklist task on a gate inside its buffer,
   find the last evidence item; if the age exceeds the task's expected cadence
   (weekend-aware), emit an absence item citing that last evidence. (b) An ask
   addressed to a person with no reply from them after a cadence is an absence for the
   asker and the owner, citing the ask. Never emit an absence without a citation.
6. Rank per user: must-see lane (rule-based, never ranked away: asks addressed to the
   user, blockers on owned entities, schedule changes to owned gates, stale beliefs on
   owned entities). Ranked lane: `score = importance * role_phase_prior * personal_relevance
   * sender_affinity * novelty * urgency`, each term stored per impression. `importance`
   includes type, severity, authority, affected-role count and engagement (replies and
   reactions on the source thread). `sender_affinity` is highest for the reader's
   manager and reports, then the same team, then frequent collaborators (counted from
   shared threads in a trailing window), else neutral. An open blocker or unanswered
   ask re-surfaces when its age crosses a threshold. Stale-belief items are keys where
   the user's exposed value differs from the ledger value, weighted by the prior. Mute
   is a hard filter and wins over every term.
7. Render: sections capped (needs_you 5, stale_beliefs 3, not_happened 3, decisions 5,
   risks 5, fyi 5); caps are maximums, and a section with nothing above threshold
   renders empty. Items are diversified at render (maximal marginal relevance) so one
   entity does not fill a section; items that differ in a discriminating field (rev,
   qty, date) are exempt from the penalty. Each line: fact, specific id and rev, buffer
   or deadline math, consequence, owner, source link. Rationale on demand.
8. Feedback: button, web or CLI feedback updates the profile, records exposure, and is
   logged with run_id and item_id for evaluation. `needs_you` lines carry done (item
   resolved), snooze until a date, and not mine (reader removed from the audience).
9. Delivery: a two-tier policy. Items in the must-see lane whose urgency crosses the
   interrupt threshold (a blocker on an owned entity, an ask with a same-day due) may be
   sent in real time when the user has enabled it; everything else waits for the digest
   at the user's delivery time, on their weekdays, outside quiet hours.

## 6. World-first simulator and playback harness

- `data/world/<name>/world.yaml`: scripted ground truth: entities, gates, checklists,
  personas with channel membership, team and manager, bot personas for the systems of
  record (PLM, schedule, document control) posting into a feed channel, and dated
  events (ECOs, slips, quotes, decisions, failures), each with claims, the checklist
  tasks it is evidence for, `must_see_for` and `known_by` per persona over time.
- `digest seed --world <name>`: posts the rendered corpus into a real Slack workspace
  through the bot with per-persona display names, threads, edits and deletes, at a
  configurable pace (one world day per N minutes). The world timestamp is written into
  each message's metadata; live ingest reads it back as `ts`, so the simulated clock
  survives seeding while permalinks point at real messages.
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
- Ranking ablation: each score term switched off in turn, with must-see recall and nDCG
  per persona reported; a term that does not move the numbers is removed.
- Profile scrutability: edit a profile (role, owned subsystem, a watch prompt), re-run
  the day, and check the digest changes in the expected direction with the rationale
  naming the edit.
- Classifier comparison: triage recall against golden items, faithfulness-judge
  agreement with the hand-checked sample, and latency per call, for Jev (via
  OpenRouter), Laya (local, Linux) and the Haiku-class model on the same inputs.

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
- A11 A new user is onboarded from Slack: the profile draft inferred from their title,
  channels and threads is shown with its evidence, their confirmation and edits are
  stored with `sources` set, and their delivery time defaults to their Slack timezone.
- A12 Editing a profile field changes the next digest in the expected direction and
  the rationale names the edit (the scrutability check in section 7).

## 9. Milestones and build order

- M1 (day 1): skeleton, models, SQLite store, replay ingest, cassette wrapper with the
  API and CLI backends, world schema (bot personas, team, manager, priority) and
  generator scaffold, gate checklist config. Tests for store and replay.
- M2 (day 2): triage classifier (Jev via OpenRouter) and extraction prompt and schema
  with evidence verification; absence detector with both sources ("Not happened" ships
  first, it does not depend on the ledger); golden labels from the world script;
  extraction eval.
- M3 (day 3): claims, linking and the bi-temporal ledger; stale-belief computation;
  ranking with priors, sender affinity, engagement, re-surfacing by age, profiles,
  feedback and the `needs_you` actions; renderer with diversity and quiet days. Day-3
  kill criterion: if claim keying precision on the noisy corpus is below 0.8, the
  ledger is cut to the baseline ranker plus absences and the README says so.
- M4 (day 4): landing map; eval harness and report with the ablation table, the
  scrutability check and the classifier comparison (Laya runs on Linux); faithfulness
  judge; degradation curve; web preview with the profile page (paste supported) and
  the open-items page; the MCP server over the store with its usage skill. A1 to A8,
  A10, A12.
- M5 (day 5): live Slack mode, Block Kit, failure handling, the two-tier delivery
  policy, profile bootstrap from Slack, onboarding (DM welcome, App Home settings with
  pause and send-now, `/digest` commands), the live replay seeder. A9, A11. Docker,
  devcontainer check, README design doc, ANSWERS.md, demo screenshots.
- Days 6 to 7: adversarial validation, second-model review, retro, polish. No cuts were
  taken to fund the thesis. Cut order if a day slips: the Laya comparison, then the
  MCP server's write tools (read tools stay), then seeder pacing (batch seeding
  stays), then the App Home settings surface (the web profile page stays), then the
  web preview.

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
- Scope growth from the design review: every v0.3 addition is a small, separable piece
  with a named milestone and a place in the cut order above; none changes the M1
  interfaces beyond the world schema and the attribute vocabulary.
- Ranking factor sprawl: every term is stored per impression and the ablation table
  reports its effect; a term without measured effect on the golden corpus is removed.

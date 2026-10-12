# SPEC: role- and phase-aware daily Slack digest

Status: v0.4.2, 2026-10-12. This is the contract the tests are written against. Change it by
editing this file first, then the tests, then the code. v0.2 added the belief ledger,
absence detection, decision landing map and world-first simulator on top of the v0.1
baseline pipeline. v0.3 adds, after the design review: bot personas for systems of
record, a bi-temporal ledger, the `priority` attribute, sender affinity and engagement
in ranking, re-surfacing by age, diversity at render, actions on `needs_you` lines,
unanswered asks as absences, a classifier port (Jev, Laya) for triage and judging,
profile bootstrap and onboarding, a two-tier delivery policy, and a live replay seeder. v0.4 adds, after a review of fifteen public submissions to the same brief:
the stale-belief person-days objective, claim certainty and kind, entity dependency
edges with decision exposure and routed contradictions, three more absence sources, a
feedback event log, a reader access filter, coverage on every digest, documents and
actions as claims, and evaluation gates with baselines. v0.4.2 defines the `novelty`
term, adds a `window_recency` term for windows longer than 36 hours, and makes
the ranked-lane tie-break deterministic.

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

The objective behind all four: minimise stale-belief person-days, the working days a
reader holds a value the ledger has superseded, summed over readers and facts, under a
length budget per digest. Every ranking term earns its place by moving that sum, or must-see recall for the facts
the sum does not count (asks, blockers, absences), in the ablation; section 7 measures
both. Cost follows the same shape: extraction runs once per thread
revision and personalisation is code, so cost grows with thread revisions, not with
messages times readers.

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
  actions done, snooze, not mine, and confirm on a contradiction line; profile updates.
- World-first simulator: scripted ground truth, including bot personas for the systems
  of record, rendered into noisy Slack by a different model family; golden labels
  derived from the script.
- Evaluation harness with golden labels, claim-level faithfulness, simulated
  stale-belief metrics, a ranking ablation table, a profile scrutability check and a
  classifier comparison.
- Web preview of digests, landing map, rationale, the editable profile page and the
  open-items page, with feedback buttons.
- Onboarding and configuration: a profile draft inferred from Slack and confirmed by
  the user, and one settings surface, the web preview's profile page (role, team,
  manager, owned subsystems, watch prompts, delivery time, weekdays, quiet hours,
  pause, digest style). The onboarding DM links to it. Slack App Home settings and
  `/digest` slash commands are stretch.
- Live replay: the rendered corpus seeded into a real Slack workspace with pacing, so
  live mode can be demonstrated end to end.
- One query and feedback function layer over the store and the ranker, exposed by
  three thin adapters that contain no logic: the CLI, the HTTP endpoints of the web
  preview, and an MCP server (stdio) so engineers can query the digest from the
  assistant they already use (Claude, ChatGPT, Cursor). MCP read tools:
  `search_items`, `get_item`, `get_entity_history`, `list_open_blockers`,
  `list_absences`, `why_ranked`, `get_digest`, `get_project_state`, plus `query`, a
  read-only SQL tool on a read-only connection with a row cap and a timeout, with the
  schema shipped as an MCP resource. Write tools, each an idempotent feedback or
  profile event: `set_watch`, `mute_topic`, `mark_done`, `snooze`. Fewer than ten
  tools in the list at any time; a short usage skill ships with the server. The
  pipeline's own model calls never use MCP. The product ships no agent loop: the
  user's assistant supplies the loop, the ask-user step and any scheduling.
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
- A system of record (PLM, schedule tool, document control, issue tracker) appears as a
  bot user posting into a feed channel, live or (for PLM) rendered from a CSV by
  `digest import plm`;
  its claims carry `plm_bot` authority. The issue tracker's claims are `status`, `owner`
  and `priority` on `issue` entities.
- Channels carry `private: bool` and `external: bool`. A public channel is visible to
  every reader; a private one only to its members. Visibility, not membership, decides
  what a reader may be told. An external channel is shared with a contract manufacturer
  or supplier, who builds from the files sent into it.

Item: one extracted unit of information.
`{item_id, type, title, summary, entities[], owner, due, affected_roles[], phase_tags[],
severity, confidence, evidence[{message_ts, quote}], claims[], first_seen, last_updated,
status, resolved_by, links[{kind, item_id}]}`
- type: decision | blocker | risk | schedule_change | spec_change | bom_change | ask | fyi | absence
- entities: part numbers with rev, ECO/ECR/DEV ids, gates, tests, vendors, POs, people
- status: open | resolved | superseded; `resolved_by` is the item or message that closed
  it, so a reader told while it was open can be shown the closing evidence
- links.kind: duplicate | update_of | supersedes | contradicts | related

Claim: `{key, value, asserted_ts, observed_ts, invalid_ts, author, channel, authority,
certainty, kind, conditions, scope}`
where key is `(entity_id, attribute)` and attribute is from the closed vocabulary:
rev, lead_time, gate_date, owner, status, qty, spec_param, priority. authority is a
small enum (owner_stated, plm_bot, cm_stated, hearsay). `certainty` is decided |
reported | hedged | question; `kind` is stated (default: a fact such as a rev, owner, status or date) | target |
measured | referenced (a value carried by a file name or link, which never supersedes);
`scope` is an optional build id or expiry date; `conditions` is optional free text naming the test,
rig, load or rev a measurement was taken under.
- The ledger is bi-temporal: `asserted_ts` is when the claim was made, `observed_ts`
  when the pipeline saw it, `invalid_ts` when a later claim superseded it (null while
  current).

Ledger: current value per key with history; `supersedes` when a later authoritative
claim replaces a value; `contradicts` when concurrent claims from different channels
disagree and neither is superseded. A `plm_bot` claim is the system of record: a
lower-authority claim never supersedes it. A hedged or question claim never supersedes
anything; a target never supersedes a stated or measured value, and a target and a measured value
on one key coexist (the gap between them is the finding); two measurements with
different `conditions` never contradict; a `scope`d claim (a deviation for one build)
neither supersedes nor contradicts the unscoped value, readers who own that build read it
as current, and a scope that has ended with the old value still in use is an absence. A lower-authority claim that disagrees with a recorded
value is `pending`: the record resolves it by confirming the new value (supersession) or
restating its own (a contradiction, routed to the owner of the key as work, never
resolved in code), and silence past a cadence makes it an absence item for the owner
(section 5 step 5 source d). An action claim is a `status` claim on an entity of type po, build, release or document
(placed, started, released, sent; a tooling cut is the status of its tooling PO) whose
evidence names a part and a revision; the audit in step 6 reads those.

Entity: `{id, type, description, owner, vendor, subsystem, parent, depends_on[], aliases[],
priority, state}`
- type: part | assembly | pcba | harness | molded | battery | purchased | component |
  subsystem | document | eco | ecr | deviation | fa | po | gate | build | test |
  release | vendor | issue
- `aliases` is the extractor's glossary; `subsystem` and `parent` give the ranker
  ownership and containment; `depends_on` names the entities whose change forces a
  re-check of this one, so a change reaches owners who were never in the thread;
  `priority` defaults to the containing gate's or subsystem's.

ProjectState: `{project_id, phases[{phase, from_date}], subsystem_phases{subsystem: phase},
gates[{id, name, date, buffer_days, priority, checklist[{id, task, owner, expected_cadence,
priority, entities[]}]}]}`
- phase: concept | EVT | DVT | PVT | ramp. `phases` are the project's dated windows (as
  M1 built them); `subsystem_phases` is an optional override per subsystem. The
  role-by-phase prior reads the override for the item's subsystem when one exists, else
  the project phase; an item with no subsystem reads the project phase
- priority: critical | high | normal | low (default normal)
- `entities` on a checklist task names the entities the task touches; decision exposure
  reads it, and the world script sets it where it sets `tasks` on events

UserProfile: `{user_id, role, team, manager, projects[], owned_entities[],
watch_prompts[], interest{entity_or_topic: weight}, exposed{key: (value, ts, how)},
seen{item_id: ts}, feedback[{event, item_id, run_id, profile_version, ts, payload}],
delivery{time, weekdays[], quiet_hours, paused},
style{description, length, section_order[], sections_off[], language},
sources{field: configured | inferred | confirmed | pasted | imported}}`
- role: ME | EE | FW | supply_chain | EM | PM
- exposed.how: authored | replied | reacted | shown_in_digest | linked (the reader posted, replied to or reacted on a message carrying a file or link
  that names a value, so they hold it)
- `sources` records where each field came from, so an inferred draft is
  distinguishable from a confirmed one.
- `style.description` is the user's own words ("short, risks first, skip fyi, in
  German"); the extraction client parses it once into the structured fields beside it,
  and the renderer reads only those fields. Rendering stays template-based and
  deterministic, so evidence quotes and permalinks are never rewritten by a model.
  Stretch: slash commands `/digest now`, `/digest pause <duration>`, `/digest style
  <text>`, `/digest watch <prompt>`, `/digest mute <topic>`, `/digest why <item>`.

Digest: `{user_id, as_of, coverage{window_start, through, missing_channels[], partial}, sections{needs_you[],
stale_beliefs[], not_happened[], decisions[], risks[], fyi[]}, items[],
rationale{item_id: score_breakdown}}`
- `coverage` (M4 stretch) states the data the digest was built from: the window it covers, the time
  the store reaches, any channel not ingested, and a partial marker when a stage failed.
  A digest never looks complete when it is not.

LandingMap: per decision item, per role: landed | contradicted | unknown, each with
evidence item ids. Only contradicting evidence may mark a cell contradicted; silence is
unknown and is never coloured as bad.

## 5. Pipeline

Deterministic DAG of idempotent stages. Each stage run is keyed by
(thread_id, last_ts, prompt_version, model) and recorded in a stage_runs table.

1. Ingest: events into SQLite, idempotent on (channel_id, ts). Replay feeds the JSONL
   corpus under a simulated clock. Live uses slack_sdk with resumable backfill, Socket
   Mode events, event_id dedup and a nightly 26-hour reconciliation read. `digest import plm`
   (stretch) renders each CSV row as a `plm_bot` post into the feed channel named by
   `--channel`, which must already exist in the store with its visibility (else refused),
   `ts` from the row's `asserted_ts` and row order, so it enters this step like any event
   and obeys the replay clock; in live mode the post goes through the bot, so its
   permalink is real; a corrected row is a new post with a later `ts`, not an edit. `digest import people` sets profile fields with source `imported`,
   shown for confirmation beside inferred ones; it replaces only `inferred` or `imported`
   fields.
2. Extract: a classifier first decides whether the thread delta contains project
   information (triage; a dropped golden item counts as a miss in the eval). The
   classifier returns a probability, through the structured-output path (`extract` with
   a two-field schema) for the LLM adapter and natively for Jev; `judge` stays enum-only
   for adjudication. A thread is kept when that probability is at least 1 / (1 + the
   stated cost of a miss relative to a false keep, a constant in `config/priors.yaml`,
   default 10, a choice to revisit after the first twenty golden threads), and the triage
   decision is stored per thread revision so the rationale can read it back. Then, per thread delta, the LLM receives new
   messages, the thread root, existing items for the thread, the current ledger values
   for the entities the thread names, and the project glossary, and returns items with
   evidence and claims. Code verifies each evidence quote is a whitespace-normalized substring of the
   cited message and builds the permalink from channel and ts. Code also verifies that
   `owner`, `due` and every claim value appear in the cited text, matching numbers with
   their units and people by user id. Entity ids must match the normalizers. Items failing verification are dropped and
   counted per field. Before the model runs, a deterministic pass reads file names in
   `files[]` and URLs in the text: a revision token in a file name or a pinned-version
   URL yields a `referenced` claim on the document entity whose evidence is the file name or
   URL itself (the quote rule above applies to model-written items), and `linked`
   exposure for the poster and every reader who replied or reacted; a reference older than the ledger's value as of the message's `asserted_ts` is a
   stale-reference item, suppressed when the thread also cites the current value. A
   `ticket` reaction by a human on a message creates an `issue` entity and a `status =
   open` claim on it with `owner_stated` authority, the reactor as owner and the message as evidence; the thread's item links to it. Reactions do not change a
   thread's revision, so this runs as its own pass over new reactions, idempotent on
   (channel_id, ts, reactor). On real data the reaction is also a human label that the
   thread carries project information, which the triage evaluation reads.
3. Link: candidate pairs by shared entity key, then lexical similarity (FTS5), then an
   LLM adjudicator returning one of {duplicate, update_of, supersedes, contradicts,
   related, distinct} with quoted evidence. Claims update the ledger. `status` moves to
   resolved only on explicit evidence; on doubt the item stays open. A contradiction is
   not resolved here: it is routed to the owner of the key (section 5 step 6).
4. Project state: gates and checklists from `config/project.yaml`; the project phase is the configured window for the date, and a human may override it
   per subsystem (`subsystem_phases`).
5. Absence. (a) For each open checklist task on a gate inside its buffer,
   find the last evidence item; if the age exceeds the task's expected cadence
   (weekend-aware), emit an absence item citing that last evidence. (b) An ask
   addressed to a person with no reply from them after a cadence is an absence for the
   asker and the owner, citing the ask. (c, stretch) A promise with an owner and a due date
   (an item with `owner` and `due`) whose due date passes with no later evidence from
   the owner is an absence for the owner and the asker, citing the promise. (d, stretch) A human claim that changes a recorded key (rev, part, qty) with no system-of-record
   claim on the same key after a cadence, including workaround language with no ECO or
   deviation after it, is an absence for the owner of the key, citing the claim. (e, stretch) A blocker with no owner and no reply after a cadence is an absence for the owners
   of the entities it names, else for the EM, citing the blocker. (f, stretch) Evidence for a checklist task that predates a revision change of the
   task's own entity makes the task an absence again ("passed on Rev B; Rev C since day
   9; no evidence on Rev C"). (g, stretch) A scoped claim whose scope has ended while the old value is still in use is
   an absence for the key's owner, citing the deviation. The window before a gate in which
   an absence can fire scales with the task's priority, and an absence that persists
   across days rises in rank and states how many days it has been silent. Never emit an
   absence without a citation.
6. Rank per user: must-see lane (rule-based, never ranked away: asks addressed to the
   user, blockers on owned entities, schedule changes to owned gates, stale beliefs on
   owned entities, a change on an entity an owned entity depends on, an open
   contradiction on an owned key, a stale value on an entity the reader's pending
   checklist task touches, a stale reference to a buildable document posted into an
   external channel for the document's owner, and an action taken against a superseded
   value: a PO, tooling PO, release or build start whose claim names a value the ledger
   had already superseded at its `asserted_ts`, for the actor and the key's owner, with
   what the actor had last been told). Before either lane, a reader sees an item only through evidence from a channel they
   can see (public, or private with the reader a member); the check fails closed, and an
   access drop is recorded per impression so "why not shown" can name it. Must-see
   lines are never cut by a section cap; the caps bound the ranked lane. Ranked lane:
   `score = importance * role_phase_prior * personal_relevance * decision_exposure *
   dependency_distance * sender_affinity * novelty * urgency`, each term stored per
   impression. `decision_exposure` rises when the reader owns a checklist task inside
   its gate buffer that touches the item's entities; `dependency_distance` is 1 when one `depends_on` edge joins an item entity to an
   owned entity and a constant below 1 otherwise (one hop, no path search); a claim still `pending` confirmation raises urgency for its owner.
   `novelty` measures what is new to the reader, not the age of a message. For an item
   with claims, it is 1 when the reader has no exposure on a claim's key, or when a
   claim's value differs from the reader's `exposed` value for that key (any
   `exposed.how`); a claim the ledger has since superseded does not count. `exposed`
   holds one value per key, so a claim of kind target or measured, or with `scope` or
   `conditions`, is not compared with it. An item not in `seen` is also 1 when it has no
   compared claim or carries a current claim that is not compared. For any item in `seen`, it is also 1 when its
   `last_updated` is later than its `seen` time. Otherwise it is a floor (default 0.3). Novelty is held at 1 for
   absences, open blockers, unanswered asks, stale-belief items, open contradictions and
   items that carry a `pending` claim, because these stay true until someone acts and
   their rank rises with age (section 5 step 5). Stretch term
   `last_to_know`: rises when every other reader in the fact's audience has been exposed
   to it and this reader has not. Stretch term `window_recency`: when the window (from
   its start, step 9, to `as_of`) is longer than 36 hours, the term falls linearly with
   elapsed time from 1 for an item whose newest evidence is at `as_of` to a floor
   (default 0.8) at the window start; evidence older than the window start takes the
   floor. The term is 1 in a window of 36 hours or less, and 1 for every item whose
   novelty is held at 1. The ablation reads it on Monday digests (world days are working
   days, so a Monday window spans a weekend) and on one persona whose delivery the eval
   run pauses for three working days. An item's newest evidence is the greatest
   `event_ts` among the current versions of the messages its `evidence` cites, so an edit
   counts as new evidence. Ranked-lane ties (scores equal to six significant
   figures) break on newest evidence, newest first, then on `item_id` ascending, so the
   order is deterministic. `importance`
   includes type, severity, authority, affected-role count and engagement (replies and
   reactions on the source thread). `sender_affinity` is highest for the reader's
   manager and reports, then the same team, then frequent collaborators (counted from
   shared threads in a trailing window), else neutral. An open blocker or unanswered
   ask re-surfaces when its age crosses a threshold. Stale-belief items are keys where
   the user's exposed value differs from the ledger value, weighted by the prior. Learned interest is clamped so repeated feedback never takes a key to zero; the two
   hard filters are mute and the visibility check, and both win over every term. Every term is a row in the ablation
   table (section 7) or it is removed.
7. Render: sections capped (needs_you 5, stale_beliefs 3, not_happened 3, decisions 5,
   risks 5, fyi 5); the caps bound the ranked lane and must-see lines sit above them, so a
   section holds its must-see lines plus up to its cap of ranked ones, and a section with
   nothing above threshold renders empty. Items are diversified at render (maximal marginal relevance) so one
   entity does not fill a section; items that differ in a discriminating field (rev,
   qty, date, status, owner) are exempt from the penalty. Each line: fact, specific id and rev, buffer
   or deadline math, consequence, owner, source link, and a history label when the item
   is not new (ongoing N days, escalated from risk to blocker, resurfaced). The
   consequence clause comes from a template table per role and item type; the fact,
   ids, values and link are identical for every reader. An item the reader was shown
   while open and that has since resolved appears once more with its closing evidence,
   whatever its score, outside the section caps and the diversity penalty; the
   visibility check and mute still apply.
   Quoted and model-written text is rendered as plain text; only constructed permalinks
   are links. Every digest carries its coverage line once built (M4 stretch), and a delivery day
   with nothing to say sends one line that says so. Rationale on demand.
8. Feedback: button, web or CLI feedback updates the profile, records exposure, and is
   logged with run_id, item_id and the profile version in force (a counter that every
   profile edit increments). Feedback is an event
   log: each event carries a typed payload (a snooze date, a topic, the id of the event it
   undoes); an undo reverses exactly the referenced event's effect; profile weights are
   recomputed from the log, a vote moves the item most, its entity less and its item type least,
   and no learned weight leaves a bounded band around the role default. `needs_you` lines carry done (resolves the item when the clicker owns it, else records
   exposure for that reader only), snooze until a date, not mine (reader removed from the
   audience), and, on a contradiction line, confirm (the owner picks a value, which
   writes an `owner_stated` claim).
9. Delivery: a two-tier policy. Items in the must-see lane whose urgency crosses the
   interrupt threshold (a blocker on an owned entity, an ask with a same-day due) may be
   sent in real time when the user has enabled it; everything else waits for the digest
   at the user's delivery time, on their weekdays, outside quiet hours. Exposure is recorded when the digest is delivered: on Slack's acceptance of the
   message for Slack, and on render for the terminal, markdown, web preview and replay. Each sent part carries the run id in
   message metadata so a retry posts only the missing parts. A digest's window starts
   where the reader's last delivered digest ended, bounded. A revoked or invalid token
   stops retries and marks the install inactive; auth failures, and a run with scheduled recipients that delivered nothing, raise an
   admin alert.

## 6. World-first simulator and playback harness

- `data/world/<name>/world.yaml`: scripted ground truth: entities, gates, checklists,
  personas with channel membership, team and manager, bot personas for the systems of
  record (PLM, schedule, document control) posting into a feed channel, and dated
  events (ECOs, slips, quotes, decisions, failures), each with claims, the checklist
  tasks it is evidence for, `must_see_for` and `known_by` per persona over time.
- The world's bot personas include an issue tracker, and the script marks some threads with a `ticket` reaction so the rule has input; in
  replay those labels are script-derived like the golden ones.
- `digest seed --world <name>`: posts the rendered corpus into a real Slack workspace
  through the bot with per-persona display names, threads, edits and deletes, at a
  configurable pace (one world day per N minutes). The world timestamp is written into
  each message's metadata; live ingest reads it back as `ts`, so the simulated clock
  survives seeding while permalinks point at real messages.
- `data/world/<name>/events.jsonl`: Slack-shaped messages rendered from the world by a
  different model family than the extractor (GPT renders, Claude extracts), with noise
  knobs: indirection, wrong claims by agents, revision typos, delayed cross-posting,
  distractor threads, late delivery and missed events (for reconciliation), a planted
  instruction aimed at the extractor, and loud threads with urgent words, many replies
  and no state change. The script holds more ranked candidates per persona-day than the affected section caps
  around the EVT to DVT change, so the caps bite and re-weighting changes the cut, quiet days with no planted change and no outstanding golden absence or contradiction,
  for abstention, at least one private channel with a persona outside it, so the
  visibility check is tested, one external channel, and documents with a revision
  history whose file names carry revision tokens, including one stale pack sent to the
  CM and one PO placed against a superseded rev. The renderer's prompt carries a negative list of the
  names and storylines used by public submissions to the same brief. One 40-message
  slice is human-authored as an anchor.
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
- Citation validity: 100% of delivered items carry verified evidence: a quote found in the
  cited message, or for a referenced claim a file name or URL present in it. Release gate.
- Faithfulness: binary judge per claim from a different model family where a key is
  present, with judge agreement on a hand-checked sample of 50.
- Ranking: must-see recall at 5 and nDCG at 5 per persona per day; role separation;
  phase sensitivity.
- Headline, on simulated personas and labelled as such: stale-belief person-days. For
  each golden claim E that supersedes E0 and each persona P in E's `must_see_for` who
  held E0's value (P is in E0's `known_by` or was shown E0), the interval starts at the
  later of E.day and the day P acquired E0's value, and `told_day` is the earliest of
  P's `known_by` day for E and the first digest day that delivers an item citing E, else
  the world's last day plus one; the metric sums `max(0, told_day - start)` over all (P, E),
  with the digest on and with it off (digest days removed); when a later claim supersedes
  E before P is told, P's interval for E ends there and a new interval starts for the
  later claim. Beside it: time-to-told per
  role (median, p90, never-told count) over every must-see fact for personas not in
  `known_by` by E.day, and novelty rate (delivered lines whose `novelty` term
  was 1 at delivery, over delivered lines). All three compute
  from the store and the world file with no model call.
- Ledger: supersession recall; contradiction precision; abstention precision (on quiet
  days, every delivered contradiction, stale-belief line or absence must match an
  outstanding golden state; any other is a false positive).
- Baselines beside every ranking metric: role-only and send-everything on the same
  golden sets, published even when a baseline wins.
- Absence: false-alarm rate on simulated silences that were not stalls.
- Reconciliation lag on the late-delivery and missed-event knobs: digest days between a
  late or missed event and its first delivery.
- Access: no delivered item cites a channel its reader cannot see, checked on a world
  with at least one private channel and a reader outside it.
- Documents and actions: stale-reference precision and recall against the script's
  document revisions; stale-action count: planted actions caught over planted actions, with n stated (the
  script does not react to the digest, so there is no on-versus-off reading).
- Duplicate rate among delivered items.
- Mirror metrics: missed golden items and hallucinated items, both counted.
- Triage recall against ticketed threads, beside recall against golden items;
  script-planted in replay, human-marked on real data, where it is the one label a
  workspace produces without the digest's own buttons.
- Degradation curve: extraction and supersession recall as the renderer's noise knobs rise.
- Extraction scoring splits objective fields (owner, due, status, claim values; a miss
  fails) from advisory ones (type disagreement between neighbours; reported), with
  entity-list Jaccard beside exact match and a drop count per verified field.
- Pairwise judging for prose ablations: for a renderer or prompt-version change the
  judge sees two digests for the same persona-day, position-swapped, and picks; win rate
  reported.
- Regression gate (M4 stretch): `digest eval --gate` compares the report to a frozen baseline, with
  golden labels and ranking config fingerprinted; CI fails on a drop beyond a stated
  tolerance per metric, on a metric present in the baseline and missing or "not run" in
  the report, on an abstention set with zero quiet days, and citation validity stays a
  hard 100 percent. A deliberate ranking change re-freezes the baseline in its own
  commit, with the before and after in the report.
- Cost and latency per stage, n beside every rate, and a "not run: reason" row for any
  configuration without its key.
- Ranking ablation: each score term switched off in turn, with stale-belief person-days (M4 stretch), must-see recall and nDCG per persona
  reported; a term that moves neither person-days nor must-see recall is removed; nDCG is reported.
- Profile scrutability: for every persona and every single-field profile edit (role,
  owned subsystem, a watch prompt) and the project-state edit of a subsystem phase,
  re-run the day on cassettes and check the digest changes in the expected direction
  with the rationale naming the edit; a parametrized test that fails when the world
  yields no cases.
- Classifier comparison (stretch): triage recall against golden items,
  faithfulness-judge agreement with the hand-checked sample, and latency per call, for
  the Haiku-class model through the gateway (core) against Jev (TypeSafe's native API,
  needs its key) and Laya (local, Linux) on the same inputs.

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
- A6 A "Not happened" line from a checklist source names the gate, buffer consumed and
  the open task; from a promise, an unconfirmed change or an unowned blocker it names
  the days silent and the audience; every line cites the last evidence. Absence
  false-alarm rate on the simulator is reported and below 20%.
- A7 The decision landing map marks a cell contradicted only with a citing item; the
  README shows one such cell with evidence on both sides.
- A8 `just ci` is green: ruff (including the annotation rules), ty with warnings as
  errors, pytest, hook guard tests, all in replay mode.
- A9 Live mode, with a bot token, backfills a channel resumably, delivers a DM, and
  handles a 429 by honouring Retry-After. Tested by injecting a fake retry handler into
  slack_sdk and with the bolt-python mock web API server pattern; no network in tests.
- A10 Web preview renders a digest, the landing map and rationale, and posts feedback
  that changes the stored profile.
- A11 (stretch) A new user is onboarded from Slack: the profile draft inferred from
  their title, channels and threads is shown with its evidence, their confirmation and
  edits are stored with `sources` set, and their delivery time defaults to their Slack
  timezone.
- A12 (stretch) Editing a profile field changes the next digest in the expected
  direction and the rationale names the edit (the scrutability check in section 7).

## 9. Milestones and build order

Core items ship in the order below. A stretch item is listed with its day and is
built on that day only after the day's core items are done, when time is left;
otherwise it waits for days 6 to 7. A stretch item never displaces a core item of a
later day. Sections 2 to 7 describe the complete design; a requirement that reads a
stretch item applies once that item is built. Until then the earlier rule holds: the
ablation reports must-see recall and nDCG, a digest carries no coverage line, and CI
runs the tests without the metric gate.

v0.4 at a glance. The cut list for the v0.4 additions, by milestone; "core" here is
what section 10 means by "section 9 names it core".
Core is three correctness rules, the one world field they read and the triage threshold
that the probability of section 5 step 2 needs; everything else v0.4 is stretch, built
after its milestone's core items, and a world field is added only when the item that
reads it is built.
- M2. Core: channel `private` (the M3 visibility filter reads it); the triage threshold
  derived from the cost of a miss (section 5 step 2). Stretch: absence sources (c)
  and (e); the reference pass; the other world schema fields (`depends_on`, task `entities`, claim `certainty`,
  `kind`, `conditions` and `scope`, channel `external`, document revision histories); field-level verification; loud-noise negatives; the `ticket`
  reaction rule; the tracker bot persona.
- M3. Core: the visibility filter; the feedback event log. Stretch: the certainty, kind
  and conditions rules; `depends_on` edges with fan-out; absence sources (d), (f) and (g); stale-reference items; the action-time
  audit; scoped claims; the external-channel stale-reference rule; ledger values in the
  extraction prompt; decision exposure; contradiction routing with the confirm action;
  the per-subsystem phase override; history labels; resolved-since lines; per-role
  consequence templates; the "last to know" term; the `window_recency` term.
- M4. Stretch: the headline metrics; the regression gate; the coverage line; baselines
  beside every rate; abstention precision; pairwise judging; `digest diff`; `why_ranked`
  on an undelivered item.
- M5. Core: exposure recorded on Slack's acceptance. Stretch: send idempotency;
  dead-credential handling; the admin alert; the "nothing today" line; the mock issue
  tracker; the CSV import.

- M1 (day 1): skeleton, models, SQLite store, replay ingest, cassette wrapper with the
  Anthropic API and gateway backends, world schema (bot personas, team, manager,
  priority) and generator scaffold, gate checklist config. Tests for store and replay.
- M2 (day 2): triage classifier (Haiku-class through the gateway) and extraction
  prompt and schema with evidence verification; absence detector ("Not happened" ships first, it does not depend on the ledger); golden labels from the world script; extraction eval. v0.4 core: channel `private`; the triage threshold derived from the cost of a miss (section 5 step 2). v0.4 stretch: absence sources (c) and (e), the
  reference pass (revision tokens in file names and links as `referenced` claims and
  `linked` exposure; the stale-reference item itself waits for the ledger in M3), the
  world schema fields the stretch items read (`depends_on`, task `entities`, claim
  `certainty`, `kind`, `conditions` and `scope`, channel `external`, documents with
  revision histories), each added when its reader is built, field-level verification,
  loud-noise negatives, the `ticket` reaction rule and the tracker bot persona. Jev triage when a TypeSafe key exists stays stretch.
- M3 (day 3): claims, linking and the bi-temporal ledger; stale-belief computation;
  ranking with priors, sender affinity, engagement, profiles, feedback and the
  `needs_you` actions; renderer with quiet days. Day-3 kill criterion: if claim keying
  precision on the noisy corpus is below 0.8, the ledger is cut to the baseline ranker
  plus absences and the README says so. v0.4 core: the visibility filter and the feedback event log (correctness rules, not
  new scope). v0.4 stretch also: the certainty, kind and conditions rules, `depends_on` edges with
  fan-out to the must-see lane,
  absence source (d) (it reads the ledger), stale-reference items, the action-time
  staleness audit (an as-of read of the ledger and the actor's exposure), scoped claims,
  the external-channel stale-reference rule, suspect evidence (source f), ended scope
  (source g).
  v0.4 stretch: ledger values in the extraction prompt, decision exposure, contradiction
  routing with the confirm action, the per-subsystem phase
  override, history labels, resolved-since lines, per-role consequence templates, the
  "last to know" term, the `window_recency` term. Re-surfacing by age and diversity at render stay
  stretch.
- M4 (day 4): landing map; eval harness and report with the ablation table;
  faithfulness judge; degradation curve; web preview with the profile page (paste
  supported, the single settings surface) and the open-items page; the MCP server's
  read tools and usage skill. A1 to A8, A10. v0.4 stretch: the headline metrics, the regression gate, the coverage line,
  baselines beside every rate, abstention
  precision, pairwise judging, `digest diff`, `why_ranked` accepting an undelivered
  item. The scrutability check (A12), the classifier comparison (Laya on Linux) and MCP
  write tools stay stretch.
- M5 (day 5): live Slack mode, Block Kit, failure handling, profile bootstrap from
  Slack with the onboarding DM, batch seeding of the corpus into a workspace. A9.
  Docker, devcontainer check, README design doc, ANSWERS.md, demo screenshots.
  Stretch: two-tier delivery; seeder pacing; App Home settings and `/digest`
  commands; natural-language style parsing (structured style fields are core); A11.
  v0.4 core: exposure recorded on Slack's acceptance (a correctness rule). v0.4 stretch: send idempotency through message metadata, dead-credential handling, the
  admin alert, the "nothing today" line, and a mock issue tracker (an in-process
  stand-in with create, assign and status behind the function layer) so a `ticket`
  reaction creates an issue live until a tracker connector exists (later.md); a file
  import command (`digest import people <csv>` for user_id, role, team, manager,
  owned_entities; `digest import plm <csv>` for entity, attribute, value, asserted_ts,
  rendered as feed posts per section 5 step 1) with two demo files generated from the
  Kestrel world script; the check is that an import into an
  empty store yields the feed claims and the profile fields (sources aside) that replay
  produces.
- Days 6 to 7: adversarial validation, second-model review, retro, polish, then
  stretch items in the order they are listed above. If a core day slips, the web
  preview is the first core item deferred, as in v0.2.

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
  with a named milestone and a place in the cut order above. The M1 interface changes
  are the world schema and project configuration (bot personas, team, manager,
  priority), the attribute vocabulary, and the gateway backend with its logical
  model names (D15). Every v0.4 addition is stretch in its milestone unless section 9 names it core. The core additions are three correctness rules (the visibility filter, the feedback
  event log and exposure on Slack's acceptance), the channel `private` field the first
  one reads, and the triage threshold derived from the cost of a miss (section 5 step 2). Every other v0.4 addition is built only
  after its milestone's core items, and a world field is added only when the item that
  reads it is built.
- Ranking factor sprawl: every term is stored per impression and the ablation table
  reports its effect; a term without measured effect on the golden corpus is removed.

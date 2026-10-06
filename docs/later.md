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
- Onboarding and configuration. DM welcome, settings modal (role, priorities, delivery
  time defaulting to the Slack timezone, weekdays, quiet hours), App Home as the
  settings page with pause and send-now, `/digest` slash commands. Trigger: a second
  real user.
- Linear connector. Webhooks into the event store (issue state, assignee, priority, due
  date, cycle changes; project update health; SLA breaches), read-only MCP for ad hoc
  questions. Trigger: the event envelope is stable and a Linear workspace exists.
- Own read-only MCP server exposing search_items, get_item, list_open_blockers,
  why_ranked, set_watch so engineers can query the digest from their own assistant.
  Trigger: the item store schema is stable.
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

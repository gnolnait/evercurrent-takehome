# Plan: a reader's recent activity lifts relevance

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run.

## Goal

The brief asks for a digest that recognizes "that people's focus changes over time". Today
`personal_relevance` reads only static facts: owned entities (1.0), the reader's role in the
item's `affected_roles` (0.8), else 0.4. A reader who starts working on an entity they do
not own, outside their role's usual items, gets no lift.

This unit adds the smallest behaviour-based signal the store already holds: the messages
the reader wrote in the last few working days. It is not the SPEC's decayed interest model,
which needs feedback events; it is a step window over the reader's own messages.

## Evidence (spike, 2026-10-13)

`notes/research/spikes/phase_focus_spike.py` patched the score on the demo days. With the
lift below (relevance 0.4 raised to 0.8 for an item that shares an entity with the reader's
recent messages, never above 0.8):

- PM, day 6: gains "TST-THM-02 thermal review open and blocking G-DVT1" and "Top-cover DFM
  review open and blocking G-DVT1"; loses "G-DVT1 schedule buffer eroding with two open
  reviews" and "PN-31020 signal harness shortage for DVT-1 kit".
- EM, day 6: gains "ETA for PN-31020 locate or replacement".
- PM, day 8: gains the top-cover DFM review line and loses the PN-31020 shortage line.
- ME, EE and supply chain: order changes only.

## Rule

- Recent messages: the reader's messages (by `user_id`) whose message time `ts` falls after
  the end of `add_working_days(day, -n)` and at or before the end of `day`, in the world's
  zone, not deleted. `n` is `daily.recent_activity.working_days` in `config/priors.yaml`,
  default 3, at least 1.
- Active entities: the entities of each current item, as `_visible` trims it for the
  reader, whose evidence cites one of those messages.
- In the ranked lane, an item whose relevance would be 0.4 and that shares an entity with
  the active entities gets relevance 0.8. Owned (1.0) and affected-role (0.8) items do not
  change. The lift never takes relevance above the affected-role level.
- The line's `reason` names the lift: `personal_relevance=0.8 (recent activity: <entities,
  sorted, comma-separated>)`.
- The must-see lane, the window and the caps do not change.

## Interface

- `Store.authored_messages(user_id: str, start: datetime, end: datetime) ->
  frozenset[tuple[str, str]]`: (channel_id, ts) of the user's messages with `start < ts <=
  end`, current versions, not deleted. Daily code does not read `Store._db`.
- `select(..., newest_evidence, recent_messages: frozenset[tuple[str, str]])`: a new
  required parameter. `select_digest` computes it with the window above and passes it.
- `DailyPriors.recent_activity: RecentActivityPriors` with `working_days: int = 3`,
  validated at least 1.

## Doc changes (first commit)

- SPEC section 5 step 6: `personal_relevance` lists the recent-activity lift, its window and
  its ceiling, and says it is a step window, not the decayed interest of section 2.
- README "What a reader sees, and why": one sentence on the lift.

## Behaviours

- RA1. An item with relevance 0.4 that cites, or shares an entity with an item that cites,
  the reader's message inside the window gets 0.8 (paired test: the same message one
  working day before the window start gives 0.4).
- RA2. Owned and affected-role relevance do not change, with or without recent messages.
- RA3. A deleted message and another user's message do not count.
- RA4. Visibility: an entity that only an item hidden from the reader carries does not
  count.
- RA5. The reason string names the lift and the entities.
- RA6. Must-see lines are the same with and without recent messages (preservation).
- RA7. `RecentActivityPriors` defaults and validation.
- RA8. `Store.authored_messages` window bounds: strictly after the start, at or before the
  end.

## Acceptance

- Tests in `tests/test_select.py`, `tests/test_daily_render.py`, `tests/test_store.py` and
  `tests/test_priors.py` cover RA1 to RA8; each new or changed test is RED-GREEN proven,
  and RA1 fails on today's code.
- `uv run digest demo` and `digest run` for the PM on day 6: the PR reports before and after
  per reader for days 5 to 8. Must-see lines do not change.
- The extraction eval report does not change.

## Not in scope

Decay, reactions and replies as signals, feedback and watch events, and the phase line.

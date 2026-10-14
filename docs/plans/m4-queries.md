# Plan: M4 unit 12a, the query function layer (D20)

Status: v0.1, 2026-10-13, 23:35 PT. Process stage 1 (`docs/process.md`), the full process
the user set for the overnight units. Builds on `main`. Unit 12b (the MCP server) and
unit 13 (the web preview's open-items page) are thin adapters over this layer.

## Goal

SPEC section 3 and D20: "one query and feedback function layer over the store and the
ranker, exposed by three thin adapters that contain no logic". This unit builds the read
half as plain functions in one module, `src/digest/queries.py`, returning frozen models.
The feedback half is the Slack session's `src/digest/feedback.py` (unit 12b's `feedback`
tool calls it once its bodies land); this module writes nothing.

## Context

Every function takes a `QueryContext`: an open `Store`, the `World`, the `Priors` and the
day, plus the window's previous digest day (default: the previous working day, as
`digest demo` does). "As of the day" means the end of that day in the world's zone, as
`select_digest` reads it. A function never opens or closes the store; the adapter does.

```python
class QueryContext(Frozen):
    store: SkipValidation[Store]
    world: SkipValidation[World]
    priors: Priors
    day: date
    previous_digest: date | None = None   # None: the previous working day
```

## Functions (each a SPEC section 3 MCP read tool)

```python
def search_items(ctx, text: str, *, limit: int = 20) -> tuple[ItemView, ...]
def get_item(ctx, item_id: str) -> ItemView | None
def get_entity_history(ctx, entity_id: str) -> tuple[ClaimView, ...]
def list_open_blockers(ctx) -> tuple[ItemView, ...]
def list_absences(ctx) -> tuple[ItemView, ...]
def why_ranked(ctx, user_id: str, item_id: str) -> LineView | None
def get_digest(ctx, user_id: str) -> DigestView
def get_project_state(ctx) -> ProjectStateView
def query(store_path: Path, sql: str, *, row_cap: int = 200, timeout_s: float = 2.0) -> QueryResult
def schema_text() -> str
```

- Q1 `search_items`: items open or resolved as of the day whose title, summary, an
  entity id or an evidence message matches `text`. Message matching reuses
  `Store.search_events` (FTS5); title, summary and entity matching is a case-insensitive
  substring test. Order: items with an FTS hit first in the FTS order, then the rest by
  `last_updated` newest first, then `item_id`; at most `limit` (1 to 100, else
  `ValueError`).
- Q2 `get_item`: the item as of the day, with its evidence, claims, task links, owner
  name, status and absence detail; `None` for an unknown id.
- Q3 `get_entity_history`: every claim on the entity across the items as of the day,
  ordered by the message ts then `item_id`, each with its item id, value, authority and
  the author's name. The ledger is not built (SPEC section 9, M3 proper), so this is the
  claims as stated, with no supersession: the view says so in a `note` field.
- Q4 `list_open_blockers`: items of type `blocker` with status `open`, newest evidence
  first, then `item_id`.
- Q5 `list_absences`: `detect_absences` for the day, in its order.
- Q6 `why_ranked`: the reader's line for the item in `select_digest` with the context's
  window (section, lane, rank, score, reason, must-see rule); `None` when the item is not
  in the reader's digest (SPEC section 9 keeps an undelivered item's explanation as
  stretch). `KeyError` for an unknown user, `ValueError` for a bot, as `profile_for`.
- Q7 `get_digest`: the reader's selection and its rendered text (`render_digest`).
- Q8 `get_project_state`: the phase that holds on the day, each gate with its date, buffer
  and whether the day is inside the buffer, and each checklist task with its owner role
  and its latest evidence day (`task_citations`), or none.
- Q9 `query`: one SQL statement on `Store.open_readonly(store_path)`, so a write is
  refused by SQLite; more than one statement is refused with `ValueError`; at most
  `row_cap` rows (1 to 1000) and a `truncated` flag; a progress handler stops it after
  `timeout_s` (0.1 to 10) with `ValueError("query timed out")`. Columns and rows are
  returned as strings or numbers. The connection is closed on every path.
- Q10 `schema_text`: the text of `src/digest/schema.sql` (the MCP resource).

Views (`ItemView`, `ClaimView`, `LineView`, `DigestView`, `ProjectStateView`,
`QueryResult`) are frozen models of plain fields so an adapter can serialize them
directly; persona ids carry the persona name beside them.

## Layers

`digest.queries` sits beside `digest.eval` and `digest.driver`, above `digest.daily`,
below `digest.room`, `digest.web` and `digest.cli`. It never imports `digest.llm` and
never builds a model request.

## Behaviours

B12.1 to B12.10, one per Q rule, on the day-6 store fixture of the room tests, with
expectations read from the committed golden labels and the recorded store (for
example: `list_open_blockers` holds the PN-11020 stale drawing pack blocker; `why_ranked`
for `U0ME01` and that blocker gives the must-see rule for an owned entity).
B12.11 No function writes: the store file and its `-wal` are byte-identical after every
function runs; `query("DELETE FROM items")` fails without a change.
B12.12 `query` limits: two statements refused; `row_cap` truncation flagged; a recursive
CTE that never ends stops within `timeout_s` plus 1 s.

## Out of scope

The feedback write (the Slack session's `feedback.py`), the MCP adapter (unit 12b), the
web page (unit 13), a reader-scoped visibility check (SPEC: the v1 server is an
administrative interface), and an explanation for an item not in the digest.

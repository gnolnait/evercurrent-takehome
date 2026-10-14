# Plan: M4 unit 12a, the query function layer (D20)

Status: v0.2, 2026-10-14, 00:40 PT. Process stage 1 (`docs/process.md`), the full process
the user set for the overnight units. Builds on `main`. Unit 12b (the MCP server) and
unit 13 (the web preview's open-items page) are thin adapters over this layer.

v0.2 folds in the plan reviews: `query` gets an authorizer (a read-only open still lets
`VACUUM INTO` and `ATTACH` write files), the day comes from the store and is checked
once, absences resolve in `get_item`, the project state lists the people and the task
state, the entity history leads with the owners, and `search_items` takes filters.

## Goal

SPEC section 3 and D20: "one query and feedback function layer over the store and the
ranker, exposed by three thin adapters that contain no logic". This unit builds the read
half as plain functions in `src/digest/queries.py`, returning frozen models. The
feedback half is the Slack session's `src/digest/feedback.py`; unit 12b's `feedback` tool
calls it. This module writes nothing. D20 says "one module"; the split into two modules
is an amendment for the user to confirm (D20 gains `digest.queries` in its layers list).

## The context (Q0)

```python
class QueryContext(Frozen):
    store: SkipValidation[Store]
    world: SkipValidation[World]
    priors: Priors
    day: date                      # the store's day; set by open_context only
    previous_digest: date | None   # None: a first digest, as in select_digest

def open_context(store, world, priors, *, previous_digest: date | None | Literal["auto"] = "auto") -> QueryContext
```

- Q0 `open_context` reads the day from the store's `meta.until` in the world's zone and
  applies the refusals `select_digest` applies (no `until`, an `until` that is not the end
  of a day, extract deltas pending), with the same messages. To share them, the checks
  move out of `select_digest` into a function `check_store_day(store, world, day)` in
  `digest/daily/render.py`, which `select_digest` calls; its behaviour is unchanged. The
  store keeps only the latest item versions, so no function takes another day.
  `"auto"` gives the previous world day, or a first digest on world day 1, as
  `digest demo` and the room do. A function never opens or closes the store.

## Functions (the SPEC section 3 read tools, plus one helper)

```python
def search_items(ctx, text: str = "", *, type: str | None = None, status: str | None = None,
                 entity: str | None = None, owner: str | None = None,
                 updated_since: date | None = None, limit: int = 20) -> tuple[ItemView, ...]
def get_item(ctx, item_id: str) -> ItemView | None
def get_entity_history(ctx, entity: str) -> EntityView | None
def list_open_blockers(ctx) -> tuple[ItemView, ...]
def list_absences(ctx) -> tuple[ItemView, ...]
def why_ranked(ctx, user_id: str, item_id: str) -> RankedLineView | None
def get_digest(ctx, user_id: str) -> DigestView
def get_project_state(ctx) -> ProjectStateView
def query(ctx, sql: str, *, row_cap: int = 200, timeout_s: float = 2.0) -> QueryResult
def schema_text() -> str
```

"The items" below means the stored items as of the day (`Store.items`) plus the absence
items of `detect_absences` for the day; absences are computed, never stored.

- Q1 `search_items`: the items that match every given filter. `text` matches the
  title, the summary, an entity id or alias, or an evidence message (`Store.search_events`,
  FTS5); the other text tests are case-insensitive substring tests. `entity` takes an id
  or an alias. `owner` takes a user id. `updated_since` keeps items whose `last_updated`
  falls on or after that day in the world's zone. `type` and `status` take the model's
  literals, else `ValueError`. At least one filter or a text is required, else
  `ValueError`. Order: items with an FTS hit first in FTS order, then by `last_updated`
  newest first, then `item_id`. `limit` is 1 to 100, else `ValueError`.
- Q2 `get_item`: the item with its evidence (with the author's name and channel name),
  claims, task links, owner name, status and, for an absence, its detail; `None` for an
  unknown id.
- Q3 `get_entity_history`: for an entity id or alias, `EntityView`: the id, type,
  description, owner role, the people who own it (each human persona whose
  `profile_for(...).owned_entities` holds it), then every claim on it across the items,
  ordered by message ts then `item_id`. Each claim has its item id, attribute, param,
  value, authority, and the author's name from the cited message (`None` when the message
  is not stored). The ledger is not built (SPEC section 9, M3 proper), so the claims are
  as stated, with no supersession; `note` says so. `None` for an unknown entity.
- Q4 `list_open_blockers`: stored items of type `blocker` with status `open`, newest
  evidence first, then `item_id`.
- Q5 `list_absences`: `detect_absences` for the day, in its order.
- Q6 `why_ranked`: the reader's line for the item in `select_digest` with the context's
  window: section, lane, rank, score and reason, built as `digest.driver` builds its line
  views; `None` when the item is not in the reader's digest (SPEC section 9 keeps an
  undelivered item's explanation as stretch). `KeyError` for an unknown user, `ValueError`
  for a bot, as `profile_for`.
- Q7 `get_digest`: the reader's selection as line views and its rendered text
  (`render_digest`).
- Q8 `get_project_state`: the day; the phase line text (`phase_line`, so it matches the
  digest); each gate with its date, buffer start (`add_working_days(date, -buffer_days)`)
  and whether the day is inside the buffer; each checklist task with its owner role and a
  state: `done` (a task link with effect `done`), `in progress` (other task links, with the
  newest link day) or `no evidence`; and the people: user id, name, role, kind. The task
  state reads `task_citations`, as `checklist_absences` does.
- Q9 `query`: one SQL statement on a new `Store.open_readonly(ctx.store.path)`
  connection with:
  - an authorizer that allows only `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION`,
    `SQLITE_RECURSIVE`, and `SQLITE_PRAGMA` for `data_version` with no argument (FTS5
    reads it); it denies the rest, so `VACUUM INTO`, `ATTACH`, temp tables and every
    other pragma (`hard_heap_limit` changes the whole process) are refused;
  - `SQLITE_LIMIT_ATTACHED` 0 and `SQLITE_LIMIT_LENGTH` 1,000,000 bytes;
  - a progress handler that stops it after `timeout_s` (0.1 to 10) with
    `ValueError("query timed out")`;
  - at most `row_cap` rows (1 to 1000), fetched with `fetchmany(row_cap + 1)`, and a
    `truncated` flag.

  An authorizer refusal or an SQL error becomes `ValueError` with SQLite's message. Cells
  are `str | int | float | None`; a BLOB becomes its hex text. The connection is closed
  on every path. `PRAGMA table_info` is refused; `schema_text` serves the schema.
- Q10 `schema_text`: the text of `src/digest/schema.sql` (the MCP resource).

Views (`ItemView`, `ClaimView`, `EntityView`, `RankedLineView`, `DigestView`,
`ProjectStateView`, `QueryResult`) are frozen models of plain fields, so an adapter can
serialize them directly; a persona id carries the persona name beside it.

## Layers

`digest.queries` is a layer of its own between `digest.room` and `digest.eval |
digest.driver`, so it can use `digest.driver`'s line views. It never imports
`digest.llm`. The import-linter layers list in `pyproject.toml` gains it.

## Behaviours

On the day-6 store fixture of the room tests, with expectations read from the committed
golden labels and the recorded store:

- B12.0 `open_context` refuses a store replayed past the day's end and one with extract
  deltas pending, with `select_digest`'s messages; `"auto"` on day 6 gives day 5.
- B12.1 to B12.10, one or more per rule above. Examples: `list_open_blockers` holds the
  PN-11020 stale drawing pack blocker; `why_ranked` for `U0ME01` and that blocker gives
  the must-see lane and its rule; `get_item` returns an absence id from `list_absences`;
  `get_entity_history` for an alias gives the same view as for the id, with the ME among
  the owners; `get_project_state` lists every persona and a task state per checklist task.
- B12.11 No function writes: the store file and its `-wal` are byte-identical after every
  function runs. `query` refuses `DELETE FROM items`, `VACUUM INTO '<tmp>/copy.db'` (no
  file is created), `ATTACH '<tmp>/x.db' AS x` (no file is created), `CREATE TEMP TABLE`,
  and `PRAGMA hard_heap_limit=1000`.
- B12.12 `query` limits: two statements refused; `row_cap` truncation flagged; NULL and
  BLOB cells; `SELECT length(hex(zeroblob(2000000)))` refused; an FTS5 `MATCH` query
  allowed; `SELECT count(*) FROM (WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1
  FROM c) SELECT n FROM c)` stops within `timeout_s` plus 1 s.

## Out of scope

The feedback write (the Slack session's `feedback.py`), the MCP adapter (unit 12b), the
web page (unit 13), a reader-scoped visibility check (SPEC: the v1 server is an
administrative interface), an explanation for an item not in the digest, and claim
supersession (the ledger).

# Plan: M4 unit 13, the open-items page in the demo room

Status: v0.1, 2026-10-14, 05:20 PT. Process stage 1 (`docs/process.md`). Stacked on unit
11 (branch `m3-failures`, PR #83, on #81) with unit 12a merged in (branch `m4-queries`,
PR #84): the page lives in the room's web app and reads through the query layer.

## Goal

SPEC section 3 lists an open-items page in the web preview. The room shows each reader's
digest; this page shows what the project holds open, so a presenter can answer "what is
blocked, what has not happened, what does the record say about X" in front of the
interviewers. It is a thin adapter over `digest.queries` (D20): it holds no ranking or
detection logic of its own.

## Rules

- I1. `GET /items` renders `items.html` in the room's style (`room.html` and its
  stylesheet). With no query string it shows two sections: "Open blockers"
  (`list_open_blockers`) and "Not happened" (`list_absences`). Each item shows its
  title, type, status, owner name, the date of its newest evidence, and each evidence
  quote with the author's name, the channel name and the permalink.
- I2. A search form sends `text`, `type`, `status` and `owner` (a select of the human
  personas) as a GET query. With any of them set, the page shows one "Results" section
  from `search_items` with those filters and `limit=50`, above the two default sections.
  Empty form fields count as not set.
- I3. A `ValueError` from the query layer (an unknown type, no filter) answers 400 with
  the page and the message as a refusal, as the room page does.
- I4. The page reads the room as it is now: the room's current store (the room store when
  message changes exist, else the base store), the room's current world, and the room's
  window (the previous working day, as the room selects). `Room` gains
  `items_context()`, a context manager that opens that store read-only, yields
  `open_context(store, world, priors, previous_digest=add_working_days(day, -1))` and
  closes the store. A protocol `ItemsRoom(RoomLike)` with `runtime_checkable` names it;
  a room without it answers `GET /items` with 404.
- I5. The room page header links to `/items`; the items page header links back to `/`.
- I6. Autoescape stays on; no template marks data as safe; no script; no external asset.

## Behaviours

On the day-6 store fixture of the room tests, with a fake room whose `items_context`
opens that store:

- B13.1 `GET /items` answers 200 with an "Open blockers" section whose item titles equal
  `list_open_blockers` in order, and a "Not happened" section equal to `list_absences`.
- B13.2 One rendered item shows its owner name, each evidence quote, its author name, its
  channel name and a link to its permalink.
- B13.3 `GET /items?type=ask&status=open` shows a "Results" section equal to
  `search_items(type="ask", status="open", limit=50)`; empty fields are ignored.
- B13.4 `GET /items?type=nonsense` answers 400 with the query layer's message.
- B13.5 A message with `<script>` in an evidence quote renders escaped.
- B13.6 A real `Room` after a curated message change: `items_context` reads the room
  store, so the change's new item appears on the page; after `reset`, it is gone.
- B13.7 A room without `items_context` answers 404 at `/items`; the room page links to
  `/items`.

## Out of scope

Feedback buttons (they wait for `digest.feedback`), the entity history and project-state
views (the MCP server serves them), and editing from this page.

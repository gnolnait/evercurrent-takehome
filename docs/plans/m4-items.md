# Plan: M4 unit 13, the open-items page in the demo room

Status: v0.2, 2026-10-14, 05:50 PT. Process stage 1 (`docs/process.md`). Stacked on unit
11 (branch `m3-failures`, PR #83, on #81) with unit 12a merged in (branch `m4-queries`,
PR #84): the page lives in the room's web app and reads through the query layer.

v0.2 folds in the plan reviews: each item renders as the room renders a digest line
(people named, the absence detail kept), each item says which readers' digests carry
it, the type and status fields are selects, and the page shows no evidence date (the
views do not carry the edited time).

## Goal

SPEC section 3 lists an open-items page in the web preview. The room shows each reader's
digest; this page shows what the project holds open, so a presenter can answer "what is
blocked, what has not happened, which items cite X" in front of the
interviewers. It is a thin adapter over `digest.queries` (D20): it holds no ranking or
detection logic of its own.

## Rules

- I1. `GET /items` renders `items.html` in the room's style (`room.html` and its
  stylesheet). With no query string it shows two sections: "Open blockers"
  (`list_open_blockers`) and "Not happened" (`list_absences`). Each item shows the
  heading and the summary that the room shows for a digest line (`item_parts`, below),
  the owner's name or "no owner", each evidence quote with the author's name, the
  channel name and the permalink, and the line "In the digest of:" with the name and role
  of each room reader whose current selection holds the item, or "In no reader's digest".
  The last line is the page's point: the project knows the item; it shows who was told.
- I1a. `digest/daily/render.py` gains `item_parts(item, names, tasks) -> tuple[str, str]`
  with the body of `line_parts`, which then calls it with `line.item`; the room page's
  text does not change.
- I2. A search form sends `text`, `type` (a select over the item types and "any"),
  `status` (a select over the item statuses and "any") and `owner` (a select of the
  human personas and "any") as a GET query. With a text or any select not on "any", the
  page shows one "Results" section from `search_items` with those filters and
  `limit=50`, above the two default sections. Results items also show their type and
  status; the default sections do not (they are all open blockers or absences).
- I3. A `ValueError` from the query layer (an unknown type, no filter) answers 400 with
  the page and the message as a refusal, as the room page does.
- I4. The page reads the room as it is now: the room's current store (the room store when
  message changes exist, else the base store) and the room's current world. `Room` gains
  `items_context()`, a context manager that opens that store read-only, yields
  `open_context(store, world, priors)` and closes the store. A protocol
  `ItemsRoom(RoomLike, Protocol)` with `runtime_checkable` names it; a room without it
  answers `GET /items` with 404.
- I5. The room page header links to `/items`; the items page header links back to `/`.
- I6. Autoescape stays on; no template marks data as safe; no script; no external asset.

## Behaviours

On the day-6 store fixture of the room tests, with a fake room whose `items_context`
opens that store:

- B13.1 `GET /items` answers 200 with an "Open blockers" section whose headings equal
  `item_parts` of `list_open_blockers` in order, and a "Not happened" section equal to
  `list_absences` the same way; an ask absence's heading names people, not user ids.
- B13.2 One rendered item shows its owner name, each evidence quote, its author name, its
  channel name and a link to its permalink; a checklist absence shows "no owner" and its
  days silent in the summary.
- B13.2a The "In the digest of:" line names exactly the readers whose room selection holds
  the item, read from the fake room's state.
- B13.3 `GET /items?type=ask&status=open` shows a "Results" section equal to
  `search_items(type="ask", status="open", limit=50)`; "any" and empty values are ignored.
- B13.4 `GET /items?type=nonsense` (a crafted URL) answers 400 with the query layer's message.
- B13.5 A message with `<script>` in an evidence quote renders escaped.
- B13.6 A real `Room` after a curated message change: `items_context` reads the room
  store, so the change's new item appears on the page; after `reset`, it is gone.
- B13.7 A room without `items_context` answers 404 at `/items`; the room page links to
  `/items`.
- B13.8 `line_parts` output is unchanged for every line of the recorded selections
  (the room page tests stay green unchanged).

## Out of scope

Feedback buttons (they wait for `digest.feedback`), the entity history and project-state
views (the MCP server serves them), and editing from this page.

# Plan: M3 unit 11, failures become refusals (model calls and the room page)

Status: v0.2, 2026-10-14, 00:10 PT. Process stage 1 (`docs/process.md`), the full
process the user set for the overnight units: both review lanes on the plan, the tests
and the code, the secondary lens panel on the code, an adversarial pass, a retro entry.
Stacked on unit 10 (branch `m3-room-edits`, PR #81).

v0.2 folds in the plan reviews: the Anthropic half is cut (the room builds only the
gateway backend), no new exception class (nothing would read it), `/reset` gets a real
fix (it dropped the room's message changes when its rebuild failed), `/save` keeps its
existing handler, and the refusal status and the chained exception are stated.

## Goal

Two failures found by unit 9's and unit 10's adversarial passes answer a bare HTTP 500
in the demo room. Make each a refusal the presenter can read, with the room's state
unchanged:

1. With `digest serve --record`, a network failure or timeout on the live gateway call
   escapes `digest.llm` as a raw `httpx2` exception and reaches `POST /edit` uncaught.
2. Any exception other than `ValueError` while the room rebuilds (`POST /edit`,
   `POST /reset`) answers a bare 500. A failed reset also drops the room's message
   changes, because `Room.reset` clears them before it rebuilds.

## Rules

- F1. `OpenRouter.complete` turns any `httpx2.RequestError` from `self.client.post`
  (transport, timeout, decoding and redirect errors) into
  `LLMOutputError("openrouter unreachable: <ExceptionClass>", raw="")`, raised
  `from None`, so no traceback carries the request, the key or a chained message. It
  first logs one event `llm_unreachable` with fields `backend` and `error_class`, so the
  transport class reaches the log (`extract_failed` logs only `LLMOutputError`). A
  success reply whose body is not JSON becomes
  `LLMOutputError("openrouter reply is not JSON", raw=response.text)`. A JSON reply of
  the wrong shape (not an object, `choices` missing, null or empty, a choice without
  `message` or `finish_reason`, a `message` without `content` or with a `content`
  that is neither text nor null, a `usage` that is not an object) becomes
  `LLMOutputError("openrouter reply has an unexpected shape", raw=response.text)`. The
  adversarial pass found these crash `digest extract` today. `run_extract` counts
  either as a failed thread (it catches `LLMOutputError`), and the wrapper does not retry
  it (it retries only `LLMOutputInvalid`).
  The `LLMOutputError` docstring ("The backend answered") changes to cover a backend that
  did not answer. Effect on the CLI with the gateway backend (the default): `digest extract` prints the failed count and exits 0,
  as it does today for an HTTP status error, and the thread stays pending for the next
  run. Today it crashes with a traceback. `digest demo` replays only, so it never
  reaches the gateway.
- F2. The room's refusal for a failed live call names the network as a cause: "the live
  model call failed (a network failure, an auth or rate-limit refusal, another HTTP error,
  or output that did not validate)". The comments in `Room` that list the failure set
  change with it.
- F3. `Room.reset` builds the recorded state first and changes `_store`, `_changed` and
  `_state` only after the build succeeds; a failed reset keeps every message change, so a
  later save still holds them.
- F4. In `digest/web/app.py`, `POST /edit` and `POST /reset` catch any `Exception` other
  than `ValueError`, log one event `room_rebuild_failed` with fields `route` and
  `error_class` (per `.claude/rules/logging.md`) and `exc_info`, so the server log keeps
  the traceback that a bare 500 logs today, and answer status 500 with the full page
  and the refusal "The room could not apply this change (<ErrorClass>); the room is
  unchanged." The page renderer gains a keyword `status` for this; every other refusal
  keeps 400. `ValueError` keeps its 400 and its message.
- F5. The F4 refusal on the page names the exception class only, never its message: an
  exception message can hold a path, a token or a model reply. The traceback goes only to
  the local server log.

## Behaviours

B11.1 The gateway backend with the key `sk-test-SENTINEL-KEY` over an
`httpx2.MockTransport` whose handler raises `httpx2.ConnectError`, `httpx2.ReadTimeout` or
`httpx2.DecodingError`, each built with the message `BODY-SENTINEL` and the key: the
raised `LLMOutputError` has the message exactly `"openrouter unreachable: <Class>"`,
`raw == ""` and `__suppress_context__` true; one `llm_unreachable` record is logged with
`backend == "openrouter"` and `error_class == "<Class>"` and no sentinel in any field. A
handler that raises `TypeError` propagates `TypeError` unchanged.
B11.2 A 200 reply with the body `not json` raises `LLMOutputError` with the message
exactly `"openrouter reply is not JSON"`.
B11.3 A record-mode room whose gateway raises a transport error (seam: `from_settings`
in `digest.room` patched to return an `LLM` over the B11.1 backend) refuses an unrecorded
message change with a `ValueError` whose text holds the F2 cause "a network failure",
and keeps its state (a store file left by a Windows handle is tolerated, as
`_remove_store` does).
B11.4 A room with a curated message change applied, whose next build raises
`RuntimeError`: `reset` raises, the room's state and message changes are unchanged, and
a following `save` writes a corpus that holds the curated change (`save` does not
rebuild; the patched `_build` raises once only).
B11.5 `POST /edit` and `POST /reset` with a fake room that raises
`RuntimeError("ROOM-SENTINEL C:/secret")` answer 500 with the page and the exact F4
refusal; neither the page nor any log record field other than the traceback holds
`ROOM-SENTINEL`; exactly one `room_rebuild_failed` record has `route` equal to the route,
`error_class == "RuntimeError"` and `exc_info` set; a following `GET /` answers 200.
B11.6 `POST /edit` and `POST /reset` with a fake room that raises `ValueError("bad
field")` answer 400 with "bad field" on the page and log no `room_rebuild_failed`.
`POST /save` keeps its existing `OSError` and `ValueError` refusal with status 400.

## Test seams

`httpx2.MockTransport` for the gateway client, a monkeypatched `digest.room.from_settings`,
a monkeypatched `Room._build` for B11.4, a fake `RoomLike` whose methods raise, `caplog`
for the log event. No network, no key.

## Out of scope

- The Anthropic backend: the room builds only the gateway backend (room.py), so no demo
  path reaches it.
- Retries or backoff for transport errors (`docs/later.md`, "Transient backoff for 429
  and 5xx on the gateway during record runs").
- `POST /save`: it already refuses `OSError` and `ValueError`, which is what a save raises.
- A malformed proxy setting that makes the gateway client's constructor raise: not
  reproduced in a demo setup. Known limit: such a `ValueError` message reaches the page
  as every `ValueError` refusal does.
- The extraction merge that keeps an edited message's old claims (unit 10, M8).

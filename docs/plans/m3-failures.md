# Plan: M3 unit 11, failures become refusals (model calls and the room page)

Status: v0.1, 2026-10-13, 23:20 PT. Process stage 1 (`docs/process.md`), the full
process the user set for the overnight units: both review lanes on the plan, the tests
and the code, the secondary lens panel on the code, an adversarial pass, a retro entry.
Stacked on unit 10 (branch `m3-room-edits`, PR #81).

## Goal

Two failures found by unit 9's and unit 10's adversarial passes answer a bare HTTP 500
in the demo room. Make both a refusal the presenter can read, with the room's state
unchanged:

1. With `digest serve --record`, a network failure or timeout on a live model call
   (`httpx2.TransportError` from the gateway client; `anthropic.APIConnectionError`,
   `anthropic.APITimeoutError` and `anthropic.APIStatusError` from the Anthropic client)
   escapes `digest.llm` as a raw exception and reaches `POST /edit` uncaught.
2. Any exception other than `ValueError` while the room rebuilds (`POST /edit`,
   `POST /reset`, `POST /save`) answers a bare 500.

## Rules

- F1. Each backend in `digest.llm` turns its client's transport and status exceptions into
  a new `LLMUnavailable(LLMOutputError)` with a message naming the backend and the
  exception class only (`"openrouter unreachable: ConnectTimeout"`), never the request,
  the key or the response body; `raw` is the empty string. Because it is an
  `LLMOutputError`, `run_extract` counts the thread as failed, as it does today for an HTTP
  status error, and the room refuses the change with its existing reason for a failed
  live call. `LLMUnavailable` is not `LLMOutputInvalid`, so the wrapper's one retry does
  not apply to it.
- F2. The import-linter contract that keeps `httpx2` and `anthropic` inside `digest.llm`
  stays as it is: nothing outside `digest.llm` names those packages.
- F3. In `digest/web/app.py`, `POST /edit`, `POST /reset` and `POST /save` catch any
  `Exception` other than `ValueError` at the route, log one event `room_rebuild_failed`
  with fields `route` and `error_class` (per `.claude/rules/logging.md`), and answer 500
  with the full page and the refusal "The room could not apply this change
  (<ErrorClass>); the room is unchanged." The room's state is unchanged (unit 10 already
  keeps it so). `ValueError` keeps its 400 and its message.
- F4. The refusal names the exception class only, never its message: an exception
  message can hold a path, a token or a model reply.

## Interface

`digest/llm/__init__.py`: `class LLMUnavailable(LLMOutputError)`, constructed with
`(message: str)` and `raw=""`.

`digest/llm/openrouter.py` and `digest/llm/anthropic_api.py`: `complete` wraps its
client call as F1 says. No signature changes.

`digest/web/app.py`: no signature changes; the three routes gain the F3 handler.

## Behaviours

B11.1 The gateway backend with an `httpx2.MockTransport` that raises `httpx2.ConnectError`
and one that raises `httpx2.ReadTimeout` raises `LLMUnavailable` whose message names the
class and holds neither the API key nor the request body.
B11.2 The Anthropic backend with a fake client whose `messages.create` raises each of the
three SDK exceptions raises `LLMUnavailable` the same way.
B11.3 `LLM.extract` does not retry an `LLMUnavailable` (the backend is called once).
B11.4 A room in record mode whose backend raises `LLMUnavailable` refuses the change with
a `ValueError`, keeps its state and leaves no new store file (extends unit 10's B10.3).
B11.5 `POST /edit`, `POST /reset` and `POST /save` with a room that raises `RuntimeError`
answer 500 with the page and the F3 refusal; the refusal does not contain the
exception's message; one `room_rebuild_failed` event is logged; a following `GET /`
answers 200 with the previous state.
B11.6 `ValueError` refusals keep their 400 and their message (unit 9 and 10 tests stay
green unchanged).

## Test seams

`httpx2.MockTransport` for the gateway client, a fake `anthropic.Anthropic` with a
`messages.create` that raises, a fake `RoomLike` whose methods raise, `caplog` for the
log event. No network, no key.

## Out of scope

Retries or backoff for transport errors (`docs/later.md` has "Transient backoff for 429
and 5xx on the gateway during record runs"); the extraction merge that keeps an edited
message's old claims (unit 10, M8).

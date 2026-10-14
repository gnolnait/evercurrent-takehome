# Plan: M4 unit 12b, the MCP server over the query layer (D20)

Status: v0.2, 2026-10-14, 02:00 PT. Process stage 1 (`docs/process.md`), the full process
the user set for the overnight units. Stacked on unit 12a (branch `m4-queries`).

v0.2 folds in the plan reviews: each call opens the store and builds its context (a
context kept for the server's life reads a stale day and holds a Windows file handle),
the default store is the demo store `digest serve` uses, `query` takes no cap or
timeout arguments, and a stdio subprocess test guards stdout.

## Goal

SPEC section 3: an MCP server (stdio), so the operator of the store can ask the
assistant they already use about the project. The server is a thin adapter over
`digest.queries`: each tool calls one function and returns its view. It holds no logic.

## The SDK decision (for the user to confirm)

D20's amendment of 2026-10-11 says the server targets protocol 2026-07-28 through the
Python SDK 2.x. A spike on this laptop (Windows 11 ARM64, Python 3.12) found that SDK
2.x cannot be imported here: `mcp.server` imports `cryptography`, which has no win_arm64
wheel and does not build from source. SDK 1.30.0 (protocol 2025-11-25, which current
clients speak) imports and runs when uv drops the `crypto` extra of `pyjwt`.

- M0. The `mcp` extra pins `mcp==1.30.0`. `[tool.uv] override-dependencies` holds
  `pyjwt>=2.10`, which removes the `crypto` extra. The server uses no feature that
  needs it (no OAuth, no HTTP transport).
- D20's amendment of 2026-10-11 is revised in place to say this, and to name the move to
  SDK 2.x when `cryptography` ships a win_arm64 wheel. The tool code does not change
  between the two SDKs for the features used here (tools, one resource, stdio).

## Rules

- M1. `src/digest/mcp_server.py` builds a `FastMCP("downstream")` server with the eight
  SPEC read tools and `query`, each a function of the same name, parameters and return
  type as in `digest.queries`, minus the context. `query` takes only `sql`; the adapter
  uses the function's default cap and timeout. FastMCP serializes the return value
  (probed on 1.30.0): a view model gives its JSON fields as `structuredContent`; a tuple
  or an optional view is wrapped as `{"result": ...}`. The tool docstring is the
  description the assistant reads: one sentence on what it answers, one on its arguments.
- M2. The resource `schema://store` returns `schema_text()`.
- M3. The server loads the world and the priors once at start. Each tool call opens the
  store with `Store.open_readonly`, builds the context with `open_context`, calls the
  function and closes the store, so a replay between two calls is seen and no handle is
  held. At start the server opens the store once to check it: a missing file, a store
  whose `meta.world` differs from the world, or a store that `open_context` refuses stops
  the start with its message on stderr and exit status 1.
- M4. FastMCP turns any exception from a tool into a tool error (`isError`) with the text
  "Error executing tool <name>: <message>" (probed on 1.30.0). The server adds no handler
  of its own. The v1 server is an administrative interface, so the message may name the
  store's own data.
- M5. `digest mcp [--day N] [--world <name> --db <path>]` starts the server on stdio.
  Without `--db` it builds the bundled world's store for world day N (default 6) as
  `digest serve` does, and keeps it for the server's life. With `--db` it serves that
  store, and `--world` defaults to the store's `meta.world`. The command imports
  `digest.mcp_server` inside the function, with a comment, and refuses with "install the
  mcp extra: uv sync --extra mcp" when the package is missing, as `serve` does for `web`.
  Nothing but the protocol goes to stdout: logs and progress go to stderr.
- M6. The `feedback` tool waits for the Slack session's `digest.feedback` module. Until
  then the list holds nine tools. When it lands, `feedback` is one tool with an `action`
  field that calls that module on a separate write connection; that change is its own
  unit.
- M7. A usage note `docs/mcp.md` gives the client configuration for Claude Desktop and
  Claude Code (`uv run --directory <repo> python -m digest mcp`, so the world, the priors
  and the demo store resolve against the repo), the tool list, and three example
  questions. It is the "short usage skill" of SPEC section 3. The README line and the
  ANSWERS line that call the MCP server designed but not built change to built.
- M8. Layer: `digest.mcp_server` sits with `digest.web` as a sibling under `digest.cli`.

## Behaviours

On the day-6 store fixture, through `create_connected_server_and_client_session` (an
in-memory client, no subprocess, no network):

- B12b.1 `list_tools` gives exactly the nine names, each with a non-empty description.
- B12b.2 Each tool called with the arguments of one unit 12a behaviour returns
  `structuredContent` equal to FastMCP's form of that function's result: the view's JSON
  fields, or `{"result": ...}` for a tuple or an optional.
- B12b.3 `get_item` for an unknown id returns `{"result": null}`; `why_ranked` for an
  unknown user returns a tool error.
- B12b.4 `list_resources` holds `schema://store`; reading it returns the text of
  `src/digest/schema.sql`.
- B12b.5 `digest mcp --db <path>` for a store with no `meta.until`, and for a store whose
  world differs from `--world`, exits 1 with nothing on stdout.
- B12b.6 The MCP stdio client starts `sys.executable -m digest mcp --world kestrel --db
  <copy of the day-6 store>` as a subprocess, lists the tools and calls
  `list_open_blockers`; the call succeeds (any stray stdout breaks the protocol).
- B12b.7 After the server starts, the store file is replaced by a copy of another store;
  the next `get_project_state` call reports the new store's day.

## Out of scope

The `feedback` tool (M6), an HTTP transport and per-user tokens (`docs/later.md`), a
reader-scoped server, and prompts or sampling.

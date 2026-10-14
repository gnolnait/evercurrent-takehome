# Ask your assistant about the project (MCP server)

`digest mcp` starts an MCP server on stdio. Use it to ask Claude Desktop, Claude Code or
another MCP client about the items, the absences and the digests in a store. The server
reads the store only. It applies no reader visibility check, so give it only to the
operator of the store (D20).

## Install

Install the `mcp` extra once:

```bash
uv sync --extra mcp
```

The client starts the server with `uv run --directory <repo> python -m digest mcp`.
`--directory` makes uv run the command in the repo, so the world, the priors and the
demo store resolve against the repo. Replace `<repo>` with the full path of your clone.

Without `--db`, the server builds the store of the bundled Kestrel K2 world for world
day 6, from the recorded model calls. Add `--day N` for another world day. To serve your
own store, add `--db <path>`; `--world` defaults to the world that the store holds. No
API key is necessary.

## Claude Desktop

Add the server to `claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "downstream": {
      "command": "uv",
      "args": ["run", "--directory", "<repo>", "python", "-m", "digest", "mcp"]
    }
  }
}
```

## Claude Code

Add the server from a shell:

```bash
claude mcp add downstream -- uv run --directory <repo> python -m digest mcp
```

Or put the same server in `.mcp.json` at the root of a project:

```json
{
  "mcpServers": {
    "downstream": {
      "command": "uv",
      "args": ["run", "--directory", "<repo>", "python", "-m", "digest", "mcp"]
    }
  }
}
```

## Tools

Each tool calls one function of `src/digest/queries.py` and returns its result as JSON.
Each call opens the store again, so a replay between two calls shows in the next call.

| Tool | What it answers |
| --- | --- |
| `search_items` | The items that match a text and the filters (type, status, entity, owner, updated_since). |
| `get_item` | One item with its evidence, its owner and the authors of the messages. |
| `get_entity_history` | An entity, its owners and every claim on it. |
| `list_open_blockers` | The open blockers, newest evidence first. |
| `list_absences` | The checklist tasks and the asks that went silent on the day. |
| `why_ranked` | The section, the lane, the rank and the reason of an item in a reader's digest. |
| `get_digest` | A reader's digest, as lines and as rendered text. |
| `get_project_state` | The phase, the gates, the checklist task states and the people. |
| `query` | One read-only SQL statement, at most 200 rows and 2 seconds. |

The resource `schema://store` holds the SQL schema of the store. Read it before you
write a `query`.

## Example questions

- "Which blockers are open, and who said so last?"
- "Why is the stale drawing pack for PN-11020 in the digest of U0ME01?"
- "Show every claim on the chassis plate, and who owns it."

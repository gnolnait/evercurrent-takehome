"""The MCP server (stdio) over the query function layer (plan docs/plans/m4-mcp.md).

A thin adapter: each tool opens the store read-only, builds the context, calls one
function of `digest.queries` and returns its view. It holds no logic of its own.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from digest.priors import Priors
from digest.world.schema import World

SCHEMA_URI = "schema://store"


def check_store(store_path: Path, world: World, world_name: str) -> None:
    """Refuse a store the server cannot serve, with ValueError (plan M3).

    A missing file, a store whose `meta.world` is not `world_name`, and a store that
    `open_context` refuses are refused.
    """
    raise NotImplementedError


def build_server(store_path: Path, world: World, priors: Priors) -> FastMCP:
    """The server with the nine read tools and the schema resource (plan M1, M2, M3)."""
    raise NotImplementedError

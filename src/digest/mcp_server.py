"""The MCP server (stdio) over the query function layer (plan docs/plans/m4-mcp.md).

A thin adapter: each tool opens the store read-only, builds the context, calls one
function of `digest.queries` and returns its view. It holds no logic of its own.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from digest import queries
from digest.priors import Priors
from digest.queries import (
    DigestView,
    EntityView,
    ItemView,
    ProjectStateView,
    QueryContext,
    QueryResult,
    RankedLineView,
)
from digest.store import Store
from digest.world.schema import World

SCHEMA_URI = "schema://store"


def check_store(store_path: Path, world: World, world_name: str, priors: Priors) -> None:
    """Refuse a store the server cannot serve, with ValueError (plan M3).

    A missing file, a file that is not a store of this schema, a store whose `meta.world`
    is not `world_name`, and a store that `open_context` refuses are refused.
    """
    if not store_path.is_file():
        raise ValueError(f"no store at {store_path}; run digest replay first")
    try:
        store = Store.open_readonly(store_path)
    except sqlite3.Error as error:  # a file that is not SQLite
        raise ValueError(f"cannot open store {store_path}: {error}") from error
    with store:
        stored = store.get_meta("world")
        if stored != world_name:
            raise ValueError(f"store {store_path} holds world {stored!r}, not {world_name!r}")
        queries.open_context(store, world, priors)


def build_server(store_path: Path, world: World, priors: Priors) -> FastMCP:
    """The server with the nine read tools and the schema resource (plan M1, M2, M3)."""
    server = FastMCP("downstream")

    @server.tool()
    def search_items(
        text: str = "",
        *,
        type: str | None = None,
        status: str | None = None,
        entity: str | None = None,
        owner: str | None = None,
        updated_since: date | None = None,
        limit: int = 20,
    ) -> tuple[ItemView, ...]:
        """Find the items that match a text and every given filter, best match first.

        Give a text, at least one filter (type, status, entity id or alias, owner user id,
        updated_since day), or both; limit is from 1 to 100.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.search_items(
                ctx,
                text,
                type=type,
                status=status,
                entity=entity,
                owner=owner,
                updated_since=updated_since,
                limit=limit,
            )

    @server.tool()
    def get_item(item_id: str) -> ItemView | None:
        """Get one item (a stored item or an absence) with its evidence; null for an unknown id.

        Give the item_id as search_items or a digest line shows it.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.get_item(ctx, item_id)

    @server.tool()
    def get_entity_history(entity: str) -> EntityView | None:
        """Get an entity, its owners and every claim on it; null for an unknown entity.

        Give the entity id or an alias, in any case.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.get_entity_history(ctx, entity)

    @server.tool()
    def list_open_blockers() -> tuple[ItemView, ...]:
        """List the open blockers, newest evidence first.

        The tool takes no arguments.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.list_open_blockers(ctx)

    @server.tool()
    def list_absences() -> tuple[ItemView, ...]:
        """List the absences of the day: what should have happened and did not.

        The tool takes no arguments.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.list_absences(ctx)

    @server.tool()
    def why_ranked(user_id: str, item_id: str) -> RankedLineView | None:
        """Explain the place and the reason of an item in a reader's digest; null when absent.

        Give the reader's Slack user_id and the item_id.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.why_ranked(ctx, user_id, item_id)

    @server.tool()
    def get_digest(user_id: str) -> DigestView:
        """Get a reader's digest for the store's day, as lines and as rendered text.

        Give the reader's Slack user_id.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.get_digest(ctx, user_id)

    @server.tool()
    def get_project_state() -> ProjectStateView:
        """Get the phase, the gates, the checklist task states and the people on the day.

        The tool takes no arguments.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.get_project_state(ctx)

    @server.tool()
    def query(sql: str) -> QueryResult:
        """Run one read-only SQL statement on the store; at most 200 rows, at most 2 seconds.

        Give one SELECT statement; read the resource schema://store for the tables.
        """
        with _context(store_path, world, priors) as ctx:
            return queries.query(ctx, sql)

    @server.resource(SCHEMA_URI, mime_type="text/plain")
    def schema() -> str:
        """The SQL schema of the store."""
        return queries.schema_text()

    return server


@contextmanager
def _context(store_path: Path, world: World, priors: Priors) -> Iterator[QueryContext]:
    """Open the store read-only and build the context; close the store at the end."""
    with Store.open_readonly(store_path) as store:
        yield queries.open_context(store, world, priors)

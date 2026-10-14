"""Unit 12b: B12b.1 through B12b.7 in docs/plans/m4-mcp.md v0.2."""

import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent, TextResourceContents
from pydantic import TypeAdapter
from typer.testing import CliRunner

import test_room
from digest.cli import app
from digest.mcp_server import build_server
from digest.priors import Priors
from digest.store import Store
from digest.world.schema import World

day6_store = test_room.day6_store
priors = test_room.priors
world = test_room.world

ROOT = Path(__file__).resolve().parents[1]
TOOL_CASES = (
    ("search_items", {"text": "standoff locations", "limit": 100}, True),
    ("get_item", {"item_id": test_room.STALE_PACK}, True),
    ("get_entity_history", {"entity": "chassis plate"}, True),
    ("list_open_blockers", {}, True),
    ("list_absences", {}, True),
    ("why_ranked", {"user_id": "U0ME01", "item_id": test_room.STALE_PACK}, True),
    ("get_digest", {"user_id": "U0ME01"}, False),
    ("get_project_state", {}, False),
    ("query", {"sql": "SELECT NULL AS empty, X'00FF' AS bytes"}, False),
)
TOOL_NAMES = {name for name, _, _ in TOOL_CASES}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_MODE", "replay")
    for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def day5_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp-day5") / "kestrel.sqlite"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DIGEST_MODE", "replay")
        for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            patch.delenv(key, raising=False)
        for args in (
            ["replay", "--world", "kestrel", "--until", "2026-10-18", "--db", str(path)],
            ["extract", "--world", "kestrel", "--db", str(path)],
        ):
            result = CliRunner().invoke(app, args)
            assert result.exit_code == 0, (result.output, result.exception)
    return path


@pytest.mark.anyio
async def test_list_tools_has_nine_described_tools(
    day6_store: Path, world: World, priors: Priors
) -> None:
    """Setup day 6 / Action list tools / Assert nine names and descriptions (B12b.1)."""
    server = build_server(day6_store, world, priors)
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.list_tools()

    assert len(result.tools) == 9
    assert {tool.name for tool in result.tools} == TOOL_NAMES
    assert all(tool.description and tool.description.strip() for tool in result.tools)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "wrapped"), TOOL_CASES, ids=[row[0] for row in TOOL_CASES]
)
async def test_tool_structured_content_matches_query(
    day6_store: Path,
    world: World,
    priors: Priors,
    name: str,
    arguments: dict[str, Any],
    wrapped: bool,
) -> None:
    """Setup day 6 / Action call a tool / Assert the query's JSON result (B12b.2)."""
    server = build_server(day6_store, world, priors)
    # The query stubs cannot yet build QueryContext's schema; reach the MCP stub first.
    from digest import queries

    with Store.open_readonly(day6_store) as store:
        ctx = queries.open_context(store, world, priors)
        expected = TypeAdapter(Any).dump_python(
            getattr(queries, name)(ctx, **arguments), mode="json", by_alias=True
        )
    if wrapped:
        expected = {"result": expected}

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool(name, arguments)

    assert not result.isError, result.content
    assert result.structuredContent == expected


@pytest.mark.anyio
async def test_unknown_item_is_null_and_unknown_user_is_tool_error(
    day6_store: Path, world: World, priors: Priors
) -> None:
    """Setup unknown ids / Action get and rank / Assert null and a tool error (B12b.3)."""
    server = build_server(day6_store, world, priors)
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        item = await client.call_tool("get_item", {"item_id": "no-such-item"})
        ranked = await client.call_tool(
            "why_ranked", {"user_id": "U-MISSING", "item_id": test_room.STALE_PACK}
        )

    assert not item.isError, item.content
    assert item.structuredContent == {"result": None}
    assert ranked.isError
    message = "\n".join(part.text for part in ranked.content if isinstance(part, TextContent))
    assert "Error executing tool why_ranked:" in message
    assert "U-MISSING" in message


@pytest.mark.anyio
async def test_schema_resource_returns_store_schema(
    day6_store: Path, world: World, priors: Priors
) -> None:
    """Setup day 6 / Action list and read schema / Assert the schema file text (B12b.4)."""
    server = build_server(day6_store, world, priors)
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        resources = await client.list_resources()
        assert "schema://store" in {str(resource.uri) for resource in resources.resources}
        result = await client.read_resource("schema://store")

    assert len(result.contents) == 1
    content = result.contents[0]
    assert isinstance(content, TextResourceContents)
    assert content.text == (ROOT / "src/digest/schema.sql").read_text("utf-8")


@pytest.mark.parametrize("fault", ["no-until", "wrong-world"])
def test_cli_refuses_invalid_store_without_stdout(
    day6_store: Path, tmp_path: Path, fault: str
) -> None:
    """Setup invalid metadata / Action start CLI / Assert exit 1 and no stdout (B12b.5)."""
    path = tmp_path / "invalid.sqlite"
    shutil.copy2(day6_store, path)
    with closing(sqlite3.connect(path)) as db, db:
        if fault == "no-until":
            db.execute("DELETE FROM meta WHERE key = 'until'")
        else:
            db.execute("UPDATE meta SET value = 'other-world' WHERE key = 'world'")
    args = ["mcp", "--db", str(path)]
    if fault == "wrong-world":
        args += ["--world", "kestrel"]

    result = CliRunner().invoke(app, args, catch_exceptions=False)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "until" in result.stderr if fault == "no-until" else "world" in result.stderr


@pytest.mark.anyio
async def test_stdio_lists_tools_and_calls_blockers(
    day6_store: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Setup a store copy / Action use the stdio client / Assert valid MCP replies (B12b.6)."""
    path = tmp_path / "stdio.sqlite"
    shutil.copy2(day6_store, path)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "digest", "mcp", "--world", "kestrel", "--db", str(path)],
        cwd=ROOT,
        env={"DIGEST_MODE": "replay", "NO_COLOR": "1"},
    )

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool("list_open_blockers", {})

    assert len(tools.tools) == 9
    assert {tool.name for tool in tools.tools} == TOOL_NAMES
    assert not result.isError, result.content
    assert result.structuredContent is not None
    assert test_room.STALE_PACK in {
        view["item"]["item_id"] for view in result.structuredContent["result"]
    }
    assert not [
        record for record in caplog.records if record.name == "mcp.client.stdio" and record.exc_info
    ]


@pytest.mark.anyio
async def test_replacing_store_changes_project_day(
    day6_store: Path, day5_store: Path, tmp_path: Path, world: World, priors: Priors
) -> None:
    """Setup day 6 / Action replace with day 5 between calls / Assert the new day (B12b.7)."""
    path = tmp_path / "served.sqlite"
    shutil.copy2(day6_store, path)
    server = build_server(path, world, priors)

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        before = await client.call_tool("get_project_state", {})
        assert not before.isError, before.content
        assert before.structuredContent is not None
        assert before.structuredContent["day"] == "2026-10-19"

        shutil.copy2(day5_store, path)
        after = await client.call_tool("get_project_state", {})

    assert not after.isError, after.content
    assert after.structuredContent is not None
    assert after.structuredContent["day"] == "2026-10-18"

"""Unit 12b: B12b.1 through B12b.7 in docs/plans/m4-mcp.md v0.2."""

import inspect
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
from pydantic import AnyUrl, TypeAdapter
from typer.testing import CliRunner

import test_room
from digest import queries
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

    for tool in result.tools:
        parameters = {
            name: parameter
            for name, parameter in inspect.signature(getattr(queries, tool.name)).parameters.items()
            if name != "ctx" and (tool.name != "query" or name == "sql")
        }
        assert set(tool.inputSchema["properties"]) == set(parameters), tool.name
        required = [
            name
            for name, parameter in parameters.items()
            if parameter.default is inspect.Parameter.empty
        ]
        assert sorted(tool.inputSchema.get("required", [])) == sorted(required), tool.name


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
async def test_search_items_passes_filters_and_limit(
    day6_store: Path, world: World, priors: Priors
) -> None:
    """Setup three matching asks / Action filter and limit / Assert the query result (B12b.2)."""
    server = build_server(day6_store, world, priors)

    filters = {"type": "ask", "status": "open", "owner": "U0SC01"}
    with Store.open_readonly(day6_store) as store:
        ctx = queries.open_context(store, world, priors)
        matches = queries.search_items(ctx, type="ask", status="open", owner="U0SC01", limit=100)
        assert len(matches) > 1
        limited = queries.search_items(ctx, type="ask", status="open", owner="U0SC01", limit=1)
        assert len(limited) == 1
        expected = TypeAdapter(Any).dump_python(limited, mode="json", by_alias=True)

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool("search_items", {**filters, "limit": 1})

    assert not result.isError, result.content
    assert result.structuredContent == {"result": expected}


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
        result = await client.read_resource(AnyUrl("schema://store"))

    assert len(result.contents) == 1
    content = result.contents[0]
    assert isinstance(content, TextResourceContents)
    assert content.text == (ROOT / "src/digest/schema.sql").read_text("utf-8")


@pytest.mark.parametrize("fault", ["no-until", "wrong-world", "missing-file"])
def test_cli_refuses_invalid_store_without_stdout(
    day6_store: Path, tmp_path: Path, fault: str
) -> None:
    """Setup an invalid store / Action start CLI / Assert exit 1 and no stdout (B12b.5)."""
    path = tmp_path / "invalid.sqlite"
    if fault != "missing-file":
        shutil.copy2(day6_store, path)
        with closing(sqlite3.connect(path)) as db, db:
            if fault == "no-until":
                db.execute("DELETE FROM meta WHERE key = 'until'")
            else:
                db.execute("UPDATE meta SET value = 'other-world' WHERE key = 'world'")
    else:
        assert not path.exists()
    args = ["mcp", "--db", str(path)]
    if fault == "wrong-world":
        args += ["--world", "kestrel"]

    result = CliRunner().invoke(app, args, catch_exceptions=False)

    assert result.exit_code == 1
    assert result.stdout == ""
    if fault != "missing-file":
        assert "until" in result.stderr if fault == "no-until" else "world" in result.stderr


@pytest.mark.anyio
@pytest.mark.parametrize("explicit_world", [True, False], ids=["explicit-world", "store-world"])
async def test_stdio_lists_tools_and_calls_blockers(
    day6_store: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    world: World,
    priors: Priors,
    explicit_world: bool,
) -> None:
    """Setup a store copy / Action use the stdio client / Assert valid MCP replies (B12b.6)."""
    path = tmp_path / "stdio.sqlite"
    shutil.copy2(day6_store, path)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "digest", "mcp", "--db", str(path)]
        + (["--world", "kestrel"] if explicit_world else []),
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
        state = await client.call_tool("get_project_state", {})

    assert len(tools.tools) == 9
    assert {tool.name for tool in tools.tools} == TOOL_NAMES
    assert not result.isError, result.content
    assert result.structuredContent is not None
    assert test_room.STALE_PACK in {
        view["item"]["item_id"] for view in result.structuredContent["result"]
    }

    with Store.open_readonly(path) as store:
        ctx = queries.open_context(store, world, priors)
        expected = TypeAdapter(Any).dump_python(
            queries.get_project_state(ctx), mode="json", by_alias=True
        )
    assert not state.isError, state.content
    assert state.structuredContent == expected
    assert not [
        record for record in caplog.records if record.name == "mcp.client.stdio" and record.exc_info
    ]


@pytest.mark.anyio
async def test_stdio_default_store_uses_requested_day(world: World) -> None:
    """Setup the default store / Action start day 6 / Assert tools and the world date (B12b.6)."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "digest", "mcp", "--day", "6"],
        cwd=ROOT,
        env={"DIGEST_MODE": "replay", "NO_COLOR": "1"},
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool("get_project_state", {})

    assert len(tools.tools) == 9
    assert {tool.name for tool in tools.tools} == TOOL_NAMES
    assert not result.isError, result.content
    assert result.structuredContent is not None
    assert result.structuredContent["day"] == world.date_of(6).isoformat()


@pytest.mark.anyio
async def test_replacing_store_refreshes_every_tool(
    day6_store: Path, day5_store: Path, tmp_path: Path, world: World, priors: Priors
) -> None:
    """Setup day 6 / Action replace with day 5 / Assert each tool reads the new store (B12b.7)."""
    path = tmp_path / "served.sqlite"
    shutil.copy2(day6_store, path)
    server = build_server(path, world, priors)

    arguments_by_name = {
        "search_items": {"type": "blocker", "limit": 100},
        "get_item": {"item_id": "absence:G-DVT1.thermal"},
        "get_entity_history": {"entity": "TST-THM-02"},
        "why_ranked": {
            "user_id": "U0ME01",
            "item_id": "C0ENGEF:1791993300.010026#1791993300.010026#0",
        },
        "query": {"sql": "SELECT value FROM meta WHERE key = 'until'"},
    }
    cases = [
        (name, arguments_by_name.get(name, args), wrapped) for name, args, wrapped in TOOL_CASES
    ]

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        before = {}
        for name, arguments, _ in cases:
            result = await client.call_tool(name, arguments)
            assert not result.isError, (name, result.content)
            before[name] = result.structuredContent

        shutil.copy2(day5_store, path)
        with Store.open_readonly(day5_store) as store:
            ctx = queries.open_context(store, world, priors)
            for name, arguments, wrapped in cases:
                expected = TypeAdapter(Any).dump_python(
                    getattr(queries, name)(ctx, **arguments), mode="json", by_alias=True
                )
                if wrapped:
                    expected = {"result": expected}
                assert before[name] != expected, (name, "the fixture must expose a cached result")
                after = await client.call_tool(name, arguments)
                assert not after.isError, (name, after.content)
                assert after.structuredContent == expected, name

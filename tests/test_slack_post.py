from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import digest.cli as cli
from digest.cli import app
from digest.daily.profile import Profile
from digest.daily.select import Line, Section, Selection
from digest.extract.stage import ExtractReport
from digest.extract.verify import DROP_FIELDS
from digest.models import Evidence, Item
from digest.store import Store
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_PATH = ROOT / "data" / "world" / "kestrel" / "world.yaml"
DAY = date(2026, 10, 14)
TS = "1760000000.000001"
WORKSPACE = "https://kestrel-k2.slack.com"
WEBHOOK_URL = "https://hooks.slack.test/services/secret-value"
runner = CliRunner()


@pytest.fixture
def world() -> World:
    return load_world(WORLD_PATH)


@pytest.fixture
def profile() -> Profile:
    return Profile(
        user_id="U0ME01",
        name="Nadia Brenner",
        role="ME",
        team="mechanical",
        owned_entities=("PN-51010",),
        channels=("C0KPROG", "C0ENGME"),
    )


def evidence(number: int = 1) -> Evidence:
    return Evidence(
        channel_id="C0ENGME",
        message_ts=f"1760000000.{number:06d}",
        quote="source text",
        permalink=f"{WORKSPACE}/archives/C0ENGME/p1760000000{number:06d}",
    )


def item(number: int = 1, **changes: Any) -> Item:
    values: dict[str, Any] = {
        "item_id": f"ITEM-{number}",
        "thread_id": f"C0ENGME:{TS}",
        "type": "decision",
        "title": "Drawing released",
        "summary": "Release PN-51010 Rev C for tooling.",
        "entities": ("PN-51010", "G-TR1"),
        "owner": "U0SC01",
        "due": None,
        "affected_roles": ("ME", "supply_chain", "EM"),
        "severity": "high",
        "confidence": "high",
        "evidence": (evidence(number),),
        "first_seen": TS,
        "last_updated": TS,
    }
    return Item(**(values | changes))


def line(value: Item, section: Section = "decisions", *, must_see: bool = False) -> Line:
    return Line(
        item=value,
        section=section,
        must_see=must_see,
        reason="test selection",
        score=0 if must_see else 0.5,
    )


def selection(profile: Profile, *lines: Line) -> Selection:
    return Selection(profile=profile, day=DAY, lines=lines, hidden=0)


def slack_message(world: World, selected: Selection) -> dict[str, Any]:
    try:
        module = import_module("digest.daily.slack")
    except ModuleNotFoundError:
        pytest.fail("digest.daily.slack.slack_message is missing", pytrace=False)
    return module.slack_message(world, selected)


def block_text(block: dict[str, Any]) -> str:
    text = block.get("text")
    assert isinstance(text, dict)
    value = text.get("text")
    assert isinstance(value, str)
    return value


def test_slack_message_has_header_section_titles_lines_and_sources(
    world: World, profile: Profile
) -> None:
    """Set up two sections; build the message; assert its Block Kit shape and content."""
    selected = selection(
        profile,
        line(item(evidence=(evidence(1), evidence(2))), must_see=True),
        line(item(3, type="risk", title="Tooling risk"), "risks"),
    )

    message = slack_message(world, selected)

    header = "Daily digest for Nadia Brenner (ME) - 2026-10-14"
    assert set(message) == {"text", "blocks"}
    assert message["text"] == header
    assert message["blocks"][0]["type"] == "header"
    assert message["blocks"][0]["text"]["type"] == "plain_text"
    assert block_text(message["blocks"][0]) == header
    blocks = message["blocks"][1:]
    assert [block["type"] for block in blocks] == ["section"] * 4
    texts = [block_text(block) for block in blocks]
    assert texts[0] == "*Decisions*"
    assert "*Drawing released*" in texts[1]
    assert "*[must see]*" in texts[1]
    assert f"<{evidence(1).permalink}|source>" in texts[1]
    assert f"<{evidence(2).permalink}|source>" in texts[1]
    assert texts[2] == "*Risks*"
    assert "*Tooling risk*" in texts[3]


def test_slack_message_escapes_model_text_before_mrkdwn(world: World, profile: Profile) -> None:
    selected = selection(
        profile,
        line(
            item(
                title="Alert & <@U123> >",
                summary="Do not open <https://x|y> & wait > now.",
            )
        ),
    )

    message = slack_message(world, selected)

    rendered = block_text(message["blocks"][2])
    assert "*Alert &amp; &lt;@U123&gt; &gt;*" in rendered
    assert "Do not open &lt;https://x|y&gt; &amp; wait &gt; now." in rendered
    assert "<@U123>" not in rendered
    assert "<https://x|y>" not in rendered
    assert re.findall(r"<[^>]+>", rendered) == [f"<{evidence().permalink}|source>"]


def test_slack_message_cuts_a_long_line_with_ellipsis(world: World, profile: Profile) -> None:
    selected = selection(profile, line(item(summary="x" * 4000)))

    message = slack_message(world, selected)

    rendered = block_text(message["blocks"][2])
    assert len(rendered) <= 3000
    assert rendered.endswith("\N{HORIZONTAL ELLIPSIS}")


def test_slack_message_limits_blocks_and_reports_omitted_lines(
    world: World, profile: Profile
) -> None:
    lines = tuple(line(item(number, title=f"Decision {number}")) for number in range(1, 52))

    message = slack_message(world, selection(profile, *lines))

    assert len(message["blocks"]) == 50
    assert block_text(message["blocks"][-1]) == "4 lines left out."


class FakeResponse:
    def __init__(self, status_code: int = 200, body: str = "ok") -> None:
        self.status_code = status_code
        self.body = body


class FakeWebhookClient:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = responses or [FakeResponse()]

    def send(self, *, text: str, blocks: list[dict[str, Any]]) -> FakeResponse:
        self.calls.append({"text": text, "blocks": blocks})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def successful_extract(store: Store, *args: object, **kwargs: object) -> ExtractReport:
    for key, value in {
        "world": "kestrel",
        "timezone": "America/Los_Angeles",
        "workspace_url": WORKSPACE,
        "until": "2026-10-20T06:59:59.999999+00:00",
    }.items():
        store.set_meta(key, value)
    store.upsert_items([item()], prompt_version="m2.1", model="extract")
    return ExtractReport(
        threads=0,
        kept=0,
        dropped=0,
        failed=0,
        items_written=1,
        drops=dict.fromkeys(DROP_FIELDS, 0),
    )


def failed_extract(store: Store, *args: object, **kwargs: object) -> ExtractReport:
    report = successful_extract(store, *args, **kwargs)
    return report.model_copy(update={"threads": 1, "failed": 1, "items_written": 0})


def prepare_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: FakeWebhookClient,
    *,
    extract: Callable[..., ExtractReport] = successful_extract,
    webhook_url: str | None = WEBHOOK_URL,
) -> None:
    cassettes = tmp_path / "cassettes"
    cassettes.mkdir()
    # The replaced extract step does not read this cassette.
    (cassettes / "unused.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassettes))
    if webhook_url is None:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", webhook_url)
    monkeypatch.setattr(cli, "_extract_days", extract)
    monkeypatch.setattr("digest.daily.render.detect_absences", lambda *args, **kwargs: [])

    def client_for(url: str) -> FakeWebhookClient:
        assert url == webhook_url
        return client

    monkeypatch.setattr(cli, "_webhook_client", client_for, raising=False)


def test_cli_post_sends_three_readers_in_order_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 0, result.output
    assert [call["text"] for call in client.calls] == [
        "Daily digest for Nadia Brenner (ME) - 2026-10-19",
        "Daily digest for Hamid Rostami (supply_chain) - 2026-10-19",
        "Daily digest for Grace Okonkwo (EM) - 2026-10-19",
    ]


def test_cli_post_without_webhook_url_exits_one_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, client, webhook_url=None)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "SLACK_WEBHOOK_URL" in result.output
    assert client.calls == []


def test_cli_post_stops_after_slack_refuses_a_message_without_leaking_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeWebhookClient([FakeResponse(400, "invalid_blocks")])
    prepare_post(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "Nadia Brenner" in result.output
    assert "400" in result.output
    assert "invalid_blocks" in result.output
    assert len(client.calls) == 1
    assert WEBHOOK_URL not in result.output


def test_cli_post_build_failure_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, client, extract=failed_extract)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert re.search(r"1 failed thread", result.output, re.IGNORECASE), result.output
    assert client.calls == []

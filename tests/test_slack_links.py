from __future__ import annotations

import re
import traceback
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from typer.testing import CliRunner

import digest.cli as cli
from digest.cli import app
from digest.daily.profile import Profile
from digest.daily.select import Line, Selection
from digest.daily.slack import slack_message
from digest.models import Evidence, Item
from digest.seed import MappedFile, MappedMessage, SeedMap
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_PATH = ROOT / "data" / "world" / "kestrel" / "world.yaml"
DAY = date(2026, 10, 14)
BOT_TOKEN = "xox" + "b-link-test-secret"
WEBHOOK_URL = "https://hooks.slack.com/services/link-test-secret"
DM_CHANNEL = "D012ABC"
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
        channels=("C0ENGME", "C0KPROG"),
    )


def evidence(channel: str, ts: str, permalink: str | None = None) -> Evidence:
    return Evidence(
        channel_id=channel,
        message_ts=ts,
        quote="source text",
        permalink=permalink or f"https://world.slack.com/archives/{channel}/p{ts.replace('.', '')}",
    )


def item(number: int, sources: tuple[Evidence, ...], **changes: Any) -> Item:
    values: dict[str, Any] = {
        "item_id": f"ITEM-{number}",
        "thread_id": f"{sources[0].channel_id}:{sources[0].message_ts}",
        "type": "decision",
        "title": "Drawing released",
        "summary": "Release PN-51010 Rev C for tooling.",
        "entities": ("PN-51010",),
        "owner": None,
        "due": None,
        "affected_roles": ("ME",),
        "severity": "high",
        "confidence": "high",
        "evidence": sources,
        "first_seen": sources[0].message_ts,
        "last_updated": sources[-1].message_ts,
    }
    return Item(**(values | changes))


def selection(profile: Profile, *items: Item, hidden: int = 0) -> Selection:
    lines = tuple(
        Line(item=value, section="decisions", must_see=False, reason="test", score=0.5)
        for value in items
    )
    return Selection(profile=profile, day=DAY, lines=lines, hidden=hidden)


def seed_map(
    *,
    world: str = "kestrel",
    team_id: str = "T-MAPPED",
    messages: dict[str, MappedMessage] | None = None,
    files: dict[str, MappedFile] | None = None,
) -> SeedMap:
    return SeedMap(
        team_id=team_id,
        workspace_url="https://ws.slack.com",
        world=world,
        messages=messages or {},
        files=files or {},
    )


def section_texts(message: dict[str, Any]) -> list[str]:
    return [block["text"]["text"] for block in message["blocks"] if block["type"] == "section"]


def line_text(message: dict[str, Any]) -> str:
    return next(text for text in section_texts(message) if "Drawing released" in text)


class FakeResponse:
    status_code = 200
    body = "ok"


class FakeWebhookClient:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events = events

    def send(self, *, text: str, blocks: list[dict[str, Any]]) -> FakeResponse:
        if self.events is not None:
            self.events.append("send")
        self.calls.append({"text": text, "blocks": blocks})
        return FakeResponse()


class FakeSlackWebClient:
    def __init__(
        self,
        *,
        team_id: str = "T-MAPPED",
        auth_error: Exception | None = None,
    ) -> None:
        self.team_id = team_id
        self.auth_error = auth_error
        self.calls: list[dict[str, Any]] = []

    def auth_test(self) -> dict[str, str]:
        self.calls.append({"method": "auth_test"})
        if self.auth_error is not None:
            raise self.auth_error
        return {"team_id": self.team_id}

    def conversations_open(self, *, users: str) -> dict[str, dict[str, str]]:
        self.calls.append({"method": "conversations_open", "users": users})
        return {"channel": {"id": DM_CHANNEL}}

    def chat_postMessage(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        self.calls.append(
            {"method": "chat_postMessage", "channel": channel, "text": text, "blocks": blocks}
        )


def prepare_cli(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    selections: list[Selection],
    webhook: FakeWebhookClient,
) -> None:
    monkeypatch.setattr(cli, "_demo_selections", lambda day: (world, selections))
    monkeypatch.setattr(cli, "_webhook_url", lambda: WEBHOOK_URL)
    monkeypatch.setattr(cli, "_webhook_client", lambda url: webhook)


def save_map(tmp_path: Path, value: SeedMap) -> Path:
    path = tmp_path / "seed-map.json"
    value.save(path)
    return path


def test_mapped_root_reply_files_and_one_channel_render_in_source_order(
    world: World, profile: Profile
) -> None:
    root = evidence("C0ENGME", "1760000000.000001")
    reply = evidence("C0ENGME", "1760000000.000002")
    links = seed_map(
        messages={
            "C0ENGME:1760000000.000001": MappedMessage(
                channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
            ),
            "C0ENGME:1760000000.000002": MappedMessage(
                channel="C111", ts="1761000000.000002", thread_ts="1761000000.000001"
            ),
        },
        files={
            "C0ENGME:1760000000.000001/a&b.pdf": MappedFile(
                permalink="https://ws.slack.com/files/U1/F1/a.pdf"
            ),
            "C0ENGME:1760000000.000001/drawing<rev>.step": MappedFile(
                permalink="https://ws.slack.com/files/U1/F2/drawing.step"
            ),
        },
    )

    text = line_text(slack_message(world, selection(profile, item(1, (root, reply))), links))

    expected = (
        "<https://ws.slack.com/archives/C111/p1761000000000001|source> "
        "<https://ws.slack.com/files/U1/F1/a.pdf|a&amp;b.pdf> "
        "<https://ws.slack.com/files/U1/F2/drawing.step|drawing&lt;rev&gt;.step> "
        "<https://ws.slack.com/archives/C111/p1761000000000002?"
        "thread_ts=1761000000.000001&cid=C111|source> "
        "in <#C111>"
    )
    assert text.splitlines()[-1] == expected
    assert text.count("<#C111>") == 1


def test_mapped_sources_in_two_channels_render_channels_in_source_order_without_files(
    world: World, profile: Profile
) -> None:
    sources = (
        evidence("C0ENGME", "1760000000.000001"),
        evidence("C0KPROG", "1760000000.000002"),
    )
    links = seed_map(
        messages={
            "C0ENGME:1760000000.000001": MappedMessage(
                channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
            ),
            "C0KPROG:1760000000.000002": MappedMessage(
                channel="C222", ts="1762000000.000001", thread_ts="1762000000.000001"
            ),
        }
    )

    text = line_text(slack_message(world, selection(profile, item(1, sources)), links))

    assert text.splitlines()[-1] == (
        "<https://ws.slack.com/archives/C111/p1761000000000001|source> "
        "<https://ws.slack.com/archives/C222/p1762000000000001|source> in <#C111> <#C222>"
    )
    assert "files" not in text


def test_unmapped_source_keeps_the_world_permalink(world: World, profile: Profile) -> None:
    source = evidence("C0ENGME", "1760000000.000001", "https://world.slack.com/original")

    text = line_text(slack_message(world, selection(profile, item(1, (source,))), seed_map()))

    assert text.splitlines()[-1] == "<https://world.slack.com/original|source>"
    assert "<#" not in text


def test_none_links_render_exactly_like_the_omitted_argument(
    world: World, profile: Profile
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))

    assert slack_message(world, selected, links=None) == slack_message(world, selected)


def test_mapped_message_escapes_text_keeps_verbatim_and_cuts_complete_tokens(
    world: World, profile: Profile
) -> None:
    source = evidence("C0ENGME", "1760000000.000001")
    links = seed_map(
        messages={
            "C0ENGME:1760000000.000001": MappedMessage(
                channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
            )
        },
        files={
            "C0ENGME:1760000000.000001/a&<b>.pdf": MappedFile(
                permalink="https://ws.slack.com/files/U1/F1/a.pdf"
            )
        },
    )
    selected = selection(
        profile,
        item(1, (source,), summary="A & B < C > D " + "x" * 2900),
    )

    message = slack_message(world, selected, links)
    text = line_text(message)

    assert "A &amp; B &lt; C &gt; D" in text
    assert len(text) <= 3000
    assert text.endswith("…")
    assert re.search(r"<[^>]*$|&[a-z]*$", text[:-1]) is None
    for block in message["blocks"]:
        if block["type"] == "section":
            assert block["text"]["type"] == "mrkdwn"
            assert block["text"]["verbatim"] is True


def test_cli_without_seed_map_does_not_authenticate(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    client = FakeSlackWebClient()
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 0, result.output
    assert [call["method"] for call in client.calls] == [
        "conversations_open",
        "chat_postMessage",
        "chat_postMessage",
        "chat_postMessage",
    ]
    assert webhook.calls == []


@pytest.mark.parametrize(
    ("map_case", "reason"),
    [
        ("missing", "seed-map.json"),
        ("malformed", "seed-map.json"),
        ("other-world", "another-world"),
    ],
)
def test_cli_rejects_invalid_seed_map_before_build_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    map_case: str,
    reason: str,
) -> None:
    path = tmp_path / "seed-map.json"
    if map_case == "malformed":
        path.write_text("not json", encoding="utf-8")
    elif map_case == "other-world":
        seed_map(world="another-world").save(path)
    builds: list[int] = []
    sends = FakeWebhookClient()
    monkeypatch.setattr(cli, "_demo_selections", lambda day: builds.append(day))
    monkeypatch.setattr(cli, "_webhook_url", lambda: WEBHOOK_URL)
    monkeypatch.setattr(cli, "_webhook_client", lambda url: sends)

    result = runner.invoke(app, ["post", "--seed-map", str(path)])

    assert result.exit_code == 1, result.output
    assert reason in result.output
    assert builds == []
    assert sends.calls == []


def test_cli_dm_rejects_seed_map_for_other_team_before_open_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    path = save_map(tmp_path, seed_map(team_id="T-OTHER"))
    client = FakeSlackWebClient(team_id="T-ACTUAL")
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--seed-map", str(path)])

    assert result.exit_code == 1, result.output
    assert client.calls == [{"method": "auth_test"}]
    assert webhook.calls == []


def test_cli_dm_auth_failure_is_safe_and_opens_no_dm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    path = save_map(tmp_path, seed_map())
    error = SlackApiError(f"auth failed with {BOT_TOKEN}", {"error": "invalid_auth"})
    client = FakeSlackWebClient(auth_error=error)
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_env_source", lambda key: "environment")
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--seed-map", str(path)])

    assert result.exit_code == 1, result.output
    assert "invalid_auth" in result.output
    assert BOT_TOKEN not in result.output
    assert BOT_TOKEN not in "".join(traceback.format_exception(result.exception))
    assert client.calls == [{"method": "auth_test"}]
    assert webhook.calls == []


def test_cli_counts_each_unmapped_occurrence_before_send_and_ignores_hidden_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    mapped = evidence("C0ENGME", "1760000000.000001")
    missing = evidence("C0ENGME", "1760000000.000002")
    repeated = item(1, (mapped, missing))
    selections = [
        selection(profile, repeated, hidden=4),
        selection(profile, repeated),
        selection(profile, item(2, (mapped,))),
    ]
    path = save_map(
        tmp_path,
        seed_map(
            messages={
                "C0ENGME:1760000000.000001": MappedMessage(
                    channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
                )
            }
        ),
    )
    events: list[str] = []
    webhook = FakeWebhookClient(events)
    prepare_cli(monkeypatch, world, selections, webhook)
    original_echo = cli.typer.echo

    def record_echo(message: object = None, **kwargs: Any) -> None:
        events.append(str(message))
        original_echo(message, **kwargs)

    monkeypatch.setattr(cli.typer, "echo", record_echo)

    result = runner.invoke(app, ["post", "--seed-map", str(path)])

    assert result.exit_code == 0, result.output
    count_line = "2 source links are not in the seed map"
    assert result.output.count(count_line) == 1
    assert events.index(count_line) < events.index("send")
    assert len(webhook.calls) == 3


def test_cli_prints_no_unmapped_count_when_every_source_is_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    path = save_map(
        tmp_path,
        seed_map(
            messages={
                "C0ENGME:1760000000.000001": MappedMessage(
                    channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
                )
            }
        ),
    )
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)

    result = runner.invoke(app, ["post", "--seed-map", str(path)])

    assert result.exit_code == 0, result.output
    assert "source links are not in the seed map" not in result.output
    assert len(webhook.calls) == 3

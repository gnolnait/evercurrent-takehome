from __future__ import annotations

import json
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
from digest.models import Evidence, Item
from digest.seed import (
    MappedFile,
    MappedMessage,
    SeedMap,
    channel_of,
    files_of,
    message_permalink,
)
from digest.world.load import load_world
from digest.world.schema import World
from slack_checks import build_digest

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


def block_text(message: dict[str, Any]) -> str:
    return "\n".join(
        block["text"]["text"]
        if block["type"] == "section"
        else "\n".join(element["text"] for element in block["elements"])
        for block in message["blocks"]
        if block["type"] in {"section", "context"}
    )


def dm_payloads(client: FakeSlackWebClient) -> list[dict[str, Any]]:
    return [
        {"text": call["text"], "blocks": call["blocks"]}
        for call in client.calls
        if call["method"] == "chat_postMessage"
    ]


def line_text(message: dict[str, Any]) -> str:
    blocks = message["blocks"]
    index = next(
        index
        for index, block in enumerate(blocks)
        if block["type"] == "section" and "Drawing released" in block["text"]["text"]
    )
    return blocks[index]["text"]["text"] + "\n" + blocks[index + 1]["elements"][0]["text"]


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
        events: list[str] | None = None,
    ) -> None:
        self.team_id = team_id
        self.auth_error = auth_error
        self.events = events
        self.calls: list[dict[str, Any]] = []

    def auth_test(self) -> dict[str, str]:
        if self.events is not None:
            self.events.append("auth_test")
        self.calls.append({"method": "auth_test"})
        if self.auth_error is not None:
            raise self.auth_error
        return {"team_id": self.team_id}

    def conversations_open(self, *, users: str) -> dict[str, dict[str, str]]:
        if self.events is not None:
            self.events.append("conversations_open")
        self.calls.append({"method": "conversations_open", "users": users})
        return {"channel": {"id": DM_CHANNEL}}

    def chat_postMessage(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        if self.events is not None:
            self.events.append("chat_postMessage")
        self.calls.append(
            {"method": "chat_postMessage", "channel": channel, "text": text, "blocks": blocks}
        )


def prepare_cli(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    selections: list[Selection],
    webhook: FakeWebhookClient,
) -> None:
    monkeypatch.setattr(cli, "_demo_selections", lambda day: (world, selections, {}))
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
            "C0ENGME:1760000000.000001/z&last.pdf": MappedFile(
                permalink="https://ws.slack.com/files/U1/F9/z.pdf"
            ),
            "C0ENGME:1760000000.000001/a<first>.step": MappedFile(
                permalink="https://ws.slack.com/files/U1/F1/a.step"
            ),
            "C0KPROG:1760000000.000099/unrelated.pdf": MappedFile(
                permalink="https://ws.slack.com/files/U1/NO/unrelated.pdf"
            ),
            "C0ENGME:1760000000.000001-not-a-child/prefix.pdf": MappedFile(
                permalink="https://ws.slack.com/files/U1/NO/prefix.pdf"
            ),
        },
    )

    text = line_text(
        build_digest(world, selection(profile, item(1, (root, reply))), links=links)[0]
    )

    expected = (
        "*Decision* · Open since Thu 9 Oct · "
        "<https://ws.slack.com/archives/C111/p1761000000000001|Thread> in <#C111> · "
        ":paperclip: <https://ws.slack.com/files/U1/F9/z.pdf|z&amp;last.pdf> "
        "<https://ws.slack.com/files/U1/F1/a.step|a&lt;first&gt;.step>"
    )
    assert text.splitlines()[-1] == expected
    assert text.count("<#C111>") == 1
    assert "unrelated.pdf" not in text
    assert "prefix.pdf" not in text


def test_mapped_sources_in_two_channels_render_channels_in_source_order_without_files(
    world: World, profile: Profile
) -> None:
    sources = (
        evidence("C0KPROG", "1760000000.000002"),
        evidence("C0ENGME", "1760000000.000001"),
    )
    links = seed_map(
        messages={
            "C0KPROG:1760000000.000002": MappedMessage(
                channel="C222", ts="1762000000.000001", thread_ts="1762000000.000001"
            ),
            "C0ENGME:1760000000.000001": MappedMessage(
                channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
            ),
        }
    )

    text = line_text(build_digest(world, selection(profile, item(1, sources)), links=links)[0])

    assert text.splitlines()[-1] == (
        "*Decision* · Open since Thu 9 Oct · "
        "<https://ws.slack.com/archives/C222/p1762000000000001|Thread> in <#C222>"
    )
    assert "<#C111>" not in text
    assert "files" not in text


def test_mixed_mapped_and_unmapped_sources_keep_source_order(
    world: World, profile: Profile
) -> None:
    mapped = evidence("C0KPROG", "1760000000.000002")
    unmapped = evidence("C0ENGME", "1760000000.000001", "https://world.slack.com/original")
    links = seed_map(
        messages={
            "C0KPROG:1760000000.000002": MappedMessage(
                channel="C222", ts="1762000000.000001", thread_ts="1762000000.000001"
            )
        }
    )

    text = line_text(
        build_digest(world, selection(profile, item(1, (mapped, unmapped))), links=links)[0]
    )

    assert text.splitlines()[-1] == (
        "*Decision* · Open since Thu 9 Oct · "
        "<https://ws.slack.com/archives/C222/p1762000000000001|Thread> in <#C222>"
    )
    assert "https://world.slack.com" not in text


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
            f"C0ENGME:1760000000.000001/{'z' * 3100}&<cut>.pdf": MappedFile(
                permalink="https://ws.slack.com/files/U1/F1/a.pdf"
            )
        },
    )
    selected = selection(
        profile,
        item(1, (source,), summary="A & B < C > D " + "x" * 2750),
    )

    message = build_digest(world, selected, links=links)[0]
    text = line_text(message)

    assert "A &amp; B &lt; C &gt; D" in text
    mapped_root = "<https://ws.slack.com/archives/C111/p1761000000000001|Thread>"
    assert mapped_root in text
    assert text.count(mapped_root) == 1
    assert "https://ws.slack.com/files/U1/F1/a.pdf" not in text
    assert len(text.splitlines()[-1]) <= 3000
    assert text.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert re.search(r"<[^>]*$|&[a-z]*$", text[:-1]) is None
    assert all(
        block["text"]["type"] == "mrkdwn" and block["text"]["verbatim"] is True
        for block in message["blocks"]
        if block["type"] == "section"
    )


def test_uncut_mapped_message_escapes_summary_and_file_name(world: World, profile: Profile) -> None:
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
    selected = selection(profile, item(1, (source,), summary="A & B < C > D"))

    text = line_text(build_digest(world, selected, links=links)[0])

    assert "A &amp; B &lt; C &gt; D" in text
    assert "<https://ws.slack.com/archives/C111/p1761000000000001|Thread>" in text
    assert "<https://ws.slack.com/files/U1/F1/a.pdf|a&amp;&lt;b&gt;.pdf>" in text
    assert "|Thread> in <#C111> · :paperclip:" in text


def test_message_permalink_handles_root_reply_and_absent_key() -> None:
    links = seed_map(
        messages={
            "C0ENGME:root": MappedMessage(channel="C111", ts="100.001", thread_ts="100.001"),
            "C0ENGME:reply": MappedMessage(channel="C111", ts="100.002", thread_ts="100.001"),
        }
    )

    assert message_permalink(links, "C0ENGME:root") == (
        "https://ws.slack.com/archives/C111/p100001"
    )
    assert message_permalink(links, "C0ENGME:reply") == (
        "https://ws.slack.com/archives/C111/p100002?thread_ts=100.001&cid=C111"
    )
    assert message_permalink(links, "C0ENGME:absent") is None


def test_channel_of_returns_mapped_channel_or_none() -> None:
    links = seed_map(
        messages={"C0ENGME:root": MappedMessage(channel="C111", ts="100.001", thread_ts="100.001")}
    )

    assert channel_of(links, "C0ENGME:root") == "C111"
    assert channel_of(links, "C0ENGME:absent") is None


def test_files_of_keeps_map_order_and_isolates_message_prefix() -> None:
    links = seed_map(
        files={
            "C0ENGME:root/z-last.pdf": MappedFile(permalink="https://files/z"),
            "C0ENGME:root/a-first.pdf": MappedFile(permalink="https://files/a"),
            "C0ENGME:rootish/not-a-child.pdf": MappedFile(permalink="https://files/no"),
            "C0KPROG:root/unrelated.pdf": MappedFile(permalink="https://files/unrelated"),
        }
    )

    assert files_of(links, "C0ENGME:root") == [
        ("z-last.pdf", "https://files/z"),
        ("a-first.pdf", "https://files/a"),
    ]
    assert files_of(links, "C0ENGME:absent") == []


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
    assert dm_payloads(client) == [build_digest(world, selected)[0]] * 3
    assert webhook.calls == []


@pytest.mark.parametrize(
    ("map_case", "reason"),
    [
        ("missing", "seed-map.json"),
        ("malformed", "seed-map.json"),
        ("other-world", "another world"),
    ],
)
@pytest.mark.parametrize("delivery", ["webhook", "dm"])
def test_cli_rejects_invalid_seed_map_before_build_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    map_case: str,
    reason: str,
    delivery: str,
) -> None:
    path = tmp_path / "seed-map.json"
    if map_case == "malformed":
        path.write_text("not json", encoding="utf-8")
    elif map_case == "other-world":
        seed_map(world="another-world").save(path)
    builds: list[int] = []
    sends = FakeWebhookClient()
    client = FakeSlackWebClient()
    monkeypatch.setattr(cli, "_demo_selections", lambda day: builds.append(day))
    monkeypatch.setattr(cli, "_webhook_url", lambda: WEBHOOK_URL)
    monkeypatch.setattr(cli, "_webhook_client", lambda url: sends)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)
    arguments = ["post", "--seed-map", str(path)]
    if delivery == "dm":
        arguments[1:1] = ["--dm", "U012ABC"]

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1, result.output
    assert reason in result.output
    if map_case == "other-world":
        assert "another-world" not in result.output
    assert builds == []
    assert client.calls == []
    assert sends.calls == []


@pytest.mark.parametrize("delivery", ["webhook", "dm"])
@pytest.mark.parametrize(
    "map_case",
    [
        "dotenv",
        "invalid-field",
        "token-message-key",
        "token-file-key",
        "token-channel-key",
    ],
)
def test_cli_invalid_seed_map_never_discloses_file_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
    map_case: str,
) -> None:
    path = tmp_path / "seed-map.json"
    if map_case == "dotenv":
        content = f"BOT_TOKEN={BOT_TOKEN}"
    else:
        content_value = seed_map().model_dump(mode="json")
        if map_case == "invalid-field":
            content_value["version"] = BOT_TOKEN
        elif map_case == "token-channel-key":
            content_value["channels"] = {BOT_TOKEN: 5}
        else:
            field = "messages" if map_case == "token-message-key" else "files"
            content_value[field] = {BOT_TOKEN: {}}
        content = json.dumps(content_value)
    path.write_text(content, encoding="utf-8")
    builds: list[int] = []
    webhook = FakeWebhookClient()
    client = FakeSlackWebClient()
    monkeypatch.setattr(cli, "_demo_selections", lambda day: builds.append(day))
    monkeypatch.setattr(cli, "_webhook_url", lambda: WEBHOOK_URL)
    monkeypatch.setattr(cli, "_webhook_client", lambda url: webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)
    arguments = ["post", "--seed-map", str(path)]
    if delivery == "dm":
        arguments[1:1] = ["--dm", "U012ABC"]

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1, result.output
    assert BOT_TOKEN not in result.output
    assert BOT_TOKEN not in "".join(traceback.format_exception(result.exception))
    assert builds == []
    assert client.calls == []
    assert webhook.calls == []


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
    reason = result.output.lower()
    assert "team" in reason or ("t-other" in reason and "t-actual" in reason)
    assert client.calls == [{"method": "auth_test"}]
    assert webhook.calls == []


def test_cli_dm_team_mismatch_never_discloses_map_team_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    path = save_map(tmp_path, seed_map(team_id=BOT_TOKEN))
    client = FakeSlackWebClient(team_id="T-ACTUAL")
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--seed-map", str(path)])

    assert result.exit_code == 1, result.output
    assert "team" in result.output.lower()
    assert BOT_TOKEN not in result.output
    assert client.calls == [{"method": "auth_test"}]
    assert webhook.calls == []


@pytest.mark.parametrize(
    "error",
    [
        SlackApiError(f"auth failed with {BOT_TOKEN}", {"error": "invalid_auth"}),
        RuntimeError(f"auth failed with {BOT_TOKEN}"),
    ],
    ids=["slack-api-error", "generic-error"],
)
def test_cli_dm_auth_failure_is_safe_and_opens_no_dm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
    error: Exception,
) -> None:
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    path = save_map(tmp_path, seed_map())
    client = FakeSlackWebClient(auth_error=error)
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_env_source", lambda key: "environment")
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--seed-map", str(path)])

    assert result.exit_code == 1, result.output
    assert BOT_TOKEN not in result.output
    assert BOT_TOKEN not in "".join(traceback.format_exception(result.exception))
    assert client.calls == [{"method": "auth_test"}]
    assert webhook.calls == []


def test_cli_dm_with_matching_team_sends_three_mapped_payloads_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    source = evidence("C0ENGME", "1760000000.000001")
    selections = [selection(profile, item(number, (source,))) for number in range(1, 4)]
    path = save_map(
        tmp_path,
        seed_map(
            team_id="T-ACTUAL",
            messages={
                "C0ENGME:1760000000.000001": MappedMessage(
                    channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
                )
            },
            files={
                "C0ENGME:1760000000.000001/z&drawing.step": MappedFile(
                    permalink="https://ws.slack.com/files/U1/F9/drawing.step"
                )
            },
        ),
    )
    loaded = SeedMap.load(path)
    assert loaded is not None
    client = FakeSlackWebClient(team_id="T-ACTUAL")
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, selections, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--seed-map", str(path)])

    assert result.exit_code == 0, result.output
    assert [call["method"] for call in client.calls] == [
        "auth_test",
        "conversations_open",
        "chat_postMessage",
        "chat_postMessage",
        "chat_postMessage",
    ]
    assert client.calls[1] == {"method": "conversations_open", "users": "U012ABC"}
    assert all(call["channel"] == DM_CHANNEL for call in client.calls[2:])
    assert dm_payloads(client) == [
        message
        for selected in selections
        for message in build_digest(world, selected, links=loaded)
    ]
    delivered = block_text(dm_payloads(client)[0])
    assert "https://ws.slack.com/archives/C111/p1761000000000001" in delivered
    assert "https://ws.slack.com/files/U1/F9/drawing.step" in delivered
    assert "<#C111>" in delivered
    assert webhook.calls == []


def test_cli_counts_every_unmapped_occurrence_before_the_block_cut_and_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    mapped = evidence("C0ENGME", "1760000000.000001")
    missing = (
        evidence("C0ENGME", "1760000000.000002", "https://world.slack.com/missing-2"),
        evidence("C0ENGME", "1760000000.000003", "https://world.slack.com/missing-3"),
        evidence("C0ENGME", "1760000000.000004", "https://world.slack.com/missing-4"),
    )
    repeated = item(1, (mapped, *missing))
    filler = [item(number, (mapped,)) for number in range(100, 148)]
    late_missing = (
        evidence("C0ENGME", "1760000000.000005", "https://world.slack.com/late-5"),
        evidence("C0ENGME", "1760000000.000006", "https://world.slack.com/late-6"),
    )
    selections = [
        selection(profile, repeated),
        selection(profile, repeated),
        selection(profile, *filler, item(148, late_missing)),
    ]
    path = save_map(
        tmp_path,
        seed_map(
            messages={
                "C0ENGME:1760000000.000001": MappedMessage(
                    channel="C111", ts="1761000000.000001", thread_ts="1761000000.000001"
                )
            },
            files={
                "C0ENGME:1760000000.000001/z&drawing.step": MappedFile(
                    permalink="https://ws.slack.com/files/U1/F9/drawing.step"
                )
            },
        ),
    )
    loaded = SeedMap.load(path)
    assert loaded is not None
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
    count_line = "8 source links are not in the seed map"
    assert result.output.count(count_line) == 1
    assert events.index(count_line) < events.index("send")
    assert webhook.calls == [
        message
        for selected in selections
        for message in build_digest(world, selected, links=loaded, threaded=False)
    ]
    delivered = block_text(webhook.calls[0])
    assert "https://ws.slack.com/archives/C111/p1761000000000001" in delivered
    assert "https://ws.slack.com/files/U1/F9/drawing.step" in delivered
    assert "<#C111>" in delivered
    cut_payload = block_text(webhook.calls[2])
    assert "https://world.slack.com/late-5" not in cut_payload
    assert "https://world.slack.com/late-6" not in cut_payload


def test_cli_dm_reports_three_unmapped_sources_before_destination_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
) -> None:
    selections = [
        selection(
            profile,
            item(
                number,
                (
                    evidence(
                        "C0ENGME",
                        f"1760000000.00000{number}",
                        f"https://world.slack.com/missing-{number}",
                    ),
                ),
            ),
        )
        for number in range(1, 4)
    ]
    path = save_map(tmp_path, seed_map(team_id="T-ACTUAL"))
    events: list[str] = []
    client = FakeSlackWebClient(team_id="T-ACTUAL", events=events)
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, selections, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)
    original_echo = cli.typer.echo

    def record_echo(message: object = None, **kwargs: Any) -> None:
        events.append(str(message))
        original_echo(message, **kwargs)

    monkeypatch.setattr(cli.typer, "echo", record_echo)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--seed-map", str(path)])

    assert result.exit_code == 0, result.output
    count_line = "3 source links are not in the seed map"
    assert result.output.count(count_line) == 1
    destination = next(event for event in events if "sending" in event.lower())
    assert events.index(count_line) < events.index(destination)
    assert events.index(count_line) < events.index("conversations_open")
    assert [call["method"] for call in client.calls] == [
        "auth_test",
        "conversations_open",
        "chat_postMessage",
        "chat_postMessage",
        "chat_postMessage",
    ]
    assert webhook.calls == []


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
            },
            files={
                "C0ENGME:1760000000.000001/z&drawing.step": MappedFile(
                    permalink="https://ws.slack.com/files/U1/F9/drawing.step"
                )
            },
        ),
    )
    loaded = SeedMap.load(path)
    assert loaded is not None
    webhook = FakeWebhookClient()
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)

    result = runner.invoke(app, ["post", "--seed-map", str(path)])

    assert result.exit_code == 0, result.output
    assert "source links are not in the seed map" not in result.output
    assert webhook.calls == [build_digest(world, selected, links=loaded, threaded=False)[0]] * 3
    delivered = block_text(webhook.calls[0])
    assert "https://ws.slack.com/archives/C111/p1761000000000001" in delivered
    assert "https://ws.slack.com/files/U1/F9/drawing.step" in delivered
    assert "<#C111>" in delivered


@pytest.mark.parametrize(
    "field, nested",
    [
        ("team_id", None),
        ("workspace_url", None),
        ("channels", None),
        ("messages", "channel"),
        ("messages", "ts"),
        ("messages", "thread_ts"),
        ("files", "permalink"),
    ],
)
@pytest.mark.parametrize("delivery", ["webhook", "dm"])
def test_cli_seed_map_rejects_values_that_can_inject_slack_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
    field: str,
    nested: str | None,
    delivery: str,
) -> None:
    """A25: reject each unsafe field before Slack sees a call."""
    content: dict[str, Any] = {
        "team_id": "T123ABC",
        "workspace_url": "https://scratch.slack.com",
        "world": "kestrel",
        "channels": {"C0ENGME": "C123ABC"},
        "messages": {
            "C0ENGME:1760000000.000001": {
                "channel": "C123ABC",
                "ts": "1761000000.000001",
                "thread_ts": "1761000000.000001",
            }
        },
        "files": {
            "C0ENGME:1760000000.000001/drawing.pdf": {
                "permalink": "https://scratch.slack.com/files/U1/F1/drawing.pdf"
            }
        },
    }
    bad = "<!here>unsafe>"
    if nested is not None:
        entry = next(iter(content[field].values()))
        entry[nested] = bad
    elif field == "channels":
        content[field]["C0ENGME"] = bad
    else:
        content[field] = bad
    path = tmp_path / "seed-map.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    webhook = FakeWebhookClient()
    client = FakeSlackWebClient(team_id="T123ABC")
    selected = selection(profile, item(1, (evidence("C0ENGME", "1760000000.000001"),)))
    prepare_cli(monkeypatch, world, [selected] * 3, webhook)
    monkeypatch.setattr(cli, "_env_value", lambda key: BOT_TOKEN)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)
    arguments = ["post", "--seed-map", str(path)]
    if delivery == "dm":
        arguments[1:1] = ["--dm", "U012ABC"]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 1, result.output
    assert field in result.output
    assert bad not in result.output
    assert client.calls == []
    assert webhook.calls == []


def test_seed_map_in_real_form_loads(tmp_path: Path) -> None:
    """A25: valid Slack values load without a change."""
    content = {
        "team_id": "T123ABC",
        "workspace_url": "https://scratch-123.slack.com",
        "world": "kestrel",
        "channels": {"C0ENGME": "C123ABC"},
        "messages": {
            "C0ENGME:1760000000.000001": {
                "channel": "C123ABC",
                "ts": "1761000000.000002",
                "thread_ts": "1761000000.000001",
            }
        },
        "files": {
            "C0ENGME:1760000000.000001/drawing.pdf": {
                "permalink": "https://scratch-123.slack.com/files/U1/F1/drawing_rev-1.2%20final.pdf"
            }
        },
    }
    path = tmp_path / "seed-map.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    loaded = SeedMap.load(path)
    assert loaded.team_id == content["team_id"]
    assert loaded.workspace_url == content["workspace_url"]
    assert loaded.channels == content["channels"]
    assert {key: value.model_dump() for key, value in loaded.messages.items()} == content[
        "messages"
    ]
    assert {key: value.model_dump() for key, value in loaded.files.items()} == content["files"]

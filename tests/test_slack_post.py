from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
from typer.testing import CliRunner

import digest.cli as cli
from digest.cli import app
from digest.daily.profile import Profile
from digest.daily.render import render_digest
from digest.daily.select import Line, Section, Selection
from digest.daily.slack import slack_message
from digest.extract.stage import ExtractReport
from digest.extract.verify import DROP_FIELDS
from digest.models import Evidence, Item
from digest.project import Gate
from digest.store import Store
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_PATH = ROOT / "data" / "world" / "kestrel" / "world.yaml"
DAY = date(2026, 10, 14)
PHASE = (
    "Phase: EVT since 2026-08-03. Next gate: G-TR1 "
    "Top-cover tooling and texture release (PN-51010), in 4 working days."
)
TS = "1760000000.000001"
WORKSPACE = "https://kestrel-k2.slack.com"
WEBHOOK_URL = "https://hooks.slack.com/services/secret-value"
# Build fake tokens from parts to keep the secret scanner quiet.
BOT_TOKEN = "xox" + "b-" + "bot-secret-value"
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


def block_text(block: dict[str, Any]) -> str:
    text = block.get("text")
    assert isinstance(text, dict)
    value = text.get("text")
    assert isinstance(value, str)
    return value


def mrkdwn_text_objects(value: Any) -> list[dict[str, Any]]:
    """Return all mrkdwn text objects nested in a Block Kit value."""
    if isinstance(value, dict):
        found = [value] if value.get("type") == "mrkdwn" else []
        return found + [item for child in value.values() for item in mrkdwn_text_objects(child)]
    if isinstance(value, list):
        return [item for child in value for item in mrkdwn_text_objects(child)]
    return []


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
    assert message["text"] == header + "\nFirst digest: the last 5 working days\n" + PHASE
    assert message["blocks"][0]["type"] == "header"
    assert message["blocks"][0]["text"]["type"] == "plain_text"
    assert block_text(message["blocks"][0]) == header
    blocks = message["blocks"][1:]
    assert [block["type"] for block in blocks] == ["section"] * 5
    texts = [block_text(block) for block in blocks]
    assert texts[0] == "First digest: the last 5 working days\n" + PHASE
    assert texts[1] == "*Decisions*"
    assert "*Drawing released*" in texts[2]
    assert "*[must see]*" in texts[2]
    assert f"<{evidence(1).permalink}|source>" in texts[2]
    assert f"<{evidence(2).permalink}|source>" in texts[2]
    assert texts[3] == "*Risks*"
    assert "*Tooling risk*" in texts[4]


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        (None, "First digest: the last 3 working days"),
        (date(2026, 10, 13), "Since your digest of 2026-10-13"),
    ],
    ids=["first-digest", "previous-digest"],
)
@pytest.mark.parametrize("empty", [False, True], ids=["with-lines", "empty-day"])
def test_slack_window_matches_terminal_as_verbatim_section(
    world: World, profile: Profile, previous: date | None, expected: str, empty: bool
) -> None:
    selected = Selection(
        profile=profile,
        day=DAY,
        lines=() if empty else (line(item()),),
        hidden=0,
        previous_digest=previous,
        first_working_days=3,
    )

    message = slack_message(world, selected)
    terminal_window = render_digest(world, selected).splitlines()[1]

    assert terminal_window == expected
    assert message["blocks"][1] == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": terminal_window + "\n" + PHASE, "verbatim": True},
    }
    if empty:
        assert len(message["blocks"]) == 3
        assert block_text(message["blocks"][2]) == "Nothing new for Nadia Brenner."


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        (None, "First digest: the last 3 working days"),
        (date(2026, 10, 13), "Since your digest of 2026-10-13"),
    ],
    ids=["first-digest", "previous-digest"],
)
def test_slack_fallback_has_escaped_header_then_window(
    world: World, profile: Profile, previous: date | None, expected: str
) -> None:
    selected = Selection(
        profile=profile.model_copy(update={"name": "Nadia & <ME>"}),
        day=DAY,
        lines=(),
        hidden=0,
        previous_digest=previous,
        first_working_days=3,
    )

    message = slack_message(world, selected)

    assert message["text"] == (
        "Daily digest for Nadia &amp; &lt;ME&gt; (ME) - 2026-10-14\n" + expected + "\n" + PHASE
    )


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

    assert len(message["blocks"]) == 4
    rendered = block_text(message["blocks"][3])
    assert "*Alert &amp; &lt;@U123&gt; &gt;*" in rendered
    assert "Do not open &lt;https://x|y&gt; &amp; wait &gt; now." in rendered
    assert "<@U123>" not in rendered
    assert "<https://x|y>" not in rendered
    assert re.findall(r"<[^>]+>", rendered) == [f"<{evidence().permalink}|source>"]


def test_slack_message_sets_verbatim_on_every_mrkdwn_text_object(
    world: World, profile: Profile
) -> None:
    selected = Selection(
        profile=profile,
        day=DAY,
        lines=(line(item()),),
        hidden=1,
    )

    message = slack_message(world, selected)

    text_objects = mrkdwn_text_objects(message)
    assert text_objects
    assert all(text.get("verbatim") is True for text in text_objects)


def test_slack_message_cuts_a_long_line_with_ellipsis(world: World, profile: Profile) -> None:
    selected = selection(profile, line(item(summary="x" * 4000)))

    message = slack_message(world, selected)

    assert len(message["blocks"]) == 4
    rendered = block_text(message["blocks"][3])
    assert len(rendered) <= 3000
    assert rendered.endswith("\N{HORIZONTAL ELLIPSIS}")


@pytest.mark.parametrize(
    ("count", "hidden", "kept", "omitted"),
    [(47, 0, 47, 0), (48, 0, 46, 2), (51, 0, 46, 5), (46, 2, 46, 0), (47, 2, 45, 2)],
    ids=["exactly-50", "one-over", "51-lines", "exactly-50-with-footer", "one-over-with-footer"],
)
def test_slack_message_limits_blocks_and_reports_omitted_lines(
    world: World, profile: Profile, count: int, hidden: int, kept: int, omitted: int
) -> None:
    lines = tuple(line(item(number, title=f"Decision {number}")) for number in range(1, count + 1))
    selected = Selection(profile=profile, day=DAY, lines=lines, hidden=hidden)

    message = slack_message(world, selected)

    assert len(message["blocks"]) == 50
    texts = [block_text(block) for block in message["blocks"]]
    assert [text.split("*", 2)[1] for text in texts if text.startswith("*Decision ")] == [
        f"Decision {number}" for number in range(1, kept + 1)
    ]
    assert texts[2] == "*Decisions*"
    assert [text.split("*", 2)[1] for text in texts[3 : 3 + kept]] == [
        f"Decision {number}" for number in range(1, kept + 1)
    ]
    footer = []
    if hidden:
        footer.append("2 items hidden by the visibility filter.")
    if omitted:
        footer.append(f"{omitted} lines left out.")
    assert texts[3 + kept :] == footer
    assert texts[1] == "First digest: the last 5 working days\n" + PHASE


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


class FakeSlackWebClient:
    def __init__(
        self,
        post_errors: dict[int, Exception] | None = None,
        *,
        open_error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.post_errors = post_errors or {}
        self.open_error = open_error

    def conversations_open(self, *, users: str) -> dict[str, dict[str, str]]:
        self.calls.append({"method": "conversations_open", "users": users})
        if self.open_error is not None:
            raise self.open_error
        return {"channel": {"id": DM_CHANNEL}}

    def chat_postMessage(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        post_number = sum(call["method"] == "chat_postMessage" for call in self.calls) + 1
        self.calls.append(
            {
                "method": "chat_postMessage",
                "channel": channel,
                "text": text,
                "blocks": blocks,
            }
        )
        error = self.post_errors.get(post_number)
        if error is not None:
            raise error


class RaisingGetResponse:
    def get(self, key: str) -> str:
        raise RuntimeError(f"response lookup exposed {BOT_TOKEN}")


def successful_extract(store: Store, *args: object, **kwargs: object) -> ExtractReport:
    end = args[4]
    assert isinstance(end, datetime)
    for key, value in {
        "world": "kestrel",
        "timezone": "America/Los_Angeles",
        "workspace_url": WORKSPACE,
        "until": end.isoformat(),
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
    monkeypatch.setattr(cli, "_env_value", lambda key: None)
    monkeypatch.setattr(cli, "_webhook_url", lambda: webhook_url)
    monkeypatch.setattr(cli, "_extract_days", extract)
    monkeypatch.setattr("digest.daily.render.detect_absences", lambda *args, **kwargs: [])

    def client_for(url: str) -> FakeWebhookClient:
        assert url == webhook_url
        return client

    monkeypatch.setattr(cli, "_webhook_client", client_for)


def prepare_dm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: FakeSlackWebClient,
    *,
    token: str | None = BOT_TOKEN,
    extract: Callable[..., ExtractReport] = successful_extract,
) -> FakeWebhookClient:
    webhook_client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, webhook_client, extract=extract)
    monkeypatch.setattr(
        cli,
        "_env_value",
        lambda key: token if key == "SLACK_BOT_TOKEN" else None,
    )

    def client_for(value: str) -> FakeSlackWebClient:
        assert value == token
        return client

    monkeypatch.setattr(cli, "_slack_web_client", client_for)
    return webhook_client


def test_cli_post_dm_opens_one_conversation_and_sends_three_readers_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
) -> None:
    client = FakeSlackWebClient()
    webhook_client = prepare_dm(tmp_path, monkeypatch, client)
    selections: list[Selection] = []
    select_digest = cli.select_digest

    def capture_selection(*args: Any, **kwargs: Any) -> Selection:
        selected = select_digest(*args, **kwargs)
        selections.append(selected)
        return selected

    monkeypatch.setattr(cli, "select_digest", capture_selection)

    result = runner.invoke(app, ["post", "--dm", "U012ABC", "--day", "5"])

    assert result.exit_code == 0, result.output
    assert [selected.profile.role for selected in selections] == ["ME", "supply_chain", "EM"]
    assert client.calls == [
        {"method": "conversations_open", "users": "U012ABC"},
        *[
            {
                "method": "chat_postMessage",
                "channel": DM_CHANNEL,
                **slack_message(world, selected),
            }
            for selected in selections
        ],
    ]
    assert webhook_client.calls == []


@pytest.mark.parametrize(
    ("token", "withheld_fragments"),
    [
        # Build fake tokens from parts to keep the secret scanner quiet.
        ("not" + "xox" + "b-" + "SENSITIVE-CREDENTIAL", ("SENSITIVE", "CREDENTIAL")),
        (None, ()),
    ],
    ids=["wrong-prefix", "missing"],
)
def test_cli_post_dm_rejects_invalid_bot_token_before_build_without_leaking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    withheld_fragments: tuple[str, ...],
) -> None:
    client = FakeSlackWebClient()
    builds: list[None] = []

    def record_extract(store: Store, *args: object, **kwargs: object) -> ExtractReport:
        builds.append(None)
        return successful_extract(store, *args, **kwargs)

    webhook_client = prepare_dm(
        tmp_path,
        monkeypatch,
        client,
        token=token,
        extract=record_extract,
    )

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "SLACK_BOT_TOKEN" in result.output
    assert builds == []
    assert client.calls == []
    assert webhook_client.calls == []
    assert all(fragment not in result.output for fragment in withheld_fragments)


def test_cli_post_dm_rejects_invalid_user_id_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeSlackWebClient()
    builds: list[None] = []

    def record_extract(store: Store, *args: object, **kwargs: object) -> ExtractReport:
        builds.append(None)
        return successful_extract(store, *args, **kwargs)

    webhook_client = prepare_dm(tmp_path, monkeypatch, client, extract=record_extract)

    result = runner.invoke(app, ["post", "--dm", "U012-ABC"])

    assert result.exit_code == 1, result.output
    assert "--dm" in result.output
    assert builds == []
    assert client.calls == []
    assert webhook_client.calls == []


def test_cli_post_dm_open_slack_api_error_has_no_token_in_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = SlackApiError(
        f"open failed with {BOT_TOKEN}",
        {"error": "missing_scope"},
    )
    client = FakeSlackWebClient(open_error=error)
    webhook_client = prepare_dm(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "cannot open the DM with U012ABC: missing_scope" in result.output
    assert client.calls == [{"method": "conversations_open", "users": "U012ABC"}]
    assert webhook_client.calls == []
    exception_chain = "".join(traceback.format_exception(result.exception))
    assert BOT_TOKEN not in exception_chain


def test_cli_post_dm_stops_after_slack_api_error_and_reports_only_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = SlackApiError(
        f"post failed with {BOT_TOKEN}",
        {"error": "missing_scope"},
    )
    client = FakeSlackWebClient({2: error})
    webhook_client = prepare_dm(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "cannot post the digest for Hamid Rostami: missing_scope" in result.output
    assert sum(call["method"] == "chat_postMessage" for call in client.calls) == 2
    assert webhook_client.calls == []
    exception_chain = "".join(traceback.format_exception(result.exception))
    for sensitive in (BOT_TOKEN, "bot-secret-value"):
        assert sensitive not in result.output
        assert sensitive not in exception_chain


def test_cli_post_dm_reports_arbitrary_post_exception_without_leaking_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeSlackWebClient({1: LookupError(f"post failed with {BOT_TOKEN}")})
    webhook_client = prepare_dm(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "cannot post the digest for Nadia Brenner: LookupError" in result.output
    assert sum(call["method"] == "chat_postMessage" for call in client.calls) == 1
    assert webhook_client.calls == []
    exception_chain = "".join(traceback.format_exception(result.exception))
    for sensitive in (BOT_TOKEN, "bot-secret-value"):
        assert sensitive not in result.output
        assert sensitive not in exception_chain


@pytest.mark.parametrize(
    "response",
    [
        [BOT_TOKEN],
        BOT_TOKEN,
        None,
        RaisingGetResponse(),
    ],
    ids=["list", "string", "none", "raising-get"],
)
def test_slack_failure_handles_any_response_without_leaking_token(response: Any) -> None:
    error = SlackApiError(f"post failed with {BOT_TOKEN}", response)

    failure = cli._slack_failure(error)

    assert len(failure) <= 80
    assert "\n" not in failure
    assert BOT_TOKEN not in failure


@pytest.mark.parametrize(
    "response",
    [
        [BOT_TOKEN],
        BOT_TOKEN,
        None,
        RaisingGetResponse(),
    ],
    ids=["list", "string", "none", "raising-get"],
)
def test_cli_post_dm_malformed_slack_response_has_safe_refusal_and_exception_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
) -> None:
    error = SlackApiError(f"post failed with {BOT_TOKEN}", response)
    client = FakeSlackWebClient({1: error})
    prepare_dm(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "cannot post the digest for Nadia Brenner: SlackApiError" in result.output
    assert len(result.output) <= 300
    exception_chain = "".join(traceback.format_exception(result.exception))
    assert BOT_TOKEN not in result.output
    assert BOT_TOKEN not in exception_chain


def test_slack_web_client_retries_rate_limits() -> None:
    client = cli._slack_web_client(BOT_TOKEN)

    assert any(isinstance(handler, RateLimitErrorRetryHandler) for handler in client.retry_handlers)


@pytest.mark.parametrize(
    ("use_dm", "destination"),
    [(True, "DM"), (False, "webhook")],
    ids=["dm", "webhook"],
)
def test_cli_post_announces_count_and_destination_before_first_send_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_dm: bool,
    destination: str,
) -> None:
    events: list[str] = []

    class TrackingWebhookClient(FakeWebhookClient):
        def send(self, *, text: str, blocks: list[dict[str, Any]]) -> FakeResponse:
            events.append("send")
            return super().send(text=text, blocks=blocks)

    class TrackingSlackWebClient(FakeSlackWebClient):
        def chat_postMessage(
            self,
            *,
            channel: str,
            text: str,
            blocks: list[dict[str, Any]],
        ) -> None:
            events.append("send")
            super().chat_postMessage(channel=channel, text=text, blocks=blocks)

    original_echo = cli.typer.echo

    def record_echo(message: object = None, **kwargs: Any) -> None:
        events.append(str(message))
        original_echo(message, **kwargs)

    monkeypatch.setattr(cli.typer, "echo", record_echo)
    if use_dm:
        prepare_dm(tmp_path, monkeypatch, TrackingSlackWebClient())
        arguments = ["post", "--dm", "U012ABC"]
    else:
        prepare_post(tmp_path, monkeypatch, TrackingWebhookClient())
        arguments = ["post"]

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output
    announcement = events[0]
    assert announcement != "send"
    assert re.search(r"\b3\b", announcement)
    assert destination in announcement
    assert WEBHOOK_URL not in announcement
    assert BOT_TOKEN not in announcement


def test_cli_post_dm_partial_failure_explains_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = SlackApiError("second post failed", {"error": "channel_not_found"})
    client = FakeSlackWebClient({2: error})
    prepare_dm(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "1 digest was already sent" in result.output
    assert "rerun sends all 3 again" in result.output


@pytest.mark.parametrize(
    ("arguments", "variable"),
    [
        (["post", "--dm", "U012ABC"], "SLACK_BOT_TOKEN"),
        (["post"], "SLACK_WEBHOOK_URL"),
    ],
    ids=["dm", "webhook"],
)
def test_cli_post_missing_credential_names_dotenv_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    variable: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(variable, False)

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1, result.output
    assert variable in result.output
    assert str(tmp_path) in result.output


@pytest.mark.parametrize("source", ["environment", ".env"])
def test_cli_post_dm_invalid_auth_names_token_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
    source: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLACK_BOT_TOKEN", False)
    if source == "environment":
        monkeypatch.setenv("SLACK_BOT_TOKEN", BOT_TOKEN)
    else:
        (tmp_path / ".env").write_text(
            f"SLACK_BOT_TOKEN={BOT_TOKEN}\n",
            encoding="utf-8",
        )
    selected = selection(profile, line(item()))
    monkeypatch.setattr(cli, "_demo_selections", lambda day: (world, [selected] * 3))
    error = SlackApiError("authentication failed", {"error": "invalid_auth"})
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: FakeSlackWebClient({1: error}))

    result = runner.invoke(app, ["post", "--dm", "U012ABC"])

    assert result.exit_code == 1, result.output
    assert "invalid_auth" in result.output
    assert f"token from {source}" in result.output
    assert BOT_TOKEN not in result.output


def test_cli_post_sends_three_readers_in_order_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    world: World,
) -> None:
    client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, client)
    selections: list[Selection] = []
    select_digest = cli.select_digest

    def capture_selection(*args: Any, **kwargs: Any) -> Selection:
        selected = select_digest(*args, **kwargs)
        selections.append(selected)
        return selected

    monkeypatch.setattr(cli, "select_digest", capture_selection)
    caplog.set_level(logging.DEBUG)

    result = runner.invoke(app, ["post", "--day", "5"])

    assert result.exit_code == 0, result.output
    assert client.calls == [slack_message(world, selected) for selected in selections]
    phase = (
        "\nPhase: EVT since 2026-08-03. Next gate: G-TR1 "
        "Top-cover tooling and texture release (PN-51010), in 2 working days."
    )
    assert [call["text"] for call in client.calls] == [
        "Daily digest for Nadia Brenner (ME) - 2026-10-16\nSince your digest of 2026-10-15" + phase,
        "Daily digest for Hamid Rostami (supply_chain) - 2026-10-16\n"
        "Since your digest of 2026-10-15" + phase,
        "Daily digest for Grace Okonkwo (EM) - 2026-10-16\nSince your digest of 2026-10-15" + phase,
    ]
    assert WEBHOOK_URL not in result.output
    assert all(WEBHOOK_URL not in str(vars(record)) for record in caplog.records)


def test_cli_post_without_webhook_url_exits_one_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, client, webhook_url=None)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "SLACK_WEBHOOK_URL" in result.output
    assert client.calls == []


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ftp://hooks.slack.test/services/SECRET",
        "https://hooks.slack.com/services/SECRET value",
    ],
    ids=["wrong-scheme-and-host", "space"],
)
def test_cli_post_rejects_invalid_webhook_url_before_build_without_leaking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_url: str,
) -> None:
    client = FakeWebhookClient()
    builds: list[None] = []

    def record_extract(store: Store, *args: object, **kwargs: object) -> ExtractReport:
        builds.append(None)
        return successful_extract(store, *args, **kwargs)

    prepare_post(
        tmp_path,
        monkeypatch,
        client,
        extract=record_extract,
        webhook_url=invalid_url,
    )

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "SLACK_WEBHOOK_URL" in result.output
    assert builds == []
    assert client.calls == []
    assert invalid_url not in result.output
    assert "SECRET" not in result.output


def test_webhook_url_reads_cwd_dotenv_and_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv_url = "https://hooks.slack.com/services/from-dotenv"
    environment_url = "https://hooks.slack.com/services/from-environment"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", False)
    (tmp_path / ".env").write_text(f"SLACK_WEBHOOK_URL={dotenv_url}\n", encoding="utf-8")

    assert cli._webhook_url() == dotenv_url

    monkeypatch.setenv("SLACK_WEBHOOK_URL", environment_url)
    assert cli._webhook_url() == environment_url


@pytest.mark.parametrize(
    ("encoding", "suffix"),
    [
        ("utf-8-sig", ""),
        ("utf-16", ""),
        ("utf-8", " # comment"),
    ],
    ids=["utf-8-bom", "utf-16-bom", "inline-comment"],
)
def test_webhook_url_reads_supported_dotenv_encodings_and_inline_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    suffix: str,
) -> None:
    dotenv_url = "https://hooks.slack.com/services/from-dotenv"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", False)
    (tmp_path / ".env").write_text(
        f"SLACK_WEBHOOK_URL={dotenv_url}{suffix}\n",
        encoding=encoding,
    )

    assert cli._webhook_url() == dotenv_url


def test_cli_post_names_unreadable_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", False)
    (tmp_path / ".env").write_bytes(b"\xff\xfe\x00")

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert ".env" in result.output


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


@pytest.mark.parametrize(
    ("status_code", "body", "withheld_fragments"),
    [
        (400, "Rejected /services/T000/B000/SECRET", ("SECRET", "/services/")),
        (
            500,
            "<html><body>Internal Server Error</body></html>",
            ("<html>", "Internal Server Error"),
        ),
    ],
    ids=["webhook-path", "html"],
)
def test_cli_post_withholds_non_code_response_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: str,
    withheld_fragments: tuple[str, ...],
) -> None:
    client = FakeWebhookClient([FakeResponse(status_code, body)])
    prepare_post(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "Nadia Brenner" in result.output
    assert str(status_code) in result.output
    assert "body withheld" in result.output.lower()
    assert all(fragment not in result.output for fragment in withheld_fragments)
    assert len(client.calls) == 1


def test_cli_post_withholds_error_code_that_appears_in_webhook_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    webhook_url = "https://hooks.slack.com/services/abc/def/secretword"
    client = FakeWebhookClient([FakeResponse(400, "secretword")])
    prepare_post(tmp_path, monkeypatch, client, webhook_url=webhook_url)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "Nadia Brenner" in result.output
    assert "400" in result.output
    assert "body withheld" in result.output.lower()
    assert "secretword" not in result.output
    assert len(client.calls) == 1


def test_cli_post_reports_arbitrary_send_exception_without_leaking_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RaisingWebhookClient(FakeWebhookClient):
        def send(self, *, text: str, blocks: list[dict[str, Any]]) -> FakeResponse:
            raise LookupError(f"send failed for {WEBHOOK_URL}")

    client = RaisingWebhookClient()
    prepare_post(tmp_path, monkeypatch, client)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "cannot post the digest for Nadia Brenner: LookupError" in result.output
    assert client.calls == []
    exception_chain = "".join(traceback.format_exception(result.exception))
    for sensitive in (WEBHOOK_URL, "/services/secret-value"):
        assert sensitive not in result.output
        assert sensitive not in exception_chain


def test_cli_post_build_failure_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeWebhookClient()
    prepare_post(tmp_path, monkeypatch, client)
    select_digest = cli.select_digest

    def fail_second_reader(*args: Any, **kwargs: Any) -> Selection:
        if args[3] == "U0SC01":
            raise ValueError("second reader build failed")
        return select_digest(*args, **kwargs)

    monkeypatch.setattr(cli, "select_digest", fail_second_reader)

    result = runner.invoke(app, ["post"])

    assert result.exit_code == 1, result.output
    assert "second reader build failed" in result.output
    assert client.calls == []


@pytest.mark.parametrize("surface", ["terminal", "slack-block", "slack-fallback"])
def test_phase_line_gate_name_is_plain_or_escaped_once(
    world: World, profile: Profile, surface: str
) -> None:
    """PL5 and PL6: flatten the name and escape each Slack line once."""
    gate = Gate(id="G-NEXT", name="Review & <fit>\n  > release", date=DAY, buffer_days=0)
    world = world.model_copy(
        update={"project": world.project.model_copy(update={"gates": (gate,)})}
    )
    selected = selection(profile.model_copy(update={"name": "Nadia & <ME>"}))
    plain = "Phase: EVT since 2026-08-03. Next gate: G-NEXT Review & <fit> > release, today."
    escaped = (
        "Phase: EVT since 2026-08-03. Next gate: G-NEXT "
        "Review &amp; &lt;fit&gt; &gt; release, today."
    )

    if surface == "terminal":
        assert render_digest(world, selected).splitlines()[2] == plain
    else:
        message = slack_message(world, selected)
        window = "First digest: the last 5 working days"
        if surface == "slack-block":
            assert message["blocks"][1] == {
                "type": "section",
                "text": {"type": "mrkdwn", "text": window + "\n" + escaped, "verbatim": True},
            }
        else:
            assert message["text"] == (
                "Daily digest for Nadia &amp; &lt;ME&gt; (ME) - 2026-10-14\n"
                + window
                + "\n"
                + escaped
            )


@pytest.mark.parametrize(
    ("day", "phase"),
    [(date(2026, 8, 2), None), (date(2026, 8, 3), "Phase: EVT since 2026-08-03.")],
    ids=["before-first-phase", "first-phase-start"],
)
def test_renderers_omit_phase_before_first_window(
    world: World, profile: Profile, day: date, phase: str | None
) -> None:
    """PL7: omit the phase before its first window and show it at the start."""
    world = world.model_copy(update={"project": world.project.model_copy(update={"gates": ()})})
    selected = Selection(profile=profile, day=day, lines=(), hidden=0)
    header = f"Daily digest for Nadia Brenner (ME) - {day}"
    window = "First digest: the last 5 working days"
    context = window if phase is None else window + "\n" + phase

    assert render_digest(world, selected) == (
        header + "\n" + context + "\n\nNothing new for Nadia Brenner."
    )
    message = slack_message(world, selected)
    assert message["text"] == header + "\n" + context
    assert block_text(message["blocks"][1]) == context
    assert len(message["blocks"]) == 3

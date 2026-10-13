from __future__ import annotations

import json
import re
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.slack_response import SlackResponse
from typer.testing import CliRunner

import digest.cli as cli
from digest.cli import app
from digest.models import Event
from digest.replay import read_events
from digest.seed import (
    FILE_SCOPE,
    REQUIRED_SCOPES,
    AuthInfo,
    MappedMessage,
    Post,
    RateLimited,
    SeedMap,
    SlackChannel,
    SlackWebSeedClient,
    placeholder_file,
    plan_seed,
    seed,
)
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_PATH = ROOT / "data" / "world" / "kestrel" / "world.yaml"
EVENTS_PATH = ROOT / "data" / "world" / "kestrel" / "events.jsonl"
WORKSPACE = "https://seed-test.slack.com"
BOT_TOKEN = "xox" + "b-" + "seed-test-secret"
ALL_SCOPES = REQUIRED_SCOPES | {FILE_SCOPE}
WRITE_METHODS = {"join", "post", "upload"}
runner = CliRunner()


@pytest.fixture
def world() -> World:
    return load_world(WORLD_PATH)


@pytest.fixture
def corpus(world: World) -> list[Event]:
    return list(read_events(EVENTS_PATH, world.workspace_url))


class FakeSlackSeedClient:
    """Record every attempt and optionally raise at a global call number."""

    def __init__(
        self,
        world: World,
        *,
        channels: Sequence[SlackChannel] | None = None,
        scopes: frozenset[str] = ALL_SCOPES,
        failures: dict[int, Exception] | None = None,
        timeline: list[tuple[str, object]] | None = None,
        team_id: str = "T-SEED",
    ) -> None:
        self.auth = AuthInfo(team_id=team_id, url=WORKSPACE, scopes=scopes)
        self.channels = list(channels) if channels is not None else slack_channels(world)
        self.failures = failures or {}
        self.calls: list[dict[str, Any]] = []
        self.timeline = timeline

    def _record(self, method: str, **values: object) -> dict[str, Any]:
        call = {"method": method, **values}
        self.calls.append(call)
        if self.timeline is not None:
            self.timeline.append(("call", method))
        error = self.failures.get(len(self.calls))
        if error is not None:
            raise error
        return call

    def auth_test(self) -> AuthInfo:
        self._record("auth_test")
        return self.auth

    def list_channels(self) -> list[SlackChannel]:
        self._record("list_channels")
        return self.channels

    def join(self, channel_id: str) -> None:
        self._record("join", channel_id=channel_id)

    def post(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None,
        username: str,
        icon_emoji: str,
        metadata: dict[str, object],
    ) -> str:
        call = self._record(
            "post",
            channel_id=channel_id,
            text=text,
            thread_ts=thread_ts,
            username=username,
            icon_emoji=icon_emoji,
            metadata=metadata,
        )
        number = sum(item.get("result") is not None for item in calls(self, "post")) + 1
        result = f"1700000000.{number:06d}"
        call["result"] = result
        return result

    def upload(self, channel_id: str, thread_ts: str, filename: str, content: bytes) -> str:
        call = self._record(
            "upload",
            channel_id=channel_id,
            thread_ts=thread_ts,
            filename=filename,
            content=content,
        )
        result = f"{WORKSPACE}/files/{filename}"
        call["result"] = result
        return result


def slack_channels(world: World) -> list[SlackChannel]:
    return [
        SlackChannel(
            id=f"S-{channel.id}",
            name=channel.name,
            is_private=channel.private,
            is_member=True,
        )
        for channel in world.channels
    ]


def calls(client: FakeSlackSeedClient, method: str) -> list[dict[str, Any]]:
    return [call for call in client.calls if call["method"] == method]


def successful_calls(client: FakeSlackSeedClient, method: str) -> list[dict[str, Any]]:
    return [call for call in calls(client, method) if "result" in call]


def assert_no_writes(client: FakeSlackSeedClient) -> None:
    assert not any(call["method"] in WRITE_METHODS for call in client.calls)


def recording_sleep(timeline: list[tuple[str, object]]) -> Callable[[float], None]:
    def sleep(seconds: float) -> None:
        timeline.append(("sleep", seconds))

    return sleep


def event(
    *,
    channel: str = "C0ENGME",
    ts: str = "1791819000.100000",
    thread_ts: str | None = None,
    user: str = "U0ME01",
    text: str = "Seed me",
    files: tuple[str, ...] = (),
    edited: bool = False,
    deleted: bool = False,
) -> Event:
    root = thread_ts or ts
    return Event(
        source="replay",
        channel_id=channel,
        channel_name="test-channel",
        thread_ts=root,
        ts=ts,
        event_ts=ts,
        user_id=user,
        text=text,
        files=files,
        permalink=f"{WORKSPACE}/archives/{channel}/p{ts.replace('.', '')}",
        edited=edited,
        deleted=deleted,
    )


def post(
    *,
    key: str = "C0ENGME:1791819000.100000",
    world_channel: str = "C0ENGME",
    root_key: str | None = None,
    files: tuple[str, ...] = (),
    reactions: int = 0,
) -> Post:
    world_ts = key.split(":", 1)[1]
    root = root_key or key
    return Post(
        key=key,
        world_channel=world_channel,
        root_key=root,
        text="Seed me",
        username="Nadia Brenner",
        icon_emoji=":gear:",
        metadata={
            "event_type": "downstream_world_event",
            "event_payload": {
                "world": "kestrel",
                "channel": world_channel,
                "ts": world_ts,
                "thread_ts": root.split(":", 1)[1],
            },
        },
        files=files,
        reactions=reactions,
    )


def write_map(path: Path, value: SeedMap) -> None:
    path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")


def prepare_cli(monkeypatch: pytest.MonkeyPatch, client: FakeSlackSeedClient) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        cli,
        "_env_value",
        lambda key: BOT_TOKEN if key == "SLACK_BOT_TOKEN" else None,
    )

    def client_for(token: str) -> FakeSlackSeedClient:
        assert token == BOT_TOKEN
        return client

    monkeypatch.setattr(cli, "_seed_client", client_for)


def slack_response(
    *,
    data: dict[str, Any],
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> SlackResponse:
    return SlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/test",
        req_args={},
        data=data,
        headers=headers or {},
        status_code=status,
    )


def test_b2_b3_b5_kestrel_posts_in_world_order_with_threads_personas_and_metadata(
    tmp_path: Path, world: World, corpus: list[Event]
) -> None:
    """Plan the corpus; seed it; compare each call with its source event."""
    planned = plan_seed(world, corpus, None)
    client = FakeSlackSeedClient(world)

    report = seed(
        world,
        planned,
        client,
        tmp_path / "seed.json",
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
    )

    ordered = sorted(enumerate(corpus), key=lambda pair: (pair[1].ts, pair[0]))
    post_calls = successful_calls(client, "post")
    personas = {persona.user_id: persona for persona in world.personas}
    emoji = {
        "ME": ":gear:",
        "EE": ":zap:",
        "FW": ":floppy_disk:",
        "supply_chain": ":package:",
        "EM": ":clipboard:",
        "PM": ":calendar:",
        None: ":robot_face:",
    }
    slack_roots: dict[str, str] = {}
    assert len(planned) == len(post_calls) == 132
    assert [item.key for item in planned] == [
        f"{source.channel_id}:{source.ts}" for _, source in ordered
    ]
    for call, (_, source) in zip(post_calls, ordered, strict=True):
        persona = personas[source.user_id]
        root_key = f"{source.channel_id}:{source.thread_ts}"
        expected_thread = None if source.ts == source.thread_ts else slack_roots[root_key]
        assert call["thread_ts"] == expected_thread
        assert call["username"] == persona.name
        assert call["icon_emoji"] == emoji[persona.role]
        assert call["metadata"] == {
            "event_type": "downstream_world_event",
            "event_payload": {
                "world": "kestrel",
                "channel": source.channel_id,
                "ts": source.ts,
                "thread_ts": source.thread_ts,
            },
        }
        if source.ts == source.thread_ts:
            slack_roots[root_key] = call["result"]
    assert report.posts == 132
    assert report.files == 5
    assert report.channels == 8
    assert report.reactions_not_seeded == 72
    assert report.complete is True


def test_b4_mentions_use_persona_names_and_unknown_for_an_unknown_id(world: World) -> None:
    source = event(text="Hi <@U0ME01> and <@U99999>; keep  two spaces.")

    planned = plan_seed(world, [source], None)

    assert planned[0].text == "Hi @Nadia Brenner and @unknown; keep  two spaces."
    assert "<@" not in planned[0].text


@pytest.mark.parametrize("field", ["edited", "deleted"])
def test_b7_edit_or_delete_is_refused_with_the_world_message_key(field: str, world: World) -> None:
    source = event(edited=field == "edited", deleted=field == "deleted")
    key = f"{source.channel_id}:{source.ts}"

    with pytest.raises(ValueError, match=re.escape(key)):
        plan_seed(world, [source], None)


def test_b6_files_follow_the_post_use_the_root_and_are_saved(tmp_path: Path, world: World) -> None:
    source = event(files=("PN-11020_FAI_photos.pdf", "BLD-DVT1_build_log_2026-10-23.xlsx"))
    client = FakeSlackSeedClient(world)
    map_path = tmp_path / "seed.json"

    seed(
        world,
        plan_seed(world, [source], None),
        client,
        map_path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
    )

    assert [call["method"] for call in client.calls[-3:]] == ["post", "upload", "upload"]
    posted_ts = successful_calls(client, "post")[0]["result"]
    uploads = successful_calls(client, "upload")
    assert [call["filename"] for call in uploads] == list(source.files)
    assert [call["thread_ts"] for call in uploads] == [posted_ts, posted_ts]
    saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    mapped = [
        saved.files[f"{source.channel_id}:{source.ts}/{name}"].permalink for name in source.files
    ]
    assert mapped == [f"{WORKSPACE}/files/{name}" for name in source.files]
    pdf = placeholder_file(source.files[0])
    assert pdf.startswith(b"%PDF")
    assert source.files[0].encode() in pdf
    assert b"Kestrel K2 test data" in pdf
    assert placeholder_file(source.files[1]) == b"Kestrel K2 test data\n"


def test_b9_resume_skips_done_posts_and_a_complete_map(
    tmp_path: Path, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    planned = [post(key=f"C0ENGME:179181900{i}.100000") for i in range(3)]
    map_path = tmp_path / "seed.json"
    stopped = RuntimeError("stop after the first post")
    first = FakeSlackSeedClient(world, failures={4: stopped})

    with pytest.raises(RuntimeError, match="first post"):
        seed(world, planned, first, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    checkpoint = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    assert list(checkpoint.messages) == [planned[0].key]
    assert checkpoint.complete is False
    resumed = FakeSlackSeedClient(world)
    report = seed(world, planned, resumed, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    output = capsys.readouterr().out.lower()
    resumed_world_ts = [
        call["metadata"]["event_payload"]["ts"] for call in successful_calls(resumed, "post")
    ]
    assert resumed_world_ts == [
        planned[1].key.split(":", 1)[1],
        planned[2].key.split(":", 1)[1],
    ]
    assert "accepted" in output and "repeat" in output
    assert report.posts == 2
    complete = FakeSlackSeedClient(world)
    rerun = seed(world, planned, complete, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    assert_no_writes(complete)
    assert rerun.posts == rerun.files == rerun.channels == 0


def test_b9_resume_between_two_uploads_retries_only_the_missing_file(
    tmp_path: Path, world: World
) -> None:
    planned = [post(files=("first.pdf", "second.pdf"))]
    map_path = tmp_path / "seed.json"
    first = FakeSlackSeedClient(world, failures={5: RuntimeError("second upload stopped")})

    with pytest.raises(RuntimeError, match="second upload"):
        seed(world, planned, first, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    resumed = FakeSlackSeedClient(world)
    report = seed(world, planned, resumed, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    assert successful_calls(resumed, "post") == []
    assert [call["filename"] for call in successful_calls(resumed, "upload")] == ["second.pdf"]
    assert (report.posts, report.files, report.channels) == (0, 1, 1)


@pytest.mark.parametrize(
    ("channel_id", "replacement", "message"),
    [
        ("C0ENGME", None, "eng-mech"),
        ("C0LEADS", None, "missing or not shared with the bot"),
        (
            "C0ENGME",
            SlackChannel("S-WRONG", "eng-mech", is_private=True, is_member=True),
            "visibility mismatch",
        ),
    ],
    ids=["missing-public", "private-not-visible", "visibility-mismatch"],
)
def test_b1_channel_problem_stops_before_a_write_and_names_the_problem(
    tmp_path: Path,
    world: World,
    channel_id: str,
    replacement: SlackChannel | None,
    message: str,
) -> None:
    selected = event(channel=channel_id)
    world_name = next(channel.name for channel in world.channels if channel.id == channel_id)
    available = [channel for channel in slack_channels(world) if channel.name != world_name]
    if replacement is not None:
        available.append(replacement)
    client = FakeSlackSeedClient(world, channels=available)

    with pytest.raises(Exception, match=re.escape(message)) as caught:
        seed(
            world,
            plan_seed(world, [selected], None),
            client,
            tmp_path / "seed.json",
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert_no_writes(client)
    assert world_name in str(caught.value)


def test_b1_public_channel_is_joined_before_its_first_post(tmp_path: Path, world: World) -> None:
    available = slack_channels(world)
    index = next(i for i, channel in enumerate(available) if channel.name == "eng-mech")
    available[index] = SlackChannel("S-C0ENGME", "eng-mech", False, False)
    client = FakeSlackSeedClient(world, channels=available)

    seed(
        world,
        plan_seed(world, [event()], None),
        client,
        tmp_path / "seed.json",
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
    )

    methods = [call["method"] for call in client.calls]
    assert methods.index("join") < methods.index("post")
    assert calls(client, "join") == [{"method": "join", "channel_id": "S-C0ENGME"}]
    saved = SeedMap.model_validate_json((tmp_path / "seed.json").read_text(encoding="utf-8"))
    assert saved.channels["C0ENGME"] == "S-C0ENGME"


class PagingWebClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def conversations_list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return {
                "channels": [{"id": "C1", "name": "one", "is_private": False, "is_member": True}],
                "response_metadata": {"next_cursor": "page-2"},
            }
        return {
            "channels": [{"id": "C2", "name": "two", "is_private": True, "is_member": False}],
            "response_metadata": {"next_cursor": ""},
        }


def test_b1_adapter_lists_public_and_private_channels_until_cursor_is_empty() -> None:
    web = PagingWebClient()

    channels = SlackWebSeedClient(web).list_channels()

    assert channels == [
        SlackChannel("C1", "one", False, True),
        SlackChannel("C2", "two", True, False),
    ]
    assert len(web.calls) == 2
    assert web.calls[0]["types"] == "public_channel,private_channel"
    assert web.calls[0].get("cursor", "") == ""
    assert web.calls[1]["types"] == "public_channel,private_channel"
    assert web.calls[1]["cursor"] == "page-2"


@pytest.mark.parametrize(
    ("target", "failure_call"),
    [("list_channels", 2), ("post", 3), ("upload", 4)],
)
def test_b11_rate_limit_waits_then_retries_the_same_operation_once(
    tmp_path: Path, world: World, target: str, failure_call: int
) -> None:
    waits: list[float] = []
    client = FakeSlackSeedClient(world, failures={failure_call: RateLimited(3)})

    report = seed(
        world,
        [post(files=("one.pdf",))],
        client,
        tmp_path / "seed.json",
        pace=0,
        sleep=waits.append,
        dry_run=False,
    )

    assert waits == [3]
    assert len(calls(client, target)) == 2
    assert (report.posts, report.files) == (1, 1)


def test_b11_six_rate_limits_stop_and_leave_an_incomplete_map(tmp_path: Path, world: World) -> None:
    waits: list[float] = []
    map_path = tmp_path / "seed.json"
    client = FakeSlackSeedClient(
        world,
        failures={number: RateLimited(3) for number in range(2, 8)},
    )

    with pytest.raises(RateLimited):
        seed(
            world,
            [post()],
            client,
            map_path,
            pace=0,
            sleep=waits.append,
            dry_run=False,
        )

    assert len(calls(client, "list_channels")) == 6
    assert waits == [3] * 5
    assert SeedMap.model_validate_json(map_path.read_text(encoding="utf-8")).complete is False


@pytest.mark.parametrize(("field", "wrong"), [("team_id", "T-OTHER"), ("world", "other")])
def test_b10_map_identity_mismatch_stops_before_any_write(
    tmp_path: Path, world: World, field: str, wrong: str
) -> None:
    values = {
        "team_id": "T-SEED",
        "workspace_url": WORKSPACE,
        "world": world.name,
    }
    values[field] = wrong
    map_path = tmp_path / "seed.json"
    write_map(
        map_path,
        SeedMap(
            team_id=values["team_id"],
            workspace_url=values["workspace_url"],
            world=values["world"],
        ),
    )
    client = FakeSlackSeedClient(world)

    with pytest.raises(Exception, match=re.escape(wrong)):
        seed(world, [post()], client, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    assert_no_writes(client)


class FakeWebClient:
    def __init__(
        self, *, post_error: Exception | None = None, upload_error: Exception | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.post_error = post_error
        self.upload_error = upload_error

    def auth_test(self) -> SlackResponse:
        self.calls.append({"method": "auth_test"})
        return slack_response(
            data={"ok": True, "team_id": "T-SEED", "url": WORKSPACE},
            headers={"x-oauth-scopes": ",".join(sorted(ALL_SCOPES))},
        )

    def conversations_list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "conversations_list", **kwargs})
        return {
            "channels": [
                {"id": "S-C0ENGME", "name": "eng-mech", "is_private": False, "is_member": True}
            ],
            "response_metadata": {"next_cursor": ""},
        }

    def conversations_join(self, *, channel: str) -> None:
        self.calls.append({"method": "conversations_join", "channel": channel})

    def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append({"method": "chat_postMessage", **kwargs})
        if self.post_error is not None:
            raise self.post_error
        return {"ts": "1700000000.000100"}

    def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "files_upload_v2", **kwargs})
        if self.upload_error is not None:
            raise self.upload_error
        return {"file": {"permalink": f"{WORKSPACE}/files/F1/name"}}


def test_b11_adapter_turns_real_429_into_rate_limited() -> None:
    response = slack_response(
        data={"ok": False, "error": "ratelimited"},
        status=429,
        headers={"Retry-After": "3"},
    )
    web = FakeWebClient(post_error=SlackApiError("rate limited", response))

    with pytest.raises(RateLimited) as caught:
        SlackWebSeedClient(web).post(
            "C1",
            "text",
            thread_ts=None,
            username="Name",
            icon_emoji=":gear:",
            metadata={"event_type": "test"},
        )

    assert caught.value.retry_after == 3


def test_b11_adapter_post_sends_persona_metadata_and_thread_and_upload_returns_permalink() -> None:
    web = FakeWebClient()
    adapter = SlackWebSeedClient(web)
    metadata = {"event_type": "downstream_world_event", "event_payload": {"world": "kestrel"}}

    message_ts = adapter.post(
        "C1",
        "text",
        thread_ts="1700000000.000001",
        username="Nadia Brenner",
        icon_emoji=":gear:",
        metadata=metadata,
    )
    permalink = adapter.upload("C1", message_ts, "name.xlsx", b"bytes")

    assert web.calls == [
        {
            "method": "chat_postMessage",
            "channel": "C1",
            "text": "text",
            "thread_ts": "1700000000.000001",
            "username": "Nadia Brenner",
            "icon_emoji": ":gear:",
            "metadata": metadata,
        },
        {
            "method": "files_upload_v2",
            "channel": "C1",
            "thread_ts": message_ts,
            "filename": "name.xlsx",
            "content": b"bytes",
        },
    ]
    assert message_ts == "1700000000.000100"
    assert permalink == f"{WORKSPACE}/files/F1/name"


def test_b11_request_error_from_upload_is_not_retried(tmp_path: Path, world: World) -> None:
    error = SlackRequestError("byte transfer stopped")
    web = FakeWebClient(upload_error=error)

    with pytest.raises(SlackRequestError, match="byte transfer"):
        seed(
            world,
            [post(files=("name.xlsx",))],
            SlackWebSeedClient(web),
            tmp_path / "seed.json",
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert sum(call["method"] == "files_upload_v2" for call in web.calls) == 1


def test_b2_reply_uses_a_root_from_the_saved_map(tmp_path: Path, world: World) -> None:
    root_key = "C0ENGME:1791819000.100000"
    reply = post(key="C0ENGME:1791819001.100000", root_key=root_key)
    map_path = tmp_path / "seed.json"
    write_map(
        map_path,
        SeedMap(
            team_id="T-SEED",
            workspace_url=WORKSPACE,
            world=world.name,
            channels={"C0ENGME": "S-C0ENGME"},
            messages={
                root_key: MappedMessage(
                    channel="S-C0ENGME",
                    ts="1700000000.000001",
                    thread_ts="1700000000.000001",
                )
            },
        ),
    )
    client = FakeSlackSeedClient(world)

    seed(world, [reply], client, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    assert successful_calls(client, "post")[0]["thread_ts"] == "1700000000.000001"


def test_b2_reply_without_a_saved_or_earlier_root_stops_before_any_write(
    tmp_path: Path, world: World
) -> None:
    reply = post(
        key="C0ENGME:1791819001.100000",
        root_key="C0ENGME:1791819000.100000",
    )
    client = FakeSlackSeedClient(world)

    with pytest.raises(Exception, match=re.escape(reply.key)):
        seed(
            world,
            [reply],
            client,
            tmp_path / "seed.json",
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert_no_writes(client)


def test_b13_pace_precedes_each_write_after_the_first_and_zero_disables_it(
    tmp_path: Path, world: World
) -> None:
    timeline: list[tuple[str, object]] = []
    client = FakeSlackSeedClient(world, timeline=timeline)
    planned = [post(files=("one.pdf", "two.pdf"))]

    seed(
        world,
        planned,
        client,
        tmp_path / "paced.json",
        pace=1.2,
        sleep=recording_sleep(timeline),
        dry_run=False,
    )

    writes_and_sleeps = [
        entry for entry in timeline if entry[0] == "sleep" or entry[1] in {"post", "upload"}
    ]
    assert writes_and_sleeps == [
        ("call", "post"),
        ("sleep", 1.2),
        ("call", "upload"),
        ("sleep", 1.2),
        ("call", "upload"),
    ]
    zero_timeline: list[tuple[str, object]] = []
    zero = FakeSlackSeedClient(world, timeline=zero_timeline)
    seed(
        world,
        planned,
        zero,
        tmp_path / "zero.json",
        pace=0,
        sleep=recording_sleep(zero_timeline),
        dry_run=False,
    )
    assert not any(entry[0] == "sleep" for entry in zero_timeline)


def test_b13_retry_wait_is_separate_from_pace_wait(tmp_path: Path, world: World) -> None:
    timeline: list[tuple[str, object]] = []
    client = FakeSlackSeedClient(world, failures={3: RateLimited(3)}, timeline=timeline)

    seed(
        world,
        [post(files=("one.pdf",))],
        client,
        tmp_path / "seed.json",
        pace=1.2,
        sleep=recording_sleep(timeline),
        dry_run=False,
    )

    writes_and_sleeps = [
        entry for entry in timeline if entry[0] == "sleep" or entry[1] in {"post", "upload"}
    ]
    assert writes_and_sleeps == [
        ("call", "post"),
        ("sleep", 3),
        ("sleep", 1.2),
        ("call", "post"),
        ("sleep", 1.2),
        ("call", "upload"),
    ]


@pytest.mark.parametrize(
    ("missing", "files"),
    [(FILE_SCOPE, ("one.pdf",)), ("chat:write.customize", ())],
)
def test_b14_missing_required_scope_stops_real_run_before_any_write(
    tmp_path: Path, world: World, missing: str, files: tuple[str, ...]
) -> None:
    client = FakeSlackSeedClient(world, scopes=ALL_SCOPES - {missing})

    with pytest.raises(Exception, match=re.escape(missing)):
        seed(
            world,
            [post(files=files)],
            client,
            tmp_path / "seed.json",
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert_no_writes(client)


@pytest.mark.parametrize("until_day", [0, 11])
def test_command_until_day_rejects_values_outside_the_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    until_day: int,
) -> None:
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(tmp_path / "seed.json"),
            "--until-day",
            str(until_day),
            "--pace",
            "0",
        ],
    )

    assert result.exit_code == 1, result.output
    assert str(until_day) in result.output
    assert_no_writes(client)


def test_command_until_day_uses_world_timezone_and_later_run_adds_only_day_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)
    map_path = tmp_path / "seed.json"

    first = runner.invoke(
        app,
        ["seed", "--world", "kestrel", "--map", str(map_path), "--until-day", "1", "--pace", "0"],
    )
    first_count = len(successful_calls(client, "post"))
    second = runner.invoke(
        app,
        ["seed", "--world", "kestrel", "--map", str(map_path), "--until-day", "2", "--pace", "0"],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first_count == 10
    assert len(successful_calls(client, "post")) - first_count == 18
    saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    assert len(saved.messages) == 28


def test_command_dry_run_reports_channel_counts_missing_scope_and_missing_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    available = [channel for channel in slack_channels(world) if channel.name != "kestrel-program"]
    client = FakeSlackSeedClient(world, channels=available, scopes=ALL_SCOPES - {FILE_SCOPE})
    prepare_cli(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(tmp_path / "seed.json"),
            "--until-day",
            "1",
            "--dry-run",
            "--pace",
            "0",
        ],
    )

    assert result.exit_code == 1, result.output
    assert_no_writes(client)
    plm_line = next(line for line in result.output.splitlines() if "plm-feed" in line)
    program_line = next(line for line in result.output.splitlines() if "kestrel-program" in line)
    assert re.search(r"\b5\b", plm_line) and re.search(r"\b1\b", plm_line)
    assert re.search(r"\b5\b", program_line)
    assert "kestrel-program" in result.output
    assert FILE_SCOPE in result.output


def test_command_dry_run_lists_missing_customize_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    client = FakeSlackSeedClient(world, scopes=ALL_SCOPES - {"chat:write.customize"})
    prepare_cli(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(tmp_path / "seed.json"),
            "--until-day",
            "1",
            "--dry-run",
            "--pace",
            "0",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "chat:write.customize" in result.output
    assert_no_writes(client)


def test_b12_summary_counts_full_rerun_and_one_missing_upload_with_same_reactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)
    map_path = tmp_path / "seed.json"
    args = ["seed", "--world", "kestrel", "--map", str(map_path), "--pace", "0"]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    raw = json.loads(map_path.read_text(encoding="utf-8"))
    missing_key = next(key for key in raw["files"] if key.endswith("PN-11020_CMM_report.pdf"))
    del raw["files"][missing_key]
    raw["complete"] = False
    map_path.write_text(json.dumps(raw), encoding="utf-8")
    third = runner.invoke(app, args)

    assert first.exit_code == second.exit_code == third.exit_code == 0
    assert first.output.strip().splitlines()[-1] == (
        f"seeded 132 posts and 5 files in 8 channels; 72 reactions not seeded; map {map_path}"
    )
    assert second.output.strip().splitlines()[-1] == (
        f"seeded 0 posts and 0 files in 0 channels; 72 reactions not seeded; map {map_path}"
    )
    assert third.output.strip().splitlines()[-1] == (
        f"seeded 0 posts and 1 files in 1 channels; 72 reactions not seeded; map {map_path}"
    )


def test_b12_cli_never_leaks_token_in_output_or_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    client = FakeSlackSeedClient(
        world,
        failures={1: LookupError(f"authentication exposed {BOT_TOKEN}")},
    )
    prepare_cli(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(tmp_path / "seed.json"),
            "--until-day",
            "1",
            "--pace",
            "0",
        ],
    )

    assert result.exit_code == 1, result.output
    assert result.exception is not None
    exception_chain = "".join(traceback.format_exception(result.exception))
    assert BOT_TOKEN not in result.output
    assert BOT_TOKEN not in exception_chain
    assert "LookupError" in result.output

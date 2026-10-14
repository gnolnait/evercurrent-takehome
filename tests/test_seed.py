from __future__ import annotations

import json
import logging
import re
import shutil
import traceback
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import slack_sdk
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.slack_response import SlackResponse
from typer.testing import CliRunner

import digest.cli as cli
from digest.cli import app
from digest.models import Event, Reaction
from digest.replay import read_events
from digest.seed import (
    RESUME_LIMIT,
    AuthInfo,
    MappedFile,
    MappedMessage,
    Post,
    RateLimited,
    SeedBlocked,
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
ALL_SCOPES = frozenset(
    {
        "chat:write",
        "chat:write.customize",
        "channels:read",
        "channels:join",
        "groups:read",
        "files:write",
    }
)
WRITE_METHODS = {"join", "post", "upload"}
runner = CliRunner()
DAY_ONE_DRY_RUN_LINES = [
    "kestrel-program: 5 posts, 0 files",
    "plm-feed: 5 posts, 1 files",
]


@pytest.fixture
def world() -> World:
    return load_world(WORLD_PATH)


@pytest.fixture
def corpus(world: World) -> list[Event]:
    return list(read_events(EVENTS_PATH, world.workspace_url))


class FakeSlackSeedClient:
    """Record attempts and optionally fail a named operation attempt."""

    def __init__(
        self,
        world: World,
        *,
        channels: Sequence[SlackChannel] | None = None,
        scopes: frozenset[str] = ALL_SCOPES,
        failures: dict[tuple[str, int], Exception] | None = None,
        timeline: list[tuple[str, object]] | None = None,
        team_id: str = "T-SEED",
        before_call: Callable[[str, int], None] | None = None,
    ) -> None:
        self.auth = AuthInfo(team_id=team_id, url=WORKSPACE, scopes=scopes)
        self.channels = list(channels) if channels is not None else slack_channels(world)
        self.failures = failures or {}
        self.calls: list[dict[str, Any]] = []
        self.timeline = timeline
        self.before_call = before_call
        self.attempts: dict[str, int] = {}

    def _record(self, method: str, **values: object) -> dict[str, Any]:
        attempt = self.attempts.get(method, 0) + 1
        self.attempts[method] = attempt
        if self.before_call is not None:
            self.before_call(method, attempt)
        call = {"method": method, **values}
        self.calls.append(call)
        if self.timeline is not None:
            self.timeline.append(("call", method))
        error = self.failures.get((method, attempt))
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

    def update(self, channel_id: str, ts: str, text: str, *, metadata: dict[str, object]) -> None:
        self._record("update", channel_id=channel_id, ts=ts, text=text, metadata=metadata)


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


def rewritten_text(source: Event, world: World) -> str:
    names = {persona.user_id: persona.name for persona in world.personas}
    return re.sub(
        r"<@([^>]+)>", lambda match: f"@{names.get(match.group(1), 'unknown')}", source.text
    )


def assert_no_writes(client: FakeSlackSeedClient) -> None:
    assert not any(call["method"] in WRITE_METHODS for call in client.calls)


def assert_mapped_messages(saved: SeedMap, client: FakeSlackSeedClient) -> None:
    for call in successful_calls(client, "post"):
        payload = call["metadata"]["event_payload"]
        mapped = saved.messages[f"{payload['channel']}:{payload['ts']}"]
        assert mapped.channel == call["channel_id"]
        assert mapped.ts == call["result"]
        assert mapped.thread_ts == (call["thread_ts"] or call["result"])


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


def timestamp_at(world: World, day: int, at: time) -> str:
    local = datetime.combine(world.date_of(day), at, tzinfo=ZoneInfo(world.timezone))
    return f"{local.timestamp():.6f}"


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
    upload_calls = successful_calls(client, "upload")
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
    slack_ids = {channel.id: f"S-{channel.id}" for channel in world.channels}
    assert len(planned) == len(post_calls) == 132
    assert [item.key for item in planned] == [
        f"{source.channel_id}:{source.ts}" for _, source in ordered
    ]
    for call, (_, source) in zip(post_calls, ordered, strict=True):
        persona = personas[source.user_id]
        root_key = f"{source.channel_id}:{source.thread_ts}"
        expected_thread = None if source.ts == source.thread_ts else slack_roots[root_key]
        expected_text = rewritten_text(source, world)
        assert call["channel_id"] == slack_ids[source.channel_id]
        assert call["text"] == expected_text
        assert "<@" not in call["text"]
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
    expected_uploads = [
        (slack_ids[source.channel_id], filename)
        for _, source in ordered
        for filename in source.files
    ]
    assert len(upload_calls) == len(expected_uploads) == 5
    for call, (channel_id, filename) in zip(upload_calls, expected_uploads, strict=True):
        assert call["channel_id"] == channel_id
        assert call["filename"] == filename
        assert call["content"] == placeholder_file(filename)
    assert report.posts == 132
    assert report.files == 5
    assert report.channels == 8
    assert report.reactions_not_seeded == 72
    assert report.complete is True
    saved = SeedMap.model_validate_json((tmp_path / "seed.json").read_text(encoding="utf-8"))
    for call, (_, source) in zip(post_calls, ordered, strict=True):
        mapped = saved.messages[f"{source.channel_id}:{source.ts}"]
        root_key = f"{source.channel_id}:{source.thread_ts}"
        assert mapped.channel == call["channel_id"]
        assert mapped.ts == call["result"]
        assert mapped.thread_ts == slack_roots[root_key]


def test_b4_mentions_use_persona_names_and_unknown_for_an_unknown_id(world: World) -> None:
    source = event(text="Hi <@U0ME01> and <@U99999>; keep  two spaces.")

    planned = plan_seed(world, [source], None)

    assert planned[0].text == "Hi @Nadia Brenner and @unknown; keep  two spaces."
    assert "<@" not in planned[0].text


def test_b2_plan_sorts_by_timestamp_and_keeps_input_order_for_ties(world: World) -> None:
    late = event(channel="C0ENGME", ts="1791819002.100000", text="late")
    tied_first = event(channel="C0ENGEF", ts="1791819001.100000", text="tied first")
    tied_second = event(channel="C0ENGME", ts="1791819001.100000", text="tied second")
    early = event(channel="C0SCM", ts="1791819000.100000", text="early")

    planned = plan_seed(world, [late, tied_first, tied_second, early], None)

    assert [item.text for item in planned] == ["early", "tied first", "tied second", "late"]


def test_a10_until_day_uses_world_timezone_boundary_and_extends_exact_keys(world: World) -> None:
    before = event(channel="C0ENGME", ts=timestamp_at(world, 1, time(23, 30)), text="day one")
    after = event(channel="C0ENGEF", ts=timestamp_at(world, 2, time(0, 30)), text="day two")

    first = plan_seed(world, [after, before], until_day=1)
    second = plan_seed(world, [after, before], until_day=2)

    before_key = f"{before.channel_id}:{before.ts}"
    after_key = f"{after.channel_id}:{after.ts}"
    assert [item.key for item in first] == [before_key]
    assert [item.key for item in second] == [before_key, after_key]
    assert {item.key for item in second} - {item.key for item in first} == {after_key}


@pytest.mark.parametrize("field", ["edited", "deleted"])
def test_b7_edit_or_delete_is_refused_with_the_world_message_key(field: str, world: World) -> None:
    source = event(edited=field == "edited", deleted=field == "deleted")
    key = f"{source.channel_id}:{source.ts}"

    with pytest.raises(ValueError, match=re.escape(key)):
        plan_seed(world, [source], None)


def test_b7_edit_outside_selected_days_does_not_stop_the_run(world: World) -> None:
    selected = event(ts=timestamp_at(world, 1, time(12)), text="selected")
    later_edit = event(ts=timestamp_at(world, 2, time(12)), text="later", edited=True)

    planned = plan_seed(world, [later_edit, selected], until_day=1)

    assert [item.key for item in planned] == [f"{selected.channel_id}:{selected.ts}"]


def test_b6_files_follow_the_post_use_the_root_and_are_saved(tmp_path: Path, world: World) -> None:
    source = event(
        text="Send <@U0ME01> these files",
        files=("PN-11020_FAI_photos.pdf", "BLD-DVT1_build_log_2026-10-23.xlsx"),
    )
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
    posted = successful_calls(client, "post")[0]
    uploads = successful_calls(client, "upload")
    assert posted["channel_id"] == "S-C0ENGME"
    assert posted["text"] == rewritten_text(source, world)
    assert "<@" not in posted["text"]
    assert [call["channel_id"] for call in uploads] == ["S-C0ENGME", "S-C0ENGME"]
    assert [call["filename"] for call in uploads] == list(source.files)
    assert [call["thread_ts"] for call in uploads] == [posted_ts, posted_ts]
    assert [call["content"] for call in uploads] == [
        placeholder_file(name) for name in source.files
    ]
    saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    mapped_message = saved.messages[f"{source.channel_id}:{source.ts}"]
    assert mapped_message.channel == posted["channel_id"]
    assert mapped_message.ts == posted_ts
    assert mapped_message.thread_ts == posted_ts
    mapped = [
        saved.files[f"{source.channel_id}:{source.ts}/{name}"].permalink for name in source.files
    ]
    assert mapped == [f"{WORKSPACE}/files/{name}" for name in source.files]
    pdf = placeholder_file(source.files[0])
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert source.files[0].encode() in pdf
    assert b"Kestrel K2 test data" in pdf
    assert "Kestrel K2 test data" in placeholder_file(source.files[1]).decode("utf-8")


def test_b9_resume_skips_done_posts_and_a_complete_map(
    tmp_path: Path, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    planned = [post(key=f"C0ENGME:179181900{i}.100000") for i in range(3)]
    map_path = tmp_path / "seed.json"
    stopped = RuntimeError("stop after the first post")

    def check_checkpoint(method: str, attempt: int) -> None:
        if (method, attempt) == ("post", 2):
            saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
            assert set(saved.messages) == {planned[0].key}
            assert planned[1].key not in saved.messages

    first = FakeSlackSeedClient(
        world,
        failures={("post", 2): stopped},
        before_call=check_checkpoint,
    )

    with pytest.raises(Exception) as caught:
        seed(world, planned, first, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    assert caught.value is not None
    capsys.readouterr()
    checkpoint = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    assert list(checkpoint.messages) == [planned[0].key]
    assert_mapped_messages(checkpoint, first)
    assert checkpoint.complete is False
    resumed = FakeSlackSeedClient(world)
    report = seed(world, planned, resumed, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    resumed_output = capsys.readouterr().out
    resumed_world_ts = [
        call["metadata"]["event_payload"]["ts"] for call in successful_calls(resumed, "post")
    ]
    assert resumed_world_ts == [
        planned[1].key.split(":", 1)[1],
        planned[2].key.split(":", 1)[1],
    ]
    assert RESUME_LIMIT in resumed_output
    assert report.posts == 2
    complete = FakeSlackSeedClient(world)
    rerun = seed(world, planned, complete, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    rerun_output = capsys.readouterr().out
    assert_no_writes(complete)
    assert rerun.posts == rerun.files == rerun.channels == 0
    assert RESUME_LIMIT not in rerun_output


def test_b9_resume_between_two_uploads_retries_only_the_missing_file(
    tmp_path: Path, world: World
) -> None:
    planned = [post(files=("first.pdf", "second.pdf"))]
    map_path = tmp_path / "seed.json"

    def check_checkpoint(method: str, attempt: int) -> None:
        if method != "upload":
            return
        saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
        first_key = f"{planned[0].key}/first.pdf"
        second_key = f"{planned[0].key}/second.pdf"
        assert planned[0].key in saved.messages
        if attempt == 1:
            assert first_key not in saved.files
        if attempt == 2:
            assert first_key in saved.files
            assert second_key not in saved.files

    first = FakeSlackSeedClient(
        world,
        failures={("upload", 2): RuntimeError("second upload stopped")},
        before_call=check_checkpoint,
    )

    with pytest.raises(Exception) as caught:
        seed(world, planned, first, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    assert caught.value is not None
    first_checkpoint = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    assert_mapped_messages(first_checkpoint, first)
    saved_root_ts = first_checkpoint.messages[planned[0].key].thread_ts
    resumed = FakeSlackSeedClient(world)
    report = seed(world, planned, resumed, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    assert successful_calls(resumed, "post") == []
    resumed_uploads = successful_calls(resumed, "upload")
    assert [call["filename"] for call in resumed_uploads] == ["second.pdf"]
    assert resumed_uploads[0]["thread_ts"] == saved_root_ts
    assert (report.posts, report.files, report.channels) == (0, 1, 1)
    assert report.map_path == map_path


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
    selected = event(channel=channel_id, ts="1791819001.100000")
    available_first = event(channel="C0ENGEF", ts="1791819000.100000", text="available first")
    world_name = next(channel.name for channel in world.channels if channel.id == channel_id)
    available = [channel for channel in slack_channels(world) if channel.name != world_name]
    if replacement is not None:
        available.append(replacement)
    client = FakeSlackSeedClient(world, channels=available)

    with pytest.raises(Exception, match=re.escape(message)) as caught:
        seed(
            world,
            plan_seed(world, [available_first, selected], None),
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

    def conversations_list(self, **kwargs: Any) -> SlackResponse:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return slack_response(
                data={
                    "ok": True,
                    "channels": [
                        {"id": "C1", "name": "one", "is_private": False, "is_member": True}
                    ],
                    "response_metadata": {"next_cursor": "page-2"},
                }
            )
        return slack_response(
            data={
                "ok": True,
                "channels": [{"id": "C2", "name": "two", "is_private": True, "is_member": False}],
                "response_metadata": {"next_cursor": ""},
            }
        )


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
    "target",
    ["list_channels", "post", "upload"],
)
def test_b11_rate_limit_waits_then_retries_the_same_operation_once(
    tmp_path: Path, world: World, target: str
) -> None:
    waits: list[float] = []
    client = FakeSlackSeedClient(world, failures={(target, 1): RateLimited(3)})

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
    attempts = calls(client, target)
    assert len(attempts) == 2
    assert {key: value for key, value in attempts[0].items() if key != "result"} == {
        key: value for key, value in attempts[1].items() if key != "result"
    }
    assert (report.posts, report.files) == (1, 1)


def test_b11_six_rate_limits_stop_before_first_write_and_leave_no_map(
    tmp_path: Path, world: World
) -> None:
    waits: list[float] = []
    map_path = tmp_path / "seed.json"
    client = FakeSlackSeedClient(
        world,
        failures={("list_channels", attempt): RateLimited(3) for attempt in range(1, 7)},
    )

    with pytest.raises(Exception) as caught:
        seed(
            world,
            [post()],
            client,
            map_path,
            pace=0,
            sleep=waits.append,
            dry_run=False,
        )

    assert caught.value is not None
    assert len(calls(client, "list_channels")) == 6
    assert waits == [3] * 5
    assert not map_path.exists()


def test_b15_join_client_error_before_first_post_leaves_no_map(
    tmp_path: Path, world: World
) -> None:
    map_path = tmp_path / "seed.json"
    available = slack_channels(world)
    index = next(i for i, channel in enumerate(available) if channel.name == "eng-mech")
    available[index] = SlackChannel("S-C0ENGME", "eng-mech", False, False)
    client = FakeSlackSeedClient(
        world,
        channels=available,
        failures={("join", 1): RuntimeError("join stopped")},
    )

    with pytest.raises(RuntimeError, match="join stopped"):
        seed(
            world,
            [post()],
            client,
            map_path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert not calls(client, "post")
    assert not calls(client, "upload")
    assert not map_path.exists()


def test_b11_exhausted_upload_retries_keep_the_acknowledged_post(
    tmp_path: Path, world: World
) -> None:
    map_path = tmp_path / "seed.json"
    planned = post(files=("one.pdf",))
    client = FakeSlackSeedClient(
        world,
        failures={("upload", attempt): RateLimited(3) for attempt in range(1, 7)},
    )

    with pytest.raises(Exception) as caught:
        seed(world, [planned], client, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    assert caught.value is not None
    saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    posted = successful_calls(client, "post")[0]
    assert saved.messages[planned.key].channel == posted["channel_id"]
    assert saved.messages[planned.key].ts == posted["result"]
    assert saved.messages[planned.key].thread_ts == posted["result"]
    assert saved.files == {}


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

    with pytest.raises(Exception) as caught:
        seed(world, [post()], client, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    assert_no_writes(client)
    assert field in str(caught.value) or world.name in str(caught.value)


@pytest.mark.parametrize("map_entry", ["channels", "messages"])
def test_a19_channel_id_drift_raises_seed_blocked_before_any_write(
    tmp_path: Path, world: World, map_entry: str
) -> None:
    planned = post()
    if map_entry == "channels":
        saved = SeedMap(
            team_id="T-SEED",
            workspace_url=WORKSPACE,
            world=world.name,
            channels={planned.world_channel: "S-OLD"},
        )
    else:
        saved = SeedMap(
            team_id="T-SEED",
            workspace_url=WORKSPACE,
            world=world.name,
            messages={
                planned.key: MappedMessage(
                    channel="S-OLD",
                    ts="1700000000.000001",
                    thread_ts="1700000000.000001",
                )
            },
        )
    map_path = tmp_path / "seed.json"
    write_map(map_path, saved)
    client = FakeSlackSeedClient(world)

    with pytest.raises(SeedBlocked, match="eng-mech"):
        seed(world, [planned], client, map_path, pace=0, sleep=lambda _: None, dry_run=False)

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

    def conversations_list(self, **kwargs: Any) -> SlackResponse:
        self.calls.append({"method": "conversations_list", **kwargs})
        return slack_response(
            data={
                "ok": True,
                "channels": [
                    {
                        "id": "S-C0ENGME",
                        "name": "eng-mech",
                        "is_private": False,
                        "is_member": True,
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            }
        )

    def conversations_join(self, *, channel: str) -> SlackResponse:
        self.calls.append({"method": "conversations_join", "channel": channel})
        return slack_response(data={"ok": True, "channel": {"id": channel}})

    def chat_postMessage(self, **kwargs: Any) -> SlackResponse:
        self.calls.append({"method": "chat_postMessage", **kwargs})
        if self.post_error is not None:
            raise self.post_error
        return slack_response(data={"ok": True, "ts": "1700000000.000100"})

    def files_upload_v2(self, **kwargs: Any) -> SlackResponse:
        self.calls.append({"method": "files_upload_v2", **kwargs})
        if self.upload_error is not None:
            raise self.upload_error
        file = {"id": "F1", "permalink": f"{WORKSPACE}/files/F1/name"}
        return slack_response(data={"ok": True, "file": file, "files": [file]})


def test_cli_seed_client_builds_web_client_without_retry_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}
    web = object()

    def factory(*, token: str, retry_handlers: list[object]) -> object:
        recorded.update(token=token, retry_handlers=retry_handlers)
        return web

    monkeypatch.setattr(slack_sdk, "WebClient", factory)

    adapter = cli._seed_client(BOT_TOKEN)

    assert recorded == {"token": BOT_TOKEN, "retry_handlers": []}
    assert isinstance(adapter, SlackWebSeedClient)
    assert adapter._web is web


def test_b11_adapter_auth_test_reads_identity_and_scopes_from_slack_response() -> None:
    web = FakeWebClient()

    auth = SlackWebSeedClient(web).auth_test()

    assert auth == AuthInfo(team_id="T-SEED", url=WORKSPACE, scopes=ALL_SCOPES)


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
    map_path = tmp_path / "seed.json"

    with pytest.raises(Exception) as caught:
        seed(
            world,
            [post(files=("name.xlsx",))],
            SlackWebSeedClient(web),
            map_path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert caught.value is not None
    assert sum(call["method"] == "files_upload_v2" for call in web.calls) == 1
    saved = SeedMap.model_validate_json(map_path.read_text(encoding="utf-8"))
    assert list(saved.messages) == ["C0ENGME:1791819000.100000"]
    mapped = saved.messages["C0ENGME:1791819000.100000"]
    assert mapped.channel == "S-C0ENGME"
    assert mapped.ts == "1700000000.000100"
    assert mapped.thread_ts == "1700000000.000100"
    assert saved.files == {}


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
    valid_first = post(key="C0ENGEF:1791818999.100000", world_channel="C0ENGEF")
    client = FakeSlackSeedClient(world)

    with pytest.raises(Exception, match=re.escape(reply.key)):
        seed(
            world,
            [valid_first, reply],
            client,
            tmp_path / "seed.json",
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
        )

    assert_no_writes(client)


def test_b2_reply_whose_root_appears_only_later_stops_before_any_write(
    tmp_path: Path, world: World
) -> None:
    root = post(key="C0ENGME:1791819002.100000")
    reply = post(key="C0ENGME:1791819001.100000", root_key=root.key)
    client = FakeSlackSeedClient(world)

    with pytest.raises(Exception, match=re.escape(reply.key)):
        seed(
            world,
            [reply, root],
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
    client = FakeSlackSeedClient(
        world,
        failures={("upload", 1): RateLimited(3)},
        timeline=timeline,
    )

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
    assert writes_and_sleeps[0:3] == [
        ("call", "post"),
        ("sleep", 1.2),
        ("call", "upload"),
    ]
    assert set(writes_and_sleeps[3:5]) == {("sleep", 1.2), ("sleep", 3)}
    assert writes_and_sleeps[5] == ("call", "upload")


def test_b13_first_write_retry_has_no_pace_wait(tmp_path: Path, world: World) -> None:
    timeline: list[tuple[str, object]] = []
    client = FakeSlackSeedClient(
        world,
        failures={("post", 1): RateLimited(3)},
        timeline=timeline,
    )

    seed(
        world,
        [post()],
        client,
        tmp_path / "seed.json",
        pace=1.2,
        sleep=recording_sleep(timeline),
        dry_run=False,
    )

    writes_and_sleeps = [entry for entry in timeline if entry[0] == "sleep" or entry[1] == "post"]
    assert writes_and_sleeps == [("call", "post"), ("sleep", 3), ("call", "post")]


@pytest.mark.parametrize(
    ("missing", "files"),
    [("files:write", ("one.pdf",)), ("chat:write.customize", ())],
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


def test_b14_selection_without_files_does_not_require_files_write(
    tmp_path: Path, world: World
) -> None:
    client = FakeSlackSeedClient(
        world,
        scopes=frozenset(
            {
                "chat:write",
                "chat:write.customize",
                "channels:read",
                "channels:join",
                "groups:read",
            }
        ),
    )

    report = seed(
        world,
        [post()],
        client,
        tmp_path / "seed.json",
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
    )

    assert report.posts == 1


@pytest.mark.parametrize("blocker", ["missing-scope", "missing-channel"])
def test_a21_first_refusal_leaves_no_map_or_resume_warning(
    tmp_path: Path,
    world: World,
    capsys: pytest.CaptureFixture[str],
    blocker: str,
) -> None:
    map_path = tmp_path / "seed.json"
    planned = [post()]
    channels = slack_channels(world)
    scopes = ALL_SCOPES
    if blocker == "missing-scope":
        scopes = ALL_SCOPES - {"chat:write.customize"}
    else:
        channels = [channel for channel in channels if channel.name != "eng-mech"]
    blocked = FakeSlackSeedClient(world, channels=channels, scopes=scopes)

    with pytest.raises(SeedBlocked):
        seed(world, planned, blocked, map_path, pace=0, sleep=lambda _: None, dry_run=False)

    capsys.readouterr()
    assert not map_path.exists()
    unblocked = FakeSlackSeedClient(world)
    seed(world, planned, unblocked, map_path, pace=0, sleep=lambda _: None, dry_run=False)
    output = capsys.readouterr().out
    assert RESUME_LIMIT not in output


def test_a22_default_map_retries_auth_and_reports_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    waits: list[float] = []
    client = FakeSlackSeedClient(world, failures={("auth_test", 1): RateLimited(3)})
    monkeypatch.chdir(tmp_path)

    report = seed(
        world,
        [post()],
        client,
        None,
        pace=0,
        sleep=waits.append,
        dry_run=False,
    )

    expected = tmp_path / ".slack-seed" / "T-SEED-kestrel.json"
    assert waits == [3]
    assert len(calls(client, "auth_test")) == 2
    assert expected.exists()
    assert report.map_path.resolve() == expected.resolve()


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
    assert_mapped_messages(saved, client)


@pytest.mark.parametrize(
    ("scenario", "exit_code", "expected_lines"),
    [
        (
            "missing-channel",
            1,
            [*DAY_ONE_DRY_RUN_LINES, "missing channel: kestrel-program"],
        ),
        (
            "missing-scope",
            1,
            [*DAY_ONE_DRY_RUN_LINES, "missing scope: files:write"],
        ),
        ("unblocked", 0, DAY_ONE_DRY_RUN_LINES),
    ],
    ids=["missing-channel", "missing-scope", "unblocked"],
)
def test_command_dry_run_reports_exact_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    scenario: str,
    exit_code: int,
    expected_lines: list[str],
) -> None:
    channels = slack_channels(world)
    scopes = ALL_SCOPES
    if scenario == "missing-channel":
        channels = [channel for channel in channels if channel.name != "kestrel-program"]
    if scenario == "missing-scope":
        scopes = ALL_SCOPES - {"files:write"}
    client = FakeSlackSeedClient(world, channels=channels, scopes=scopes)
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

    assert result.exit_code == exit_code, result.output
    assert result.output.splitlines() == expected_lines
    assert_no_writes(client)


def test_a19_command_dry_run_reports_channel_id_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)
    map_path = tmp_path / "seed.json"
    channel = next(channel for channel in world.channels if channel.name == "kestrel-program")
    write_map(
        map_path,
        SeedMap(
            team_id="T-SEED",
            workspace_url=WORKSPACE,
            world=world.name,
            channels={channel.id: "S-OLD"},
        ),
    )

    result = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(map_path),
            "--until-day",
            "1",
            "--dry-run",
            "--pace",
            "0",
        ],
    )

    assert result.exit_code == 1, result.output
    assert result.output.splitlines() == [
        *DAY_ONE_DRY_RUN_LINES,
        "channel id changed: kestrel-program",
    ]
    assert_no_writes(client)


def test_command_dry_run_counts_only_pending_work_from_partial_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    corpus: list[Event],
) -> None:
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)
    planned = plan_seed(world, corpus, 1)
    file_post = next(item for item in planned if item.files)
    done_message = file_post
    done_file_key = f"{file_post.key}/{file_post.files[0]}"
    map_path = tmp_path / "seed.json"
    write_map(
        map_path,
        SeedMap(
            team_id="T-SEED",
            workspace_url=WORKSPACE,
            world=world.name,
            messages={
                done_message.key: MappedMessage(
                    channel=f"S-{done_message.world_channel}",
                    ts="1700000000.000001",
                    thread_ts="1700000000.000001",
                )
            },
            files={done_file_key: MappedFile(permalink=f"{WORKSPACE}/files/F1/name")},
        ),
    )

    result = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(map_path),
            "--until-day",
            "1",
            "--dry-run",
            "--pace",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "kestrel-program: 5 posts, 0 files",
        "plm-feed: 4 posts, 0 files",
    ]
    assert_no_writes(client)


def test_b8_small_selection_reports_same_reaction_count_after_resume(
    tmp_path: Path, world: World
) -> None:
    source = event().model_copy(
        update={
            "reactions": (
                Reaction(name="eyes", user_id="U0EE01", ts="1791819001.100000"),
                Reaction(name="ticket", user_id="U0SC01", ts="1791819002.100000"),
            )
        }
    )
    planned = plan_seed(world, [source], None)
    map_path = tmp_path / "seed.json"

    first = seed(
        world,
        planned,
        FakeSlackSeedClient(world),
        map_path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
    )
    resumed_client = FakeSlackSeedClient(world)
    resumed = seed(
        world,
        planned,
        resumed_client,
        map_path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
    )

    assert first.reactions_not_seeded == 2
    assert resumed.reactions_not_seeded == 2
    assert_no_writes(resumed_client)


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


@pytest.mark.parametrize(
    "error",
    [
        LookupError(f"authentication exposed {BOT_TOKEN}"),
        SlackApiError(
            "authentication failed",
            slack_response(data={"ok": False, "error": BOT_TOKEN}),
        ),
        SlackRequestError(f"request exposed {BOT_TOKEN}"),
    ],
    ids=["generic", "slack-api", "slack-request"],
)
def test_b12_cli_never_leaks_token_in_output_logs_or_exception_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    client = FakeSlackSeedClient(
        world,
        failures={("auth_test", 1): error},
    )
    prepare_cli(monkeypatch, client)
    caplog.set_level(logging.DEBUG)

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
    assert BOT_TOKEN not in caplog.text
    assert all(BOT_TOKEN not in record.getMessage() for record in caplog.records)
    assert BOT_TOKEN not in exception_chain
    assert "run" in result.output.lower() and "again" in result.output.lower()


def test_a20_cli_redacts_value_error_from_post_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeSlackSeedClient(
        world,
        failures={("post", 1): ValueError(f"post exposed {BOT_TOKEN}")},
    )
    prepare_cli(monkeypatch, client)
    caplog.set_level(logging.DEBUG)

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
    assert "ValueError" in result.output
    assert "run the command again" in result.output
    assert BOT_TOKEN not in result.output
    assert BOT_TOKEN not in caplog.text
    assert all(BOT_TOKEN not in record.getMessage() for record in caplog.records)
    assert BOT_TOKEN not in exception_chain


def test_a20_cli_prints_seed_blocked_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    available = [channel for channel in slack_channels(world) if channel.name != "kestrel-program"]
    client = FakeSlackSeedClient(world, channels=available)
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
    assert "kestrel-program" in result.output
    assert not (tmp_path / "seed.json").exists()


def test_a22_command_without_map_uses_team_default_and_retries_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    client = FakeSlackSeedClient(world, failures={("auth_test", 1): RateLimited(0)})
    prepare_cli(monkeypatch, client)
    shutil.copytree(ROOT / "data" / "world" / "kestrel", tmp_path / "data" / "world" / "kestrel")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["seed", "--world", "kestrel", "--until-day", "1", "--pace", "0"],
    )

    expected = tmp_path / ".slack-seed" / "T-SEED-kestrel.json"
    assert result.exit_code == 0, result.output
    assert len(calls(client, "auth_test")) == 2
    assert expected.exists()


# Reseed an edited corpus (RS2 to RS9).


def reseed_map(path: Path, world: World, mapped: Sequence[Post]) -> SeedMap:
    messages = {
        item.key: MappedMessage(
            channel=f"S-{item.world_channel}",
            ts=f"1700000000.{900 - index:06d}",
            thread_ts=f"1700000000.{900 - index:06d}",
        )
        for index, item in enumerate(mapped)
    }
    for item in mapped:
        if item.root_key != item.key:
            messages[item.key].thread_ts = messages[item.root_key].ts
    value = SeedMap(
        team_id="T-SEED",
        workspace_url=WORKSPACE,
        world=world.name,
        complete=True,
        channels={item.world_channel: f"S-{item.world_channel}" for item in mapped},
        messages=messages,
        files={
            f"{item.key}/{name}": MappedFile(permalink=f"{WORKSPACE}/files/{name}")
            for item in mapped
            for name in item.files
        },
    )
    write_map(path, value)
    return value


def update_call(item: Post, saved: SeedMap) -> dict[str, Any]:
    mapped = saved.messages[item.key]
    return {
        "method": "update",
        "channel_id": mapped.channel,
        "ts": mapped.ts,
        "text": item.text,
        "metadata": item.metadata,
    }


def reseed_writes(client: FakeSlackSeedClient) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in call.items() if key != "result"}
        for call in client.calls
        if call["method"] in {*WRITE_METHODS, "update"}
    ]


def test_rs2_updates_roots_and_replies_and_skips_uncompared_text(
    tmp_path: Path, world: World
) -> None:
    root, reply, same, unreferenced = plan_seed(
        world,
        [
            event(ts="1791819003.100000", text="no reference"),
            event(ts="1791819001.100000", thread_ts="1791819000.100000", text="old reply"),
            event(ts="1791819002.100000", text="unchanged"),
            event(text="old root"),
        ],
        None,
    )
    changed_root = replace(root, text=reply.text, metadata={"event_type": "new metadata"})
    changed_reply = replace(reply, text="new reply")
    changed_metadata = replace(same, metadata={"event_type": "changed metadata only"})
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [unreferenced, reply, same, root])
    client = FakeSlackSeedClient(world)

    report = seed(
        world,
        [changed_root, changed_reply, changed_metadata, replace(unreferenced, text="also changed")],
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=[same, reply, root],
    )

    assert reseed_writes(client) == [
        update_call(changed_root, saved),
        update_call(changed_reply, saved),
    ]
    assert (report.posts, report.updated, report.files, report.channels) == (0, 2, 0, 1)


def test_rs3_new_replies_and_roots_mix_with_updates_in_world_order(
    tmp_path: Path, world: World
) -> None:
    sources = [
        event(ts="1791819003.100000", thread_ts="1791819002.100000", text="new root reply"),
        event(ts="1791819001.100000", thread_ts="1791819000.100000", text="new reply"),
        event(text="edited root"),
        event(ts="1791819002.100000", text="new root"),
        event(channel="C0ENGEF", ts="1791819001.100000", text="edited EE root"),
    ]
    planned = plan_seed(world, sources, None)
    root, new_reply, ee, new_root, root_reply = planned
    reference = [replace(ee, text="old EE"), replace(root, text="old ME")]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, reference)
    client = FakeSlackSeedClient(world)

    report = seed(
        world,
        planned,
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=reference,
    )

    def expected_post(item: Post, thread: str | None) -> dict[str, Any]:
        return {
            "method": "post",
            "channel_id": f"S-{item.world_channel}",
            "text": item.text,
            "thread_ts": thread,
            "username": item.username,
            "icon_emoji": item.icon_emoji,
            "metadata": item.metadata,
        }

    assert reseed_writes(client) == [
        update_call(root, saved),
        expected_post(new_reply, saved.messages[root.key].ts),
        update_call(ee, saved),
        expected_post(new_root, None),
        expected_post(root_reply, "1700000000.000002"),
    ]
    assert (report.posts, report.updated, report.channels) == (3, 2, 2)
    after = SeedMap.load(path)
    assert after is not None
    assert after.messages[root.key] == saved.messages[root.key]
    assert after.messages[ee.key] == saved.messages[ee.key]
    assert_mapped_messages(after, client)


def test_rs4_removed_keys_follow_reference_order_and_are_not_deleted(
    tmp_path: Path, world: World
) -> None:
    kept, early, late, never_mapped, map_only = [
        post(key=f"C0ENGME:179181900{i}.100000") for i in range(5)
    ]
    reference = [late, never_mapped, kept, early]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [early, map_only, kept, late])
    changed = replace(kept, text="edited kept message")
    client = FakeSlackSeedClient(world)

    report = seed(
        world,
        [changed],
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=reference,
    )

    assert report.removed == (late.key, early.key)
    assert reseed_writes(client) == [update_call(changed, saved)]
    assert all("delete" not in call["method"] for call in client.calls)
    after = SeedMap.load(path)
    assert after is not None
    assert after.messages == saved.messages


@pytest.mark.parametrize("missing_file", [False, True], ids=["all-mapped", "one-missing"])
def test_rs5_update_uploads_only_missing_files_into_the_reply_thread(
    tmp_path: Path, world: World, missing_file: bool
) -> None:
    root = post()
    reply = post(
        key="C0ENGME:1791819001.100000", root_key=root.key, files=("z-last.pdf", "a-first.xlsx")
    )
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [reply, root])
    if missing_file:
        del saved.files[f"{reply.key}/a-first.xlsx"]
        write_map(path, saved)
    changed = replace(reply, text="edited file message")
    client = FakeSlackSeedClient(world)

    report = seed(
        world,
        [root, changed],
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=[reply, root],
    )

    expected = [update_call(changed, saved)]
    if missing_file:
        expected.append(
            {
                "method": "upload",
                "channel_id": saved.messages[reply.key].channel,
                "thread_ts": saved.messages[root.key].ts,
                "filename": "a-first.xlsx",
                "content": placeholder_file("a-first.xlsx"),
            }
        )
    assert reseed_writes(client) == expected
    assert (report.updated, report.posts, report.files, report.channels) == (
        1,
        0,
        int(missing_file),
        1,
    )
    after = SeedMap.load(path)
    assert after is not None
    assert after.messages == saved.messages
    assert after.files[f"{reply.key}/z-last.pdf"] == saved.files[f"{reply.key}/z-last.pdf"]
    assert after.files[f"{reply.key}/a-first.xlsx"].permalink == f"{WORKSPACE}/files/a-first.xlsx"


@pytest.mark.parametrize("pace", [0, 1.2])
def test_rs6_updates_share_pacing_with_posts_and_uploads(
    tmp_path: Path, world: World, pace: float
) -> None:
    root = post()
    reply = post(key="C0ENGME:1791819002.100000", root_key=root.key)
    added = post(key="C0ENGME:1791819001.100000", root_key=root.key, files=("one.pdf",))
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [reply, root])
    timeline: list[tuple[str, object]] = []
    client = FakeSlackSeedClient(world, timeline=timeline)
    changed_root, changed_reply = replace(root, text="new root"), replace(reply, text="new reply")

    seed(
        world,
        [changed_root, added, changed_reply],
        client,
        path,
        pace=pace,
        sleep=recording_sleep(timeline),
        dry_run=False,
        reference=[reply, root],
    )

    writes = reseed_writes(client)
    assert writes[0] == update_call(changed_root, saved)
    assert writes[-1] == update_call(changed_reply, saved)
    assert writes[1] == {
        "method": "post",
        "channel_id": "S-C0ENGME",
        "text": added.text,
        "thread_ts": saved.messages[root.key].ts,
        "username": added.username,
        "icon_emoji": added.icon_emoji,
        "metadata": added.metadata,
    }
    assert writes[2] == {
        "method": "upload",
        "channel_id": "S-C0ENGME",
        "thread_ts": saved.messages[root.key].ts,
        "filename": "one.pdf",
        "content": placeholder_file("one.pdf"),
    }
    expected: list[tuple[str, object]] = [("call", "update")]
    for method in ["post", "upload", "update"]:
        if pace:
            expected.append(("sleep", pace))
        expected.append(("call", method))
    assert [
        entry
        for entry in timeline
        if entry[0] == "sleep" or entry[1] in {"update", "post", "upload"}
    ] == expected


@pytest.mark.parametrize("failures", [1, 5, 6])
@pytest.mark.parametrize("target", [1, 2], ids=["first-write", "later-write"])
def test_rs6_update_retries_the_same_arguments_at_the_retry_limit(
    tmp_path: Path, world: World, failures: int, target: int
) -> None:
    reference = [post(key="C0ENGME:1791819001.100000"), post()]
    changed = [
        replace(item, text=f"edited {index}") for index, item in enumerate(reversed(reference))
    ]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, reference)
    waits: list[float] = []
    client = FakeSlackSeedClient(
        world,
        failures={
            ("update", target + offset): RateLimited(offset + 3) for offset in range(failures)
        },
    )

    if failures == 6:
        with pytest.raises(RateLimited):
            seed(
                world,
                changed,
                client,
                path,
                pace=1.2,
                sleep=waits.append,
                dry_run=False,
                reference=reference,
            )
    else:
        report = seed(
            world,
            changed,
            client,
            path,
            pace=1.2,
            sleep=waits.append,
            dry_run=False,
            reference=reference,
        )
        assert report.updated == 2
    assert [wait for wait in waits if wait != 1.2] == [
        float(offset + 3) for offset in range(min(failures, 5))
    ]
    assert waits.count(1.2) == (min(failures + 1, 6) if target == 2 else int(failures < 6))
    if target == 2:
        assert waits[0] == 1.2
    else:
        assert waits[0] == 3
    expected = [update_call(item, saved) for item in changed[: target - 1]]
    expected += [update_call(changed[target - 1], saved)] * min(failures + 1, 6)
    if failures < 6:
        expected += [update_call(item, saved) for item in changed[target:]]
    assert reseed_writes(client) == expected
    after = SeedMap.load(path)
    assert after is not None
    assert after.complete is (failures < 6)
    assert after.messages == saved.messages


@pytest.mark.parametrize("loaded_complete", [False, True])
def test_rs7_update_only_marks_map_incomplete_then_complete_without_checkpoints(
    tmp_path: Path,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded_complete: bool,
) -> None:
    root, other = post(), post(key="C0ENGME:1791819001.100000", files=("mapped.pdf",))
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [other, root])
    saved.complete = loaded_complete
    write_map(path, saved)
    before = path.read_bytes()
    available = [
        replace(channel, is_member=False) if channel.name == "eng-mech" else channel
        for channel in slack_channels(world)
    ]
    snapshots: list[SeedMap] = []
    save = SeedMap.save

    def record_save(value: SeedMap, target: Path) -> None:
        snapshots.append(value.model_copy(deep=True))
        save(value, target)

    def before_update(method: str, attempt: int) -> None:
        if method == "join":
            assert path.read_bytes() == before
            assert snapshots == []
        if method == "update":
            current = SeedMap.load(path)
            assert current is not None and current.complete is False
            assert current.messages == saved.messages and current.files == saved.files
            assert len(snapshots) == 1

    monkeypatch.setattr(SeedMap, "save", record_save)
    changed = [replace(root, text="new root"), replace(other, text="new other")]
    failed = FakeSlackSeedClient(
        world,
        channels=available,
        before_call=before_update,
        failures={("join", 1): RuntimeError("join stopped")},
    )
    with pytest.raises(RuntimeError, match="join stopped"):
        seed(
            world,
            changed,
            failed,
            path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
            reference=[other, root],
        )
    assert path.read_bytes() == before
    assert snapshots == []
    assert reseed_writes(failed) == [{"method": "join", "channel_id": "S-C0ENGME"}]
    assert (RESUME_LIMIT in capsys.readouterr().out) is (not loaded_complete)
    client = FakeSlackSeedClient(world, channels=available, before_call=before_update)

    report = seed(
        world,
        changed,
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=[other, root],
    )

    assert reseed_writes(client) == [
        {"method": "join", "channel_id": "S-C0ENGME"},
        *[update_call(item, saved) for item in changed],
    ]
    assert report.updated == 2 and report.complete is True
    assert [value.complete for value in snapshots] == [False, True]
    assert all(
        value.messages == saved.messages and value.files == saved.files for value in snapshots
    )
    assert (RESUME_LIMIT in capsys.readouterr().out) is (not loaded_complete)


@pytest.mark.parametrize("operation", ["post", "upload"])
def test_rs7_acknowledged_post_or_upload_is_saved_before_interruption_and_skipped_on_resume(
    tmp_path: Path, world: World, operation: str
) -> None:
    first = post()
    middle = post(
        key="C0ENGME:1791819001.100000",
        root_key=first.key,
        files=("new.pdf",) if operation == "upload" else (),
    )
    last = post(key="C0ENGME:1791819002.100000")
    reference = [first, last, middle] if operation == "upload" else [first, last]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, reference)
    file_key = f"{middle.key}/new.pdf"
    if operation == "upload":
        del saved.files[file_key]
        write_map(path, saved)
    changed_first, changed_last = replace(first, text="new first"), replace(last, text="new last")
    selected_middle = replace(middle, text="new middle") if operation == "upload" else middle
    selected = [changed_first, selected_middle, changed_last]
    stop_attempt = 3 if operation == "upload" else 2

    def interrupt_after_acknowledgement(method: str, attempt: int) -> None:
        if method != "update" or attempt != stop_attempt:
            return
        current = SeedMap.load(path)
        assert current is not None and current.complete is False
        acknowledged = successful_calls(stopped, operation)
        assert len(acknowledged) == 1
        if operation == "post":
            assert current.messages[middle.key] == MappedMessage(
                channel="S-C0ENGME",
                ts=acknowledged[0]["result"],
                thread_ts=saved.messages[first.key].ts,
            )
            assert current.files == saved.files
        else:
            assert current.files[file_key].permalink == acknowledged[0]["result"]
            assert current.messages == saved.messages
        raise RuntimeError("stopped after acknowledgement")

    stopped = FakeSlackSeedClient(world, before_call=interrupt_after_acknowledgement)
    with pytest.raises(RuntimeError, match="stopped after acknowledgement"):
        seed(
            world,
            selected,
            stopped,
            path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
            reference=reference,
        )

    checkpoint = SeedMap.load(path)
    assert checkpoint is not None and checkpoint.complete is False
    assert [call["method"] for call in reseed_writes(stopped)] == (
        ["update", "update", "upload"] if operation == "upload" else ["update", "post"]
    )
    resumed = FakeSlackSeedClient(world)
    report = seed(
        world,
        selected,
        resumed,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=reference,
    )
    repeated = (
        [changed_first, selected_middle, changed_last]
        if operation == "upload"
        else [changed_first, changed_last]
    )
    assert reseed_writes(resumed) == [update_call(item, saved) for item in repeated]
    assert (report.posts, report.files, report.updated, report.complete) == (
        0,
        0,
        len(repeated),
        True,
    )
    after = SeedMap.load(path)
    assert after is not None and after.complete is True
    assert after.messages == checkpoint.messages and after.files == checkpoint.files


def test_rs7_stopped_update_is_repeated_on_resume_against_the_reference(
    tmp_path: Path, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = post(), post(key="C0ENGME:1791819001.100000")
    reference = [second, first]
    changed = [replace(first, text="new first"), replace(second, text="new second")]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, reference)
    stopped = FakeSlackSeedClient(world, failures={("update", 2): RuntimeError("update stopped")})

    with pytest.raises(RuntimeError, match="update stopped"):
        seed(
            world,
            changed,
            stopped,
            path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
            reference=reference,
        )

    assert reseed_writes(stopped) == [update_call(item, saved) for item in changed]
    after = SeedMap.load(path)
    assert after is not None and after.complete is False
    assert after.messages == saved.messages and after.files == saved.files
    assert RESUME_LIMIT not in capsys.readouterr().out
    resumed = FakeSlackSeedClient(world)
    report = seed(
        world,
        changed,
        resumed,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=reference,
    )
    assert reseed_writes(resumed) == [update_call(item, saved) for item in changed]
    assert report.updated == 2 and report.complete is True
    assert RESUME_LIMIT in capsys.readouterr().out


@pytest.mark.parametrize("placement", ["world_channel", "root_key"])
@pytest.mark.parametrize("changed_text", [False, True])
@pytest.mark.parametrize("mapped", [False, True])
def test_rs7a_placement_check_precedes_all_writes_even_for_unchanged_text(
    tmp_path: Path, world: World, placement: str, changed_text: bool, mapped: bool
) -> None:
    first = post()
    later = post(key="C0ENGME:1791819001.100000")
    reference = [later, first]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, reference)
    if not mapped:
        del saved.messages[later.key]
        write_map(path, saved)
    changed = replace(first, text="first update")
    candidate = replace(later, text="later update") if changed_text else later
    wrong = replace(
        candidate, **{placement: "C0ENGEF" if placement == "world_channel" else first.key}
    )
    available = [
        replace(channel, is_member=False) if channel.name == "eng-mech" else channel
        for channel in slack_channels(world)
    ]
    blocked = FakeSlackSeedClient(world, channels=available)
    before = path.read_bytes()

    with pytest.raises(SeedBlocked, match=re.escape(later.key)):
        seed(
            world,
            [changed, wrong],
            blocked,
            path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
            reference=reference,
        )

    assert reseed_writes(blocked) == []
    assert path.read_bytes() == before
    allowed = FakeSlackSeedClient(world, channels=available)
    seed(
        world,
        [changed, candidate],
        allowed,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=reference,
    )
    expected = [{"method": "join", "channel_id": "S-C0ENGME"}, update_call(changed, saved)]
    if not mapped:
        expected.append(
            {
                "method": "post",
                "channel_id": "S-C0ENGME",
                "text": candidate.text,
                "thread_ts": None,
                "username": candidate.username,
                "icon_emoji": candidate.icon_emoji,
                "metadata": candidate.metadata,
            }
        )
    elif changed_text:
        expected.append(update_call(candidate, saved))
    assert reseed_writes(allowed) == expected


@pytest.mark.parametrize("mapped_reply", [False, True])
@pytest.mark.parametrize("changed_text", [False, True])
def test_rs7a_reference_root_does_not_satisfy_a_later_missing_root(
    tmp_path: Path, world: World, mapped_reply: bool, changed_text: bool
) -> None:
    first = post()
    root = post(key="C0ENGME:1791819001.100000")
    reply = post(key="C0ENGME:1791819002.100000", root_key=root.key)
    reference = [reply, root, first]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, reference)
    del saved.messages[root.key]
    if not mapped_reply:
        del saved.messages[reply.key]
    write_map(path, saved)
    changed = replace(first, text="first update")
    changed_reply = replace(reply, text="reply update") if changed_text else reply
    blocked = FakeSlackSeedClient(world)
    before = path.read_bytes()

    with pytest.raises(SeedBlocked, match=re.escape(reply.key)):
        seed(
            world,
            [changed, changed_reply],
            blocked,
            path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
            reference=reference,
        )

    assert reseed_writes(blocked) == []
    assert path.read_bytes() == before
    allowed = FakeSlackSeedClient(world)
    report = seed(
        world,
        [changed, root, changed_reply],
        allowed,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=reference,
    )
    writes = reseed_writes(allowed)
    assert writes[0] == update_call(changed, saved)
    assert writes[1] == {
        "method": "post",
        "channel_id": "S-C0ENGME",
        "text": root.text,
        "thread_ts": None,
        "username": root.username,
        "icon_emoji": root.icon_emoji,
        "metadata": root.metadata,
    }
    if mapped_reply and changed_text:
        assert writes[2] == update_call(changed_reply, saved)
    elif not mapped_reply:
        assert writes[2] == {
            "method": "post",
            "channel_id": "S-C0ENGME",
            "text": changed_reply.text,
            "thread_ts": "1700000000.000001",
            "username": reply.username,
            "icon_emoji": reply.icon_emoji,
            "metadata": reply.metadata,
        }
    assert len(writes) == 2 + int(not mapped_reply or changed_text)
    assert report.updated == 1 + int(mapped_reply and changed_text)


@pytest.mark.parametrize(
    "guard", ["channel", "scope", "files-scope", "team", "world", "channel-id"]
)
@pytest.mark.parametrize("changed_text", [False, True])
def test_rs7a_checks_mapped_posts_before_the_first_update(
    tmp_path: Path, world: World, guard: str, changed_text: bool
) -> None:
    original = post(files=("already-mapped.pdf",))
    changed = replace(original, text="edited") if changed_text else original
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [original])
    available = slack_channels(world)
    scopes = ALL_SCOPES
    team = "T-SEED"
    if guard == "channel":
        available = [channel for channel in available if channel.name != "eng-mech"]
    elif guard == "scope":
        scopes -= {"chat:write.customize"}
    elif guard == "files-scope":
        scopes -= {"files:write"}
    elif guard == "team":
        team = "T-OTHER"
    elif guard == "world":
        saved.world = "another-world"
        write_map(path, saved)
    else:
        available = [
            replace(channel, id="S-NEW") if channel.name == "eng-mech" else channel
            for channel in available
        ]
    client = FakeSlackSeedClient(world, channels=available, scopes=scopes, team_id=team)
    before = path.read_bytes()

    with pytest.raises(SeedBlocked):
        seed(
            world,
            [changed],
            client,
            path,
            pace=0,
            sleep=lambda _: None,
            dry_run=False,
            reference=[original],
        )

    assert reseed_writes(client) == []
    assert path.read_bytes() == before
    saved.world = world.name
    write_map(path, saved)
    allowed = FakeSlackSeedClient(world)
    report = seed(
        world,
        [changed],
        allowed,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=[original],
    )
    assert reseed_writes(allowed) == ([update_call(changed, saved)] if changed_text else [])
    assert report.updated == int(changed_text)


def test_rs7a_reference_only_channels_do_not_select_channels(tmp_path: Path, world: World) -> None:
    kept = post()
    removed = post(key="C0ENGEF:1791819001.100000", world_channel="C0ENGEF")
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [removed, kept])
    changed = replace(kept, text="edited")
    available = [channel for channel in slack_channels(world) if channel.id != "S-C0ENGEF"]
    client = FakeSlackSeedClient(world, channels=available)

    report = seed(
        world,
        [changed],
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=[removed, kept],
    )

    assert reseed_writes(client) == [update_call(changed, saved)]
    assert report.updated == 1 and report.removed == (removed.key,)


@pytest.mark.parametrize("blocked", [False, True])
def test_rs8_dry_run_prints_updates_and_removals_before_blockers_without_writes(
    tmp_path: Path, world: World, capsys: pytest.CaptureFixture[str], blocked: bool
) -> None:
    kept, early, late = [post(key=f"C0ENGME:179181900{i}.100000") for i in range(3)]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [early, kept, late])
    changed = replace(kept, text="edited")
    added = post(key="C0ENGME:1791819003.100000")
    scopes = ALL_SCOPES - {"chat:write.customize"} if blocked else ALL_SCOPES
    client = FakeSlackSeedClient(world, scopes=scopes)
    before = path.read_bytes()

    report = seed(
        world,
        [changed, added],
        client,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=True,
        reference=[late, kept, early],
    )

    expected = [
        "eng-mech: 1 posts, 0 files",
        "updates: 1",
        f"removed: {late.key}",
        f"removed: {early.key}",
    ]
    if blocked:
        expected.append("missing scope: chat:write.customize")
    assert capsys.readouterr().out.splitlines() == expected
    assert reseed_writes(client) == [] and path.read_bytes() == before
    assert (report.posts, report.updated, report.files, report.channels) == (0, 0, 0, 0)
    assert report.complete is (not blocked)
    allowed = FakeSlackSeedClient(world)
    seed(
        world,
        [changed],
        allowed,
        path,
        pace=0,
        sleep=lambda _: None,
        dry_run=False,
        reference=[late, kept, early],
    )
    assert reseed_writes(allowed) == [update_call(changed, saved)]


@pytest.mark.parametrize("empty_reference", [False, True], ids=["none", "empty"])
def test_rs8_empty_reference_selects_new_summary_and_skips_mapped_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World, empty_reference: bool
) -> None:
    original_event = event().model_copy(
        update={"reactions": (Reaction(name="eyes", user_id="U0EE01", ts="1791819001.100000"),)}
    )
    edited_event = original_event.model_copy(update={"text": "edited"})
    original = plan_seed(world, [original_event], None)[0]
    changed = plan_seed(world, [edited_event], None)[0]
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, [original])
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)
    destination = tmp_path / "data" / "world" / "kestrel"
    shutil.copytree(ROOT / "data" / "world" / "kestrel", destination)
    (destination / "events.jsonl").write_text(
        edited_event.model_dump_json() + "\n", encoding="utf-8"
    )
    reference_path = tmp_path / "reference.jsonl"
    reference_path.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = ["seed", "--world", "kestrel", "--map", str(path), "--pace", "0"]
    if empty_reference:
        args.extend(["--reference", str(reference_path)])

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    counts = "0 posts, 0 updates and 0 files" if empty_reference else "0 posts and 0 files"
    assert result.output.splitlines() == [
        f"seeded {counts} in 0 channels; 1 reactions not seeded; map {path}"
    ]
    assert reseed_writes(client) == []
    reference_path.write_text(original_event.model_dump_json() + "\n", encoding="utf-8")
    compared = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, compared)
    monkeypatch.chdir(tmp_path)
    positive = runner.invoke(
        app,
        [
            "seed",
            "--world",
            "kestrel",
            "--map",
            str(path),
            "--reference",
            str(reference_path),
            "--pace",
            "0",
        ],
    )
    assert positive.exit_code == 0, positive.output
    assert reseed_writes(compared) == [update_call(changed, saved)]
    assert positive.output.splitlines()[-1] == (
        f"seeded 0 posts, 1 updates and 0 files in 1 channels; 1 reactions not seeded; map {path}"
    )


class UpdateWebClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def chat_update(self, **kwargs: Any) -> SlackResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return slack_response(data={"ok": True, "ts": "1700000000.000700"})


@pytest.mark.parametrize("rate_limited", [False, True])
def test_rs9_adapter_update_sends_exact_arguments_and_maps_429(rate_limited: bool) -> None:
    response = slack_response(
        data={"ok": False, "error": "ratelimited"}, status=429, headers={"Retry-After": "7"}
    )
    web = UpdateWebClient(SlackApiError("rate limited", response) if rate_limited else None)
    adapter = SlackWebSeedClient(web)
    metadata = {"event_type": "downstream_world_event", "event_payload": {"world": "kestrel"}}

    if rate_limited:
        with pytest.raises(RateLimited) as caught:
            adapter.update("S-C0ENGME", "1700000000.000700", "edited", metadata=metadata)
        assert caught.value.retry_after == 7
    else:
        assert adapter.update("S-C0ENGME", "1700000000.000700", "edited", metadata=metadata) is None
    assert web.calls == [
        {"channel": "S-C0ENGME", "ts": "1700000000.000700", "text": "edited", "metadata": metadata}
    ]


@pytest.mark.parametrize("mode", ["both", "events-only", "reference-only"])
def test_rs9_cli_reads_selected_corpora_and_applies_until_day_to_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World, mode: str
) -> None:
    root_ts = timestamp_at(world, 1, time(23, 30))
    reply_ts = timestamp_at(world, 1, time(23, 31))
    removed_ts = timestamp_at(world, 1, time(23, 32))
    second_removed_ts = timestamp_at(world, 1, time(23, 33))
    later_ts = timestamp_at(world, 2, time(0, 30))
    root = event(ts=root_ts, text="old <@U0ME01>")
    removed = event(ts=removed_ts, text="removed")
    later = event(ts=later_ts, text="old day two")
    second_removed = event(ts=second_removed_ts, text="also removed")
    reference_events = [root, removed, second_removed, later]
    edited_events = [
        root.model_copy(update={"text": "new <@U0ME01>"}),
        event(ts=reply_ts, thread_ts=root_ts, text="new reply"),
        later.model_copy(update={"text": "new day two"}),
    ]
    client = FakeSlackSeedClient(world)
    prepare_cli(monkeypatch, client)
    destination = tmp_path / "data" / "world" / "kestrel"
    shutil.copytree(ROOT / "data" / "world" / "kestrel", destination)
    reference_path, edited_path = tmp_path / "reference.jsonl", tmp_path / "edited.jsonl"
    reference_path.write_text(
        "".join(item.model_dump_json() + "\n" for item in reference_events), encoding="utf-8"
    )
    edited_path.write_text(
        "".join(item.model_dump_json() + "\n" for item in edited_events), encoding="utf-8"
    )
    (destination / "events.jsonl").write_text(
        edited_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "seed.json"
    saved = reseed_map(path, world, plan_seed(world, reference_events, None))
    args = ["seed", "--world", "kestrel", "--map", str(path), "--until-day", "1", "--pace", "0"]
    if mode != "reference-only":
        args.extend(["--events", str(edited_path)])
        # The explicit corpus must win over the world's default file.
        (destination / "events.jsonl").write_text(root.model_dump_json() + "\n", encoding="utf-8")
    if mode != "events-only":
        args.extend(["--reference", str(reference_path)])

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    changed_root, added = plan_seed(world, edited_events, 1)
    expected = [] if mode == "events-only" else [update_call(changed_root, saved)]
    expected.append(
        {
            "method": "post",
            "channel_id": saved.messages[changed_root.key].channel,
            "text": "new reply",
            "thread_ts": saved.messages[changed_root.key].ts,
            "username": added.username,
            "icon_emoji": added.icon_emoji,
            "metadata": added.metadata,
        }
    )
    assert reseed_writes(client) == expected
    counts = "1 posts and 0 files" if mode == "events-only" else "1 posts, 1 updates and 0 files"
    suffix = [f"seeded {counts} in 1 channels; 0 reactions not seeded; map {path}"]
    if mode != "events-only":
        suffix.extend(
            [
                f"not deleted: {removed.channel_id}:{removed.ts}",
                f"not deleted: {second_removed.channel_id}:{second_removed.ts}",
            ]
        )
    assert result.output.splitlines()[-len(suffix) :] == suffix
    after = SeedMap.load(path)
    assert (
        after is not None
        and after.messages[f"{later.channel_id}:{later.ts}"]
        == saved.messages[f"{later.channel_id}:{later.ts}"]
    )

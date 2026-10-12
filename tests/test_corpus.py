import json
import re
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, time
from functools import partialmethod
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from digest.cli import app
from digest.corpus.avoid import AvoidNames, find_avoided, load_avoid_names
from digest.corpus.prompts import render_request
from digest.corpus.render import RenderError, render_world, thread_model
from digest.llm import LLMRequest, LLMResponse, strict_schema
from digest.llm.cassette import Cassettes, request_key
from digest.llm.client import LLM
from digest.models import Event, permalink_for, ts_micros
from digest.replay import read_events
from digest.settings import Settings
from digest.world.manifest import Manifest, ThreadRef, load_manifest, write_manifest
from digest.world.schema import World, WorldEvent

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = "https://kestrel-test.slack.com"
EMPTY_AVOID = AvoidNames(names=(), identifiers=(), phrases=())
runner = CliRunner()


def world_data() -> dict[str, Any]:
    return {
        "name": "small",
        "program": "Kestrel K2",
        "workspace_url": WORKSPACE,
        "timezone": "America/Los_Angeles",
        "start_date": "2026-10-12",
        "days": 5,
        "project": {
            "project_id": "kestrel-test",
            "name": "Compact warehouse mover",
            "phases": [{"phase": "EVT", "from_date": "2026-10-12"}],
            "gates": [],
        },
        "channels": [
            {
                "id": "CMAIN",
                "name": "kestrel-program",
                "purpose": "Coordinate the build",
            },
            {"id": "COTHER", "name": "private-lab", "purpose": "Restricted tests"},
        ],
        "personas": [
            {
                "user_id": "UME",
                "name": "Morgan Vale",
                "role": "ME",
                "channels": ["CMAIN"],
                "aliases": ["mechanical owner"],
            },
            {
                "user_id": "UEE",
                "name": "Riley Shaw",
                "role": "EE",
                "channels": ["CMAIN"],
                "aliases": ["electrical owner"],
            },
            {
                "user_id": "UEM",
                "name": "Casey Holt",
                "role": "EM",
                "channels": ["CMAIN"],
            },
            {
                "user_id": "BPLM",
                "name": "plm_bot",
                "kind": "bot",
                "channels": ["CMAIN"],
                "authority": "plm_bot",
            },
            {
                "user_id": "UOUT",
                "name": "Taylor Quill",
                "role": "PM",
                "channels": ["COTHER"],
            },
        ],
        "entities": [
            {
                "id": "PN-71010",
                "type": "part",
                "description": "Drive plate",
                "aliases": ["drive plate", "motor plate"],
            }
        ],
        "events": [
            {
                "id": "E-ASK",
                "day": 2,
                "channel": "CMAIN",
                "poster": "UEM",
                "kind": "ask",
                "summary": "The manager asks for the plate release status.",
                "scenario": "S-SHARED",
                "noise": {"delayed_crosspost": True},
                "ask": {"of": "UEE", "answered_day": 3},
            },
            {
                "id": "E-DECISION",
                "day": 1,
                "channel": "CMAIN",
                "poster": "UME",
                "kind": "decision",
                "summary": "The team releases the drive plate at Rev B.",
                "scenario": "S-SHARED",
                "claims": [{"entity": "PN-71010", "attribute": "rev", "value": "B"}],
                "noise": {"indirection": False},
            },
        ],
    }


def small_world() -> World:
    return World.model_validate(world_data())


def rendered_messages(event: WorldEvent) -> list[dict[str, Any]]:
    if event.kind == "ask":
        return [
            {
                "poster": event.poster,
                "day_offset": 0,
                "minutes_after_start": 2,
                "text": "<@UEE> can you confirm the release status?",
                "files": [],
                "reactions": [],
            },
            {
                "poster": "UEE",
                "day_offset": 1,
                "minutes_after_start": 10,
                "text": "Confirmed. It is released.",
                "files": [],
                "reactions": [{"name": "white_check_mark", "user_id": "UME"}],
            },
        ]
    return [
        {
            "poster": event.poster,
            "day_offset": 0,
            "minutes_after_start": 0,
            "text": "PN-71010 (drive plate) is now Rev B.",
            "files": ["PN-71010-rev-B.pdf"],
            "reactions": [],
        },
        {
            "poster": "UEE",
            "day_offset": 0,
            "minutes_after_start": 5,
            "text": "Electrical is clear.",
            "files": [],
            "reactions": [{"name": "+1", "user_id": "UEM"}],
        },
        {
            "poster": "UME",
            "day_offset": 0,
            "minutes_after_start": 8,
            "text": "I will send the released file.",
            "files": [],
            "reactions": [],
        },
    ]


def earlier_events(world: World, event: WorldEvent) -> tuple[WorldEvent, ...]:
    if event.scenario is None:
        return ()
    position = world.events.index(event)
    return tuple(item for item in world.events[:position] if item.scenario == event.scenario)


def request_for(world: World, event: WorldEvent, avoid: AvoidNames) -> LLMRequest:
    return render_request(
        world,
        event,
        earlier_events(world, event),
        avoid,
        thread_model(world, event).model_json_schema(),
    )


def put_response(
    cassettes: Cassettes,
    request: LLMRequest,
    messages: list[dict[str, Any]],
) -> None:
    data = {"messages": messages}
    cassettes.put(
        request_key(request),
        request,
        LLMResponse(
            text=json.dumps(data),
            data=data,
            backend="openrouter",
            model="gpt-render-test",
        ),
    )


def replay_llm(
    tmp_path: Path,
    world: World,
    manifest: Manifest,
    avoid: AvoidNames = EMPTY_AVOID,
    overrides: dict[str, list[dict[str, Any]]] | None = None,
) -> LLM:
    cassettes = Cassettes(tmp_path / "cassettes")
    overrides = overrides or {}
    for event in world.events:
        ref = manifest.threads.get(event.id)
        if ref is not None and ref.authored == "human":
            continue
        request = request_for(world, event, avoid)
        put_response(cassettes, request, overrides.get(event.id, rendered_messages(event)))
    return LLM(None, cassettes, "replay")


def write_events(path: Path, events: list[Event]) -> None:
    path.write_text("".join(event.model_dump_json() + "\n" for event in events), encoding="utf-8")


def anchor_event(world: World, event: WorldEvent) -> Event:
    zone = ZoneInfo(world.timezone)
    moment = datetime.combine(world.date_of(event.day), time(8, 30), zone)
    seconds = int(moment.timestamp())
    ts = f"{seconds}.000100"
    channel = next(item for item in world.channels if item.id == event.channel)
    return Event(
        source="replay",
        channel_id=channel.id,
        channel_name=channel.name,
        thread_ts=ts,
        ts=ts,
        event_ts=ts,
        user_id=event.poster,
        text="Human-authored anchor line.",
        permalink=permalink_for(world.workspace_url, channel.id, ts, ts),
    )


class RecordingLLM(LLM):
    def __init__(self, root: Path) -> None:
        super().__init__(None, Cassettes(root), "replay")
        self.requests: list[LLMRequest] = []

    def extract[T: BaseModel](self, request: LLMRequest, model_cls: type[T]) -> T:
        self.requests.append(request)
        return super().extract(request, model_cls)


def schema_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from schema_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from schema_nodes(child)


def test_thread_model_schema_is_strict_and_channel_scoped() -> None:
    world = small_world()
    event = world.events[1]
    model = thread_model(world, event)

    schema = strict_schema(model.model_json_schema())
    for node in schema_nodes(schema):
        properties = node.get("properties")
        if properties is not None:
            assert set(node["required"]) == set(properties)
        if "minItems" in node:
            assert node["minItems"] <= 1
    poster = next(
        definition["properties"]["poster"]
        for definition in schema["$defs"].values()
        if "poster" in definition.get("properties", {})
    )
    reaction_user = next(
        definition["properties"]["user_id"]
        for definition in schema["$defs"].values()
        if "user_id" in definition.get("properties", {})
    )
    assert set(poster["enum"]) == {"UME", "UEE", "UEM", "BPLM"}
    assert set(reaction_user["enum"]) == {"UME", "UEE", "UEM"}
    invalid = {"messages": rendered_messages(event)}
    invalid["messages"][0]["poster"] = "UOUT"
    with pytest.raises(ValidationError, match="poster"):
        model.model_validate(invalid)


def test_render_request_carries_context_rules_and_no_internal_ids(tmp_path: Path) -> None:
    world = small_world()
    manifest = Manifest(threads={})
    avoid = AvoidNames(
        names=("Forbidden Person",),
        identifiers=("BAD-100",),
        phrases=("other robot program",),
    )
    cassettes = Cassettes(tmp_path / "cassettes")
    expected_requests: dict[str, LLMRequest] = {}
    for event in world.events:
        request = request_for(world, event, avoid)
        expected_requests[event.id] = request
        put_response(cassettes, request, rendered_messages(event))
    llm = RecordingLLM(cassettes.root)

    report = render_world(
        world,
        llm,
        tmp_path / "anchor.jsonl",
        manifest,
        avoid,
        tmp_path / "events.jsonl",
        tmp_path / "manifest.json",
    )

    assert report.rendered == 2
    assert llm.requests == [expected_requests["E-DECISION"], expected_requests["E-ASK"]]
    for request in llm.requests:
        assert request.model == "render"
        assert (
            request.output_schema
            == request_for(
                world,
                next(
                    event
                    for event in world.events
                    if event.day == (1 if request is llm.requests[0] else 2)
                ),
                avoid,
            ).output_schema
        )
        assert len(request.messages) == 1
        prompt = request.messages[0].content
        assert world.program in prompt
        assert world.project.name in prompt
        assert "kestrel-program" in prompt and "Coordinate the build" in prompt
        assert all(value in prompt for value in ("UME", "Morgan Vale", "ME", "mechanical owner"))
        assert "Monday" in prompt or "Tuesday" in prompt
        assert "E-ASK" not in prompt and "E-DECISION" not in prompt
        assert "S-SHARED" not in prompt
        assert "scenario" not in prompt.casefold()
    decision_prompt = llm.requests[0].messages[0].content
    assert all(
        value in decision_prompt
        for value in (
            "UME",
            "decision",
            "The team releases the drive plate at Rev B.",
            "PN-71010",
            "drive plate",
            "motor plate",
            "B",
            "indirection",
            "False",
            "The manager asks for the plate release status.",
            "3",
            "8",
        )
    )
    ask_prompt = llm.requests[1].messages[0].content
    assert all(
        value in ask_prompt
        for value in ("UEM", "ask", "UEE", str(world.date_of(3)), "delayed_crosspost", "2")
    )
    system = llm.requests[0].system
    folded = system.casefold()
    assert all(
        entry.casefold() in folded for entry in (*avoid.names, *avoid.identifiers, *avoid.phrases)
    )
    for concepts in (
        ("short", "brief"),
        ("informal", "chat"),
        ("<@u", "mention"),
        ("part number", "identifier"),
        ("revision", "rev"),
        ("narrat", "invent"),
        ("would know", "knowledge", "authority"),
        ("indirection", "implied"),
        ("delayed_crosspost", "cross-post", "restat"),
        ("output", "json", "schema"),
    ):
        assert any(concept in folded for concept in concepts)
    assert "scenario" not in folded


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("fyi", (1, 3)),
        ("bom_change", (1, 3)),
        ("decision", (3, 8)),
        ("risk", (3, 8)),
        ("schedule_change", (3, 8)),
        ("spec_change", (3, 8)),
        ("blocker", (3, 6)),
        ("distractor", (2, 6)),
    ],
)
def test_render_request_sets_the_message_count_for_each_kind(
    kind: str, expected: tuple[int, int]
) -> None:
    data = world_data()
    event = data["events"][1]
    event["kind"] = kind
    world = World.model_validate(data)

    prompt = request_for(world, world.events[1], EMPTY_AVOID).messages[0].content

    low, high = expected
    range_pattern = rf"(?:{low}\s*(?:-|to)\s*{high}|between\s+{low}\s+and\s+{high})"
    assert re.search(
        rf"(?:message.{{0,40}}{range_pattern}|{range_pattern}.{{0,40}}message)",
        prompt,
        re.IGNORECASE,
    )


def test_render_request_states_when_an_ask_is_never_answered() -> None:
    data = world_data()
    data["events"] = [data["events"][0]]
    data["events"][0]["ask"]["answered_day"] = None
    world = World.model_validate(data)

    prompt = request_for(world, world.events[0], EMPTY_AVOID).messages[0].content

    assert "UEE" in prompt
    assert re.search(
        r"never\s+(?:answered|replied)|(?:no|without)\s+(?:answer|reply)", prompt, re.I
    )
    assert re.search(r"(?:target|message).{0,30}\b1\b", prompt, re.IGNORECASE)


def test_render_assigns_timestamps_fields_mentions_reactions_and_permalinks(tmp_path: Path) -> None:
    data = world_data()
    data["events"][0]["day"] = 1
    data["events"][0]["ask"]["answered_day"] = 2
    data["events"] = [data["events"][1], data["events"][0]]
    world = World.model_validate(data)
    manifest = Manifest(threads={})
    anchor = tmp_path / "anchor.jsonl"
    anchor.write_text("", encoding="utf-8")
    llm = replay_llm(tmp_path, world, manifest)

    render_world(
        world,
        llm,
        anchor,
        manifest,
        EMPTY_AVOID,
        tmp_path / "events.jsonl",
        tmp_path / "manifest.json",
    )
    events = list(read_events(tmp_path / "events.jsonl", world.workspace_url))

    assert [event.ts.rsplit(".", 1)[1] for event in events] == [
        "010000",
        "010001",
        "010002",
        "010003",
        "010004",
    ]
    zone = ZoneInfo(world.timezone)
    expected = [
        datetime.combine(world.date_of(1), time(8, 30), zone),
        datetime.combine(world.date_of(1), time(8, 35), zone),
        datetime.combine(world.date_of(1), time(8, 38), zone),
        datetime.combine(world.date_of(1), time(8, 57), zone),
        datetime.combine(world.date_of(2), time(9, 5), zone),
    ]
    assert [event.at.astimezone(zone).replace(microsecond=0) for event in events] == expected
    roots = [event for event in events if event.thread_ts == event.ts]
    assert [event.user_id for event in roots] == ["UME", "UEM"]
    assert all(event.event_ts == event.ts for event in events)
    assert events[3].mentions == ("UEE",)
    assert events[4].reactions[0].user_id == "UME"
    assert ts_micros(events[4].reactions[0].ts) - ts_micros(events[4].ts) == 1_000_000
    channels = {channel.id: channel for channel in world.channels}
    for event in events:
        channel = channels[event.channel_id]
        assert event.channel_name == channel.name
        assert event.permalink == permalink_for(
            world.workspace_url, event.channel_id, event.ts, event.thread_ts
        )


@pytest.mark.parametrize(
    ("case", "rule"),
    [
        ("missing-entity", r"claim|entity"),
        ("avoided-name", r"avoid|negative"),
        ("zero-messages", r"message|empty"),
        ("wrong-root-poster", r"poster"),
        ("root-day-offset", r"root.*day|day.*root"),
        ("non-last-day-offset", r"day.?offset|reply.*day"),
        ("reply-precedes-root", r"preced|before.*root"),
        ("timestamp-date", r"date|midnight|timestamp"),
        ("ask-reply-not-last", "last"),
        ("unanswered-ask-posts", r"unanswered|never.*answer|ask"),
        ("answered-ask-one-message", r"at least two|two.*message|reply"),
    ],
)
def test_render_rejects_invalid_thread_without_writes(tmp_path: Path, case: str, rule: str) -> None:
    data = world_data()
    event_index = 0 if case.startswith(("ask-", "unanswered", "answered")) else 1
    if case == "unanswered-ask-posts":
        data["events"][0]["ask"]["answered_day"] = None
    world = World.model_validate(data)
    event = world.events[event_index]
    messages = deepcopy(rendered_messages(event))
    avoid = EMPTY_AVOID
    if case == "missing-entity":
        messages[0]["text"] = "The release is now Rev B."
    elif case == "avoided-name":
        messages[0]["text"] += " Alex approved it."
        avoid = AvoidNames(names=("Alex",), identifiers=(), phrases=())
    elif case == "zero-messages":
        messages = []
    elif case == "wrong-root-poster":
        messages[0]["poster"] = "UEE"
    elif case == "root-day-offset":
        messages[0]["day_offset"] = 1
    elif case == "non-last-day-offset":
        messages[1]["day_offset"] = 1
    elif case == "reply-precedes-root":
        messages[1]["minutes_after_start"] = -1
    elif case == "timestamp-date":
        messages[0]["minutes_after_start"] = 1_000
    elif case == "ask-reply-not-last":
        messages.insert(
            1,
            {
                "poster": "UEE",
                "day_offset": 0,
                "minutes_after_start": 4,
                "text": "Early reply.",
                "files": [],
                "reactions": [],
            },
        )
        messages[-1]["poster"] = "UME"
        messages[-1]["day_offset"] = 1
    elif case == "unanswered-ask-posts":
        messages[1]["day_offset"] = 0
    elif case == "answered-ask-one-message":
        messages = messages[:1]
    anchored = [(other, anchor_event(world, other)) for other in world.events if other is not event]
    manifest = Manifest(
        threads={
            other.id: ThreadRef(
                channel_id=row.channel_id,
                thread_ts=row.thread_ts,
                authored="human",
            )
            for other, row in anchored
        }
    )
    anchor = tmp_path / "anchor.jsonl"
    write_events(anchor, [row for _, row in anchored])
    llm = replay_llm(tmp_path, world, manifest, avoid, {event.id: messages})
    request = request_for(world, event, avoid)
    events_out = tmp_path / "events.jsonl"
    manifest_out = tmp_path / "manifest.json"
    events_out.write_bytes(b"unchanged events")
    manifest_out.write_bytes(b"unchanged manifest")

    with pytest.raises(RenderError) as excinfo:
        render_world(world, llm, anchor, manifest, avoid, events_out, manifest_out)

    message = str(excinfo.value)
    assert event.id in message
    assert re.search(rule, message, re.IGNORECASE)
    assert request_key(request) in message
    assert events_out.read_bytes() == b"unchanged events"
    assert manifest_out.read_bytes() == b"unchanged manifest"


def test_indirection_allows_a_missing_claim_entity_and_empty_message_lists(tmp_path: Path) -> None:
    data = world_data()
    data["events"] = [data["events"][1]]
    data["events"][0]["noise"]["indirection"] = True
    world = World.model_validate(data)
    messages = rendered_messages(world.events[0])
    messages[0]["text"] = "The released configuration is now Rev B."
    messages[0]["files"] = []
    manifest = Manifest(threads={})
    anchor = tmp_path / "anchor.jsonl"
    anchor.write_text("", encoding="utf-8")
    llm = replay_llm(tmp_path, world, manifest, overrides={world.events[0].id: messages})

    report = render_world(
        world,
        llm,
        anchor,
        manifest,
        EMPTY_AVOID,
        tmp_path / "events.jsonl",
        tmp_path / "manifest.json",
    )

    events = list(read_events(tmp_path / "events.jsonl", world.workspace_url))
    assert report.messages == 3
    assert events[0].files == ()
    assert events[0].reactions == ()


def test_render_merges_anchor_updates_manifest_and_is_byte_stable(tmp_path: Path) -> None:
    world = small_world()
    human = world.events[0]
    anchor_row = anchor_event(world, human)
    anchor = tmp_path / "anchor.jsonl"
    write_events(anchor, [anchor_row])
    manifest = Manifest(
        threads={
            human.id: ThreadRef(
                channel_id=anchor_row.channel_id,
                thread_ts=anchor_row.thread_ts,
                authored="human",
            )
        }
    )
    llm = replay_llm(tmp_path, world, manifest)
    events_out = tmp_path / "events.jsonl"
    manifest_out = tmp_path / "manifest.json"

    first_report = render_world(world, llm, anchor, manifest, EMPTY_AVOID, events_out, manifest_out)
    first = events_out.read_bytes(), manifest_out.read_bytes()
    second_report = render_world(
        world, llm, anchor, manifest, EMPTY_AVOID, events_out, manifest_out
    )

    events = list(read_events(events_out, world.workspace_url))
    written_manifest = load_manifest(manifest_out)
    assert [event.event_ts for event in events] == sorted(event.event_ts for event in events)
    assert anchor_row in events
    assert written_manifest.threads[human.id] == manifest.threads[human.id]
    assert written_manifest.threads["E-DECISION"].authored == "render"
    assert first_report == second_report
    assert (events_out.read_bytes(), manifest_out.read_bytes()) == first


def test_all_human_world_rewrites_files_unchanged_without_model_calls(tmp_path: Path) -> None:
    world = small_world()
    anchored = sorted(
        ((event, anchor_event(world, event)) for event in world.events),
        key=lambda pair: pair[1].event_ts,
    )
    anchor_rows = [row for _, row in anchored]
    anchor = tmp_path / "anchor.jsonl"
    write_events(anchor, anchor_rows)
    manifest = Manifest(
        threads={
            event.id: ThreadRef(
                channel_id=row.channel_id, thread_ts=row.thread_ts, authored="human"
            )
            for event, row in anchored
        }
    )
    events_out = tmp_path / "events.jsonl"
    manifest_out = tmp_path / "manifest.json"
    events_out.write_bytes(anchor.read_bytes())
    write_manifest(manifest, manifest_out)
    before = events_out.read_bytes(), manifest_out.read_bytes()
    llm = RecordingLLM(tmp_path / "empty-cassettes")

    report = render_world(world, llm, anchor, manifest, EMPTY_AVOID, events_out, manifest_out)

    assert report.rendered == 0
    assert report.reused == 2
    assert report.messages == 0
    assert report.messages_per_thread == {}
    assert report.chars_per_message == ()
    assert llm.requests == []
    assert (events_out.read_bytes(), manifest_out.read_bytes()) == before


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Alex met ALEX; Jun replied to jun.", ["Alex", "Jun"]),
        ("Alexander met June.", []),
        ("Use (E204), not XE204 or E204A.", ["E204"]),
        ("The board bring-up is done; keyboard bring-upstream is not.", ["board bring-up"]),
        ("BAD-2 then alex then BAD-2 again", ["BAD-2", "Alex"]),
    ],
)
def test_find_avoided_matches_boundaries_case_order_and_uniqueness(
    text: str, expected: list[str]
) -> None:
    avoid = AvoidNames(
        names=("Alex", "Jun"), identifiers=("E204", "BAD-2"), phrases=("board bring-up",)
    )
    assert find_avoided(text, avoid) == expected


def test_bundled_avoid_list_is_populated_and_absent_from_world_corpus() -> None:
    avoid = load_avoid_names(ROOT / "data/world/avoid-names.yaml")
    assert avoid.names
    assert avoid.identifiers
    assert avoid.phrases
    world_dir = ROOT / "data/world/kestrel"
    paths = [world_dir / "world.yaml", world_dir / "events.jsonl"]
    paths.extend(
        path for name in ("anchor.jsonl", "golden.jsonl") if (path := world_dir / name).exists()
    )
    for path in paths:
        assert find_avoided(path.read_text(encoding="utf-8"), avoid) == [], path


def test_bundled_anchor_and_manifest_pin_the_nine_human_lines() -> None:
    world_dir = ROOT / "data/world/kestrel"
    anchor = world_dir / "anchor.jsonl"
    events = world_dir / "events.jsonl"
    manifest_path = world_dir / "manifest.json"

    assert anchor.is_file()
    assert manifest_path.is_file()
    assert anchor.read_bytes() == events.read_bytes()
    assert len(anchor.read_bytes().splitlines()) == 9
    lines = list(read_events(anchor, "https://kestrel-k2.slack.com"))
    manifest = load_manifest(manifest_path)
    expected = {
        "N1-TORQUE": ("C0EXTCM", "1791898872.000100"),
        "S1-AGREE": ("C0ENGME", "1791911662.000400"),
        "S1-CUT": ("C0EXTCM", "1792068738.000800"),
    }
    assert set(manifest.threads) == set(expected)
    for event_id, (channel_id, thread_ts) in expected.items():
        ref = manifest.threads[event_id]
        assert (ref.channel_id, ref.thread_ts, ref.authored) == (channel_id, thread_ts, "human")
        assert any(line.channel_id == channel_id and line.thread_ts == thread_ts for line in lines)


def test_render_report_counts_only_rendered_threads_and_message_lengths(tmp_path: Path) -> None:
    world = small_world()
    human = world.events[0]
    anchor_row = anchor_event(world, human)
    anchor = tmp_path / "anchor.jsonl"
    write_events(anchor, [anchor_row])
    manifest = Manifest(
        threads={
            human.id: ThreadRef(
                channel_id=anchor_row.channel_id,
                thread_ts=anchor_row.thread_ts,
                authored="human",
            )
        }
    )
    rendered = rendered_messages(world.events[1])
    llm = replay_llm(tmp_path, world, manifest, overrides={"E-DECISION": rendered})

    report = render_world(
        world,
        llm,
        anchor,
        manifest,
        EMPTY_AVOID,
        tmp_path / "events.jsonl",
        tmp_path / "manifest.json",
    )

    assert report.rendered == 1
    assert report.reused == 1
    assert report.messages == len(rendered)
    assert report.messages_per_thread == {"E-DECISION": len(rendered)}
    assert report.chars_per_message == tuple(len(message["text"]) for message in rendered)


def prepare_cli_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict[str, Any]] | None,
) -> World:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_WORLD", "small")
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_BACKEND", "openrouter")
    monkeypatch.setenv("DIGEST_CASSETTES", str(tmp_path / "data/cassettes"))
    monkeypatch.setattr(Settings, "__init__", partialmethod(Settings.__init__, _env_file=None))
    data = world_data()
    data["events"] = [data["events"][1]]
    world = World.model_validate(data)
    world_dir = tmp_path / "data/world/small"
    world_dir.mkdir(parents=True)
    (world_dir / "world.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    (world_dir / "anchor.jsonl").write_text("", encoding="utf-8")
    write_manifest(Manifest(threads={}), world_dir / "manifest.json")
    avoid_path = tmp_path / "data/world/avoid-names.yaml"
    avoid_path.write_text(
        "names: [Alex]\nidentifiers: [BAD-100]\nphrases: [bad phrase]\n", encoding="utf-8"
    )
    if messages is not None:
        avoid = load_avoid_names(avoid_path)
        request = request_for(world, world.events[0], avoid)
        put_response(Cassettes(tmp_path / "data/cassettes"), request, messages)
    return world


def test_cli_world_render_succeeds_in_replay_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = small_world()
    prepared = rendered_messages(world.events[1])
    prepare_cli_world(tmp_path, monkeypatch, prepared)

    result = runner.invoke(app, ["world", "render", "--world", "small"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "data/world/small/events.jsonl").is_file()
    assert (
        load_manifest(tmp_path / "data/world/small/manifest.json").threads["E-DECISION"].authored
        == "render"
    )


@pytest.mark.parametrize("failure", ["render-error", "cassette-miss"])
def test_cli_world_render_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    messages = None
    if failure == "render-error":
        messages = rendered_messages(small_world().events[1])
        messages[0]["text"] += " Alex approved it."
    prepare_cli_world(tmp_path, monkeypatch, messages)

    result = runner.invoke(app, ["world", "render", "--world", "small"])

    assert result.exit_code == 1, result.output
    if failure == "render-error":
        assert "E-DECISION" in result.output
        assert re.search(r"avoid|Alex", result.output, re.IGNORECASE)
    else:
        assert re.search(r"cassette|no cassette", result.output, re.IGNORECASE)

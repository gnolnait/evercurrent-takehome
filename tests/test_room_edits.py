"""M3 unit 10: message changes, corpus copies, and reseed controls."""

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from digest.llm import LLMRequest, LLMResponse, load_model_ids
from digest.llm.anthropic_api import AnthropicBackend
from digest.llm.cassette import CassetteMiss, Cassettes, request_key
from digest.llm.client import LLM
from digest.llm.openrouter import OpenRouterBackend
from digest.models import Event
from digest.priors import load_priors
from digest.replay import read_events
from digest.room import (
    Edit,
    EditMessage,
    MessageView,
    PostMessage,
    Room,
    RoomBase,
    RoomState,
    RunScenario,
    SetRole,
)
from digest.settings import Settings
from digest.store import Store
from digest.web.app import create_app
from digest.world.load import load_world
from test_extract import extracted_item, extraction_data, response
from test_room import DAY, WORLD_PATH, base_for
from test_room import day6_store as day6_store
from test_web import FakeRoom, Page

WORLD_DIR = WORLD_PATH.parent
CURATED = WORLD_DIR / "room-edits.yaml"
D2 = RunScenario(kind="run_scenario", name="d2-bracket-delay")
D3 = RunScenario(kind="run_scenario", name="d3-gate-delay")
ROLE = SetRole(kind="set_role", user_id="U0SC01", role="EM")
POST = PostMessage(
    kind="post_message", user_id="U0CM01", channel_id="C0EXTCM", text="Room test note."
)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_MODE", "replay")
    for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def base(request: pytest.FixtureRequest) -> RoomBase:
    # The day-6 store fixture is imported from test_room; asking for it by name keeps the
    # import from looking unused or redefined to the linters.
    store: Path = request.getfixturevalue("day6_store")
    return base_for(store, load_world(WORLD_PATH), load_priors())


@pytest.fixture
def corpus() -> list[Event]:
    return [
        Event.model_validate_json(line)
        for line in (WORLD_DIR / "events.jsonl").read_bytes().splitlines()
    ]


@pytest.fixture
def cassette_copy(tmp_path: Path) -> Path:
    return Path(shutil.copytree("data/cassettes", tmp_path / "cassettes"))


@pytest.fixture
def offline_responses(monkeypatch: pytest.MonkeyPatch, cassette_copy: Path) -> list[LLMRequest]:
    """Supply empty model results for arbitrary edits, only in the temporary cassettes.

    Scenario and curated tests do not use this fixture. Their results must be recorded.
    Extraction, validation, storage, and selection still run normally.
    """
    complete = LLM.complete
    requests: list[LLMRequest] = []

    def respond(client: LLM, request: LLMRequest) -> LLMResponse:
        assert client.cassettes.root.resolve() == cassette_copy.resolve()
        requests.append(request)
        try:
            return complete(client, request)
        except CassetteMiss:
            assert request.model in {"classify", "extract"}
            data: dict[str, Any] = {"rationale": "Offline room mechanics test."}
            data.update({"probability": 1.0} if request.model == "classify" else {"items": []})
            response = LLMResponse(
                text=json.dumps(data), data=data, backend="test", model="room-test"
            )
            client.cassettes.put(request_key(request), request, response)
            return complete(client, request)

    monkeypatch.setattr(LLM, "complete", respond)
    return requests


def edit_message(event: Event, text: str = "Updated room note.") -> EditMessage:
    return EditMessage(
        kind="edit_message",
        channel_id=event.channel_id,
        ts=event.ts,
        text=text,
        files=("replacement-pack.pdf",),
    )


def snapshot(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def fail_events_write(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.open

    def fail(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path.name == "events.jsonl" and mode == "w":
            with original(path, mode, *args, **kwargs) as file:
                file.write('{"partial":')
            raise OSError("events.jsonl write interrupted")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)


def saved_events(path: Path) -> list[Event]:
    return [
        Event.model_validate_json(line)
        for line in (path / "events.jsonl").read_bytes().splitlines()
    ]


def forbid_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("Replay built a model backend")

    monkeypatch.setattr(OpenRouterBackend, "__init__", fail)
    monkeypatch.setattr(AnthropicBackend, "__init__", fail)


@pytest.mark.parametrize(
    ("scenario", "entity", "quote", "owner"),
    [
        (D2, "PN-11051", "first articles move from 19 Oct to 2 Nov", "U0ME01"),
        (D3, "G-DVT1", "G-DVT1 moves from 22 Oct to 29 Oct", "U0EM01"),
    ],
    ids=["d2-bracket-delay", "d3-gate-delay"],
)
def test_scenario_routes_extracted_blocker(
    base: RoomBase,
    scenario: RunScenario,
    entity: str,
    quote: str,
    owner: str,
) -> None:
    room = Room(base)
    before = room.state()
    state = room.apply(scenario)
    matches = {
        selection.profile.user_id: [
            line
            for line in selection.lines
            if entity in line.item.entities
            and any(quote in evidence.quote for evidence in line.item.evidence)
        ]
        for selection in state.selections
    }
    assert len(matches[owner]) == 1, "The scenario blocker is missing from its reader's digest"
    blocker = matches[owner][0]
    assert blocker.item.type == "blocker"
    assert {uid for uid, lines in matches.items() if any(line.must_see for line in lines)} == {
        owner
    }
    if scenario == D3:
        assert matches["U0ME01"] == matches["U0SC01"] == []
    assert state.world == before.world
    assert any(blocker.item.item_id == line.item_id for diff in state.diffs for line in diff.added)
    assert any(state.last_effect)
    posted = [message for message in state.messages if message.posted]
    assert len(posted) == 1
    assert quote in posted[0].text
    expected_time = "17:30" if scenario == D2 else "17:40"
    assert (
        datetime.fromtimestamp(float(posted[0].ts), ZoneInfo(base.world.timezone)).strftime("%H:%M")
        == expected_time
    )


@pytest.mark.parametrize("case", ["unknown", "after-day-6", "bot", "nonmember", "wrong-thread"])
def test_message_refusal_keeps_state(
    base: RoomBase,
    corpus: list[Event],
    tmp_path: Path,
    case: str,
) -> None:
    if case == "after-day-6":
        future = next(
            e
            for e in corpus
            if datetime.fromtimestamp(float(e.ts), ZoneInfo(base.world.timezone)).date() > DAY
        )
        path = tmp_path / "future.sqlite"
        with Store.open(base.store_path) as original, original.copy_to(path) as copy:
            copy.upsert_events([future])
        base = base.model_copy(update={"store_path": path})
        change: Edit = edit_message(future)
        reason = r"(?i)(day.?6|after|beyond|2026-10-19)"
    elif case == "unknown":
        change = EditMessage(
            kind="edit_message", channel_id="C0EXTCM", ts="1.000000", text="Missing"
        )
        reason = r"(?i)(not.*(found|exist)|unknown|no .*message)"
    elif case == "bot":
        change = POST.model_copy(update={"user_id": "B0PLM01", "channel_id": "C0PLMFD"})
        reason = r"(?i)(bot|human|person)"
    elif case == "nonmember":
        assert (
            "C0LEADS"
            not in next(p for p in base.world.personas if p.user_id == POST.user_id).channels
        )
        change = POST.model_copy(update={"channel_id": "C0LEADS"})
        reason = r"(?i)(member|not in|does not belong)"
    else:
        root = next(e for e in corpus if e.channel_id == "C0PLMFD" and e.thread_ts == e.ts)
        change = POST.model_copy(update={"thread_ts": root.ts})
        reason = r"(?i)(thread|root)"
    room = Room(base)
    before = room.state()
    with pytest.raises(ValueError, match=reason):
        room.apply(change)
    assert room.state() == before


def test_same_log_gives_equal_states_and_reset_restarts_stamp(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    offline_responses: list[LLMRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rooms = [Room(base, cassettes=cassette_copy) for _ in range(2)]
    change = edit_message(corpus[0])
    for room in rooms:
        room.apply(ROLE)
        room.apply(change)
        room.apply(POST)
    assert rooms[0].state() == rooms[1].state()
    assert rooms[0].state().message_changes == 2
    with monkeypatch.context() as patch:
        patch.chdir(tmp_path)
        copies = [room.save(tmp_path / ".room" / f"copy-{i}") for i, room in enumerate(rooms)]
    assert snapshot(copies[0]) == snapshot(copies[1])
    rows = saved_events(copies[0])
    edited = next(e for e in rows if (e.channel_id, e.ts) == (change.channel_id, change.ts))
    zone = ZoneInfo(base.world.timezone)
    assert edited.edited is False
    assert edited.event_ts == edited.ts
    posted = next(e for e in rows if e.text == POST.text)
    assert datetime.fromtimestamp(float(posted.ts), zone) == datetime(
        2026, 10, 19, 18, 2, tzinfo=zone
    )
    assert posted.event_ts == posted.ts
    assert posted.thread_ts == posted.ts
    assert rooms[0].reset().message_changes == 0
    reset_state = rooms[0].apply(POST)
    fresh_state = Room(base, cassettes=cassette_copy).apply(POST)
    assert reset_state == fresh_state
    new = next(m for m in reset_state.messages if m.posted)
    assert datetime.fromtimestamp(float(new.ts), zone) == datetime(2026, 10, 19, 18, 1, tzinfo=zone)


def test_edit_and_reply_reach_extraction_and_preserve_message_identity(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    offline_responses: list[LLMRequest],
) -> None:
    room = Room(base, cassettes=cassette_copy)
    original = corpus[0]  # A bot message is a valid edit target.
    assert original.user_id == "B0PLM01"
    change = edit_message(original)
    state = room.apply(change)
    matches = [
        m for m in state.messages if (m.channel_id, m.ts) == (original.channel_id, original.ts)
    ]
    assert len(matches) == 1
    message = matches[0]
    assert (message.text, message.files, message.edited, message.posted) == (
        change.text,
        change.files,
        True,
        False,
    )
    assert (message.user_id, message.thread_ts) == (original.user_id, original.thread_ts)
    root = next(e for e in corpus if e.channel_id == POST.channel_id and e.thread_ts == e.ts)
    reply = POST.model_copy(update={"thread_ts": root.ts})
    state = room.apply(reply)
    posted = [m for m in state.messages if m.posted]
    assert len(posted) == 1
    assert (posted[0].user_id, posted[0].channel_id, posted[0].thread_ts, posted[0].text) == (
        reply.user_id,
        reply.channel_id,
        root.ts,
        reply.text,
    )
    extraction_texts = [
        m.content
        for request in offline_responses
        if request.model == "extract"
        for m in request.messages
    ]
    assert any(change.text in text for text in extraction_texts)
    assert any(reply.text in text for text in extraction_texts)
    offline_responses.clear()
    files_only = change.model_copy(update={"files": ("files-only-revision.pdf",)})
    room.apply(files_only)
    assert any(
        files_only.files[0] in message.content
        for request in offline_responses
        if request.model == "extract"
        for message in request.messages
    )


def test_scenario_discards_earlier_message_changes(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    offline_responses: list[LLMRequest],
) -> None:
    room = Room(base, cassettes=cassette_copy)
    room.apply(edit_message(corpus[0]))
    room.apply(POST)
    room.apply(D2)
    state = room.apply(D3)
    posted = [m for m in state.messages if m.posted]
    assert len(posted) == 1
    assert "G-DVT1 moves" in posted[0].text
    restored = next(
        m for m in state.messages if (m.channel_id, m.ts) == (corpus[0].channel_id, corpus[0].ts)
    )
    assert restored.text == corpus[0].text
    assert restored.files == corpus[0].files
    assert restored.edited == corpus[0].edited


def test_unrecorded_edit_is_atomic_and_builds_no_backend(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_backend(monkeypatch)
    room = Room(base, cassettes=cassette_copy)
    room.apply(D2)
    before = room.state()
    cassettes_before = snapshot(cassette_copy)
    store_before = base.store_path.read_bytes()
    with pytest.raises(ValueError, match="not recorded"):
        room.apply(edit_message(corpus[0], "Unrecorded room edit 7e78b806; lunar test fixture."))
    assert room.state() == before
    assert snapshot(cassette_copy) == cassettes_before
    assert base.store_path.read_bytes() == store_before
    assert room.apply(ROLE).messages == before.messages


def test_curated_edits_replay_first_after_reset(base: RoomBase) -> None:
    if not CURATED.exists():
        pytest.skip("room-edits.yaml is not recorded yet; curated edits are recorded tonight")
    entries = yaml.safe_load(CURATED.read_text(encoding="utf-8"))
    assert isinstance(entries, list) and entries, "Curated edits must be a nonempty list"
    room = Room(base)
    adapter: TypeAdapter[Edit] = TypeAdapter(Edit)
    for entry in entries:
        assert entry["name"] and entry["description"]
        change = adapter.validate_python(entry["edit"])
        assert isinstance(change, (EditMessage, PostMessage, RunScenario))
        room.reset()
        state = room.apply(change)
        assert state.message_changes == 1, entry["name"]
        if isinstance(change, EditMessage):
            actual = next(
                m for m in state.messages if (m.channel_id, m.ts) == (change.channel_id, change.ts)
            )
            assert (actual.text, actual.files) == (change.text, change.files), entry["name"]
        else:
            assert any(m.posted for m in state.messages), entry["name"]


@pytest.fixture
def thermal_review() -> PostMessage:
    entries = yaml.safe_load(CURATED.read_text(encoding="utf-8"))
    entry = next(entry for entry in entries if entry["name"] == "thermal-review-closed")
    return PostMessage.model_validate(entry["edit"])


def test_record_transport_failure_refuses_change_and_keeps_state(
    base: RoomBase,
    cassette_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup a record room with a failed gateway / Action post / Assert refusal and no change."""
    room = Room(base, record=True, cassettes=cassette_copy)
    before = room.state().model_copy(deep=True)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ConnectError("BODY-SENTINEL sk-test-SENTINEL-KEY", request=request)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        backend = OpenRouterBackend("sk-test-SENTINEL-KEY", load_model_ids(), client=client)

        def gateway(settings: Settings) -> LLM:
            assert settings.mode == "record"
            assert settings.backend == "openrouter"
            return LLM(backend, Cassettes(cassette_copy), "record")

        monkeypatch.setattr("digest.room.from_settings", gateway)
        with pytest.raises(ValueError) as caught:
            room.apply(POST)

    assert calls
    assert (
        "the live model call failed (a network failure, an auth or rate-limit refusal, "
        "another HTTP error, or output that did not validate)"
    ) in str(caught.value)
    assert room.state() == before


def test_failed_reset_preserves_curated_change_in_saved_corpus(
    base: RoomBase,
    thermal_review: PostMessage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup a curated change / Action fail reset then save / Assert the change survives."""
    room = Room(base)
    before = room.apply(thermal_review).model_copy(deep=True)
    posted = next(message for message in before.messages if message.posted)
    build = Room._build
    calls = 0

    def fail_once(self: Room, *args: Any, **kwargs: Any) -> RoomState:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("reset build failed")
        return build(self, *args, **kwargs)

    monkeypatch.setattr(Room, "_build", fail_once)
    with pytest.raises(RuntimeError, match=r"^reset build failed$"):
        room.reset()
    monkeypatch.chdir(tmp_path)
    rows = saved_events(room.save(tmp_path / ".room" / "copy"))

    assert calls == 1
    assert room.state() == before
    assert room.state().message_changes == 1
    assert [
        event.text
        for event in rows
        if (event.channel_id, event.ts) == (posted.channel_id, posted.ts)
    ] == [thermal_review.text]

    state = room.apply(ROLE)
    assert posted in state.messages
    assert state.message_changes == 1


def test_fresh_curated_edit_replaces_scenario_and_keeps_world_edit(
    base: RoomBase, thermal_review: PostMessage
) -> None:
    room = Room(base)
    scenario = next(m for m in room.apply(D2).messages if m.posted)
    world_state = room.apply(ROLE, fresh=True)
    assert scenario in world_state.messages
    assert world_state.message_changes == 1

    room.apply(thermal_review, fresh=True)

    state = room.state()
    assert state.message_changes == 1
    assert state.edits == (ROLE, thermal_review)
    assert next(p.role for p in state.world.personas if p.user_id == ROLE.user_id) == "EM"
    posted = [m for m in state.messages if m.posted]
    assert [m.text for m in posted] == [thermal_review.text]
    zone = ZoneInfo(base.world.timezone)
    assert datetime.fromtimestamp(float(posted[0].ts), zone) == datetime(
        2026, 10, 19, 18, 1, tzinfo=zone
    )
    as_of = datetime(2026, 10, 19, 23, 59, 59, tzinfo=zone)
    assert room._store is not None
    with Store.open_readonly(room._store) as store:
        curated_thread = store.events_in_thread(
            f"{thermal_review.channel_id}:{thermal_review.thread_ts}", as_of
        )
        scenario_thread = store.events_in_thread(
            f"{scenario.channel_id}:{scenario.thread_ts}", as_of
        )
    assert any(e.text == thermal_review.text and e.ts == posted[0].ts for e in curated_thread)
    assert not any(e.ts == scenario.ts for e in scenario_thread)


def test_curated_edit_without_fresh_after_scenario_is_not_recorded(
    base: RoomBase, thermal_review: PostMessage
) -> None:
    room = Room(base)
    room.apply(D2)
    room.apply(ROLE)
    before = room.state()
    assert before.message_changes == 1

    with pytest.raises(ValueError, match="not recorded"):
        room.apply(thermal_review)

    assert room.state() == before


def test_curated_and_scenario_forms_apply_fresh(
    base: RoomBase, thermal_review: PostMessage
) -> None:
    room = Room(base)
    scenario = next(m for m in room.apply(D2).messages if m.posted)
    entries = yaml.safe_load(CURATED.read_text(encoding="utf-8"))
    labels = {entry["name"] for entry in entries} | {f"Run {D2.name}", f"Run {D3.name}"}
    with TestClient(create_app(room)) as client:
        page = Page(client.get("/").text)
        forms = {
            button.text().strip(): form
            for form in page.find("form")
            for button in form.nodes()
            if button.tag == "button" and button.text().strip() in labels
        }
        assert forms.keys() == labels
        for label, form in forms.items():
            assert form.attrs.get("method") == "post", label
            assert form.attrs.get("action") == "/edit", label
            assert any(
                node.tag == "input"
                and node.attrs.get("type") == "hidden"
                and node.attrs.get("name") == "fresh"
                and node.attrs.get("value") == "true"
                for node in form.nodes()
            ), label
        fields = {
            str(node.attrs["name"]): node.attrs.get("value") or ""
            for node in forms["thermal-review-closed"].nodes()
            if node.tag == "input" and "name" in node.attrs
        }

        result = client.post("/edit", data=fields, follow_redirects=False)

        assert result.status_code == 303
        page = Page(client.get(result.headers["location"]).text)
    messages = {node.text() for node in page.find("p") if node.attrs.get("class") == "msg-text"}
    assert thermal_review.text in messages
    assert scenario.text not in messages
    assert room.state().message_changes == 1


def test_world_edit_after_scenario_builds_no_model_request(
    base: RoomBase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = Room(base)
    changed = room.apply(D2)
    assert any(m.posted for m in changed.messages), "Scenario must apply before the world edit"

    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("A world edit built a model request")

    monkeypatch.setattr(LLMRequest, "__init__", fail)
    monkeypatch.setattr(LLM, "complete", fail)
    state = room.apply(ROLE)
    assert next(p.role for p in state.world.personas if p.user_id == ROLE.user_id) == ROLE.role
    assert state.messages == changed.messages
    assert state.message_changes == changed.message_changes


def test_save_preserves_untouched_lines_and_committed_corpus(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    offline_responses: list[LLMRequest],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world_dir = WORLD_DIR.resolve()
    committed = snapshot(world_dir)
    room = Room(base, cassettes=cassette_copy)
    room.apply(D2)
    change = edit_message(corpus[0])
    room.apply(change)
    room.apply(POST)
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".room" / "kestrel"
    assert room.save(destination) == destination
    first = snapshot(destination)
    assert room.save(destination) == destination
    assert snapshot(destination) == first
    assert snapshot(world_dir) == committed
    for name, content in committed.items():
        if name != "events.jsonl":
            assert first[name] == content, name
    original_lines = {
        (e.channel_id, e.ts): line
        for e, line in zip(corpus, committed["events.jsonl"].splitlines(keepends=True), strict=True)
    }
    rows = saved_events(destination)
    raw_lines = (destination / "events.jsonl").read_bytes().splitlines(keepends=True)
    assert [e.ts for e in rows] == sorted(e.ts for e in rows)
    assert len(rows) == len(corpus) + 2
    by_key = {(e.channel_id, e.ts): (e, line) for e, line in zip(rows, raw_lines, strict=True)}
    assert len(by_key) == len(rows)
    for key, raw in original_lines.items():
        if key != (change.channel_id, change.ts):
            assert by_key[key][1] == raw, key
    edited = by_key[(change.channel_id, change.ts)][0]
    assert (edited.text, edited.files, edited.edited) == (change.text, change.files, False)
    assert edited.event_ts == edited.ts
    assert list(read_events(destination / "events.jsonl", base.world.workspace_url)) == rows
    added = [e for e in rows if (e.channel_id, e.ts) not in original_lines]
    assert {e.text for e in added} == {
        POST.text,
        next(m.text for m in room.state().messages if m.posted and "PN-11051" in m.text),
    }
    assert edited.model_dump(exclude={"text", "files", "edited", "event_ts"}) == corpus[
        0
    ].model_dump(exclude={"text", "files", "edited", "event_ts"})


def test_failed_save_keeps_previous_copy_and_removes_temporary_folder(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = Room(base)
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".room" / "copy"
    room.save(destination)
    before = snapshot(destination)
    siblings = set(destination.parent.iterdir())
    room.apply(D2)

    with monkeypatch.context() as patch:
        fail_events_write(patch)
        with pytest.raises(OSError, match=r"events\.jsonl write interrupted"):
            room.save(destination)

    assert snapshot(destination) == before
    assert set(destination.parent.iterdir()) == siblings


@pytest.mark.parametrize(
    "destination",
    [
        "data/world",
        "data/world/kestrel",
        ".",
        "..",
        "../copy",
        ".room",
        ".room/..",
        ".room/../copy",
        ".room/nested/..",
        ".room-other/copy",
    ],
)
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_save_refuses_paths_outside_room_before_any_write(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    absolute: bool,
) -> None:
    room = Room(base)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / destination if absolute else Path(destination)

    def refuse_write(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Save attempted a file system write before refusing the path")

    with monkeypatch.context() as patch:
        for name in ("mkdir", "open", "rename", "rmdir", "unlink"):
            patch.setattr(Path, name, refuse_write)
        patch.setattr(tempfile, "mkdtemp", refuse_write)
        patch.setattr(shutil, "copytree", refuse_write)
        patch.setattr(shutil, "rmtree", refuse_write)
        with pytest.raises(ValueError):
            room.save(path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("link", "redirect"),
    [
        ("symlink", None),
        ("junction", None),
        ("root", "elsewhere"),
        ("root", "data/world"),
        ("child", "data/world"),
        ("child", "data/world/kestrel"),
        ("external", "data/world/kestrel"),
    ],
    ids=[
        "root-symlink",
        "root-junction",
        "root-resolves-elsewhere",
        "root-resolves-to-world",
        "child-resolves-to-world-root",
        "child-resolves-to-world-copy",
        "external-alias-resolves-to-world-copy",
    ],
)
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_save_refuses_links_and_world_aliases_before_any_write(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link: str,
    redirect: str | None,
    absolute: bool,
) -> None:
    room = Room(base)
    monkeypatch.chdir(tmp_path)
    cwd = Path.cwd().resolve()
    root = cwd / ".room"
    alias = cwd / "world-alias" if link == "external" else root / "copy"
    source = root if link == "root" else alias
    path = alias if absolute else alias.relative_to(cwd)
    resolve = Path.resolve
    is_symlink = Path.is_symlink
    is_junction = Path.is_junction

    def resolve_alias(path: Path, strict: bool = False) -> Path:
        resolved = resolve(path, strict=strict)
        if redirect is not None and resolved.is_relative_to(source):
            return cwd / redirect / resolved.relative_to(source)
        return resolved

    def refuse_write(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Save attempted a file system write before refusing the path")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", resolve_alias)
        patch.setattr(
            Path, "is_symlink", lambda p: (p == root and link == "symlink") or is_symlink(p)
        )
        patch.setattr(
            Path, "is_junction", lambda p: (p == root and link == "junction") or is_junction(p)
        )
        for name in (
            "mkdir",
            "open",
            "write_text",
            "write_bytes",
            "rename",
            "replace",
            "rmdir",
            "unlink",
        ):
            patch.setattr(Path, name, refuse_write)
        patch.setattr(tempfile, "mkdtemp", refuse_write)
        patch.setattr(shutil, "copytree", refuse_write)
        patch.setattr(shutil, "rmtree", refuse_write)
        with pytest.raises(ValueError):
            room.save(path)

    assert list(tmp_path.iterdir()) == []


def test_save_warns_when_previous_copy_cannot_be_deleted(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = Room(base)
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".room" / "copy"
    room.save(destination)
    before = snapshot(destination)
    room.apply(D2)
    expected = snapshot(room.save(tmp_path / ".room" / "expected"))
    assert expected != before
    remove = shutil.rmtree
    leftovers: list[Path] = []

    def fail_delete(path: Path, ignore_errors: bool = False) -> None:
        path = Path(path)
        if path.parent == destination.parent and snapshot(path) == before:
            leftovers.append(path)
            raise PermissionError("previous copy is locked")
        remove(path, ignore_errors=ignore_errors)

    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING):
        patch.setattr(shutil, "rmtree", fail_delete)
        assert room.save(destination) == destination

    assert snapshot(destination) == expected
    assert len(leftovers) == 1
    assert snapshot(leftovers[0]) == before
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "room_folder_left"
    assert Path(warnings[0].__dict__["path"]) == leftovers[0]


def test_save_keeps_both_copies_when_install_and_restore_fail(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = Room(base)
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".room" / "copy"
    room.save(destination)
    before = snapshot(destination)
    room.apply(D2)
    expected = snapshot(room.save(tmp_path / ".room" / "expected"))
    assert expected != before
    rename = Path.rename
    attempts: list[Path] = []

    def fail_rename(path: Path, target: Path) -> Path:
        if target == destination:
            attempts.append(path)
            raise PermissionError("install or restore is locked")
        return rename(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_rename)
        with pytest.raises(OSError) as error:
            room.save(destination)

    assert len(attempts) == 2
    staging, previous = attempts
    assert staging != previous
    assert not destination.exists()
    assert snapshot(staging) == expected
    assert snapshot(previous) == before
    assert str(staging) in str(error.value)
    assert str(previous) in str(error.value)


def test_messages_cover_day_six_and_count_changes_since_reset(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    offline_responses: list[LLMRequest],
) -> None:
    room = Room(base, cassettes=cassette_copy)
    state = room.state()
    expected = {
        (e.channel_id, e.ts): e
        for e in corpus
        if datetime.fromtimestamp(float(e.event_ts), ZoneInfo(base.world.timezone)).date() <= DAY
    }
    assert {(m.channel_id, m.ts) for m in state.messages} == expected.keys()
    assert len(state.messages) == len(expected)
    assert not any(m.posted for m in state.messages)
    assert state.message_changes == 0
    assert [(m.channel_id, m.thread_ts, m.ts) for m in state.messages] == sorted(
        (m.channel_id, m.thread_ts, m.ts) for m in state.messages
    )
    names = {p.user_id: p.name for p in base.world.personas}
    for message in state.messages:
        original = expected[(message.channel_id, message.ts)]
        assert (message.text, message.files, message.author, message.channel_name) == (
            original.text,
            original.files,
            names[original.user_id],
            original.channel_name,
        )
    assert room.apply(edit_message(corpus[0])).message_changes == 1
    assert room.apply(ROLE).message_changes == 1
    state = room.apply(POST)
    assert state.message_changes == 2
    assert [m.text for m in state.messages if m.posted] == [POST.text]
    assert room.reset() == Room(base, cassettes=cassette_copy).state()


class FakeReseed:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, bool]] = []
        self.report = "Updated: 1; posted: 2; missing: 3 (not deleted). PN-11051 report."

    def __call__(self, copy_dir: Path, *, dry_run: bool) -> str:
        self.calls.append((copy_dir, dry_run))
        assert (copy_dir / "events.jsonl").is_file()
        return self.report


def page_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the page's default .room destination and corpus copy under tmp_path."""
    shutil.copytree(WORLD_DIR, tmp_path / WORLD_DIR)
    shutil.copytree("data/cassettes", tmp_path / "data/cassettes")
    monkeypatch.chdir(tmp_path)


def test_reseed_dry_run_shows_report_without_real_call(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = Room(base)
    room.apply(D2)
    page_directory(tmp_path, monkeypatch)
    reseed = FakeReseed()
    with TestClient(create_app(room, reseed=reseed)) as client:
        saved = client.post("/save")
        assert saved.status_code == 200
        assert reseed.calls == []
        response = client.post("/reseed?dry_run=true")
    assert response.status_code == 200
    assert len(reseed.calls) == 1
    assert reseed.calls[0][1] is True
    assert reseed.report in Page(response.text).root.text()
    assert "golden" in saved.text.lower()
    assert re.search(r"(?i)(no longer|not.*(describe|match)|unchanged)", saved.text)


def test_real_reseed_requires_save_and_separate_request(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = Room(base)
    room.apply(D2)
    page_directory(tmp_path, monkeypatch)
    reseed = FakeReseed()
    with TestClient(create_app(room, reseed=reseed)) as client:
        refused = client.post("/reseed?dry_run=false")
        assert refused.status_code == 400
        assert "sav" in Page(refused.text).root.text().lower()
        assert reseed.calls == []
        assert client.post("/save").status_code == 200
        assert reseed.calls == []
        assert client.post("/reseed?dry_run=true").status_code == 200
        assert [dry_run for _, dry_run in reseed.calls] == [True]
        response = client.post("/reseed?dry_run=false")
    assert response.status_code == 200
    assert [dry_run for _, dry_run in reseed.calls] == [True, False]
    assert reseed.calls[0][0] == reseed.calls[1][0]
    assert reseed.calls[0][0].resolve() == tmp_path / ".room" / "kestrel"
    assert reseed.report in Page(response.text).root.text()


def test_failed_save_blocks_reseed_until_save_succeeds(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = Room(base)
    room.apply(D2)
    page_directory(tmp_path, monkeypatch)
    reseed = FakeReseed()
    with TestClient(create_app(room, reseed=reseed)) as client:
        assert client.post("/save").status_code == 200
        assert reseed.calls == []

        with monkeypatch.context() as patch:
            fail_events_write(patch)
            failed = client.post("/save")

        assert failed.status_code == 400
        assert "events.jsonl write interrupted" in Page(failed.text).root.text()
        refused = client.post("/reseed?dry_run=false")
        assert refused.status_code == 400
        assert "sav" in Page(refused.text).root.text().lower()
        assert reseed.calls == []

        assert client.post("/save").status_code == 200
        assert reseed.calls == []
        allowed = client.post("/reseed?dry_run=false")

    assert allowed.status_code == 200
    assert reseed.calls == [(Path(".room") / base.world.name, False)]
    assert reseed.report in Page(allowed.text).root.text()


@pytest.mark.parametrize("dry_run", ["true", "false"])
def test_reseed_refuses_changes_after_save(
    base: RoomBase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: str,
) -> None:
    room = Room(base)
    room.apply(D2)
    page_directory(tmp_path, monkeypatch)
    reseed = FakeReseed()
    with TestClient(create_app(room, reseed=reseed)) as client:
        assert client.post("/save").status_code == 200
        assert client.post("/reseed?dry_run=true").status_code == 200
        assert len(reseed.calls) == 1
        saved = reseed.calls[0][0]
        before = snapshot(saved)
        room.apply(D3)
        response = client.post(f"/reseed?dry_run={dry_run}")
    assert response.status_code == 400
    assert "sav" in Page(response.text).root.text().lower()
    assert reseed.calls == [(saved, True)]
    assert snapshot(saved) == before


def test_seed_panel_and_routes_exist_only_with_reseed(base: RoomBase) -> None:
    room = Room(base)
    with TestClient(create_app(room, reseed=FakeReseed())) as enabled:
        page = Page(enabled.get("/").text)
        assert "Seed" in {h.text().strip() for h in page.find("h2")}
        actions = {f.attrs.get("action") for f in page.find("form")}
        assert {"/reseed?dry_run=true", "/reseed?dry_run=false"} <= actions
    with TestClient(create_app(room)) as disabled:
        page = Page(disabled.get("/").text)
        assert "Seed" not in {h.text().strip() for h in page.find("h2")}
        assert not any("/reseed" in (f.attrs.get("action") or "") for f in page.find("form"))


@pytest.mark.parametrize("kind", ["edit_message", "post_message", "run_scenario"])
def test_message_forms_change_the_digest(
    base: RoomBase,
    corpus: list[Event],
    cassette_copy: Path,
    offline_responses: list[LLMRequest],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    room = Room(base, cassettes=cassette_copy)
    before = room.state()
    text = "Blocker: PN-11051 bracket inspection failed; stop the build."
    if kind == "edit_message":
        target = corpus[0]
        form = {
            "kind": kind,
            "channel_id": target.channel_id,
            "ts": target.ts,
            "text": text,
            "files": "inspection.pdf\nmeasurements.csv",
        }
        message_ts = target.ts
    elif kind == "post_message":
        form = {
            "kind": kind,
            "user_id": POST.user_id,
            "channel_id": POST.channel_id,
            "text": text,
            "thread_ts": "",
        }
        message_ts = (
            f"{datetime(2026, 10, 19, 18, 1, tzinfo=ZoneInfo(base.world.timezone)).timestamp():.6f}"
        )
    else:
        form = {"kind": kind, "name": D3.name}
        message_ts = ""
        text = "G-DVT1 moves from 22 Oct to 29 Oct"

    if kind != "run_scenario":
        complete = LLM.complete

        def extract_blocker(client: LLM, request: LLMRequest) -> LLMResponse:
            if request.model == "extract" and any(text in m.content for m in request.messages):
                data = extraction_data(
                    extracted_item(
                        type="blocker",
                        title="Bracket inspection failed",
                        summary=text,
                        entities=["PN-11051"],
                        owner="U0ME01",
                        affected_roles=["ME"],
                        evidence=[{"message_ts": message_ts, "quote": text}],
                    )
                )
                client.cassettes.put(request_key(request), request, response(data))
            return complete(client, request)

        monkeypatch.setattr(LLM, "complete", extract_blocker)

    with TestClient(create_app(room)) as client:
        result = client.post("/edit", data=form, follow_redirects=False)
        assert result.status_code == 303
        page = Page(client.get(result.headers["location"]).text)
    state = room.state()
    assert state.message_changes == 1
    assert state.selections != before.selections
    assert any(state.last_effect)
    matches = [
        line
        for selection in state.selections
        for line in selection.lines
        if any(text in evidence.quote for evidence in line.item.evidence)
    ]
    assert matches, "The submitted message must reach a reader's digest"
    digest = next(
        section
        for section in page.find("section")
        if any(node.tag == "h2" and node.text() == "Digests" for node in section.nodes())
    )
    assert any(line.item.title in digest.text() for line in matches)
    if kind == "edit_message":
        changed = next(
            m for m in state.messages if (m.channel_id, m.ts) == (target.channel_id, target.ts)
        )
        assert changed.text == text
        assert changed.files == ("inspection.pdf", "measurements.csv")
    elif kind == "post_message":
        posted = next(m for m in state.messages if m.posted)
        assert posted.text == text
        assert posted.thread_ts == posted.ts


def test_message_text_and_file_names_are_escaped_without_external_references(
    base: RoomBase,
) -> None:
    text = '<script>alert("message")</script> <a href="https://example.invalid/">part</a>'
    filename = '<script>alert("file")</script>.pdf'
    state = (
        Room(base)
        .state()
        .model_copy(
            update={
                "messages": (
                    MessageView(
                        channel_id="C0EXTCM",
                        channel_name="ext-sunrise-cm",
                        thread_ts="1792456200.000000",
                        ts="1792456200.000000",
                        user_id="U0CM01",
                        author="Room author",
                        text=text,
                        files=(filename,),
                        edited=True,
                        posted=False,
                    ),
                )
            }
        )
    )
    with TestClient(create_app(FakeRoom(state))) as client:
        response = client.get("/")
    assert response.status_code == 200
    page = Page(response.text)
    assert text in page.root.text()
    assert filename in page.root.text() or any(
        filename in (n.attrs.get("value") or "") for n in page.root.nodes()
    )
    assert escape("<script>") in response.text
    # Only the room's own pages are links; the message text adds none.
    assert [n.attrs.get("href") for n in page.find("a")] in ([], ["/items"])
    assert not page.find("script")
    for node in page.root.nodes():
        for name, value in node.attrs.items():
            if name in {"href", "src", "srcset", "action", "formaction", "data", "poster"}:
                assert not re.search(r"(?i)(https?:|//|javascript:|data:)", value or "")
    assert not re.search(r"(?i)(@import|url\s*\()", response.text)

"""M3 unit 10: message changes, corpus copies, and reseed controls."""

import json
import re
import shutil
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from digest.llm import LLMRequest, LLMResponse
from digest.llm.anthropic_api import AnthropicBackend
from digest.llm.cassette import CassetteMiss, request_key
from digest.llm.client import LLM
from digest.llm.openrouter import OpenRouterBackend
from digest.models import Event
from digest.priors import load_priors
from digest.room import (
    Edit,
    EditMessage,
    MessageView,
    PostMessage,
    Room,
    RoomBase,
    RunScenario,
    SetRole,
)
from digest.store import Store
from digest.web.app import create_app
from digest.world.load import load_world
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
) -> None:
    rooms = [Room(base, cassettes=cassette_copy) for _ in range(2)]
    change = edit_message(corpus[0])
    for room in rooms:
        room.apply(ROLE)
        room.apply(change)
        room.apply(POST)
    assert rooms[0].state() == rooms[1].state()
    assert rooms[0].state().message_changes == 2
    copies = [room.save(tmp_path / f"copy-{i}") for i, room in enumerate(rooms)]
    assert snapshot(copies[0]) == snapshot(copies[1])
    rows = saved_events(copies[0])
    edited = next(e for e in rows if (e.channel_id, e.ts) == (change.channel_id, change.ts))
    zone = ZoneInfo(base.world.timezone)
    assert datetime.fromtimestamp(float(edited.event_ts), zone) == datetime(
        2026, 10, 19, 18, 1, tzinfo=zone
    )
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
) -> None:
    committed = snapshot(WORLD_DIR)
    room = Room(base, cassettes=cassette_copy)
    room.apply(D2)
    change = edit_message(corpus[0])
    room.apply(change)
    room.apply(POST)
    destination = tmp_path / ".room" / "kestrel"
    assert room.save(destination) == destination
    first = snapshot(destination)
    assert room.save(destination) == destination
    assert snapshot(destination) == first
    assert snapshot(WORLD_DIR) == committed
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
    assert (edited.text, edited.files, edited.edited) == (change.text, change.files, True)
    added = [e for e in rows if (e.channel_id, e.ts) not in original_lines]
    assert {e.text for e in added} == {
        POST.text,
        next(m.text for m in room.state().messages if m.posted and "PN-11051" in m.text),
    }
    assert edited.model_dump(exclude={"text", "files", "edited", "event_ts"}) == corpus[
        0
    ].model_dump(exclude={"text", "files", "edited", "event_ts"})


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
    assert reseed.report in Page(response.text).root.text()


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
        assert disabled.post("/reseed?dry_run=true").status_code == 404
        assert disabled.post("/reseed?dry_run=false").status_code == 404


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
    assert not page.find("a")
    assert not page.find("script")
    for node in page.root.nodes():
        for name, value in node.attrs.items():
            if name in {"href", "src", "srcset", "action", "formaction", "data", "poster"}:
                assert not re.search(r"(?i)(https?:|//|javascript:|data:)", value or "")
    assert not re.search(r"(?i)(@import|url\s*\()", response.text)

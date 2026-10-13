"""M3 unit 9: the page contract, with no room implementation or network."""

from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from digest.daily.profile import Profile
from digest.daily.render import line_parts
from digest.daily.select import Selection
from digest.driver import Change, LineView, ReaderDiff
from digest.room import (
    Edit,
    RoomState,
    SetGateDate,
    SetMembership,
    SetOwner,
    SetPrivate,
    SetRole,
    SetTaskOwner,
)
from digest.settings import Settings
from digest.web.app import create_app, server_config
from digest.world.load import load_world
from test_daily_render import absence_item, item, line

DAY = date(2026, 10, 19)
READERS = ("U0ME01", "U0SC01", "U0EM01")
NO_CHANGE = "This edit changed no digest."
REFUSAL = "Cannot set gate G-DVT1 to a weekend <Saturday> & keep working days."
ROLE_FORM = {"kind": "set_role", "user_id": "U0SC01", "role": "EM"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list["Element | str"] = field(default_factory=list)

    def text(self) -> str:
        return "".join(c.text() if isinstance(c, Element) else c for c in self.children)

    def nodes(self) -> list["Element"]:
        return [self, *(n for c in self.children if isinstance(c, Element) for n in c.nodes())]


class Page(HTMLParser):
    """Read HTML semantics without prescribing template classes or panel ids."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def find(self, tag: str) -> list[Element]:
        return [node for node in self.root.nodes() if node.tag == tag]

    def texts(self) -> set[str]:
        return {" ".join(node.text().split()) for node in self.root.nodes()}


class FakeRoom:
    def __init__(self, initial: RoomState, after: RoomState | None = None) -> None:
        self.initial = initial
        self.after = after or initial
        self.current = initial
        self.received: list[Edit] = []
        self.reset_calls = 0

    def state(self) -> RoomState:
        return self.current

    def apply(self, edit: Edit) -> RoomState:
        self.received.append(edit)
        if isinstance(edit, SetGateDate) and edit.date == date(2026, 10, 24):
            raise ValueError(REFUSAL)
        self.current = self.after.model_copy(update={"edits": (*self.current.edits, edit)})
        return self.current

    def reset(self) -> RoomState:
        self.reset_calls += 1
        self.current = self.initial
        return self.current


@pytest.fixture
def state() -> RoomState:
    world = load_world(Path("data/world/kestrel/world.yaml"))
    # Distinct names and task text catch use of the recorded world in line_parts.
    world = world.model_copy(
        update={
            "personas": tuple(
                p.model_copy(update={"name": f"{p.name} <script>persona</script>"})
                if p.user_id == "U0ME01"
                else p
                for p in world.personas
            ),
            "channels": tuple(
                c.model_copy(update={"purpose": f"{c.purpose} <script>channel</script>"})
                if c.id == "C0ENGME"
                else c
                for c in world.channels
            ),
            "project": world.project.model_copy(
                update={
                    "gates": tuple(
                        g.model_copy(
                            update={
                                "checklist": tuple(
                                    t.model_copy(
                                        update={"task": "Review the room FAI", "owner": "EM"}
                                    )
                                    if t.id == "G-DVT1.fai"
                                    else t
                                    for t in g.checklist
                                )
                            }
                        )
                        for g in world.project.gates
                    )
                }
            ),
        }
    )
    selections = []
    for index, user_id in enumerate(READERS):
        persona = next(p for p in world.personas if p.user_id == user_id)
        assert persona.role is not None
        profile = Profile(
            user_id=user_id,
            name=persona.name,
            role=persona.role,
            team=persona.team,
            owned_entities=("PN-51010",),
            channels=persona.channels,
        )
        rows = (
            line(absence_item("checklist"), "not_happened", must_see=True),
            line(absence_item("ask"), "not_happened"),
            line(
                item(
                    item_id=f"DECISION-{index}",
                    title=f"Release {index} <script>item</script>",
                    summary=f"Use revision {index}.\nKeep & inspect.",
                    owner="U0ME01",
                ),
                "decisions",
                must_see=True,
            ),
            line(
                item(
                    item_id=f"FYI-{index}",
                    type="fyi",
                    title=f"Unowned update {index}",
                    summary=f"Build photo {index} is ready.",
                    owner=None,
                    entities=(),
                ),
                "fyi",
            ),
        )
        selections.append(Selection(profile=profile, day=DAY, lines=rows, hidden=index))
    return RoomState(
        world=world,
        edits=(),
        selections=tuple(selections),
        diffs=tuple(ReaderDiff(user_id=u, added=(), removed=(), changed=()) for u in READERS),
        reworded=((), (), ()),
        last_effect=(0, 0, 0),
        owners={"PN-51010": READERS},
    )


@pytest.fixture
def changed_state(state: RoomState) -> RoomState:
    added = LineView(
        item_id="NEW",
        title="New room decision",
        section="decisions",
        lane="ranked",
        rank=1,
        reason="New room reason",
    )
    removed = added.model_copy(update={"item_id": "OLD", "title": "Removed room decision"})
    before = added.model_copy(update={"item_id": "MOVED", "title": "Moved room decision"})
    after = before.model_copy(update={"lane": "must_see", "rank": None, "reason": "Now owned"})
    selected = state.selections[0]
    selected = selected.model_copy(
        update={
            "lines": (
                *selected.lines,
                line(
                    item(item_id="NEW", title=added.title, summary="The new digest summary."),
                    "decisions",
                ),
                line(item(item_id="MOVED", title=after.title), "decisions", must_see=True),
            )
        }
    )
    return state.model_copy(
        update={
            "selections": (selected, *state.selections[1:]),
            "diffs": (
                ReaderDiff(
                    user_id=READERS[0],
                    added=(added,),
                    removed=(removed,),
                    changed=(
                        Change(
                            item_id="MOVED",
                            before=before,
                            after=after,
                            fields=("lane", "rank", "reason"),
                        ),
                    ),
                ),
                *state.diffs[1:],
            ),
            "reworded": (("ABS-CHECKLIST",), (), ()),
            "last_effect": (4, 0, 0),
        }
    )


def test_page_shows_world_digests_forms_and_escapes_text(state: RoomState) -> None:
    with TestClient(create_app(FakeRoom(state))) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = Page(response.text)
    assert not page.find("script")
    for word in ("persona", "channel", "item"):
        assert f"&lt;script&gt;{word}&lt;/script&gt;" in response.text
    text = page.root.text()
    for persona in state.world.personas:
        assert persona.name in text
    for channel in state.world.channels:
        assert channel.name in text
        assert channel.purpose in text
    for entity in state.world.entities:
        assert entity.id in text
        assert entity.type in text
    for gate in state.world.project.gates:
        assert gate.id in text
        assert gate.date.isoformat() in text
        for task in gate.checklist:
            assert task.task in text
    for selection in state.selections:
        for row in selection.lines:
            assert row.item.summary.replace("\n", " ") in " ".join(text.split())
            assert any(
                row.reason in node.text() and "open" not in node.attrs
                for node in page.find("details")
            )
            for evidence in row.item.evidence:
                assert evidence.permalink in text
    workspace = urlparse(state.world.workspace_url).netloc
    assert all(
        urlparse(node.attrs.get("href") or "").netloc != workspace for node in page.find("a")
    )
    assert NO_CHANGE not in text


VALID_EDITS: list[tuple[dict[str, str], Edit]] = [
    (ROLE_FORM, SetRole(kind="set_role", user_id="U0SC01", role="EM")),
    (
        {"kind": "set_membership", "user_id": "U0ME01", "channel_id": "C0ENGME", "member": "true"},
        SetMembership(kind="set_membership", user_id="U0ME01", channel_id="C0ENGME", member=True),
    ),
    (
        {"kind": "set_membership", "user_id": "U0ME01", "channel_id": "C0ENGME", "member": "false"},
        SetMembership(kind="set_membership", user_id="U0ME01", channel_id="C0ENGME", member=False),
    ),
    (
        {"kind": "set_private", "channel_id": "C0EXTCM", "private": "true"},
        SetPrivate(kind="set_private", channel_id="C0EXTCM", private=True),
    ),
    (
        {"kind": "set_private", "channel_id": "C0EXTCM", "private": "false"},
        SetPrivate(kind="set_private", channel_id="C0EXTCM", private=False),
    ),
    (
        {"kind": "set_owner", "entity_id": "PN-51010", "role": "EE"},
        SetOwner(kind="set_owner", entity_id="PN-51010", role="EE"),
    ),
    (
        {"kind": "set_owner", "entity_id": "PN-51010", "role": ""},
        SetOwner(kind="set_owner", entity_id="PN-51010", role=None),
    ),
    (
        {"kind": "set_gate_date", "gate_id": "G-DVT1", "date": "2026-10-23"},
        SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 23)),
    ),
    (
        {"kind": "set_task_owner", "task_id": "G-DVT1.fai", "role": "EM"},
        SetTaskOwner(kind="set_task_owner", task_id="G-DVT1.fai", role="EM"),
    ),
]
EDIT_IDS = [
    "role",
    "join",
    "leave",
    "private",
    "public",
    "owner",
    "owner-none",
    "gate-date",
    "task-owner",
]


def test_edit_forms_offer_contract_fields_and_boolean_selects(state: RoomState) -> None:
    with TestClient(create_app(FakeRoom(state))) as client:
        page = Page(client.get("/").text)
    forms = [f for f in page.find("form") if f.attrs.get("action") == "/edit"]
    for data, _ in VALID_EDITS:
        matching = [
            f
            for f in forms
            if any(
                n.attrs.get("name") == "kind" and n.attrs.get("value") == data["kind"]
                for n in f.nodes()
            )
        ]
        assert matching, data["kind"]
        form = matching[0]
        assert (form.attrs.get("method") or "").lower() == "post"
        controls = [n for n in form.nodes() if n.tag in {"input", "select", "button", "textarea"}]
        assert set(data) <= {n.attrs.get("name") for n in controls}
        for boolean in {"member", "private"} & data.keys():
            control = next(n for n in controls if n.attrs.get("name") == boolean)
            assert control.tag == "select"
            assert {n.attrs.get("value") for n in control.nodes() if n.tag == "option"} == {
                "true",
                "false",
            }
    assert any(
        f.attrs.get("action") == "/reset" and (f.attrs.get("method") or "").lower() == "post"
        for f in page.find("form")
    )


@pytest.mark.parametrize(("data", "expected"), VALID_EDITS, ids=EDIT_IDS)
def test_valid_edit_redirects_and_passes_exact_edit(
    state: RoomState, data: dict[str, str], expected: Edit
) -> None:
    room = FakeRoom(state)
    with TestClient(create_app(room)) as client:
        response = client.post("/edit", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert room.received == [expected]


def test_edit_refresh_shows_new_digest_diff_wording_and_edit_list(
    state: RoomState, changed_state: RoomState
) -> None:
    room = FakeRoom(state, changed_state)
    with TestClient(create_app(room)) as client:
        first = client.get("/")
        response = client.post("/edit", data=ROLE_FORM, follow_redirects=False)
        updated = client.get("/")
    assert response.status_code == 303
    assert updated.status_code == 200
    page = Page(updated.text)
    assert "The new digest summary." not in first.text
    for value in (
        "The new digest summary.",
        "New room decision",
        "Removed room decision",
        "Moved room decision",
    ):
        assert value in page.root.text()
    nodes = page.root.nodes()
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    wording = next(
        n for n in nodes if n.tag in heading_tags and n.text().strip() == "Wording changed"
    )
    wording_text = []
    for node in nodes[nodes.index(wording) + 1 :]:
        if node.tag in heading_tags and node.tag <= wording.tag:
            break
        wording_text.extend(c for c in node.children if isinstance(c, str))
    assert "ABS-CHECKLIST" in "".join(wording_text) or "Review the room FAI" in "".join(
        wording_text
    )
    first_entries = {n.text() for n in Page(first.text).find("li")}
    reader_name = state.selections[1].profile.name
    assert any(
        n.text() not in first_entries
        and ("U0SC01" in n.text() or reader_name in n.text())
        and "EM" in n.text()
        for n in page.find("li")
    )
    assert NO_CHANGE not in page.root.text()


def test_zero_effect_edit_shows_exact_notice(state: RoomState) -> None:
    with TestClient(create_app(FakeRoom(state))) as client:
        response = client.post("/edit", data=ROLE_FORM, follow_redirects=False)
        page = Page(client.get("/").text)
    assert response.status_code == 303
    assert NO_CHANGE in page.texts()


def test_refused_edit_shows_verbatim_reason_and_preserves_digest(state: RoomState) -> None:
    room = FakeRoom(state)
    with TestClient(create_app(room)) as client:
        first = client.get("/")
        response = client.post(
            "/edit", data={"kind": "set_gate_date", "gate_id": "G-DVT1", "date": "2026-10-24"}
        )
        later = client.get("/")
    assert response.status_code == 400
    page = Page(response.text)
    assert [
        n.text() for n in page.find("p") if "refusal" in (n.attrs.get("class") or "").split()
    ] == [REFUSAL]
    assert any(n.attrs.get("action") == "/edit" for n in page.find("form"))
    assert room.received == [
        SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 24))
    ]
    assert later.text == first.text
    for selection in state.selections:
        for row in selection.lines:
            assert " ".join(row.item.summary.split()) in " ".join(page.root.text().split())


MISSING_FIELDS = [
    pytest.param(
        {key: value for key, value in data.items() if key != missing},
        id=f"{data['kind']}-missing-{missing}",
    )
    for data, _ in (VALID_EDITS[i] for i in (0, 1, 3, 5, 7, 8))
    for missing in data
]
BAD_BOOLEANS = [
    pytest.param({**data, boolean: value}, id=f"{boolean}-{value or 'empty'}")
    for data, boolean in ((VALID_EDITS[1][0], "member"), (VALID_EDITS[3][0], "private"))
    for value in ("True", "FALSE", "1", "0", "on", "yes", " true", "false ", "")
]


@pytest.mark.parametrize(
    "data", [*MISSING_FIELDS, *BAD_BOOLEANS, pytest.param({"kind": "unknown"}, id="unknown-kind")]
)
def test_bad_form_returns_400_with_reason_without_calling_room(
    state: RoomState, data: dict[str, str]
) -> None:
    room = FakeRoom(state)
    with TestClient(create_app(room)) as client:
        response = client.post("/edit", data=data)
    assert response.status_code == 400
    page = Page(response.text)
    assert any(
        n.text().strip()
        for n in page.find("p")
        if "refusal" in (n.attrs.get("class") or "").split()
    )
    assert any(n.attrs.get("action") == "/edit" for n in page.find("form"))
    assert room.received == []


def test_digest_headings_and_summaries_equal_line_parts(state: RoomState) -> None:
    names = {p.user_id: p.name for p in state.world.personas}
    tasks = {(g.id, t.id): t for g in state.world.project.gates for t in g.checklist}
    with TestClient(create_app(FakeRoom(state))) as client:
        response = client.get("/")
    assert response.status_code == 200
    texts = Page(response.text).texts()
    for selection in state.selections:
        for row in selection.lines:
            heading, summary = line_parts(row, names, tasks)
            assert heading in texts
            assert summary in texts


@pytest.mark.parametrize("port", [8000, 8765])
def test_server_config_binds_loopback_and_given_port(port: int) -> None:
    app = FastAPI()
    config = server_config(app, port)
    assert config.app is app
    assert config.host == "127.0.0.1"
    assert config.port == port


def test_create_app_needs_no_api_key_or_settings(
    state: RoomState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_settings(self: Settings, *args: Any, **kwargs: Any) -> None:
        pytest.fail("create_app must not construct Settings")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(Settings, "__init__", forbid_settings)
    with TestClient(create_app(FakeRoom(state))) as client:
        assert client.get("/").status_code == 200


def test_reset_redirects_calls_room_and_restores_first_page(
    state: RoomState, changed_state: RoomState
) -> None:
    room = FakeRoom(state, changed_state)
    with TestClient(create_app(room)) as client:
        first = client.get("/")
        edit = client.post("/edit", data=ROLE_FORM, follow_redirects=False)
        assert edit.status_code == 303
        assert client.get("/").text != first.text
        response = client.post("/reset", follow_redirects=False)
        restored = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert room.reset_calls == 1
    assert restored.status_code == first.status_code == 200
    assert restored.text == first.text

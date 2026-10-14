"""M4 unit 13: the open-items page, using offline day-6 queries."""

from collections.abc import Iterator
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient

import test_room
import test_room_edits
import test_web
from digest import queries
from digest.daily.render import item_parts, line_parts
from digest.daily.select import Line
from digest.models import Item, ItemStatus, ItemType
from digest.priors import load_priors
from digest.room import PostMessage, Room, RoomBase, RoomState
from digest.store import Store
from digest.web.app import create_app
from digest.world.schema import World

day6_store = test_room.day6_store
base = test_room_edits.base
thermal_review = test_room_edits.thermal_review
offline = test_room_edits.offline
state = test_web.state
Page = test_web.Page
Element = test_web.Element
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class ItemsFakeRoom(test_web.FakeRoom):
    def __init__(self, initial: RoomState, path: Path) -> None:
        super().__init__(initial)
        self.path = path

    @contextmanager
    def items_context(self) -> Iterator[queries.QueryContext]:
        with Store.open_readonly(self.path) as store:
            yield queries.open_context(store, self.state().world, load_priors())


@pytest.fixture
def items_room(state: RoomState, day6_store: Path) -> ItemsFakeRoom:
    return ItemsFakeRoom(state, day6_store)


def as_line(item: Item) -> Line:
    return Line(item=item, section="fyi", must_see=False, score=0, reason="Page test")


def parts(item: Item, world: World) -> tuple[str, str]:
    names = {p.user_id: p.name for p in world.personas}
    tasks = {(g.id, t.id): t for g in world.project.gates for t in g.checklist}
    return line_parts(as_line(item), names, tasks)


def section(page: Page, title: str) -> Element:
    return next(
        node
        for node in page.find("section")
        if any(
            isinstance(child, Element) and child.tag in HEADINGS and child.text().strip() == title
            for child in node.children
        )
    )


def headings(node: Element) -> list[str]:
    return [n.text().strip() for n in node.nodes() if n.tag in HEADINGS]


def item_node(node: Element, heading: str) -> Element:
    # Use the smallest item container, so another item's text cannot satisfy an assertion.
    matches = [
        n
        for n in node.nodes()
        if n.tag in {"article", "li", "div", "section"} and heading in headings(n)
    ]
    assert matches, heading
    return min(matches, key=lambda n: len(n.nodes()))


def test_default_sections_match_queries(items_room: ItemsFakeRoom) -> None:
    """Setup day-6 queries / Action GET items / Assert B13.1 headings and order."""
    with items_room.items_context() as ctx:
        blockers = queries.list_open_blockers(ctx)
        absences = queries.list_absences(ctx)
    assert blockers and absences
    ask = next(v.item for v in absences if v.item.absence.source == "ask")
    with TestClient(create_app(items_room)) as client:
        response = client.get("/items")
    assert response.status_code == 200
    page = Page(response.text)
    for title, views in (("Open blockers", blockers), ("Not happened", absences)):
        assert headings(section(page, title))[1:] == [
            parts(v.item, items_room.state().world)[0] for v in views
        ]
    assert "Results" not in headings(page.root)
    heading = parts(ask, items_room.state().world)[0]
    people = {p.user_id: p.name for p in items_room.state().world.personas}
    for uid in (ask.owner, ask.absence.asker):
        assert uid and people[uid] in heading and uid not in heading
    assert any(n.attrs.get("href") == "/" for n in page.find("a"))


def test_item_parts_matches_line_text(items_room: ItemsFakeRoom) -> None:
    """Setup all day-6 item kinds / Action render items / Assert B13.1 shared line text."""
    world = items_room.state().world
    with items_room.items_context() as ctx:
        views = [
            v
            for kind in get_args(ItemType)
            for v in queries.search_items(ctx, type=kind, limit=100)
        ]
    assert views
    names = {p.user_id: p.name for p in world.personas}
    tasks = {(g.id, t.id): t for g in world.project.gates for t in g.checklist}
    expected = [parts(v.item, world) for v in views]
    assert [item_parts(v.item, names, tasks) for v in views] == expected


def test_item_details_include_owner_evidence_and_silence(items_room: ItemsFakeRoom) -> None:
    """Setup owned and checklist items / Action GET items / Assert B13.2 details per item."""
    with items_room.items_context() as ctx:
        owned = next(v for v in queries.list_open_blockers(ctx) if v.owner_name)
        absence = next(
            v for v in queries.list_absences(ctx) if v.item.absence.source == "checklist"
        )
    with TestClient(create_app(items_room)) as client:
        response = client.get("/items")
    assert response.status_code == 200
    page = Page(response.text)
    for title, view in (("Open blockers", owned), ("Not happened", absence)):
        heading, summary = parts(view.item, items_room.state().world)
        node = item_node(section(page, title), heading)
        text = " ".join(node.text().split())
        assert summary in text
        assert (view.owner_name or "no owner") in text
        for evidence in view.evidence:
            assert evidence.quote in node.text()
            assert evidence.author_name and evidence.author_name in node.text()
            assert evidence.channel_name and evidence.channel_name in node.text()
            assert any(
                n.tag == "a" and n.attrs.get("href") == evidence.permalink for n in node.nodes()
            )
    assert absence.item.owner is None
    assert f"{absence.item.absence.days_silent} working days" in summary


def test_item_readership_uses_current_selections(items_room: ItemsFakeRoom) -> None:
    """Setup selected and unselected items / Action GET twice / Assert B13.2a exact readers."""
    with items_room.items_context() as ctx:
        views = queries.list_open_blockers(ctx)
    assert len(views) >= 2
    selected, unselected = views[:2]
    initial = items_room.state()
    with TestClient(create_app(items_room)) as client:
        for reader_indexes in ((0, 2), (1,)):
            items_room.current = initial.model_copy(
                update={
                    "selections": tuple(
                        selection.model_copy(
                            update={
                                "lines": (as_line(selected.item),) if i in reader_indexes else ()
                            }
                        )
                        for i, selection in enumerate(initial.selections)
                    )
                }
            )
            response = client.get("/items")
            assert response.status_code == 200
            block = section(Page(response.text), "Open blockers")
            for view in (selected, unselected):
                node = item_node(block, parts(view.item, initial.world)[0])
                text = node.text()
                if view is unselected:
                    assert "In no reader's digest" in text
                    continue
                assert "In the digest of:" in text
                readership = text.split("In the digest of:", 1)[1]
                for i, selection in enumerate(initial.selections):
                    assert (selection.profile.name in readership) == (i in reader_indexes)
                    if i in reader_indexes:
                        assert selection.profile.role in readership


@pytest.mark.parametrize("ignored", ["", "any"])
def test_search_filters_and_form(items_room: ItemsFakeRoom, ignored: str) -> None:
    """Setup open asks / Action GET filters / Assert B13.3 results, defaults, and selects."""
    with items_room.items_context() as ctx:
        expected = queries.search_items(ctx, type="ask", status="open", limit=50)
    assert expected
    with TestClient(create_app(items_room)) as client:
        response = client.get(
            "/items", params={"type": "ask", "status": "open", "owner": ignored, "text": ""}
        )
        default = client.get("/items", params={k: ignored for k in ("type", "status", "owner")})
    assert response.status_code == default.status_code == 200
    page = Page(response.text)
    results = section(page, "Results")
    assert headings(results)[1:] == [parts(v.item, items_room.state().world)[0] for v in expected]
    titles = headings(page.root)
    assert titles.index("Results") < titles.index("Open blockers") < titles.index("Not happened")
    assert "Results" not in headings(Page(default.text).root)
    for view in expected:
        text = item_node(results, parts(view.item, items_room.state().world)[0]).text()
        assert view.item.type in text and view.item.status in text
    form = next(f for f in page.find("form") if f.attrs.get("action") == "/items")
    assert (form.attrs.get("method") or "get").lower() == "get"
    assert any(n.tag == "input" and n.attrs.get("name") == "text" for n in form.nodes())
    for name, values in (
        ("type", set(get_args(ItemType))),
        ("status", set(get_args(ItemStatus))),
        ("owner", {p.user_id for p in items_room.state().world.personas if p.kind == "human"}),
    ):
        select = next(n for n in form.nodes() if n.tag == "select" and n.attrs.get("name") == name)
        options = {n.attrs.get("value") for n in select.nodes() if n.tag == "option"}
        assert options - {"", "any"} == values
        assert options & {"", "any"}


def test_invalid_type_shows_query_refusal(items_room: ItemsFakeRoom) -> None:
    """Setup an invalid type / Action GET crafted URL / Assert B13.4 query message and 400."""
    with items_room.items_context() as ctx, pytest.raises(ValueError) as error:
        queries.search_items(ctx, type="nonsense", limit=50)
    with TestClient(create_app(items_room)) as client:
        response = client.get("/items?type=nonsense")
    assert response.status_code == 400
    page = Page(response.text)
    assert str(error.value) in page.root.text()
    assert page.find("form")


def test_evidence_quote_is_escaped(items_room: ItemsFakeRoom, tmp_path: Path) -> None:
    """Setup a script quote in a copied store / Action GET items / Assert B13.5 escaped text."""
    quote = '<script>alert("evidence")</script> & quoted'
    with items_room.items_context() as ctx:
        item = queries.list_open_blockers(ctx)[0].item
    evidence = item.evidence[0].model_copy(update={"quote": quote})
    item = item.model_copy(update={"evidence": (evidence, *item.evidence[1:])})
    path = tmp_path / "quoted.sqlite"
    with Store.open_readonly(items_room.path) as origin, origin.copy_to(path) as store:
        store.upsert_items([item], prompt_version="items-page-test", model="test")
    items_room.path = path
    with TestClient(create_app(items_room)) as client:
        response = client.get("/items")
    assert response.status_code == 200
    page = Page(response.text)
    assert not page.find("script")
    assert escape(quote) in response.text
    assert (
        quote
        in item_node(
            section(page, "Open blockers"), parts(item, items_room.state().world)[0]
        ).text()
    )


def test_curated_change_and_reset_use_current_store(
    base: RoomBase, thermal_review: PostMessage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup a real room / Action apply thermal review then reset / Assert B13.6 current items."""
    test_room_edits.forbid_backend(monkeypatch)
    room = Room(base)
    with Store.open_readonly(base.store_path) as store:
        ctx = queries.open_context(store, base.world, base.priors)
        before = queries.search_items(ctx, text="thermal", limit=50)
    old_items = {view.item.item_id: view.item for view in before}
    changed = room.apply(thermal_review, fresh=True)
    with room.items_context() as ctx:
        assert ctx.world == changed.world
        after = queries.search_items(ctx, text="thermal", limit=50)
        added = [view.item for view in after if old_items.get(view.item.item_id) != view.item]
        assert added, "The recorded thermal-review edit must change an item"
        item = next(
            item for item in added if any(e.quote in thermal_review.text for e in item.evidence)
        )
        assert queries.get_item(ctx, item.item_id) is not None
        assert ctx.store.path != base.store_path
    with TestClient(create_app(room)) as client:
        response = client.get("/items", params={"type": item.type})
        assert response.status_code == 200
        heading = parts(item, changed.world)[0]
        assert heading in headings(section(Page(response.text), "Results"))
        room.reset()
        reset = client.get("/items", params={"type": item.type})
        assert reset.status_code == 200
        assert heading not in headings(section(Page(reset.text), "Results"))
    with room.items_context() as ctx:
        assert ctx.store.path == base.store_path
        restored = queries.get_item(ctx, item.item_id)
        assert restored is not None
        assert restored.item == old_items[item.item_id]
        assert restored.item != item


def test_room_without_items_context_and_navigation(state: RoomState) -> None:
    """Setup a plain fake room / Action GET items and room / Assert B13.7 404 and link."""
    with TestClient(create_app(test_web.FakeRoom(state))) as client:
        response = client.get("/items")
        home = client.get("/")
    assert response.status_code == 404
    assert home.status_code == 200
    assert any(n.attrs.get("href") == "/items" for n in Page(home.text).find("a"))


def test_recorded_line_parts_are_unchanged(base: RoomBase) -> None:
    """Setup recorded selections / Action render each line / Assert B13.8 unchanged wording."""
    state = Room(base).state()
    absence_headings = {
        "absence:G-DVT1.emc": "Not happened: Pass the TST-EMC-01 EMC pre-scan (EE) for G-DVT1 - 3 working days silent, 4/7 buffer days used",
        "absence:G-DVT1.thermal": "Not happened: Close the TST-THM-02 thermal review (EE) for G-DVT1 - 4 working days silent, 4/7 buffer days used",
        "absence:ask:C0ENGEF:1791997800.010040#1791997800.010040#0": "Not happened: Grace Okonkwo asked Ana Folau, no reply for 3 working days",
    }
    people = {p.user_id: p.name for p in state.world.personas}
    tasks = {(g.id, t.id): t for g in state.world.project.gates for t in g.checklist}
    assert all(s.lines for s in state.selections)
    seen_absences = set()
    for selection in state.selections:
        for line in selection.lines:
            item = line.item
            if item.absence:
                heading = absence_headings[item.item_id]
                seen_absences.add(item.item_id)
            else:
                heading = " ".join(item.title.split())
                if item.entities:
                    heading += f" ({', '.join(item.entities)})"
                if item.owner:
                    heading += f" - owner {people[item.owner]}"
            assert line_parts(line, people, tasks) == (heading, " ".join(item.summary.split()))
    assert seen_absences == set(absence_headings)

"""Unit 12a: B12.0 through B12.12 in docs/plans/m4-queries.md v0.2.1."""

import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import Thread
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import test_room
from digest import queries
from digest.absence import detect_absences
from digest.cli import app
from digest.daily.render import render_digest, select_digest
from digest.models import Item
from digest.priors import Priors
from digest.queries import QueryContext
from digest.store import Store
from digest.world.schema import World

# The room tests' fixtures. Plain assignments, so ruff sees no shadowed import.
day6_store = test_room.day6_store
priors = test_room.priors
world = test_room.world

DAY = date(2026, 10, 19)
PREVIOUS = date(2026, 10, 16)
GOLDEN = Path("data/world/kestrel/golden.jsonl")
# These ids come from the extracted day-6 store, not the query functions.
STALE_PACK = "C0EXTCM:1792164600.010061#1792164600.010061#0"
REV_C = "C0PLMFD:1791908400.010010#1791908400.010010#0"
PACK_ISSUED = "C0PLMFD:1791822000.010003#1791822000.010003#0"
THERMAL = "C0KPROG:1792423800.010077#1792423800.010077#0"
RESOLVED_ASK = "C0SUPCH:1792087200.010059#1792087200.010059#0"


@pytest.fixture
def ctx(day6_store: Path, world: World, priors: Priors, tmp_path: Path) -> Iterator[QueryContext]:
    path = tmp_path / "day6.sqlite"
    shutil.copy2(day6_store, path)
    with Store.open_readonly(path) as store:
        # A fixture must not call a query stub: each test reaches its own function.
        yield QueryContext(
            store=store, world=world, priors=priors, day=DAY, previous_digest=PREVIOUS
        )


@pytest.fixture
def wal_store(ctx: QueryContext) -> Iterator[sqlite3.Connection]:
    """Keep setup WAL bytes present while the query uses its read-only store."""
    with closing(sqlite3.connect(ctx.store.path)) as db:
        with db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('query_test_marker', 'keep-store-open')"
            )
        yield db


def absences(ctx: QueryContext) -> list[Item]:
    zone = ZoneInfo(ctx.world.timezone)
    return detect_absences(
        ctx.store, ctx.world.project, ctx.priors, datetime.combine(ctx.day, time.max, zone), zone
    )


def assert_store_released(ctx: QueryContext) -> None:
    """Close the fixture handle so a leaked query handle prevents the Windows rename."""
    path = ctx.store.path
    assert isinstance(path, Path)
    ctx.store.close()
    moved = path.with_name("renamed.sqlite")
    path.rename(moved)
    moved.rename(path)


def rows(ctx: QueryContext, sql: str) -> list[dict[str, Any]]:
    """Read expected fields from the recorded database on a separate connection."""
    with closing(sqlite3.connect(ctx.store.path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql)]


def recorded(ctx: QueryContext) -> dict[str, Item]:
    cutoff = datetime.combine(ctx.day, time.max, ZoneInfo(ctx.world.timezone))
    return {item.item_id: item for item in ctx.store.items(cutoff)}


def file_bytes(path: Path) -> tuple[bytes, bytes | None]:
    wal = path.with_name(path.name + "-wal")
    return path.read_bytes(), wal.read_bytes() if wal.exists() else None


@pytest.mark.parametrize("previous", ["auto", None, date(2026, 10, 15)])
def test_open_context_window(ctx: QueryContext, previous: Any) -> None:
    """Setup day 6 / Action open the context / Assert the store day and requested window."""
    actual = queries.open_context(ctx.store, ctx.world, ctx.priors, previous_digest=previous)
    assert actual.day == DAY
    assert actual.previous_digest == (PREVIOUS if previous == "auto" else previous)
    assert actual.store is ctx.store
    assert actual.world is ctx.world
    assert actual.priors == ctx.priors


def test_open_context_first_day(
    tmp_path: Path, world: World, priors: Priors, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup world day 1 / Action open with auto / Assert this is a first digest."""
    monkeypatch.setenv("DIGEST_MODE", "replay")
    path = tmp_path / "day1.sqlite"
    for args in (
        ["replay", "--world", "kestrel", "--until", str(world.start_date), "--db", str(path)],
        ["extract", "--world", "kestrel", "--db", str(path)],
    ):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, (result.output, result.exception)
    with Store.open_readonly(path) as store:
        actual = queries.open_context(store, world, priors)
        assert actual.day == world.start_date
        assert actual.previous_digest is None


@pytest.mark.parametrize("fault", ["no-until", "pending", "midday", "invalid", "naive"])
def test_open_context_refusals(ctx: QueryContext, fault: str) -> None:
    """Setup invalid replay metadata / Action open the context / Assert the exact refusal."""
    path = ctx.store.path
    with closing(sqlite3.connect(path)) as db, db:
        if fault == "no-until":
            db.execute("DELETE FROM meta WHERE key = 'until'")
            expected = f"store {path} has no meta key 'until'; run digest replay first"
        elif fault == "pending":
            db.execute("DELETE FROM stage_runs WHERE stage = 'extract'")
            expected = (
                f"store {path} has extract deltas pending as of day {DAY}; run digest extract first"
            )
        else:
            until = {
                "midday": "2026-10-19T12:00:00-07:00",
                "invalid": "not-a-timestamp",
                "naive": "2026-10-19T23:59:59.999999",
            }[fault]
            db.execute("UPDATE meta SET value = ? WHERE key = 'until'", (until,))
            if fault == "midday":
                expected = (
                    f"store {path} is replayed until {until}, before the end of day {DAY} "
                    "(2026-10-20T06:59:59.999999+00:00); "
                    f"run digest replay --until {DAY}, then digest extract"
                )
            else:
                expected = (
                    f"store {path} meta key 'until' is {until!r}, "
                    "not an ISO timestamp with a UTC offset"
                )
    # Exact strings are from select_digest in daily/render.py.
    with pytest.raises(ValueError) as error:
        queries.open_context(ctx.store, ctx.world, ctx.priors)
    assert str(error.value) == expected


@pytest.mark.parametrize(
    ("filters", "where"),
    [
        ({"type": "blocker"}, "type = 'blocker'"),
        ({"status": "resolved"}, "status = 'resolved'"),
        ({"owner": "U0SC01"}, "owner = 'U0SC01'"),
        ({"entity": "chassis plate"}, "entities LIKE '%PN-11020%'"),
        ({"entity": "PN-11020"}, "entities LIKE '%PN-11020%'"),
        (
            {"type": "blocker", "status": "open", "owner": "U0EE01", "updated_since": DAY},
            "type = 'blocker' AND status = 'open' AND owner = 'U0EE01' "
            "AND last_updated_us >= 1792393200000000",
        ),
        ({"type": "ask", "owner": "U0ME01"}, "type = 'ask' AND owner = 'U0ME01'"),
    ],
    ids=["type", "status", "owner", "alias", "entity-id", "intersection", "no-match"],
)
def test_search_filters(ctx: QueryContext, filters: dict[str, Any], where: str) -> None:
    """Setup recorded items / Action search with filters / Assert all filters and newest order."""
    expected = rows(
        ctx, f"SELECT item_id FROM items WHERE {where} ORDER BY last_updated_us DESC, item_id"
    )
    actual = queries.search_items(ctx, **filters, limit=100)
    assert [view.item.item_id for view in actual] == [row["item_id"] for row in expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("STALE REV B DRAWING PACK", (STALE_PACK,)),
        ("FAI signoff is held", (STALE_PACK,)),
        ("standoff locations", (STALE_PACK,)),
        ("CHASSIS PLATE", (STALE_PACK, REV_C, PACK_ISSUED)),
        ("no-such-recorded-term", ()),
    ],
    ids=["title", "summary", "evidence-fts", "alias", "no-match"],
)
def test_search_text(ctx: QueryContext, text: str, expected: tuple[str, ...]) -> None:
    """Setup recorded titles and evidence / Action search text / Assert the matching item ids."""
    actual = queries.search_items(ctx, text, limit=100)
    assert tuple(view.item.item_id for view in actual) == expected


def test_search_updated_since(ctx: QueryContext) -> None:
    """Setup updates around local midnight / Action filter by day / Assert the local boundary."""
    zone = ZoneInfo(ctx.world.timezone)
    boundary = datetime.combine(DAY, time.min, zone)
    # Move two recorded items to either side of midnight in this test's copy.
    with closing(sqlite3.connect(ctx.store.path)) as db, db:
        for item_id, instant in (
            (PACK_ISSUED, boundary - timedelta(microseconds=1)),
            (REV_C, boundary),
        ):
            micros = int(instant.timestamp() * 1_000_000)
            stamp = f"{micros // 1_000_000}.{micros % 1_000_000:06d}"
            db.execute(
                "UPDATE items SET last_updated = ?, last_updated_us = ? WHERE item_id = ?",
                (stamp, micros, item_id),
            )
    universe = [*recorded(ctx).values(), *absences(ctx)]
    expected = sorted(
        (i for i in universe if datetime.fromtimestamp(float(i.last_updated), zone).date() >= DAY),
        key=lambda i: (-float(i.last_updated), i.item_id),
    )
    assert REV_C in {i.item_id for i in expected}
    assert PACK_ISSUED not in {i.item_id for i in expected}
    before = recorded(ctx)[PACK_ISSUED]
    assert datetime.fromtimestamp(float(before.last_updated), UTC).date() == DAY
    actual = queries.search_items(ctx, updated_since=DAY, limit=100)
    assert [v.item.item_id for v in actual] == [i.item_id for i in expected]


def test_search_absences(ctx: QueryContext) -> None:
    """Setup computed absences / Action filter by type / Assert all ids in search order."""
    expected = sorted(absences(ctx), key=lambda i: (-float(i.last_updated), i.item_id))
    assert expected
    actual = queries.search_items(ctx, type="absence", limit=100)
    assert [v.item.item_id for v in actual] == [i.item_id for i in expected]


def test_search_title_without_evidence_match(ctx: QueryContext) -> None:
    """Setup a title phrase absent from messages / Action search / Assert the title match."""
    text = "False-alarm short"
    assert not rows(ctx, "SELECT ts FROM events WHERE lower(text) LIKE '%false-alarm short%'")
    expected = [i.item_id for i in recorded(ctx).values() if text.lower() in i.title.lower()]
    assert len(expected) == 1
    actual = queries.search_items(ctx, text)
    assert [v.item.item_id for v in actual] == expected


def test_search_fts_precedes_newer_text_match(ctx: QueryContext) -> None:
    """Setup DVT evidence and a newer title match / Action search asks / Assert FTS comes first."""
    # FTS indexes DVT-1 as one token. Only the safety ask cites the standalone word DVT.
    actual = queries.search_items(ctx, "DVT", type="ask")
    assert tuple(view.item.item_id for view in actual) == (
        "C0ENGEF:1791997800.010040#1791997800.010040#0",
        "C0EXTCM:1792068738.000800#1792068738.000800#1",
    )


@pytest.mark.parametrize("limit", [1, 2, 100])
def test_search_limit(ctx: QueryContext, limit: int) -> None:
    """Setup three chassis items / Action limit an alias search / Assert the ordered prefix."""
    actual = queries.search_items(ctx, entity="chassis plate", limit=limit)
    assert tuple(view.item.item_id for view in actual) == (STALE_PACK, REV_C, PACK_ISSUED)[:limit]


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"type": "nonsense"},
        {"status": "nonsense"},
        {"text": "CMM", "limit": 0},
        {"text": "CMM", "limit": 101},
        {"entity": "NO-SUCH-ENTITY"},
        {"owner": "U0NOBODY"},
    ],
    ids=[
        "no-filter",
        "bad-type",
        "bad-status",
        "low-limit",
        "high-limit",
        "unknown-entity",
        "unknown-owner",
    ],
)
def test_search_refuses_invalid_filters(ctx: QueryContext, filters: dict[str, Any]) -> None:
    """Setup invalid search inputs, an unknown entity and an unknown owner / Action search /
    Assert ValueError under Q1."""
    with pytest.raises(ValueError):
        queries.search_items(ctx, **filters)


@pytest.mark.parametrize("item_id", [STALE_PACK, THERMAL, RESOLVED_ASK])
def test_get_item_fields(ctx: QueryContext, item_id: str) -> None:
    """Setup an extracted item / Action get it / Assert fields, claims, tasks and named evidence."""
    expected = recorded(ctx)[item_id]
    people = {p.user_id: p.name for p in ctx.world.personas}
    channels = {c.id: c.name for c in ctx.world.channels}
    events = {(r["channel_id"], r["ts"]): r for r in rows(ctx, "SELECT * FROM events")}
    actual = queries.get_item(ctx, item_id)
    assert actual is not None
    assert actual.item == expected
    assert actual.owner_name == people.get(expected.owner)
    assert len(actual.evidence) == len(expected.evidence)
    for evidence, source in zip(actual.evidence, expected.evidence, strict=True):
        event = events[(source.channel_id, source.message_ts)]
        assert evidence.model_dump() == {
            **source.model_dump(),
            "channel_name": channels[source.channel_id],
            "author_id": event["user_id"],
            "author_name": people[event["user_id"]],
        }


@pytest.mark.parametrize("function", ["get_item", "get_entity_history"])
def test_missing_evidence_author(ctx: QueryContext, function: str) -> None:
    """Setup a missing cited message / Action read item or history / Assert a null author."""
    evidence = recorded(ctx)[STALE_PACK].evidence[0]
    with closing(sqlite3.connect(ctx.store.path)) as db, db:
        db.execute(
            "DELETE FROM events WHERE channel_id = ? AND ts = ?",
            (evidence.channel_id, evidence.message_ts),
        )
    if function == "get_item":
        actual = queries.get_item(ctx, STALE_PACK)
        assert actual is not None
        entries = actual.evidence
    else:
        actual = queries.get_entity_history(ctx, "PN-11020")
        assert actual is not None
        entries = actual.claims
    entry = next(
        entry
        for entry in entries
        if (entry.channel_id, entry.message_ts) == (evidence.channel_id, evidence.message_ts)
    )
    assert entry.author_id is None
    assert entry.author_name is None


@pytest.mark.parametrize("function", ["get_item", "get_entity_history"])
def test_unknown_lookup(ctx: QueryContext, function: str) -> None:
    """Setup an unknown id / Action look it up / Assert None under Q2 and Q3."""
    assert getattr(queries, function)(ctx, "unknown-id") is None


def test_get_item_absence(ctx: QueryContext) -> None:
    """Setup a labelled EMC absence / Action get its id / Assert detail and list agreement."""
    label = next(
        json.loads(line)
        for line in GOLDEN.read_text().splitlines()
        if json.loads(line).get("task") == "G-DVT1.emc" and json.loads(line).get("day") == 6
    )
    actual = queries.get_item(ctx, "absence:G-DVT1.emc")
    assert actual is not None
    assert actual.item.type == "absence"
    detail = actual.item.absence
    assert detail is not None
    assert (detail.source, detail.gate, detail.task, detail.days_silent) == (
        label["source"],
        label["gate"],
        label["task"],
        label["days_silent"],
    )
    assert (detail.buffer_consumed, detail.buffer_days) == (4, 7)
    assert actual == next(
        v for v in queries.list_absences(ctx) if v.item.item_id == actual.item.item_id
    )


def test_get_item_ask_absence(ctx: QueryContext) -> None:
    """Setup a computed ask absence / Action get its id / Assert its complete item."""
    expected = next(i for i in absences(ctx) if i.absence and i.absence.source == "ask")
    actual = queries.get_item(ctx, expected.item_id)
    assert actual is not None
    assert actual.item == expected
    people = {p.user_id: p.name for p in ctx.world.personas}
    assert expected.owner is not None  # an ask absence names the person asked
    assert actual.owner_name == people[expected.owner]


def test_entity_history_inherited_owner(ctx: QueryContext) -> None:
    """Setup the Orin module / Action get history / Assert ownership through its carrier."""
    entities = {e.id: e for e in ctx.world.entities}
    # world.yaml: PN-42010 has no owner. Its parent PN-21020 belongs to EE.
    assert entities["PN-42010"].owner is None
    assert entities["PN-42010"].parent == "PN-21020"
    assert entities["PN-21020"].owner == "EE"
    owner = next(p for p in ctx.world.personas if p.user_id == "U0EE01")
    assert owner.role == "EE"
    actual = queries.get_entity_history(ctx, "PN-42010")
    assert actual is not None
    assert actual.owner_role is None
    assert [(p.user_id, p.name, p.role, p.kind) for p in actual.owners] == [
        (owner.user_id, owner.name, owner.role, owner.kind)
    ]


@pytest.mark.parametrize("entity", ["PN-11020", "chassis plate"])
def test_entity_history(ctx: QueryContext, entity: str) -> None:
    """Setup chassis claims / Action get history by id or alias / Assert owners and all claims."""
    source = next(e for e in ctx.world.entities if e.id == "PN-11020")
    events = {(r["channel_id"], r["ts"]): r for r in rows(ctx, "SELECT * FROM events")}
    people = {p.user_id: p.name for p in ctx.world.personas}
    expected = []
    for item in recorded(ctx).values():
        for claim in item.claims:
            if claim.entity == source.id:
                event = events.get((claim.channel_id, claim.message_ts))
                author = event["user_id"] if event else None
                expected.append(
                    {
                        "item_id": item.item_id,
                        **claim.model_dump(exclude={"entity"}),
                        "author_id": author,
                        "author_name": people.get(author),
                    }
                )
    expected.sort(key=lambda claim: (claim["message_ts"], claim["item_id"]))
    actual = queries.get_entity_history(ctx, entity)
    assert actual is not None
    assert (actual.entity_id, actual.type, actual.description, actual.owner_role) == (
        source.id,
        source.type,
        source.description,
        source.owner,
    )
    assert [(p.user_id, p.name, p.role, p.kind) for p in actual.owners] == [
        ("U0ME01", people["U0ME01"], "ME", "human")
    ]
    assert [claim.model_dump() for claim in actual.claims] == expected
    assert "supersession" in actual.note.lower()
    assert queries.get_entity_history(ctx, source.id) == queries.get_entity_history(
        ctx, source.aliases[1]
    )


def test_open_blockers(ctx: QueryContext) -> None:
    """Setup recorded blocker evidence / Action list open blockers / Assert membership and order."""
    events = {
        (r["channel_id"], r["ts"]): r["event_ts_us"] for r in rows(ctx, "SELECT * FROM events")
    }
    blockers = [i for i in recorded(ctx).values() if i.type == "blocker" and i.status == "open"]
    expected = sorted(
        blockers,
        key=lambda item: (
            -max(events[(e.channel_id, e.message_ts)] for e in item.evidence),
            item.item_id,
        ),
    )
    assert STALE_PACK in {item.item_id for item in expected}
    actual = queries.list_open_blockers(ctx)
    assert tuple(view.item for view in actual) == tuple(expected)


def test_absences(ctx: QueryContext) -> None:
    """Setup day-6 labels and recorded task links / Action list absences / Assert checklist facts."""
    labels = [json.loads(line) for line in GOLDEN.read_text().splitlines()]
    expected = {
        label["task"]: label
        for label in labels
        if label["kind"] == "absence"
        and label["day"] == 6
        and label["task"] in {"G-DVT1.emc", "G-DVT1.thermal"}
    }
    detected = absences(ctx)
    assert detected
    actual = queries.list_absences(ctx)
    assert [v.item.item_id for v in actual] == [i.item_id for i in detected]
    checklist = [v.item for v in actual if v.item.absence and v.item.absence.source == "checklist"]
    # Only these two stale tasks have citations in the extracted store.
    assert [item.item_id for item in checklist] == ["absence:G-DVT1.emc", "absence:G-DVT1.thermal"]
    for item in checklist:
        detail = item.absence
        assert detail is not None
        label = expected[detail.task]
        assert (detail.gate, detail.days_silent) == (label["gate"], label["days_silent"])
        assert (detail.buffer_consumed, detail.buffer_days) == (4, 7)
        assert item.affected_roles == tuple(label["audience"])
    assert len({v.item.item_id for v in actual}) == len(actual)
    safety_label = next(
        label
        for label in labels
        if label["kind"] == "absence" and label["day"] == 6 and label["cites"] == "ASK-SAF"
    )
    safety = next(
        view.item
        for view in actual
        if any(e.message_ts == "1791997800.010040" for e in view.item.evidence)
    )
    assert safety.absence is not None
    assert (safety.absence.source, safety.absence.asker, safety.absence.days_silent) == (
        "ask",
        "U0EM01",
        safety_label["days_silent"],
    )


def test_why_ranked_owned_blocker(ctx: QueryContext) -> None:
    """Setup the labelled stale pack / Action ask why ME sees it / Assert the owned-blocker rule."""
    label = next(json.loads(line) for line in GOLDEN.read_text().splitlines() if '"S4-FAI"' in line)
    assert "U0ME01" in label["must_see_for"]
    actual = queries.why_ranked(ctx, "U0ME01", STALE_PACK)
    assert actual is not None
    assert (actual.user_id, actual.user_name, actual.item_id, actual.title) == (
        "U0ME01",
        "Nadia Brenner",
        STALE_PACK,
        recorded(ctx)[STALE_PACK].title,
    )
    assert (actual.section, actual.lane, actual.rank, actual.score) == (
        "risks",
        "must_see",
        None,
        0,
    )
    assert "open blocker" in actual.reason
    assert "owned entity" in actual.reason
    assert "PN-11020" in actual.reason


def test_why_ranked_scored_line(ctx: QueryContext) -> None:
    """Setup a selected ranked line / Action ask why / Assert its rank, score and reason."""
    selection = select_digest(
        ctx.store,
        ctx.world,
        ctx.priors,
        "U0ME01",
        ctx.day,
        previous_digest=ctx.previous_digest,
    )
    ranked = [line for line in selection.lines if not line.must_see]
    assert ranked
    expected = ranked[0]
    actual = queries.why_ranked(ctx, "U0ME01", expected.item.item_id)
    assert actual is not None
    assert (
        actual.item_id,
        actual.section,
        actual.lane,
        actual.rank,
        actual.score,
        actual.reason,
    ) == (expected.item.item_id, expected.section, "ranked", 1, expected.score, expected.reason)


@pytest.mark.parametrize("item_id", ["unknown-id", PACK_ISSUED])
def test_why_ranked_not_delivered(ctx: QueryContext, item_id: str) -> None:
    """Setup an unknown or old FYI item / Action ask why ranked / Assert no delivered line."""
    assert queries.why_ranked(ctx, "U0ME01", item_id) is None


@pytest.mark.parametrize("function", ["why_ranked", "get_digest"])
@pytest.mark.parametrize(
    ("user_id", "error"), [("unknown-user", KeyError), ("B0PLM01", ValueError)]
)
def test_reader_refusals(
    ctx: QueryContext, function: str, user_id: str, error: type[Exception]
) -> None:
    """Setup a missing user or world bot / Action query a digest / Assert the reader refusal."""
    args = (ctx, user_id, STALE_PACK) if function == "why_ranked" else (ctx, user_id)
    with pytest.raises(error):
        getattr(queries, function)(*args)


def test_digest(ctx: QueryContext) -> None:
    """Setup the ME reader on day 6 / Action get digest / Assert named lines and rendered context."""
    selection = select_digest(
        ctx.store,
        ctx.world,
        ctx.priors,
        "U0ME01",
        ctx.day,
        previous_digest=ctx.previous_digest,
    )
    assert any(not line.must_see for line in selection.lines)
    expected_lines = []
    ranks: dict[str, int] = {}
    for selected in selection.lines:
        rank = None
        if not selected.must_see:
            ranks[selected.section] = ranks.get(selected.section, 0) + 1
            rank = ranks[selected.section]
        expected_lines.append(
            (
                selected.item.item_id,
                selected.section,
                "must_see" if selected.must_see else "ranked",
                rank,
                selected.score,
                selected.reason,
            )
        )
    actual = queries.get_digest(ctx, "U0ME01")
    assert [
        (line.item_id, line.section, line.lane, line.rank, line.score, line.reason)
        for line in actual.lines
    ] == expected_lines
    assert actual.text == render_digest(ctx.world, selection)
    assert (actual.user_id, actual.user_name, actual.day, actual.previous_digest) == (
        "U0ME01",
        "Nadia Brenner",
        DAY,
        PREVIOUS,
    )
    line = next(line for line in actual.lines if line.item_id == STALE_PACK)
    assert (line.lane, line.section, line.rank, line.score) == ("must_see", "risks", None, 0)
    assert line.title == recorded(ctx)[STALE_PACK].title
    assert actual.text.startswith("Daily digest for Nadia Brenner (ME) - 2026-10-19\n")
    assert "Since your digest of 2026-10-16" in actual.text
    assert "Phase: EVT since 2026-08-03. Next gate: G-TR1" in actual.text
    assert line.title in actual.text
    assert recorded(ctx)[STALE_PACK].evidence[0].permalink in actual.text
    sections = {
        section: i
        for i, section in enumerate(("needs_you", "not_happened", "decisions", "risks", "fyi"))
    }
    assert [sections[line.section] for line in actual.lines] == sorted(
        sections[line.section] for line in actual.lines
    )
    for section in sections:
        ranked = [
            line for line in actual.lines if line.section == section and line.lane == "ranked"
        ]
        assert [line.rank for line in ranked] == list(range(1, len(ranked) + 1))
    assert all(
        line.user_id == actual.user_id and line.user_name == actual.user_name
        for line in actual.lines
    )


def test_project_state(ctx: QueryContext) -> None:
    """Setup world gates and recorded task links / Action get project state / Assert every task and person."""
    actual = queries.get_project_state(ctx)
    assert actual.day == DAY
    assert actual.phase_line == (
        "Phase: EVT since 2026-08-03. Next gate: G-TR1 "
        "Top-cover tooling and texture release (PN-51010), in 1 working day."
    )
    assert [(p.user_id, p.name, p.role, p.kind) for p in actual.people] == [
        (p.user_id, p.name, p.role, p.kind)
        for p in sorted(ctx.world.personas, key=lambda p: p.user_id)
    ]
    buffers = {
        "G-EVT2-EXIT": (date(2026, 10, 12), False),
        "G-TR1": (date(2026, 10, 20), False),
        "G-DVT1": (date(2026, 10, 13), True),
    }
    assert [(g.gate_id, g.name, g.date, g.buffer_start, g.in_buffer) for g in actual.gates] == [
        (g.id, g.name, g.date, *buffers[g.id])
        for g in sorted(ctx.world.project.gates, key=lambda g: (g.date, g.id))
    ]
    links: dict[str, list[tuple[str, date]]] = {}
    zone = ZoneInfo(ctx.world.timezone)
    for row in rows(ctx, "SELECT tasks FROM items"):
        for link in json.loads(row["tasks"]):
            day = datetime.fromtimestamp(float(link["message_ts"]), zone).date()
            if day <= ctx.day:
                links.setdefault(link["task"], []).append((link["effect"], day))
    states = {
        task: (
            "done" if any(effect == "done" for effect, _ in entries) else "in progress",
            max(day for _, day in entries),
        )
        for task, entries in links.items()
    }
    for task, entries in links.items():
        if len(entries) > 1:
            actual_task = next(t for t in actual.tasks if t.task_id == task)
            assert actual_task.latest_link_day == max(day for _, day in entries)
    expected = {
        (g.id, task.id): (task.task, task.owner, *states.get(task.id, ("no evidence", None)))
        for g in ctx.world.project.gates
        for task in g.checklist
    }
    assert len(actual.tasks) == len(expected)
    assert {
        (t.gate_id, t.task_id): (t.task, t.owner_role, t.state, t.latest_link_day)
        for t in actual.tasks
    } == expected


def test_query_select(ctx: QueryContext) -> None:
    """Setup recorded item rows / Action run ordered SQL / Assert columns and complete rows."""
    sql = "SELECT item_id, title, owner FROM items ORDER BY item_id"
    expected = rows(ctx, sql)
    actual = queries.query(ctx, sql)
    assert actual.columns == ("item_id", "title", "owner")
    assert actual.rows == tuple(tuple(row.values()) for row in expected)
    assert actual.truncated is False
    assert_store_released(ctx)


def test_schema_text() -> None:
    """Setup the committed schema / Action request schema text / Assert the exact resource."""
    expected = Path("src/digest/schema.sql").read_text(encoding="utf-8")
    assert queries.schema_text() == expected


@pytest.mark.parametrize(
    "function",
    [
        "open_context",
        "search_items",
        "get_item",
        "get_entity_history",
        "list_open_blockers",
        "list_absences",
        "why_ranked",
        "get_digest",
        "get_project_state",
        "query",
        "schema_text",
    ],
)
def test_functions_leave_store_bytes(
    ctx: QueryContext, wal_store: sqlite3.Connection, function: str
) -> None:
    """Setup a store with WAL bytes / Action call each function / Assert both files stay identical."""
    assert isinstance(ctx.store.path, Path)
    before = file_bytes(ctx.store.path)
    assert before[1]
    args: dict[str, tuple[Any, ...]] = {
        "open_context": (ctx.store, ctx.world, ctx.priors),
        "search_items": (ctx, "CMM"),
        "get_item": (ctx, STALE_PACK),
        "get_entity_history": (ctx, "chassis plate"),
        "list_open_blockers": (ctx,),
        "list_absences": (ctx,),
        "why_ranked": (ctx, "U0ME01", STALE_PACK),
        "get_digest": (ctx, "U0ME01"),
        "get_project_state": (ctx,),
        "query": (ctx, "SELECT item_id FROM items"),
        "schema_text": (),
    }
    try:
        getattr(queries, function)(*args[function])
    finally:
        assert file_bytes(ctx.store.path) == before
        assert ctx.store.get_meta("query_test_marker") == "keep-store-open"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM items",
        "VACUUM INTO '{copy}'",
        "ATTACH '{attached}' AS x",
        "CREATE TEMP TABLE forbidden (n INTEGER)",
        "PRAGMA hard_heap_limit=1000",
        "PRAGMA table_info(items)",
        "PRAGMA data_version=1",
    ],
)
def test_query_refuses_writes(
    ctx: QueryContext, wal_store: sqlite3.Connection, tmp_path: Path, statement: str
) -> None:
    """Setup file targets and WAL bytes / Action try forbidden SQL / Assert refusal and no writes."""
    assert isinstance(ctx.store.path, Path)
    copy = tmp_path / "copy.db"
    attached = tmp_path / "x.db"
    sql = statement.format(
        copy=copy.as_posix().replace("'", "''"), attached=attached.as_posix().replace("'", "''")
    )
    before = file_bytes(ctx.store.path)
    try:
        with pytest.raises(ValueError, match=r"not authorized|authorization denied"):
            queries.query(ctx, sql)
    finally:
        assert file_bytes(ctx.store.path) == before
        assert not copy.exists()
        assert not attached.exists()


def test_query_refusal_closes_connection(ctx: QueryContext) -> None:
    """Setup forbidden SQL / Action query / Assert the error and release of the file."""
    with pytest.raises(ValueError, match=r"not authorized|authorization denied"):
        queries.query(ctx, "DELETE FROM items")
    assert_store_released(ctx)


@pytest.mark.parametrize("row_cap", [2, 1])
def test_query_row_cap_boundary(ctx: QueryContext, row_cap: int) -> None:
    """Setup two fixed rows / Action cap at or below two / Assert exact truncation."""
    actual = queries.query(ctx, "SELECT 1 UNION ALL SELECT 2", row_cap=row_cap)
    assert actual.rows == ((1,), (2,))[:row_cap]
    assert actual.truncated is (row_cap == 1)


@pytest.mark.parametrize("row_cap", [1, 31, 1000])
def test_query_row_cap(ctx: QueryContext, row_cap: int) -> None:
    """Setup recorded rows / Action use a row cap / Assert the prefix and exact truncation flag."""
    sql = "SELECT item_id FROM items ORDER BY item_id"
    expected = tuple((r["item_id"],) for r in rows(ctx, sql))
    actual = queries.query(ctx, sql, row_cap=row_cap)
    assert actual.rows == expected[:row_cap]
    assert actual.truncated is (len(expected) > row_cap)


@pytest.mark.parametrize(
    "options", [{"row_cap": 0}, {"row_cap": 1001}, {"timeout_s": 0.09}, {"timeout_s": 10.01}]
)
def test_query_invalid_limits(ctx: QueryContext, options: dict[str, Any]) -> None:
    """Setup limits outside Q9 / Action query / Assert ValueError."""
    with pytest.raises(ValueError):
        queries.query(ctx, "SELECT 1", **options)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT 1; SELECT 2", "one statement at a time"),
        ("SELECT length(hex(zeroblob(2000000)))", "string or blob too big"),
        ("SELECT FROM items", 'near "FROM": syntax error'),
        ("", "one SQL statement is required"),
        ("-- only a comment", "one SQL statement is required"),
    ],
    ids=["multiple", "oversized", "syntax", "empty", "comment-only"],
)
def test_query_rejects_invalid_sql(ctx: QueryContext, sql: str, message: str) -> None:
    """Setup multiple, oversized or invalid SQL / Action query / Assert ValueError."""
    with pytest.raises(ValueError, match=message):
        queries.query(ctx, sql)


@pytest.mark.parametrize("width", [100, 101])
def test_query_column_limit(ctx: QueryContext, width: int) -> None:
    """Setup a SELECT of `width` columns / Action query / Assert 100 run and 101 are refused."""
    sql = "SELECT " + ", ".join(f"{n} AS c{n}" for n in range(width))
    if width > 100:
        with pytest.raises(ValueError, match="too many columns"):
            queries.query(ctx, sql)
        return
    actual = queries.query(ctx, sql)
    assert actual.columns == tuple(f"c{n}" for n in range(width))
    assert actual.rows == (tuple(range(width)),)


@pytest.mark.parametrize("rows_count", [8, 20])
def test_query_result_budget(ctx: QueryContext, rows_count: int) -> None:
    """Setup 0.9 MB BLOB rows under the row cap / Action query / Assert the 8 MB budget bites
    at 20 rows (18 MB), fast, and not at 8 rows (7.2 MB)."""
    sql = (
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c "
        f"WHERE n < {rows_count}) SELECT zeroblob(900000) FROM c"
    )
    start = perf_counter()
    if rows_count == 20:
        with pytest.raises(ValueError, match="query result is too large"):
            queries.query(ctx, sql, row_cap=1000, timeout_s=10)
        assert perf_counter() - start < 5
        return
    actual = queries.query(ctx, sql, row_cap=1000, timeout_s=10)
    assert len(actual.rows) == rows_count
    assert actual.truncated is False


def test_query_cells(ctx: QueryContext) -> None:
    """Setup SQL scalar cells / Action query / Assert NULL, numeric types and BLOB hex text."""
    actual = queries.query(
        ctx, "SELECT NULL AS missing, X'00FF80' AS bytes, 7 AS n, 1.5 AS f, 'K2' AS text"
    )
    assert actual.columns == ("missing", "bytes", "n", "f", "text")
    assert len(actual.rows) == 1
    missing, blob, n, f, text = actual.rows[0]
    assert isinstance(blob, str)
    assert (missing, blob.lower(), n, f, text) == (None, "00ff80", 7, 1.5, "K2")
    assert actual.truncated is False


def test_query_fts(ctx: QueryContext) -> None:
    """Setup recorded FTS evidence / Action run MATCH / Assert SQLite returns the cited message."""
    sql = (
        "SELECT e.channel_id, e.ts FROM events e JOIN events_fts f ON e.rowid = f.rowid "
        "WHERE events_fts MATCH 'CMM' ORDER BY e.ts_us DESC, e.channel_id"
    )
    expected = rows(ctx, sql)
    assert expected
    actual = queries.query(ctx, sql)
    assert actual.columns == ("channel_id", "ts")
    assert actual.rows == tuple(tuple(row.values()) for row in expected)
    assert actual.truncated is False


def test_query_timeout(ctx: QueryContext) -> None:
    """Setup endless recursion / Action query for 0.5 s / Assert timeout and file release."""
    sql = (
        "SELECT count(*) FROM (WITH RECURSIVE c(n) AS "
        "(SELECT 1 UNION ALL SELECT n+1 FROM c) SELECT n FROM c)"
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            queries.query(ctx, sql, timeout_s=0.5)
        except BaseException as error:
            errors.append(error)

    start = perf_counter()
    worker = Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=10)
    elapsed = perf_counter() - start
    assert not worker.is_alive(), "query did not stop within 10 seconds"
    assert len(errors) == 1, "query must report a timeout"
    with pytest.raises(ValueError, match=r"^query timed out$"):
        raise errors[0]
    assert elapsed < 1.5
    assert_store_released(ctx)


def test_query_finite_recursion(ctx: QueryContext) -> None:
    """Setup finite recursion / Action query / Assert all five rows."""
    actual = queries.query(
        ctx,
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<5) SELECT n FROM c",
    )
    assert actual.columns == ("n",)
    assert actual.rows == ((1,), (2,), (3,), (4,), (5,))
    assert actual.truncated is False


@pytest.mark.parametrize(
    ("function", "args", "field"),
    [
        ("get_item", (STALE_PACK,), "owner_name"),
        ("get_entity_history", ("PN-11020",), "note"),
        ("why_ranked", ("U0ME01", STALE_PACK), "reason"),
        ("get_digest", ("U0ME01",), "text"),
        ("get_project_state", (), "phase_line"),
        ("query", ("SELECT 1",), "columns"),
    ],
)
def test_views_are_frozen(
    ctx: QueryContext, function: str, args: tuple[Any, ...], field: str
) -> None:
    """Setup a query result / Action change a field / Assert the frozen-view contract."""
    actual = getattr(queries, function)(ctx, *args)
    assert actual is not None
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(actual, field, getattr(actual, field))
    json.loads(actual.model_dump_json())

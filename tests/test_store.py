import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from digest import models
from digest.models import Event, Reaction
from digest.store import StageRun, Store, ThreadDelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
AS_OF = datetime(2026, 10, 14, tzinfo=UTC)
ROOT = "1760000000.000001"


def event(ts: str = ROOT, **changes: Any) -> Event:
    values: dict[str, Any] = {
        "source": "replay",
        "channel_id": "C1",
        "channel_name": "eng-mech",
        "thread_ts": ROOT,
        "ts": ts,
        "event_ts": ts,
        "user_id": "U1",
        "text": "PN-31010 Rev B",
        "permalink": f"https://kestrel.slack.com/archives/C1/p{ts.replace('.', '')}",
    }
    return Event(**(values | changes))


def run(delta: ThreadDelta, **changes: Any) -> StageRun:
    values: dict[str, Any] = {
        "stage": "extract",
        "thread_id": delta.thread_id,
        "last_ts": delta.last_ts,
        "prompt_version": "v1",
        "model": "extract",
        "run_at": AS_OF,
        "status": "ok",
    }
    return StageRun(**(values | changes))


@pytest.fixture
def store() -> Iterator[Store]:
    with Store.open(":memory:") as db:
        yield db


def test_upsert_replaces_all_non_key_fields(store: Store) -> None:
    """Setup one row; write a correction; assert every stored field is replaced."""
    original = event()
    replacement = event(
        source="slack",
        channel_name="plm-feed",
        thread_ts="1759999999.000001",
        event_ts="1760000001.000001",
        user_id="U2",
        text="PN-31010 Rev C",
        mentions=("U3", "U4"),
        reactions=(Reaction(name="eyes", user_id="U3", ts="1760000002.000001"),),
        files=("drawing.pdf",),
        permalink="https://kestrel.slack.com/corrected",
        edited=True,
        deleted=True,
    )
    assert store.upsert_events([original]) == 1
    assert store.upsert_events([replacement]) == 1
    assert store.events_in_thread(original.thread_id, AS_OF) == []
    assert store.events_in_thread(replacement.thread_id, AS_OF) == [replacement]
    assert store.upsert_events([original]) == 1
    assert store.events_in_thread(replacement.thread_id, AS_OF) == []
    assert store.events_in_thread(original.thread_id, AS_OF) == [original]


def test_upsert_key_includes_channel(store: Store) -> None:
    first, second = event(), event(channel_id="C2")
    assert store.upsert_events([first, first, second]) == 3
    assert store.events_in_thread(first.thread_id, AS_OF) == [first]
    assert store.events_in_thread(second.thread_id, AS_OF) == [second]


@pytest.mark.parametrize("deleted", [False, True])
def test_edit_and_delete_remain_readable(store: Store, deleted: bool) -> None:
    original = event(text="oldword")
    edited = event(text="newword", event_ts="1760000001.000001", edited=True)
    store.upsert_events([original, edited])
    latest = edited
    if deleted:
        latest = event(text="newword", event_ts="1760000002.000001", edited=True, deleted=True)
        store.upsert_events([latest])
    assert store.events_in_thread(original.thread_id, AS_OF) == [latest]
    assert store.search_events("newword", AS_OF) == [latest]
    assert store.search_events("oldword", AS_OF) == []


@pytest.mark.parametrize("field", ["ts", "event_ts", "thread_ts"])
@pytest.mark.parametrize("bad", ["1760000000.1", "176000000.000001", "1760000000x000001"])
def test_event_rejects_malformed_timestamp(field: str, bad: str) -> None:
    changes = {field: bad}
    if field == "ts":
        changes["event_ts"] = ROOT
    with pytest.raises(ValidationError, match=rf"(?m)^{field}\n"):
        event(**changes)


def test_event_rejects_observation_before_post() -> None:
    with pytest.raises(ValidationError, match=r"event_ts.*before ts"):
        event(event_ts="1760000000.000000")


def test_event_times_are_exact_utc() -> None:
    row = event(event_ts="1760000001.000002")
    assert row.ts_us == 1_760_000_000_000_001
    assert row.at == EPOCH + timedelta(seconds=1_760_000_000, microseconds=1)
    assert row.observed_at == EPOCH + timedelta(seconds=1_760_000_001, microseconds=2)
    assert row.at.utcoffset() == row.observed_at.utcoffset() == timedelta(0)
    assert row.thread_id == f"C1:{ROOT}"


def test_reads_bound_post_time_and_order_rows(store: Store) -> None:
    """Setup posts around a bound; read all interfaces; assert inclusive post time."""
    first = event(event_ts="1760000010.000000", edited=True)
    boundary = event("1760000000.000002")
    future = event("1760000000.000003")
    future_thread = event("1760000000.000004", thread_ts="1760000000.000004")
    store.upsert_events([future, boundary, first, future_thread])
    as_of = EPOCH + timedelta(seconds=1_760_000_000, microseconds=2)
    assert store.events_in_thread(first.thread_id, as_of) == [first, boundary]
    assert {row.ts for row in store.search_events("PN-31010", as_of)} == {first.ts, boundary.ts}
    (delta,) = store.thread_deltas("extract", "v1", "extract", as_of)
    assert delta.changed == (first, boundary)
    assert delta.root == first
    assert delta.last_ts == first.event_ts


@pytest.mark.parametrize("token", ["PN-31010", "ECO-0142", "TST-THM-02", "PART_A"])
def test_search_keeps_identifier_tokens(store: Store, token: str) -> None:
    exact = event(text=f"{token} Rev B")
    fragment = event("1760000000.000002", text=token.replace("-", " ").replace("_", " "))
    store.upsert_events([exact, fragment])
    assert store.search_events(token, AS_OF) == [exact]


def test_search_quotes_terms_and_honors_limit(store: Store) -> None:
    rows = [event(f"1760000000.{i:06d}") for i in range(1, 5)]
    rows.append(event("1760000000.000005", text="Rev B for PN-31010"))
    store.upsert_events([*rows, event("1760000000.000006", text="PN-31010 only")])
    assert store.search_events("PN-31010 Rev", AS_OF) == list(reversed(rows))
    assert store.search_events("PN-31010 Rev", AS_OF, limit=2) == list(reversed(rows[-2:]))


@pytest.mark.parametrize("root_state", ["present", "missing", "deleted"])
def test_initial_delta_preserves_nulls_and_deleted_root(store: Store, root_state: str) -> None:
    root = None if root_state == "missing" else event(deleted=root_state == "deleted")
    reply = event("1760000000.000002")
    rows = [reply] if root is None else [reply, root]
    store.upsert_events(rows)
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    assert delta.thread_id == reply.thread_id
    assert (delta.channel_id, delta.thread_ts) == ("C1", ROOT)
    assert delta.since_ts is None
    assert delta.root == root
    assert delta.last_ts == reply.event_ts
    assert delta.changed == tuple(reversed(rows))


@pytest.mark.parametrize("deleted", [False, True])
def test_older_message_revision_makes_thread_pending(store: Store, deleted: bool) -> None:
    """Setup a completed thread; revise its root; assert only the revision is changed."""
    root, reply = event(), event("1760000000.000002")
    store.upsert_events([root, reply])
    (initial,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    store.record_stage_run(run(initial))
    revised = event(event_ts="1760000001.000001", edited=not deleted, deleted=deleted)
    store.upsert_events([revised])
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    assert delta.since_ts == reply.event_ts
    assert delta.last_ts == revised.event_ts
    assert delta.root == revised
    assert delta.changed == (revised,)


def test_all_deleted_thread_is_returned_until_recorded(store: Store) -> None:
    rows = [event(deleted=True), event("1760000000.000002", deleted=True)]
    store.upsert_events(rows)
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    assert delta.changed == tuple(rows)
    assert delta.root == rows[0]
    assert delta.last_ts == rows[-1].event_ts
    store.record_stage_run(run(delta))
    assert store.thread_deltas("extract", "v1", "extract", AS_OF) == []


@pytest.mark.parametrize(
    ("stage", "pv", "model"),
    [("link", "v1", "extract"), ("extract", "v2", "extract"), ("extract", "v1", "other")],
)
def test_stage_run_scope(store: Store, stage: str, pv: str, model: str) -> None:
    store.upsert_events([event()])
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    store.record_stage_run(run(delta))
    assert store.thread_deltas("extract", "v1", "extract", AS_OF) == []
    assert store.thread_deltas(stage, pv, model, AS_OF) == [delta]


@pytest.mark.parametrize("statuses", [("failed", "ok"), ("ok", "failed")])
def test_stage_run_upserts_status(
    store: Store, statuses: tuple[Literal["ok", "failed"], ...]
) -> None:
    store.upsert_events([event()])
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    for status in statuses:
        store.record_stage_run(run(delta, status=status))
        expected = [] if status == "ok" else [delta]
        assert store.thread_deltas("extract", "v1", "extract", AS_OF) == expected


def test_since_ts_uses_newest_smaller_ok_revision(store: Store) -> None:
    """Setup unordered runs; request a delta; assert revision order selects the watermark."""
    rows = [event(f"1760000000.{i:06d}") for i in range(1, 6)]
    store.upsert_events(reversed(rows))
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    for revision, status in [(3, "ok"), (1, "ok"), (4, "failed"), (6, "ok")]:
        store.record_stage_run(run(delta, last_ts=f"1760000000.{revision:06d}", status=status))
    (actual,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    assert actual.since_ts == rows[2].event_ts
    assert actual.last_ts == rows[4].event_ts
    assert actual.changed == tuple(rows[3:])
    assert actual.root == rows[0]


def test_reaction_does_not_make_completed_thread_pending(store: Store) -> None:
    store.upsert_events([event()])
    (delta,) = store.thread_deltas("extract", "v1", "extract", AS_OF)
    store.record_stage_run(run(delta))
    reacted = event(reactions=(Reaction(name="eyes", user_id="U2", ts="1760000010.000001"),))
    store.upsert_events([reacted])
    assert store.events_in_thread(reacted.thread_id, AS_OF) == [reacted]
    assert store.thread_deltas("extract", "v1", "extract", AS_OF) == []


def test_meta_missing_and_replaced(store: Store) -> None:
    assert store.get_meta("missing") is None
    store.set_meta("world", "first")
    assert store.get_meta("world") == "first"
    store.set_meta("world", "second")
    assert store.get_meta("world") == "second"


def test_events_and_meta_commit_atomically(store: Store) -> None:
    row = event()
    invalid_meta: Any = {"until": None}
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_events([row], meta=invalid_meta)
    assert store.events_in_thread(row.thread_id, AS_OF) == []
    assert store.get_meta("until") is None
    assert store.upsert_events([row], meta={"world": "kestrel"}) == 1
    assert store.events_in_thread(row.thread_id, AS_OF) == [row]
    assert store.get_meta("world") == "kestrel"


def test_file_store_creates_parents_and_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "store.sqlite"
    with Store.open(path) as db:
        assert db.get_meta("schema_version") == "1"
        db.set_meta("world", "kestrel")
        db.upsert_events([event()])
        (delta,) = db.thread_deltas("extract", "v1", "extract", AS_OF)
        db.record_stage_run(run(delta))
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    with Store.open(path) as reopened:
        assert reopened.get_meta("world") == "kestrel"
        assert reopened.events_in_thread(event().thread_id, AS_OF) == [event()]
        assert reopened.search_events("PN-31010", AS_OF) == [event()]
        assert reopened.thread_deltas("extract", "v1", "extract", AS_OF) == []


def test_file_store_refuses_other_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    with Store.open(path) as db:
        db.set_meta("schema_version", "999")
    with pytest.raises((ValueError, RuntimeError), match=r"(?i)schema|version"), Store.open(path):
        pass


def test_file_store_refuses_unrelated_database_without_modifying_it(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (value TEXT)")
    with pytest.raises(ValueError, match=r"schema version None, expected 1"), Store.open(path):
        pass
    with sqlite3.connect(path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    assert tables == [("t",)]
    assert journal_mode == ("delete",)


@settings(max_examples=30, derandomize=True)
@given(
    st.lists(
        st.tuples(st.integers(1, 8), st.sampled_from(["C1", "C2"]), st.text(max_size=12)),
        max_size=12,
    )
)
def test_upsert_idempotence_property(values: list[tuple[int, str, str]]) -> None:
    rows = [
        event(f"1760000000.{i:06d}", channel_id=channel, text=text) for i, channel, text in values
    ]
    expected = {(row.channel_id, row.ts): row for row in rows}
    with Store.open(":memory:") as db:
        db.upsert_events(iter(rows))
        db.upsert_events(iter(rows))
        actual = [
            row
            for channel in ("C1", "C2")
            for row in db.events_in_thread(f"{channel}:{ROOT}", AS_OF)
        ]
        assert len(actual) == len(expected)
        assert {(row.channel_id, row.ts): row for row in actual} == expected


@settings(max_examples=40, derandomize=True)
@given(st.integers(1_000_000_000, 9_999_999_999), st.integers(0, 999_999))
def test_ts_round_trip_property(seconds: int, micros: int) -> None:
    ts = f"{seconds:010d}.{micros:06d}"
    row = event(ts)
    whole, fraction = divmod(row.ts_us, 1_000_000)
    assert f"{whole:010d}.{fraction:06d}" == ts
    assert row.at == EPOCH + timedelta(seconds=seconds, microseconds=micros)


@settings(max_examples=25, derandomize=True)
@given(st.lists(st.integers(1, 4), min_size=2, max_size=5), st.integers(0, 20))
def test_recorded_deltas_disappear_without_changing_others(counts: list[int], choice: int) -> None:
    """Setup generated threads; record each revision; assert other deltas stay unchanged."""
    rows = [
        event(f"1760000000.{j:06d}", channel_id=f"C{i}")
        for i, count in enumerate(counts)
        for j in range(1, count + 1)
    ]
    with Store.open(":memory:") as db:
        db.upsert_events(rows)
        pending = {d.thread_id: d for d in db.thread_deltas("extract", "v1", "extract", AS_OF)}
        assert set(pending) == {row.thread_id for row in rows}
        while pending:
            chosen = sorted(pending)[choice % len(pending)]
            db.record_stage_run(run(pending.pop(chosen)))
            assert {
                d.thread_id: d for d in db.thread_deltas("extract", "v1", "extract", AS_OF)
            } == pending


def item(**changes: Any) -> models.Item:
    values: dict[str, Any] = {
        "item_id": "I1",
        "thread_id": f"C1:{ROOT}",
        "type": "bom_change",
        "title": "Drive bracket released",
        "summary": "PN-31010 is released at Rev C.",
        "entities": ("PN-31010", "ECO-0142"),
        "owner": "U0ME01",
        "due": datetime(2026, 10, 22).date(),
        "affected_roles": ("ME", "supply_chain"),
        "severity": "high",
        "confidence": "medium",
        "evidence": (
            models.Evidence(
                message_ts=ROOT,
                quote="PN-31010 is released at Rev C.",
                permalink=event().permalink,
            ),
        ),
        "first_seen": ROOT,
        "last_updated": "1760000000.000002",
        "claims": (
            models.Claim(
                entity="PN-31010",
                attribute="rev",
                value="C",
                message_ts=ROOT,
                authority="plm_bot",
            ),
        ),
        "tasks": (models.TaskLink(task="G-DVT1.thermal", effect="done", message_ts=ROOT),),
        "status": "resolved",
        "resolved_by": "1760000000.000002",
        "absence": None,
    }
    return models.Item(**(values | changes))


def triage(**changes: Any) -> models.TriageDecision:
    values: dict[str, Any] = {
        "thread_id": f"C1:{ROOT}",
        "last_ts": ROOT,
        "prompt_version": "m2.1",
        "model": "classify",
        "probability": 0.75,
        "threshold": 1 / 11,
        "kept": True,
        "rationale": "The thread reports a released revision.",
        "decided_at": AS_OF,
    }
    return models.TriageDecision(**(values | changes))


@pytest.mark.parametrize("nulls", [False, True], ids=["all-fields", "null-columns"])
def test_items_round_trip_and_upsert_is_idempotent(store: Store, nulls: bool) -> None:
    """Setup a complete or nullable item; write it twice; assert one equal item survives."""
    row = item(owner=None, due=None, resolved_by=None, claims=(), tasks=()) if nulls else item()
    assert store.upsert_items([row], prompt_version="m2.1", model="extract") == 1
    assert store.upsert_items([row], prompt_version="m2.1", model="extract") == 1
    assert store.items_for_thread(row.thread_id) == [row]
    assert store.items(AS_OF) == [row]


def test_upsert_items_replaces_fields_and_provenance(tmp_path: Path) -> None:
    """Setup a stored item; replace its fields and provenance; assert both reads agree."""
    original = item()
    replacement = item(
        thread_id=f"C2:{ROOT}",
        type="risk",
        title="Release delayed",
        summary="The release needs another review.",
        entities=("PN-11051",),
        owner=None,
        due=None,
        affected_roles=("EM",),
        severity="low",
        confidence="high",
        evidence=(
            models.Evidence(
                message_ts="1760000000.000003",
                quote="Review needed.",
                permalink="https://kestrel.slack.com/review",
            ),
        ),
        first_seen="1760000000.000003",
        last_updated="1760000000.000004",
        claims=(),
        tasks=(),
        status="open",
        resolved_by=None,
    )
    path = tmp_path / "store.sqlite"
    with Store.open(path) as db:
        db.upsert_items([original], prompt_version="m2.1", model="extract")
        assert db.upsert_items([replacement], prompt_version="m2.2", model="other") == 1
        assert db.items_for_thread(original.thread_id) == []
        assert db.items_for_thread(replacement.thread_id) == [replacement]
        assert db.items(AS_OF) == [replacement]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT item_id, prompt_version, model FROM items"
        ).fetchall() == [(original.item_id, "m2.2", "other")]


def test_upsert_items_refuses_absence(store: Store) -> None:
    row = item(
        type="absence",
        absence=models.AbsenceDetail(
            source="ask",
            gate=None,
            task=None,
            asker="U0EM01",
            days_silent=3,
            buffer_consumed=None,
            buffer_days=None,
        ),
    )
    with pytest.raises(ValueError, match="absence"):
        store.upsert_items([row], prompt_version="m2.1", model="extract")
    assert store.items(AS_OF) == []


def test_items_filter_last_updated_and_order_first_seen(store: Store) -> None:
    """Setup items with crossed time order; read at a boundary; assert update filtering."""
    first = item(item_id="I3", first_seen="1760000000.000001", last_updated="1760000000.000004")
    second = item(item_id="I2", first_seen="1760000000.000002", last_updated="1760000000.000003")
    future = item(item_id="I1", first_seen="1760000000.000000", last_updated="1760000000.000005")
    other = item(
        item_id="I4",
        thread_id=f"C2:{ROOT}",
        first_seen="1760000000.000003",
        last_updated="1760000000.000003",
    )
    store.upsert_items([second, other, first, future], prompt_version="m2.1", model="extract")
    as_of = EPOCH + timedelta(seconds=1_760_000_000, microseconds=4)
    assert store.items(as_of) == [first, second, other]
    assert store.items_for_thread(first.thread_id) == [future, first, second]
    assert store.items_for_thread(other.thread_id) == [other]
    assert store.items_for_thread("missing") == []


def test_triage_round_trip_and_replacement(store: Store) -> None:
    """Setup a kept decision; replace its payload; assert one complete decision remains."""
    original = triage()
    replacement = triage(
        probability=0.01,
        threshold=0.2,
        kept=False,
        rationale="The thread has no project change.",
        decided_at=AS_OF + timedelta(microseconds=1),
    )
    store.record_triage(original)
    assert (
        store.triage_decision(
            original.thread_id, original.last_ts, original.prompt_version, original.model
        )
        == original
    )
    store.record_triage(original)
    store.record_triage(replacement)
    assert (
        store.triage_decision(
            original.thread_id, original.last_ts, original.prompt_version, original.model
        )
        == replacement
    )
    assert store.triage_for_thread(original.thread_id) == [replacement]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thread_id", f"C2:{ROOT}"),
        ("last_ts", "1760000000.000002"),
        ("prompt_version", "m2.2"),
        ("model", "other"),
    ],
)
def test_triage_key_uses_each_column(store: Store, field: str, value: str) -> None:
    """Setup two keys differing in one column; record both; assert independent decisions."""
    original = triage()
    other = triage(**{field: value, "rationale": "Another decision."})
    store.record_triage(original)
    assert (
        store.triage_decision(other.thread_id, other.last_ts, other.prompt_version, other.model)
        is None
    )
    store.record_triage(other)
    for row in (original, other):
        assert (
            store.triage_decision(row.thread_id, row.last_ts, row.prompt_version, row.model) == row
        )


def test_triage_for_thread_orders_revisions_and_excludes_other_threads(store: Store) -> None:
    """Setup unordered decisions in two threads; read one thread; assert revision order."""
    first = triage()
    second = triage(last_ts="1760000000.000002", decided_at=AS_OF - timedelta(days=1))
    third = triage(last_ts="1760000000.000003", decided_at=AS_OF - timedelta(days=2))
    other = triage(thread_id=f"C2:{ROOT}")
    for row in (third, other, first, second):
        store.record_triage(row)
    assert store.triage_for_thread(first.thread_id) == [first, second, third]
    assert store.triage_for_thread("missing") == []


def test_triage_decision_missing_from_empty_store(store: Store) -> None:
    assert store.triage_decision(f"C1:{ROOT}", ROOT, "m2.1", "classify") is None

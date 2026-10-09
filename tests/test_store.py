import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

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
    assert {row.ts for row in store.search_events("PN-31010 Rev", AS_OF)} == {
        row.ts for row in rows
    }
    limited = store.search_events("PN-31010 Rev", AS_OF, limit=2)
    assert len(limited) == len({row.ts for row in limited}) == 2
    assert all(row in rows for row in limited)


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
        # foreign_keys is per connection, and Store does not expose its connection.
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

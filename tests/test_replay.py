import json
import re
from datetime import UTC, datetime, timedelta
from functools import partialmethod
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from digest.cli import app
from digest.models import Event, Reaction
from digest.replay import IngestReport, ReplayFormatError, ingest, read_events
from digest.settings import Settings
from digest.store import Store
from digest.world.load import WorldHeader, load_world_header

WORKSPACE = "https://kestrel.slack.com"
ROOT = "1760000000.000001"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
AS_OF = datetime(2026, 10, 15, tzinfo=UTC)
runner = CliRunner()


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
        "permalink": f"{WORKSPACE}/archives/C1/p{ts.replace('.', '')}",
    }
    return Event(**(values | changes))


def write_events(path: Path, rows: list[Event]) -> Path:
    path.write_text("".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture
def cli_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_WORLD", "kestrel")
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_BACKEND", "openrouter")
    monkeypatch.delenv("DIGEST_DB", raising=False)
    monkeypatch.setattr(Settings, "__init__", partialmethod(Settings.__init__, _env_file=None))
    directory = tmp_path / "data" / "world" / "kestrel"
    directory.mkdir(parents=True)
    (directory / "world.yaml").write_text(
        yaml.safe_dump(
            {"name": "kestrel", "workspace_url": WORKSPACE, "timezone": "America/Los_Angeles"}
        ),
        encoding="utf-8",
    )
    write_events(directory / "events.jsonl", [event()])
    return directory


@pytest.mark.parametrize("reply", [False, True])
def test_read_events_fills_defaults_and_builds_permalink(tmp_path: Path, reply: bool) -> None:
    ts = "1760000000.000002" if reply else ROOT
    raw: dict[str, Any] = {
        "channel_id": "C1",
        "channel_name": "eng-mech",
        "ts": ts,
        "user_id": "U1",
        "text": "PN-31010 Rev B",
    }
    if reply:
        raw["thread_ts"] = ROOT
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    link = f"{WORKSPACE}/archives/C1/p{ts.replace('.', '')}"
    if reply:
        link += f"?thread_ts={ROOT}&cid=C1"
    assert list(read_events(path, WORKSPACE)) == [event(ts, permalink=link)]


def test_read_events_preserves_explicit_fields_and_equal_observation_times(tmp_path: Path) -> None:
    rows = [
        event(
            source="slack",
            mentions=("U2",),
            files=("drawing.pdf",),
            reactions=(Reaction(name="eyes", user_id="U2", ts="1760000001.000001"),),
            permalink="https://kestrel.slack.com/custom",
            edited=True,
            deleted=True,
        ),
        event(channel_id="C2", text="second channel"),
    ]
    path = write_events(tmp_path / "events.jsonl", rows)
    assert list(read_events(path, WORKSPACE)) == rows


@pytest.mark.parametrize(
    "fault",
    [
        "json",
        "unknown",
        "channel_id",
        "channel_name",
        "ts",
        "user_id",
        "text",
        "bad_ts",
        "bad_event_ts",
        "before_post",
        "out_of_order",
        "invalid_utf8",
    ],
)
def test_read_events_errors_name_file_and_line(tmp_path: Path, fault: str) -> None:
    valid = event(event_ts="1760000002.000001").model_dump(mode="json")
    invalid = dict(valid)
    if fault in {"channel_id", "channel_name", "ts", "user_id", "text"}:
        del invalid[fault]
    elif fault == "unknown":
        invalid["surprise"] = True
    elif fault == "bad_ts":
        invalid["ts"] = "1760000000.1"
    elif fault == "bad_event_ts":
        invalid["event_ts"] = "1760000002.1"
    elif fault == "before_post":
        invalid["ts"] = "1760000003.000001"
        invalid["event_ts"] = "1760000002.000001"
    elif fault == "out_of_order":
        invalid["event_ts"] = "1760000001.000001"
    path = tmp_path / "events.jsonl"
    if fault == "invalid_utf8":
        path.write_bytes(json.dumps(valid).encode() + b"\n\xff\n")
    else:
        bad_line = "{broken" if fault == "json" else json.dumps(invalid)
        path.write_text(json.dumps(valid) + "\n" + bad_line + "\n", encoding="utf-8")
    with pytest.raises(ReplayFormatError) as error:
        list(read_events(path, WORKSPACE))
    message = str(error.value)
    assert path.name in message
    assert re.search(r"(?:line\s+|:)2\b", message, re.IGNORECASE), message


def test_ingest_filters_observations_and_reactions_inclusively(tmp_path: Path) -> None:
    """Setup an edit and dated reactions; ingest at a bound; assert counts and retained rows."""
    until = EPOCH + timedelta(seconds=1_760_000_001, microseconds=1)
    early = Reaction(name="eyes", user_id="U2", ts=ROOT)
    boundary = Reaction(name="eyes", user_id="U3", ts="1760000001.000001")
    late = Reaction(name="eyes", user_id="U4", ts="1760000001.000002")
    original = event(reactions=(early, boundary, late))
    at_bound = event("1760000001.000001")
    edited = event(event_ts="1760000001.000002", text="Rev C", edited=True)
    deleted = event(event_ts="1760000002.000001", text="Rev C", edited=True, deleted=True)
    path = write_events(tmp_path / "events.jsonl", [original, at_bound, edited, deleted])
    with Store.open(":memory:") as db:
        report = ingest(db, path, until, WORKSPACE)
        assert report == IngestReport(read=4, written=2, after_until=2)
        assert db.events_in_thread(original.thread_id, AS_OF) == [
            event(reactions=(early, boundary)),
            at_bound,
        ]
        assert ingest(db, path, AS_OF, WORKSPACE) == IngestReport(read=4, written=4, after_until=0)
        assert db.events_in_thread(original.thread_id, AS_OF) == [deleted, at_bound]


def test_repeated_ingest_preserves_rows_and_reports(tmp_path: Path) -> None:
    rows = [event(), event("1760000000.000002")]
    path = write_events(tmp_path / "events.jsonl", rows)
    with Store.open(":memory:") as db:
        first = ingest(db, path, AS_OF, WORKSPACE)
        second = ingest(db, path, AS_OF, WORKSPACE)
        assert first == second == IngestReport(read=2, written=2, after_until=0)
        assert db.events_in_thread(rows[0].thread_id, AS_OF) == rows


def test_ingest_future_bound_writes_every_line(tmp_path: Path) -> None:
    rows = [event(), event("1760000000.000002")]
    path = write_events(tmp_path / "events.jsonl", rows)
    until = datetime(2300, 1, 1, tzinfo=UTC)
    with Store.open(":memory:") as db:
        assert ingest(db, path, until, WORKSPACE) == IngestReport(read=2, written=2, after_until=0)
        assert db.events_in_thread(rows[0].thread_id, until) == rows


def test_empty_corpus_has_zero_counts(tmp_path: Path) -> None:
    path = write_events(tmp_path / "events.jsonl", [])
    assert list(read_events(path, WORKSPACE)) == []
    with Store.open(":memory:") as db:
        assert ingest(db, path, AS_OF, WORKSPACE) == IngestReport(read=0, written=0, after_until=0)
        assert db.events_in_thread(f"C1:{ROOT}", AS_OF) == []


@settings(max_examples=25, derandomize=True)
@given(st.lists(st.integers(1, 4), min_size=1, max_size=12), st.integers(0, 12), st.integers(0, 12))
def test_ingest_prefix_property(keys: list[int], first_split: int, second_split: int) -> None:
    """Setup ordered corrections; ingest in two steps; assert the final snapshot equals one step."""
    rows = [
        event(
            f"1760000000.{key:06d}",
            event_ts=f"1760000001.{i:06d}",
            text=f"revision {i}",
            edited=i > 1,
            deleted=i % 3 == 0,
            reactions=(Reaction(name="eyes", user_id="U2", ts="1760000001.000006"),),
        )
        for i, key in enumerate(keys, 1)
    ]
    low, high = sorted((first_split, second_split))
    until_low = EPOCH + timedelta(seconds=1_760_000_001, microseconds=low)
    until_high = EPOCH + timedelta(seconds=1_760_000_001, microseconds=high)
    expected: dict[str, Event] = {}
    for i, row in enumerate(rows, 1):
        if i <= high:
            expected[row.ts] = event(
                row.ts,
                event_ts=row.event_ts,
                text=row.text,
                edited=row.edited,
                deleted=row.deleted,
                reactions=row.reactions if high >= 6 else (),
            )
    with TemporaryDirectory() as directory:
        path = write_events(Path(directory) / "events.jsonl", rows)
        with Store.open(":memory:") as staged, Store.open(":memory:") as direct:
            ingest(staged, path, until_low, WORKSPACE)
            report = ingest(staged, path, until_high, WORKSPACE)
            ingest(direct, path, until_high, WORKSPACE)
            assert report == IngestReport(
                read=len(rows), written=min(high, len(rows)), after_until=max(0, len(rows) - high)
            )
            wanted = [expected[ts] for ts in sorted(expected)]
            assert staged.events_in_thread(f"C1:{ROOT}", until_high) == wanted
            assert direct.events_in_thread(f"C1:{ROOT}", until_high) == wanted


@pytest.mark.parametrize("default_world", [False, True])
def test_cli_replay_prints_report_and_stores_metadata(
    cli_world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_world: bool
) -> None:
    """Setup a temporary world; replay with explicit or default options; assert rows and metadata."""
    selected_world = "test-world" if default_world else "kestrel"
    if default_world:
        renamed = cli_world.rename(cli_world.with_name(selected_world))
        (renamed / "world.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": selected_world,
                    "workspace_url": WORKSPACE,
                    "timezone": "America/Los_Angeles",
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setenv("DIGEST_WORLD", selected_world if default_world else "missing")
    db_path = (
        tmp_path / "data" / f"{selected_world}.sqlite"
        if default_world
        else tmp_path / "chosen.sqlite"
    )
    options = [] if default_world else ["--world", "kestrel", "--db", str(db_path)]
    result = runner.invoke(app, ["replay", "--until", "2026-10-13", *options])
    assert result.exit_code == 0, result.output
    for field, count in [("read", 1), ("written", 1), ("after_until", 0)]:
        assert re.search(rf"\b{field}\b[\s\"':=]+{count}\b", result.output), result.output
    assert db_path.is_file()
    with Store.open(db_path) as db:
        assert db.events_in_thread(f"C1:{ROOT}", AS_OF) == [event()]
        assert db.get_meta("world") == selected_world
        assert db.get_meta("timezone") == "America/Los_Angeles"
        assert db.get_meta("workspace_url") == WORKSPACE
        until = db.get_meta("until")
        assert until is not None
        assert datetime.fromisoformat(until) == datetime(
            2026, 10, 14, 6, 59, 59, 999999, tzinfo=UTC
        )


def test_cli_until_uses_end_of_world_day(cli_world: Path, tmp_path: Path) -> None:
    """Setup posts at local midnight; replay a date; assert the last microsecond is included."""
    end = datetime(2026, 10, 14, 6, 59, 59, 999999, tzinfo=UTC)
    seconds = (end - EPOCH).days * 86400 + (end - EPOCH).seconds
    boundary = event(f"{seconds}.999999")
    after = event(f"{seconds + 1}.000000")
    write_events(cli_world / "events.jsonl", [boundary, after])
    db_path = tmp_path / "boundary.sqlite"
    result = runner.invoke(
        app, ["replay", "--until", "2026-10-13", "--world", "kestrel", "--db", str(db_path)]
    )
    assert result.exit_code == 0, result.output
    with Store.open(db_path) as db:
        assert db.events_in_thread(boundary.thread_id, AS_OF) == [boundary]


@pytest.mark.parametrize("reason", ["missing_world", "different_world", "earlier_until"])
def test_cli_replay_refuses_invalid_target(cli_world: Path, tmp_path: Path, reason: str) -> None:
    db_path = tmp_path / "existing.sqlite"
    world = "missing" if reason == "missing_world" else "kestrel"
    original_world = "other" if reason == "different_world" else "kestrel"
    previous = (
        "2026-10-15T06:59:59.999999+00:00"
        if reason == "earlier_until"
        else "2026-10-13T06:59:59.999999+00:00"
    )
    with Store.open(db_path) as db:
        db.set_meta("world", original_world)
        db.set_meta("until", previous)
        db.upsert_events([event(text="preserved")])
    result = runner.invoke(
        app, ["replay", "--until", "2026-10-13", "--world", world, "--db", str(db_path)]
    )
    assert result.exit_code == 1, result.output
    if reason == "missing_world":
        output = result.output.lower().replace("\\", "/")
        assert "no such world" in output or "data/world/missing" in output
    elif reason == "different_world":
        assert "world" in result.output.lower() and "other" in result.output
    else:
        assert "until" in result.output.lower()
        assert re.search(r"earlier|backward|before", result.output, re.IGNORECASE)
    with Store.open(db_path) as db:
        assert db.get_meta("world") == original_world
        assert db.get_meta("until") == previous
        assert db.events_in_thread(f"C1:{ROOT}", AS_OF) == [event(text="preserved")]


def test_cli_missing_world_does_not_create_store(cli_world: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "typo.sqlite"
    result = runner.invoke(
        app, ["replay", "--until", "2026-10-13", "--world", "typo", "--db", str(db_path)]
    )
    assert result.exit_code == 1, result.output
    assert not db_path.exists()


@pytest.mark.parametrize("stored_until", ["yesterday", "2026-10-13T06:59:59"])
def test_cli_refuses_invalid_stored_until(
    cli_world: Path, tmp_path: Path, stored_until: str
) -> None:
    db_path = tmp_path / "invalid-until.sqlite"
    with Store.open(db_path) as db:
        db.set_meta("world", "kestrel")
        db.set_meta("until", stored_until)
    result = runner.invoke(
        app, ["replay", "--until", "2026-10-13", "--world", "kestrel", "--db", str(db_path)]
    )
    assert result.exit_code == 1, result.output
    assert "until" in result.output.lower()
    assert stored_until in result.output
    assert result.exc_info is None or result.exc_info[0] is SystemExit


@pytest.mark.parametrize(
    "previous",
    ["2026-10-14T06:59:59.999999+00:00", "2026-10-13T06:59:59.999999+00:00"],
    ids=["equal", "earlier"],
)
def test_cli_replay_accepts_existing_store(cli_world: Path, tmp_path: Path, previous: str) -> None:
    """Setup a compatible store; replay its day; assert its rows remain and metadata advances."""
    existing = event("1759999999.000001", text="preserved")
    db_path = tmp_path / "existing.sqlite"
    with Store.open(db_path) as db:
        db.set_meta("world", "kestrel")
        db.set_meta("until", previous)
        db.upsert_events([existing])
    result = runner.invoke(
        app, ["replay", "--until", "2026-10-13", "--world", "kestrel", "--db", str(db_path)]
    )
    assert result.exit_code == 0, result.output
    with Store.open(db_path) as db:
        until = db.get_meta("until")
        assert until is not None
        assert datetime.fromisoformat(until) == datetime(
            2026, 10, 14, 6, 59, 59, 999999, tzinfo=UTC
        )
        assert db.events_in_thread(f"C1:{ROOT}", AS_OF) == [existing, event()]


def test_cli_replay_does_not_mark_incomplete_ingest(cli_world: Path, tmp_path: Path) -> None:
    """Setup a malformed corpus; replay it; assert the failure line and absent watermark."""
    with (cli_world / "events.jsonl").open("a", encoding="utf-8") as corpus:
        corpus.write("{broken\n")
    db_path = tmp_path / "malformed.sqlite"
    result = runner.invoke(
        app, ["replay", "--until", "2026-10-13", "--world", "kestrel", "--db", str(db_path)]
    )
    assert result.exit_code != 0
    assert re.search(r"(?:line\s+|:)2\b", result.output, re.IGNORECASE), result.output
    with Store.open(db_path) as db:
        assert db.get_meta("until") is None


def test_world_header_ignores_extra_keys(tmp_path: Path) -> None:
    path = tmp_path / "world.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "kestrel",
                "timezone": "America/Los_Angeles",
                "workspace_url": WORKSPACE,
                "project": {"id": "K2"},
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    assert load_world_header(path) == WorldHeader(
        name="kestrel", workspace_url=WORKSPACE, timezone="America/Los_Angeles"
    )


@pytest.mark.parametrize("missing", ["name", "timezone", "workspace_url"])
def test_world_header_requires_each_key(tmp_path: Path, missing: str) -> None:
    raw = {"name": "kestrel", "timezone": "UTC", "workspace_url": WORKSPACE}
    del raw[missing]
    path = tmp_path / "world.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises((KeyError, ValueError), match=missing):
        load_world_header(path)

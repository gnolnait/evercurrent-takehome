import json
import os
import re
import sqlite3
import stat
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.driver import ScriptLine, StoreView, make_event
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.models import Event, Evidence, Item
from digest.replay import replay_world
from digest.store import StageRun, Store
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_DIR = ROOT / "data" / "world" / "kestrel"
SCRIPTS_DIR = WORLD_DIR / "scripts"
DAY = date(2026, 10, 19)
UNTIL = "2026-10-20T06:59:59.999999+00:00"
BASE_TS = "1792454400.000000"
TEXT = "A new what-if message."
runner = CliRunner()


@pytest.fixture(scope="module")
def world() -> World:
    return load_world(WORLD_DIR / "world.yaml")


def _event(
    ts: str = BASE_TS,
    *,
    channel_id: str = "C0EXTCM",
    channel_name: str = "ext-sunrise-cm",
    thread_ts: str | None = None,
) -> Event:
    root = thread_ts or ts
    return Event(
        source="replay",
        channel_id=channel_id,
        channel_name=channel_name,
        thread_ts=root,
        ts=ts,
        event_ts=ts,
        user_id="U0CM01",
        text="Existing message",
        permalink=f"https://kestrel-k2.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
    )


def _seed_store(path: Path, *, until: str = UNTIL, events: tuple[Event, ...] = ()) -> None:
    with Store.open(path) as store:
        store.upsert_events(
            events,
            meta={
                "world": "kestrel",
                "timezone": "America/Los_Angeles",
                "workspace_url": "https://kestrel-k2.slack.com",
                "until": until,
            },
        )
        as_of = datetime.combine(DAY, time.max, ZoneInfo("America/Los_Angeles")).astimezone(UTC)
        for delta in store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", as_of):
            store.record_stage_run(
                StageRun(
                    stage="extract",
                    thread_id=delta.thread_id,
                    last_ts=delta.last_ts,
                    prompt_version=EXTRACT_PROMPT_VERSION,
                    model="extract",
                    run_at=as_of,
                    status="ok",
                )
            )


def _database_contents(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            table: sorted(tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"'))
            for table in tables
        }


def _script_log(path: Path) -> Path:
    return Path(f"{path}.script.jsonl")


def _store_files(path: Path) -> tuple[Path, Path, Path]:
    return path, Path(f"{path}-wal"), Path(f"{path}-shm")


def _write_script(path: Path, *lines: ScriptLine) -> None:
    path.write_text(
        "".join(f"{line.model_dump_json()}\n" for line in lines),
        encoding="utf-8",
    )


def _diff_item() -> Item:
    return Item(
        item_id="ASK-GATE-MOVE",
        thread_id=f"C0LEADS:{BASE_TS}",
        type="ask",
        title="Approve the gate move",
        summary="Please approve the new DVT-1 gate date.",
        entities=(),
        owner="U0EM01",
        due=None,
        affected_roles=(),
        severity="low",
        confidence="high",
        evidence=(
            Evidence(
                channel_id="C0LEADS",
                message_ts=BASE_TS,
                quote="Please approve the new DVT-1 gate date.",
                permalink=(
                    f"https://kestrel-k2.slack.com/archives/C0LEADS/p{BASE_TS.replace('.', '')}"
                ),
            ),
        ),
        first_seen=BASE_TS,
        last_updated=BASE_TS,
    )


def _seed_diff_pair(base: Path, changed: Path) -> None:
    root = _event(
        channel_id="C0LEADS",
        channel_name="kestrel-leads",
    ).model_copy(
        update={
            "user_id": "U0PM01",
            "text": "Please approve the new DVT-1 gate date.",
        }
    )
    _seed_store(base, events=(root,))
    _seed_store(changed, events=(root,))
    with Store.open(changed) as store:
        store.upsert_items([_diff_item()], prompt_version=EXTRACT_PROMPT_VERSION, model="extract")


def _say_args(db: Path, base: Path, **changes: str) -> list[str]:
    values = {
        "--as": "U0CM01",
        "--channel": "ext-sunrise-cm",
        "--at": "2026-10-19T17:30",
        "--text": TEXT,
    } | changes
    args = ["say"]
    for option, value in values.items():
        args.extend([option, value])
    return [*args, "--db", str(db), "--base-db", str(base)]


def _assert_refused_without_write(base: Path, scratch: Path, expected: str, **changes: str) -> None:
    log = _script_log(scratch)
    log.write_text("keep this log\n", encoding="utf-8")
    before = _database_contents(base)

    result = runner.invoke(app, _say_args(scratch, base, **changes))

    assert result.exit_code == 1
    assert expected.lower() in result.output.lower()
    assert not scratch.exists()
    assert log.read_text(encoding="utf-8") == "keep this log\n"
    assert _database_contents(base) == before


def test_make_event_formats_whole_second_and_links_new_thread(world: World) -> None:
    event = make_event(
        world,
        StoreView(day=DAY, latest_event_ts=BASE_TS),
        "U0CM01",
        "ext-sunrise-cm",
        datetime(2026, 10, 19, 17, 30),
        TEXT,
        None,
    )

    assert event.model_dump(mode="json") == {
        "source": "replay",
        "channel_id": "C0EXTCM",
        "channel_name": "ext-sunrise-cm",
        "thread_ts": "1792456200.000000",
        "ts": "1792456200.000000",
        "event_ts": "1792456200.000000",
        "user_id": "U0CM01",
        "text": TEXT,
        "mentions": [],
        "reactions": [],
        "files": [],
        "permalink": "https://kestrel-k2.slack.com/archives/C0EXTCM/p1792456200000000",
        "edited": False,
        "deleted": False,
    }


def test_make_event_links_reply_to_given_thread(world: World) -> None:
    event = make_event(
        world,
        StoreView(day=DAY, latest_event_ts=None),
        "U0CM01",
        "ext-sunrise-cm",
        datetime(2026, 10, 19, 17, 30, 1),
        TEXT,
        "1792454400.000000",
    )

    assert event.ts == "1792456201.000000"
    assert event.thread_ts == "1792454400.000000"
    assert event.permalink.endswith("p1792456201000000?thread_ts=1792454400.000000&cid=C0EXTCM")


def test_make_event_refuses_time_equal_to_latest_event(world: World) -> None:
    with pytest.raises(ValueError, match=r"(?i)(strictly later|latest|after)"):
        make_event(
            world,
            StoreView(day=DAY, latest_event_ts="1792456200.000000"),
            "U0CM01",
            "ext-sunrise-cm",
            datetime(2026, 10, 19, 17, 30),
            TEXT,
            None,
        )


def test_make_event_accepts_one_second_after_latest_event(world: World) -> None:
    event = make_event(
        world,
        StoreView(day=DAY, latest_event_ts="1792456200.000000"),
        "U0CM01",
        "ext-sunrise-cm",
        datetime(2026, 10, 19, 17, 30, 1),
        TEXT,
        None,
    )

    assert event.ts == "1792456201.000000"


def test_make_event_refuses_non_working_store_day(world: World) -> None:
    with pytest.raises(ValueError, match=r"(?i)working day"):
        make_event(
            world,
            StoreView(day=date(2026, 10, 17), latest_event_ts=None),
            "U0CM01",
            "ext-sunrise-cm",
            datetime(2026, 10, 17, 12),
            TEXT,
            None,
        )


def test_make_event_refuses_later_time_on_another_working_day(world: World) -> None:
    with pytest.raises(ValueError, match=r"(?i)(store day|same day|2026-10-19)"):
        make_event(
            world,
            StoreView(day=DAY, latest_event_ts=BASE_TS),
            "U0CM01",
            "ext-sunrise-cm",
            datetime(2026, 10, 20, 9),
            TEXT,
            None,
        )


@pytest.mark.parametrize(
    ("persona", "channel", "reason"),
    [
        ("UNKNOWN", "ext-sunrise-cm", r"(?i)unknown.*persona"),
        ("B0PLM01", "plm-feed", r"(?i)bot"),
        ("U0CM01", "unknown-channel", r"(?i)unknown.*channel"),
        ("U0CM01", "eng-mech", r"(?i)(not in|member|channel list)"),
    ],
)
def test_make_event_refuses_invalid_sender_or_channel(
    world: World, persona: str, channel: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        make_event(
            world,
            StoreView(day=DAY, latest_event_ts=None),
            persona,
            channel,
            datetime(2026, 10, 19, 17, 30),
            TEXT,
            None,
        )


def test_say_script_line_round_trip_uses_as_key(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))

    result = runner.invoke(app, _say_args(scratch, base, **{"--thread": BASE_TS}))

    assert result.exit_code == 0, result.output
    assert result.output == "1792456200.000000\n"
    (encoded,) = _script_log(scratch).read_text(encoding="utf-8").splitlines()
    assert json.loads(encoded) == {
        "as": "U0CM01",
        "channel": "ext-sunrise-cm",
        "at": "2026-10-19T17:30",
        "text": TEXT,
        "thread": BASE_TS,
    }
    assert ScriptLine.model_validate_json(encoded).model_dump_json() == encoded


def test_say_keeps_base_logical_contents_with_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))
    writer = sqlite3.connect(base)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO meta (key, value) VALUES ('wal_marker', 'present')")
        writer.commit()
        assert Path(f"{base}-wal").stat().st_size > 0
        before = _database_contents(base)

        result = runner.invoke(app, _say_args(scratch, base))

        assert result.exit_code == 0, result.output
        assert _database_contents(base) == before
        with Store.open(scratch) as copied:
            assert copied.get_meta("wal_marker") == "present"
            assert len(_database_contents(scratch)["events"]) == 2
    finally:
        writer.close()


def test_say_uses_read_only_base_without_changing_database_or_wal(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))
    writer = sqlite3.connect(base)
    original_mode = stat.S_IMODE(base.stat().st_mode)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO meta (key, value) VALUES ('wal_marker', 'present')")
        writer.commit()
        wal = Path(f"{base}-wal")
        assert wal.stat().st_size > 0
        base_before = base.read_bytes()
        wal_before = wal.read_bytes()
        os.chmod(base, stat.S_IREAD)

        try:
            result = runner.invoke(app, _say_args(scratch, base))
        finally:
            os.chmod(base, original_mode)

        assert result.exit_code == 0, result.output
        assert base.read_bytes() == base_before
        assert wal.read_bytes() == wal_before
    finally:
        os.chmod(base, original_mode)
        writer.close()


@pytest.mark.parametrize("scripted", [False, True], ids=["single", "script"])
def test_say_never_opens_base_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted: bool
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    _seed_store(base, events=(_event(),))
    _write_script(
        source,
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": TEXT,
            }
        ),
    )
    original_open = Store.open

    def refuse_base_open(path: Path) -> Store:
        if path == base:
            raise AssertionError("base store opened writable")
        return original_open(path)

    monkeypatch.setattr(Store, "open", refuse_base_open)
    args = (
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)]
        if scripted
        else _say_args(scratch, base)
    )

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert result.output == "1792456200.000000\n"


def test_diff_same_store_copy_reports_no_change_and_stable_json(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    copy = tmp_path / "copy.sqlite"
    _seed_store(base)
    with Store.open(base) as store, store.copy_to(copy):
        pass

    text_result = runner.invoke(
        app, ["diff", "--as-of", str(DAY), "--db", str(copy), "--base-db", str(base)]
    )
    json_args = [
        "diff",
        "--as-of",
        str(DAY),
        "--db",
        str(copy),
        "--base-db",
        str(base),
        "--json",
    ]
    first_json = runner.invoke(app, json_args)
    second_json = runner.invoke(app, json_args)

    assert text_result.exit_code == 0, text_result.output
    assert text_result.output == "no change\n"
    assert first_json.exit_code == second_json.exit_code == 0
    assert first_json.stdout_bytes == second_json.stdout_bytes
    assert isinstance(json.loads(first_json.output), list)


@pytest.mark.parametrize("same_store", [True, False], ids=["same-store", "two-stores"])
def test_diff_never_opens_either_store_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_store: bool
) -> None:
    base = tmp_path / "base.sqlite"
    changed = base if same_store else tmp_path / "changed.sqlite"
    _seed_store(base)
    if not same_store:
        with Store.open(base) as store, store.copy_to(changed):
            pass

    def refuse_writable_open(path: Path) -> Store:
        raise AssertionError(f"store opened writable: {path}")

    monkeypatch.setattr(Store, "open", refuse_writable_open)

    result = runner.invoke(
        app,
        ["diff", "--as-of", str(DAY), "--db", str(changed), "--base-db", str(base)],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "no change\n"


@pytest.mark.parametrize("bad_side", ["base", "changed"])
@pytest.mark.parametrize(
    ("contents", "reason"),
    [(b"", r"(?i)(schema|store)"), (b"not a database", r"(?i)(database|sqlite)")],
    ids=["empty", "non-database"],
)
def test_diff_refuses_non_store_without_changing_it(
    tmp_path: Path, bad_side: str, contents: bytes, reason: str
) -> None:
    base = tmp_path / "before.data"
    changed = tmp_path / "after.data"
    bad = base if bad_side == "base" else changed
    good = changed if bad_side == "base" else base
    _seed_store(good)
    bad.write_bytes(contents)

    result = runner.invoke(
        app,
        ["diff", "--as-of", str(DAY), "--db", str(changed), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert re.search(reason, result.output), result.output
    assert bad.read_bytes() == contents


def test_diff_reports_changed_reader_then_readers_with_no_change(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    changed = tmp_path / "changed.sqlite"
    _seed_diff_pair(base, changed)

    result = runner.invoke(
        app,
        ["diff", "--as-of", str(DAY), "--db", str(changed), "--base-db", str(base)],
    )

    assert result.exit_code == 0, result.output
    assert "U0EM01" in result.output
    assert "added" in result.output.lower()
    assert "Approve the gate move" in result.output
    assert "must see: an open ask addressed to you" in result.output
    unchanged = ("U0CM01", "U0EE01", "U0FW01", "U0ME01", "U0PM01", "U0SC01")
    no_change_line = next(
        line for line in result.output.splitlines() if all(user_id in line for user_id in unchanged)
    )
    assert result.output.index(no_change_line) > result.output.index("Approve the gate move")


def test_diff_json_sorts_readers_and_keeps_empty_changes(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    changed = tmp_path / "changed.sqlite"
    _seed_diff_pair(base, changed)

    result = runner.invoke(
        app,
        [
            "diff",
            "--as-of",
            str(DAY),
            "--db",
            str(changed),
            "--base-db",
            str(base),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["user_id"] for row in rows] == [
        "U0CM01",
        "U0EE01",
        "U0EM01",
        "U0FW01",
        "U0ME01",
        "U0PM01",
        "U0SC01",
    ]
    for row in rows:
        if row["user_id"] == "U0EM01":
            assert [line["title"] for line in row["added"]] == ["Approve the gate move"]
            assert row["removed"] == row["changed"] == []
        else:
            assert row["added"] == row["removed"] == row["changed"] == []


def test_diff_user_limits_json_to_one_reader(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    changed = tmp_path / "changed.sqlite"
    _seed_diff_pair(base, changed)

    result = runner.invoke(
        app,
        [
            "diff",
            "--as-of",
            str(DAY),
            "--db",
            str(changed),
            "--base-db",
            str(base),
            "--user",
            "U0EM01",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [row["user_id"] for row in json.loads(result.output)] == ["U0EM01"]


@pytest.mark.parametrize(
    ("user_id", "refused_store", "reason"),
    [
        ("U-NOT-THERE", False, r"(?i)(unknown|no such).*U-NOT-THERE"),
        ("U0EM01", True, r"(?i)(world|until|replay)"),
    ],
    ids=["unknown-user", "refused-store"],
)
def test_diff_refusals_exit_one_with_reason(
    tmp_path: Path, user_id: str, refused_store: bool, reason: str
) -> None:
    base = tmp_path / "base.sqlite"
    changed = tmp_path / "changed.sqlite"
    _seed_store(base)
    if refused_store:
        with Store.open(changed):
            pass
    else:
        _seed_store(changed)

    result = runner.invoke(
        app,
        [
            "diff",
            "--as-of",
            str(DAY),
            "--db",
            str(changed),
            "--base-db",
            str(base),
            "--user",
            user_id,
        ],
    )

    assert result.exit_code == 1, result.output
    assert re.search(reason, result.output), result.output


def test_diff_priors_validation_refusal_names_priors_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    changed = tmp_path / "changed.sqlite"
    priors = tmp_path / "bad-priors.yaml"
    _seed_store(base)
    _seed_store(changed)
    priors.write_text("triage: {}\n", encoding="utf-8")
    monkeypatch.setattr("digest.cli.DEFAULT_PRIORS_PATH", priors)

    result = runner.invoke(
        app,
        ["diff", "--as-of", str(DAY), "--db", str(changed), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert str(priors) in result.output
    assert len(result.output.splitlines()) == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"--as": "UNKNOWN"}, "unknown"),
        ({"--as": "B0PLM01", "--channel": "plm-feed"}, "bot"),
        ({"--channel": "unknown-channel"}, "channel"),
        ({"--channel": "eng-mech"}, "channel"),
        ({"--at": "2026-10-20T09:00"}, "store"),
        ({"--at": "2026-10-19T17:00"}, "later"),
        ({"--thread": "1792450000.000000"}, "thread"),
    ],
)
def test_say_refusals_leave_store_and_script_unchanged(
    tmp_path: Path, changes: dict[str, str], reason: str
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))

    _assert_refused_without_write(base, scratch, reason, **changes)


def test_say_refuses_thread_that_is_a_reply(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    reply_ts = "1792454401.000000"
    _seed_store(base, events=(_event(), _event(reply_ts, thread_ts=BASE_TS)))

    _assert_refused_without_write(base, scratch, "top-level", **{"--thread": reply_ts})


def test_say_refuses_thread_from_another_channel_without_write(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    other_channel_root = _event(
        channel_id="C0KPROG",
        channel_name="kestrel-program",
    )
    _seed_store(base, events=(other_channel_root,))

    _assert_refused_without_write(base, scratch, "channel", **{"--thread": BASE_TS})


@pytest.mark.parametrize(
    ("changes", "reason", "cross_channel_root"),
    [
        ({"--as": "UNKNOWN"}, "unknown", False),
        ({"--at": "2026-10-20T09:00"}, "store", False),
        ({"--thread": BASE_TS}, "channel", True),
    ],
    ids=["s1", "s2", "s3"],
)
def test_say_refusals_leave_existing_scratch_and_script_unchanged(
    tmp_path: Path,
    changes: dict[str, str],
    reason: str,
    cross_channel_root: bool,
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    scratch_events = [_event("1792454401.000000")]
    if cross_channel_root:
        scratch_events.append(
            _event(
                channel_id="C0KPROG",
                channel_name="kestrel-program",
            )
        )
    _seed_store(base, events=(_event(),))
    _seed_store(scratch, events=tuple(scratch_events))
    with Store.open(scratch) as store:
        store.set_meta("scratch_marker", "keep this distinct store")
    log = _script_log(scratch)
    log.write_text("keep this existing script\n", encoding="utf-8")
    base_before = _database_contents(base)
    scratch_before = _database_contents(scratch)
    log_before = log.read_bytes()

    result = runner.invoke(app, _say_args(scratch, base, **changes))

    assert result.exit_code == 1
    assert reason.lower() in result.output.lower()
    assert _database_contents(base) == base_before
    assert _database_contents(scratch) == scratch_before
    assert log.read_bytes() == log_before


def test_say_refuses_non_working_store_day_without_write(tmp_path: Path) -> None:
    base = tmp_path / "weekend.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, until="2026-10-18T06:59:59.999999+00:00")

    _assert_refused_without_write(base, scratch, "working day", **{"--at": "2026-10-17T12:00"})


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (b"", r"(?i)(schema|store)"),
        (b"this is not sqlite", r"(?i)(database|sqlite)"),
    ],
    ids=["empty", "text"],
)
def test_say_refuses_base_that_is_not_a_store_without_changing_it(
    tmp_path: Path, contents: bytes, reason: str
) -> None:
    base = tmp_path / "artifact.data"
    scratch = tmp_path / "scratch.sqlite"
    base.write_bytes(contents)

    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 1
    assert re.search(reason, result.output), result.output
    assert base.read_bytes() == contents
    assert not any(Path(f"{scratch}{suffix}").exists() for suffix in ("", "-wal", "-shm"))


def test_say_refuses_each_path_to_same_file_without_write(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    _seed_store(base)
    before = _database_contents(base)
    parent_variant_dir = tmp_path / "dir"
    parent_variant_dir.mkdir()
    parent_variant = parent_variant_dir / ".." / base.name
    hard_link = tmp_path / "base-hard-link.sqlite"
    os.link(base, hard_link)

    for candidate in (base, parent_variant, hard_link):
        log = _script_log(candidate)
        log.write_text("keep this log\n", encoding="utf-8")

        result = runner.invoke(app, _say_args(candidate, base))

        assert result.exit_code == 1
        assert "same" in result.output.lower()
        assert _database_contents(base) == before
        assert log.read_text(encoding="utf-8") == "keep this log\n"


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        (None, "2026-10-18"),
        ("2026-10-19", "2026-10-18"),
        ("2026-10-17", "2026-10-17"),
    ],
    ids=["missing", "store-day", "earlier"],
)
def test_say_sets_extract_until_to_previous_day_without_moving_it_forward(
    tmp_path: Path, existing: str | None, expected: str
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))
    if existing is not None:
        with Store.open(base) as store:
            store.set_meta("extract_until", existing)

    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 0, result.output
    with Store.open(scratch) as store:
        assert store.get_meta("extract_until") == expected


def test_say_appends_two_events_then_refuses_backdating_without_write(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))

    first = runner.invoke(
        app,
        _say_args(scratch, base, **{"--at": "2026-10-19T17:30", "--text": "First line"}),
    )
    second = runner.invoke(
        app,
        _say_args(scratch, base, **{"--at": "2026-10-19T17:40", "--text": "Second line"}),
    )
    assert first.exit_code == second.exit_code == 0, (first.output, second.output)
    before_refusal = _database_contents(scratch)
    log = _script_log(scratch)
    log_before_refusal = log.read_bytes()
    refused = runner.invoke(
        app,
        _say_args(scratch, base, **{"--at": "2026-10-19T17:35", "--text": "Too late"}),
    )

    assert refused.exit_code == 1
    assert "later" in refused.output.lower()
    with sqlite3.connect(scratch) as connection:
        messages = connection.execute(
            "SELECT text FROM events WHERE text IN ('First line', 'Second line', 'Too late') "
            "ORDER BY event_ts_us"
        ).fetchall()
    assert messages == [("First line",), ("Second line",)]
    assert [json.loads(line)["text"] for line in log.read_text("utf-8").splitlines()] == [
        "First line",
        "Second line",
    ]
    assert _database_contents(scratch) == before_refusal
    assert log.read_bytes() == log_before_refusal


def test_say_refuses_existing_script_log_before_creating_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    log = _script_log(scratch)
    _seed_store(base, events=(_event(),))
    log.write_bytes(b"keep this session\n")
    base_before = base.read_bytes()

    def fail_copy(store: Store, target: Path) -> Store:
        raise AssertionError("copy must not run")

    monkeypatch.setattr(Store, "copy_to", fail_copy)
    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert result.output.splitlines() == [
        f"script log {log} already exists; remove it or give another --db"
    ]
    assert log.read_bytes() == b"keep this session\n"
    assert base.read_bytes() == base_before
    assert not any(Path(f"{scratch}{suffix}").exists() for suffix in ("", "-wal", "-shm"))


def test_say_refused_copy_keeps_target_that_appeared_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    other_store = {
        scratch: b"another process's store",
        Path(f"{scratch}-wal"): b"another process's wal",
        Path(f"{scratch}-shm"): b"another process's shm",
    }
    _seed_store(base, events=(_event(),))
    original_copy_to = Store.copy_to

    def refuse_copy(store: Store, target: Path) -> Store:
        for path, contents in other_store.items():
            path.write_bytes(contents)
        return original_copy_to(store, target)

    monkeypatch.setattr(Store, "copy_to", refuse_copy)
    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert len(result.output.splitlines()) == 1
    assert "exists" in result.output
    assert {path: path.read_bytes() for path in other_store} == other_store


def test_say_script_refuses_existing_session_log_before_creating_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    log = _script_log(scratch)
    _seed_store(base, events=(_event(),))
    _write_script(
        source,
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": TEXT,
            }
        ),
    )
    log.write_bytes(b"keep this session\n")
    base_before = base.read_bytes()

    def fail_copy(store: Store, target: Path) -> Store:
        raise AssertionError("copy must not run")

    monkeypatch.setattr(Store, "copy_to", fail_copy)
    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert result.output.splitlines() == [
        f"script log {log} already exists; remove it or give another --db"
    ]
    assert log.read_bytes() == b"keep this session\n"
    assert base.read_bytes() == base_before
    assert not any(path.exists() for path in _store_files(scratch))


def test_say_script_runs_lines_in_order_without_writing_session_log(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    _seed_store(base, events=(_event(),))
    lines = (
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": "First scripted line",
            }
        ),
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:40",
                "text": "Second scripted line",
            }
        ),
    )
    source.write_text(
        "".join(f"{line.model_dump_json()}\n" for line in lines),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "1792456200.000000\n1792456800.000000\n"
    with sqlite3.connect(scratch) as connection:
        messages = connection.execute(
            "SELECT text FROM events WHERE text LIKE '%scripted line' ORDER BY event_ts_us"
        ).fetchall()
    assert messages == [("First scripted line",), ("Second scripted line",)]
    assert not _script_log(scratch).exists()


def test_say_script_removes_scratch_and_prints_no_ts_when_later_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    _seed_store(base, events=(_event(),))
    lines = [
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": f"2026-10-19T17:{minute}",
                "text": f"Line at {minute}",
            }
        )
        for minute in ("30", "40")
    ]
    source.write_text(
        "".join(f"{line.model_dump_json()}\n" for line in lines),
        encoding="utf-8",
    )
    original_upsert = Store.upsert_events
    call_count = 0

    def fail_second_upsert(store: Store, *args: Any, **kwargs: Any) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise sqlite3.OperationalError("forced second-line failure")
        return original_upsert(store, *args, **kwargs)

    monkeypatch.setattr(Store, "upsert_events", fail_second_upsert)

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert not any(Path(f"{scratch}{suffix}").exists() for suffix in ("", "-wal", "-shm"))
    assert re.search(r"(?m)^\d{10}\.\d{6}$", result.output) is None
    assert result.output.splitlines() == [f"{source}:2: forced second-line failure"]


def test_say_script_rule_refusal_names_second_line_and_removes_scratch(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    _seed_store(base, events=(_event(),))
    _write_script(
        source,
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:40",
                "text": "First line",
            }
        ),
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": "Second line is earlier",
            }
        ),
    )

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert len(result.output.splitlines()) == 1
    assert result.output.startswith(f"{source}:2:")
    assert re.search(r"(?m)^\d{10}\.\d{6}$", result.output) is None
    assert not any(path.exists() for path in _store_files(scratch))


@pytest.mark.parametrize(
    "raw",
    [
        "{not json}\n",
        json.dumps(
            {
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
            }
        )
        + "\n",
    ],
    ids=["invalid-json", "bad-field"],
)
def test_say_script_malformed_line_has_one_line_path_prefix(tmp_path: Path, raw: str) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    _seed_store(base, events=(_event(),))
    source.write_text(raw, encoding="utf-8")

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert len(result.output.splitlines()) == 1
    assert result.output.startswith(f"{source}:1:")
    assert not any(path.exists() for path in _store_files(scratch))


@pytest.mark.parametrize(
    ("contents", "reason"),
    [(b"", r"(?i)(schema|store)"), (b"not a database", r"(?i)(database|sqlite)")],
    ids=["empty", "non-database"],
)
def test_say_script_refuses_non_store_base_without_changing_it(
    tmp_path: Path, contents: bytes, reason: str
) -> None:
    base = tmp_path / "artifact.data"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    base.write_bytes(contents)
    _write_script(
        source,
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": TEXT,
            }
        ),
    )

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert re.search(reason, result.output), result.output
    assert base.read_bytes() == contents
    assert not any(path.exists() for path in _store_files(scratch))


def test_say_script_keeps_u2028_in_event_text(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    text = "First paragraph\u2028second paragraph"
    _seed_store(base, events=(_event(),))
    source.write_text(
        json.dumps(
            {
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": text,
                "thread": None,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 0, result.output
    with sqlite3.connect(scratch) as connection:
        stored = connection.execute("SELECT text FROM events WHERE text = ?", (text,)).fetchone()
    assert stored == (text,)


def test_say_bad_thread_format_has_one_line_refusal(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))

    result = runner.invoke(app, _say_args(scratch, base, **{"--thread": "not-a-ts"}))

    assert result.exit_code == 1
    assert "thread" in result.output.lower()
    assert len(result.output.splitlines()) == 1


def test_say_rule_refusal_folds_newline_to_one_output_line(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))

    result = runner.invoke(app, _say_args(scratch, base, **{"--as": "UNKNOWN\nPERSONA"}))

    assert result.exit_code == 1
    assert len(result.output.splitlines()) == 1
    assert "UNKNOWN PERSONA" in result.output


def test_say_removes_fresh_scratch_when_event_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, events=(_event(),))

    def fail_upsert(store: Store, *args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("forced event write failure")

    monkeypatch.setattr(Store, "upsert_events", fail_upsert)

    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert not any(path.exists() for path in (*_store_files(scratch), _script_log(scratch)))
    lines = result.output.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{scratch}: ")
    assert "forced event write failure" in lines[0]


def test_say_keeps_existing_scratch_when_event_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    scratch_only = _event("1792454401.000000").model_copy(update={"text": "Scratch-only message"})
    _seed_store(base, events=(_event(),))
    _seed_store(scratch, events=(_event(), scratch_only))

    def fail_upsert(store: Store, *args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("forced event write failure")

    monkeypatch.setattr(Store, "upsert_events", fail_upsert)
    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert scratch.exists()
    with sqlite3.connect(scratch) as connection:
        stored = connection.execute("SELECT text FROM events ORDER BY event_ts_us").fetchall()
    assert stored == [("Existing message",), ("Scratch-only message",)]
    lines = result.output.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{scratch}: ")
    assert "forced event write failure" in lines[0]


def test_say_keeps_fresh_scratch_and_event_when_script_log_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    log = _script_log(scratch)
    _seed_store(base, events=(_event(),))
    real_open = Path.open

    def fail_log_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == log:
            raise OSError("forced script log append failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log_open)

    result = runner.invoke(app, _say_args(scratch, base))

    assert result.exit_code == 1
    assert isinstance(result.exception, OSError)
    assert str(result.exception) == "forced script log append failure"
    assert scratch.is_file()
    with sqlite3.connect(scratch) as connection:
        stored = connection.execute("SELECT text FROM events WHERE text = ?", (TEXT,)).fetchall()
    assert stored == [(TEXT,)]
    assert not log.exists()


def test_say_script_refuses_existing_db_without_write(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    source = tmp_path / "scenario.jsonl"
    _seed_store(base)
    _seed_store(scratch)
    source.write_text(
        ScriptLine(
            **{
                "as": "U0CM01",
                "channel": "ext-sunrise-cm",
                "at": "2026-10-19T17:30",
                "text": TEXT,
            }
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    log = _script_log(scratch)
    log.write_text("keep this log\n", encoding="utf-8")
    before = _database_contents(scratch)
    source_before = source.read_bytes()

    result = runner.invoke(
        app,
        ["say", "--script", str(source), "--db", str(scratch), "--base-db", str(base)],
    )

    assert result.exit_code == 1
    assert "exist" in result.output.lower()
    assert _database_contents(scratch) == before
    assert source.read_bytes() == source_before
    assert log.read_text(encoding="utf-8") == "keep this log\n"


def _find_script(text: str) -> Path:
    matches = [path for path in SCRIPTS_DIR.glob("*.jsonl") if text in path.read_text("utf-8")]
    assert len(matches) == 1, f"expected one script containing {text!r}, found {matches}"
    return matches[0]


def _extracted_day_six_base(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_MODE", "replay")
    with Store.open(path) as store:
        replay_world(store, WORLD_DIR, DAY)
    result = runner.invoke(app, ["extract", "--db", str(path)])
    assert result.exit_code == 0, result.output


def _run_script(base: Path, scratch: Path, script: Path) -> None:
    said = runner.invoke(
        app, ["say", "--script", str(script), "--db", str(scratch), "--base-db", str(base)]
    )
    assert said.exit_code == 0, said.output
    extracted = runner.invoke(app, ["extract", "--db", str(scratch)])
    assert extracted.exit_code == 0, extracted.output


def _json_diff(base: Path, scratch: Path) -> bytes:
    result = runner.invoke(
        app,
        [
            "diff",
            "--as-of",
            str(DAY),
            "--db",
            str(scratch),
            "--base-db",
            str(base),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return result.stdout_bytes


def _scenario_item(path: Path, entity: str, quote: str) -> Item:
    as_of = datetime.combine(DAY, time.max, ZoneInfo("America/Los_Angeles")).astimezone(UTC)
    with Store.open(path) as store:
        matches = [
            item
            for item in store.items(as_of)
            if entity in item.entities
            and any(quote in evidence.quote for evidence in item.evidence)
        ]
    assert len(matches) == 1, f"expected one extracted item for {entity}, found {matches}"
    return matches[0]


def test_d2_bracket_delay_reaches_only_me_must_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _find_script("first articles move from 19 Oct to 2 Nov")
    base, scratch = tmp_path / "base.sqlite", tmp_path / "scratch.sqlite"
    _extracted_day_six_base(base, monkeypatch)
    _run_script(base, scratch, script)

    item = _scenario_item(scratch, "PN-11051", "first articles move from 19 Oct to 2 Nov")
    differences = json.loads(_json_diff(base, scratch))
    must_see = {
        row["user_id"]
        for row in differences
        if any(
            line["item_id"] == item.item_id and line["lane"] == "must_see" for line in row["added"]
        )
    }

    assert item.type == "blocker"
    assert must_see == {"U0ME01"}


def test_d3_private_gate_delay_is_visible_only_to_private_channel_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _find_script("G-DVT1 moves from 22 Oct to 29 Oct")
    base, scratch = tmp_path / "base.sqlite", tmp_path / "scratch.sqlite"
    _extracted_day_six_base(base, monkeypatch)
    _run_script(base, scratch, script)

    item = _scenario_item(scratch, "G-DVT1", "G-DVT1 moves from 22 Oct to 29 Oct")
    differences = json.loads(_json_diff(base, scratch))
    added = {
        row["user_id"]: {line["item_id"]: line["lane"] for line in row["added"]}
        for row in differences
    }

    assert item.type == "blocker"
    assert added["U0EM01"][item.item_id] == "must_see"
    assert all(
        item.item_id not in added.get(user_id, set())
        for user_id in ("U0ME01", "U0EE01", "U0FW01", "U0SC01", "U0CM01")
    )


def test_d6_same_script_from_fresh_copies_has_byte_identical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _find_script("first articles move from 19 Oct to 2 Nov")
    base = tmp_path / "base.sqlite"
    _extracted_day_six_base(base, monkeypatch)
    outputs = []
    for number in (1, 2):
        scratch = tmp_path / f"scratch-{number}.sqlite"
        _run_script(base, scratch, script)
        outputs.append(_json_diff(base, scratch))

    assert outputs[0] == outputs[1]

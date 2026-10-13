import json
import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.driver import ScriptLine, StoreView, make_event
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.models import Event, Item
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
    assert event.permalink.endswith("p1792456201000000?thread_ts=1792454400.000000")


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


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"--as": "UNKNOWN"}, "unknown"),
        ({"--as": "B0PLM01", "--channel": "plm-feed"}, "bot"),
        ({"--channel": "unknown-channel"}, "channel"),
        ({"--channel": "eng-mech"}, "channel"),
        ({"--at": "2026-10-18T17:30"}, "store"),
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


def test_say_refuses_non_working_store_day_without_write(tmp_path: Path) -> None:
    base = tmp_path / "weekend.sqlite"
    scratch = tmp_path / "scratch.sqlite"
    _seed_store(base, until="2026-10-18T06:59:59.999999+00:00")

    _assert_refused_without_write(base, scratch, "working day", **{"--at": "2026-10-17T12:00"})


def test_say_refuses_same_file_without_write(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    _seed_store(base)
    log = _script_log(base)
    log.write_text("keep this log\n", encoding="utf-8")
    before = _database_contents(base)

    result = runner.invoke(app, _say_args(base, base))

    assert result.exit_code == 1
    assert "same" in result.output.lower()
    assert _database_contents(base) == before
    assert log.read_text(encoding="utf-8") == "keep this log\n"


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


def _require_scripts() -> Path:
    if not SCRIPTS_DIR.is_dir():
        pytest.skip("data/world/kestrel/scripts is not committed yet")
    return SCRIPTS_DIR


def _find_script(text: str) -> Path:
    scripts = _require_scripts()
    matches = [path for path in scripts.glob("*.jsonl") if text in path.read_text("utf-8")]
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


@pytest.mark.skipif(not SCRIPTS_DIR.is_dir(), reason="Kestrel driver scripts are not committed yet")
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


@pytest.mark.skipif(not SCRIPTS_DIR.is_dir(), reason="Kestrel driver scripts are not committed yet")
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


@pytest.mark.skipif(not SCRIPTS_DIR.is_dir(), reason="Kestrel driver scripts are not committed yet")
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

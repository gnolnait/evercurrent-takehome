import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.priors import AbsencePriors, Priors, TriagePriors, load_priors
from digest.world.golden import derive_golden, read_golden, write_golden
from digest.world.load import load_world
from digest.world.manifest import Manifest, ThreadRef, load_manifest, write_manifest
from digest.world.schema import World

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _world_data() -> dict[str, Any]:
    return {
        "name": "small",
        "program": "Kestrel K2",
        "workspace_url": "https://kestrel.slack.com",
        "timezone": "America/Los_Angeles",
        "start_date": "2026-10-12",
        "days": 5,
        "project": {
            "project_id": "small",
            "name": "Small project",
            "phases": [{"phase": "DVT", "from_date": "2026-10-12"}],
            "gates": [
                {
                    "id": "G1",
                    "name": "Build gate",
                    "date": "2026-10-16",
                    "buffer_days": 4,
                    "checklist": [
                        {
                            "id": "G1.review",
                            "task": "Review the build",
                            "owner": "ME",
                            "expected_cadence_days": 1,
                        }
                    ],
                }
            ],
        },
        "channels": [
            {"id": "C1", "name": "program", "purpose": "Program work"},
            {"id": "CF", "name": "feed", "purpose": "System records"},
        ],
        "personas": [
            {"user_id": "UASK", "name": "Asker", "role": "EM", "channels": ["C1"]},
            {"user_id": "UOWNER", "name": "Owner", "role": "ME", "channels": ["C1"]},
            {
                "user_id": "BOT",
                "name": "schedule_bot",
                "kind": "bot",
                "channels": ["CF"],
                "authority": "plm_bot",
            },
        ],
        "entities": [
            {"id": "G1", "type": "gate", "description": "Build gate"},
            {"id": "P1", "type": "part", "description": "Part one"},
        ],
        "events": [
            {
                "id": "GATE",
                "day": 1,
                "channel": "CF",
                "poster": "BOT",
                "kind": "fyi",
                "scenario": "S1",
                "summary": "The build gate is scheduled.",
                "claims": [
                    {"entity": "G1", "attribute": "gate_date", "value": "2026-10-16"},
                    {
                        "entity": "P1",
                        "attribute": "rev",
                        "value": "B",
                        "authority": "owner_stated",
                    },
                    {"entity": "G1", "attribute": "status", "value": "open"},
                ],
                "affected_roles": ["EM", "ME"],
                "must_see_for": ["UOWNER"],
                "known_by": {"UASK": 1},
            },
            {
                "id": "TOUCH",
                "day": 2,
                "channel": "C1",
                "poster": "UOWNER",
                "kind": "fyi",
                "summary": "The review started.",
                "tasks": [{"task": "G1.review", "effect": "evidence"}],
            },
            {
                "id": "ASK",
                "day": 2,
                "channel": "C1",
                "poster": "UASK",
                "kind": "ask",
                "summary": "When will the review finish?",
                "ask": {"of": "UOWNER", "answered_day": None},
                "affected_roles": ["ME", "EM"],
                "must_see_for": ["UOWNER"],
                "known_by": {"UASK": 2, "UOWNER": 2},
            },
            {
                "id": "NOISE",
                "day": 3,
                "channel": "C1",
                "poster": "UOWNER",
                "kind": "distractor",
                "summary": "Lunch is ready.",
            },
        ],
    }


def _priors() -> Priors:
    return Priors(
        triage=TriagePriors(miss_cost_ratio=10),
        absence=AbsencePriors(ask_reply_cadence_days=2),
    )


def _manifest(world: World) -> Manifest:
    return Manifest(
        threads={
            event.id: ThreadRef(
                channel_id=event.channel,
                thread_ts=f"1760{index:06d}.000001",
                authored="human" if event.id == "GATE" else "render",
            )
            for index, event in enumerate(world.events, start=1)
        }
    )


def test_derive_golden_maps_non_distractor_events_and_distractors() -> None:
    world = World.model_validate(_world_data())
    manifest = _manifest(world)

    golden = derive_golden(world, manifest, _priors())

    assert [item.event_id for item in golden.items] == ["GATE", "TOUCH", "ASK"]
    gate = golden.items[0]
    assert gate.model_dump() == {
        "event_id": "GATE",
        "scenario": "S1",
        "day": 1,
        "date": world.date_of(1),
        "channel_id": "CF",
        "thread_ts": manifest.threads["GATE"].thread_ts,
        "type": "fyi",
        "summary": "The build gate is scheduled.",
        "entities": ("G1", "P1"),
        "claims": (
            {
                "entity": "G1",
                "attribute": "gate_date",
                "value": "2026-10-16",
                "authority": "plm_bot",
                "supersedes": None,
                "contradicts": (),
            },
            {
                "entity": "P1",
                "attribute": "rev",
                "value": "B",
                "authority": "owner_stated",
                "supersedes": None,
                "contradicts": (),
            },
            {
                "entity": "G1",
                "attribute": "status",
                "value": "open",
                "authority": "plm_bot",
                "supersedes": None,
                "contradicts": (),
            },
        ),
        "tasks": (),
        "owner": None,
        "affected_roles": ("EM", "ME"),
        "must_see_for": ("UOWNER",),
        "known_by": {"UASK": 1},
    }
    assert golden.items[1].entities == ()
    assert golden.items[1].claims == ()
    assert golden.items[1].tasks == world.events[1].tasks
    assert golden.items[2].owner == "UOWNER"
    assert [row.model_dump() for row in golden.distractors] == [
        {
            "event_id": "NOISE",
            "day": 3,
            "channel_id": "C1",
            "thread_ts": manifest.threads["NOISE"].thread_ts,
        }
    ]


def test_derive_golden_missing_non_distractor_manifest_entry_names_event() -> None:
    world = World.model_validate(_world_data())
    manifest = _manifest(world)
    incomplete = manifest.model_copy(
        update={
            "threads": {key: value for key, value in manifest.threads.items() if key != "TOUCH"}
        }
    )
    with pytest.raises(KeyError, match="TOUCH"):
        derive_golden(world, incomplete, _priors())


def test_derive_golden_accepts_manifest_entry_for_distractor_only() -> None:
    raw = _world_data()
    raw["events"] = [raw["events"][-1]]
    world = World.model_validate(raw)
    manifest = _manifest(world)
    golden = derive_golden(world, manifest, _priors())
    assert golden.items == ()
    assert golden.absences == ()
    assert [row.event_id for row in golden.distractors] == ["NOISE"]


def test_derive_golden_world_without_asks_has_no_ask_absences() -> None:
    raw = _world_data()
    raw["events"] = [event for event in raw["events"] if event["id"] != "ASK"]
    world = World.model_validate(raw)
    golden = derive_golden(world, _manifest(world), _priors())
    assert not any(absence.source == "ask" for absence in golden.absences)


def test_bundled_world_golden_absences_match_project_day_table_and_audiences(
    tmp_path: Path,
) -> None:
    world = load_world(ROOT / "data/world/kestrel/world.yaml")
    manifest_path = tmp_path / "manifest.json"
    write_manifest(_manifest(world), manifest_path)
    golden = derive_golden(
        world, load_manifest(manifest_path), load_priors(ROOT / "config/priors.yaml")
    )
    expected = {
        "G-DVT1.thermal": {5, 6, 7, 8, 9},
        "G-DVT1.emc": {6, 7, 8, 9},
        "G-DVT1.fai": {4, 8, 9},
        "G-DVT1.ctb": {6, 7, 8, 9},
        "G-DVT1.battery": {4, 5, 9},
        "G-DVT1.safety": {5, 6, 7, 8, 9},
        "G-DVT1.review": {7, 8},
        "G-TR1.texture": set(),
    }
    checklist = [absence for absence in golden.absences if absence.source == "checklist"]
    for task, days in expected.items():
        assert {absence.day for absence in checklist if absence.task == task} == days
        owner = world.project.task(task).owner
        assert all(absence.audience == (owner,) for absence in checklist if absence.task == task)
    expected_cites = {
        "G-DVT1.thermal": {day: "S3-THERMAL-RUN1" for day in range(5, 10)},
        "G-DVT1.emc": {day: "S6-EMC" for day in range(6, 10)},
        "G-DVT1.fai": {4: "SCHED-1", 8: "S4-FAI", 9: "S4-FAI"},
        "G-DVT1.ctb": {day: "S5-MLCC" for day in range(6, 10)},
        "G-DVT1.battery": {4: "SCHED-1", 5: "SCHED-1", 9: "S10-UN383"},
        "G-DVT1.safety": {day: "SCHED-1" for day in range(5, 10)},
        "G-DVT1.review": {7: "SCHED-1", 8: "SCHED-1"},
    }
    event_days = {event.id: event.day for event in world.events}
    assert {(row.task, row.day): row.cites for row in checklist} == {
        (task, day): cites for task, days in expected_cites.items() for day, cites in days.items()
    }
    assert all(row.days_silent == row.day - event_days[row.cites] for row in checklist)
    asks = [absence for absence in golden.absences if absence.source == "ask"]
    assert {(absence.cites, absence.day) for absence in asks} == {
        ("ASK-SAF", day) for day in range(6, 11)
    }
    assert all(absence.audience == ("U0EM01", "U0FW01") for absence in asks)
    assert all(absence.days_silent == absence.day - 3 for absence in asks)


def test_golden_jsonl_order_kind_round_trip_and_byte_stability(tmp_path: Path) -> None:
    world = World.model_validate(_world_data())
    golden = derive_golden(world, _manifest(world), _priors())
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    write_golden(golden, first)
    loaded = read_golden(first)
    write_golden(loaded, second)

    rows = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == [
        "meta",
        "item",
        "item",
        "item",
        "absence",
        "absence",
        "absence",
        "distractor",
    ]
    assert [row["event_id"] for row in rows if row["kind"] == "item"] == [
        "GATE",
        "ASK",
        "TOUCH",
    ]
    assert [row["day"] for row in rows if row["kind"] == "absence"] == [4, 5, 5]
    assert all(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"])
        for row in rows
        if row["kind"] in {"item", "absence"}
    )
    assert loaded == golden
    assert first.read_bytes() == second.read_bytes()


def _write_bundled_inputs(tmp_path: Path, *, omit: str | None = None) -> None:
    world_dir = tmp_path / "data/world/kestrel"
    world_dir.mkdir(parents=True)
    world_dir.joinpath("world.yaml").write_bytes(
        (ROOT / "data/world/kestrel/world.yaml").read_bytes()
    )
    world = load_world(world_dir / "world.yaml")
    manifest = _manifest(world)
    if omit is not None:
        manifest = manifest.model_copy(
            update={
                "threads": {key: value for key, value in manifest.threads.items() if key != omit}
            }
        )
    write_manifest(manifest, world_dir / "manifest.json")
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("priors.yaml").write_bytes((ROOT / "config/priors.yaml").read_bytes())


def test_world_golden_cli_writes_bundled_file_and_prints_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_bundled_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["world", "golden", "--world", "kestrel"])

    assert result.exit_code == 0, result.output
    golden = read_golden(tmp_path / "data/world/kestrel/golden.jsonl")
    for count, label in [
        (len(golden.items), "items"),
        (len(golden.absences), "absences"),
        (len(golden.distractors), "distractors"),
    ]:
        assert re.search(rf"(?:{count}\D+{label}|{label}\D+{count})", result.output)


def test_world_golden_cli_missing_event_exits_one_with_event_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_bundled_inputs(tmp_path, omit="SCHED-1")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["world", "golden", "--world", "kestrel"])

    assert result.exit_code == 1
    assert "SCHED-1" in result.output

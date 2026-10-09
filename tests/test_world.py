import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from digest.cli import app
from digest.settings import Settings
from digest.world.load import load_world
from digest.world.schema import World

runner = CliRunner()


def _world() -> dict[str, Any]:
    return {
        "name": "small",
        "program": "Kestrel K2",
        "workspace_url": "https://kestrel.slack.com",
        "timezone": "America/Los_Angeles",
        "start_date": "2026-10-12",
        "days": 10,
        "project": {
            "project_id": "kestrel",
            "name": "Kestrel K2",
            "phases": [
                {"phase": "EVT", "from_date": "2026-10-12"},
                {"phase": "DVT", "from_date": "2026-10-19"},
            ],
            "gates": [
                {
                    "id": "G-DVT1",
                    "name": "DVT build",
                    "date": "2026-10-22",
                    "buffer_days": 7,
                    "checklist": [
                        {
                            "id": "G-DVT1.thermal",
                            "task": "Complete thermal testing",
                            "owner": "ME",
                            "expected_cadence_days": 1,
                        },
                    ],
                },
            ],
        },
        "channels": [
            {"id": "CMAIN", "name": "kestrel-program", "purpose": "Program updates"},
            {"id": "CFEED", "name": "plm-feed", "purpose": "System records"},
        ],
        "personas": [
            {
                "user_id": "UME",
                "name": "Mechanical engineer",
                "role": "ME",
                "manager": "UEM",
                "channels": ["CMAIN"],
                "owned_entities": ["PN-31010"],
                "aliases": ["mechanical owner"],
            },
            {"user_id": "UEM", "name": "Engineering manager", "role": "EM", "channels": ["CMAIN"]},
            {
                "user_id": "BPLM",
                "name": "plm_bot",
                "kind": "bot",
                "channels": ["CFEED"],
                "authority": "plm_bot",
            },
        ],
        "entities": [
            {"id": "drive", "type": "subsystem", "description": "Drive subsystem"},
            {
                "id": "PN-31010",
                "type": "part",
                "description": "Drive part",
                "subsystem": "drive",
                "parent": "drive",
                "aliases": ["drive part"],
                "state": {"rev": "A"},
            },
        ],
        "events": [
            {
                "id": event_id,
                "day": day,
                "channel": "CMAIN",
                "poster": "UME",
                "kind": "bom_change",
                "summary": "The part revision changed.",
                "claims": [{"entity": "PN-31010", "attribute": "rev", "value": rev}],
                "tasks": [{"task": "G-DVT1.thermal", "effect": "evidence"}],
                "must_see_for": ["UEM"],
                "known_by": {"UME": day},
            }
            for event_id, day, rev in [("E1", 1, "A"), ("E2", 2, "B"), ("E3", 3, "C")]
        ]
        + [
            {
                "id": "E0",
                "day": 1,
                "channel": "CFEED",
                "poster": "BPLM",
                "kind": "fyi",
                "summary": "The feed is available.",
            },
        ],
    }


@pytest.mark.parametrize(
    ("path", "value", "offender"),
    [
        pytest.param(("events", 1, "channel"), "missing", "E2", id="event-channel"),
        pytest.param(("events", 1, "poster"), "missing", "E2", id="event-poster"),
        pytest.param(("events", 1, "must_see_for"), ["missing"], "E2", id="must-see-persona"),
        pytest.param(("events", 1, "known_by"), {"missing": 2}, "E2", id="known-by-persona"),
        pytest.param(("events", 1, "claims", 0, "entity"), "missing", "E2", id="claim-entity"),
        pytest.param(("personas", 0, "owned_entities"), ["missing"], "UME", id="owned-entity"),
        pytest.param(("entities", 1, "subsystem"), "missing", "PN-31010", id="subsystem-id"),
        pytest.param(("entities", 1, "subsystem"), "PN-31010", "PN-31010", id="subsystem-type"),
        pytest.param(("entities", 1, "parent"), "missing", "PN-31010", id="parent-entity"),
        pytest.param(("events", 1, "tasks", 0, "task"), "missing", "E2", id="checklist-task"),
        pytest.param(("personas", 0, "channels"), ["missing"], "UME", id="persona-channel"),
        pytest.param(("personas", 0, "manager"), "missing", "UME", id="manager-id"),
        pytest.param(("personas", 0, "manager"), "UME", "UME", id="self-manager"),
        pytest.param(("personas", 0, "role"), None, "UME", id="human-without-role"),
        pytest.param(("personas", 2, "role"), "ME", "BPLM", id="bot-with-role"),
        pytest.param(("events", 1, "day"), 0, "E2", id="event-day-zero"),
        pytest.param(("events", 1, "day"), 11, "E2", id="event-day-after-world"),
        pytest.param(("events", 1, "known_by"), {"UME": 0}, "E2", id="known-day-zero"),
        pytest.param(("events", 1, "known_by"), {"UME": 11}, "E2", id="known-day-after-world"),
        pytest.param(("events", 1, "channel"), "CFEED", "E2", id="poster-not-member"),
        pytest.param(("events", 1, "must_see_for"), ["BPLM"], "E2", id="must-see-bot"),
        pytest.param(("events", 1, "known_by"), {"BPLM": 2}, "E2", id="known-by-bot"),
        pytest.param(("events", 1, "id"), "E1", "E1", id="duplicate-event-id"),
        pytest.param(
            ("entities",),
            _world()["entities"] + [_world()["entities"][1]],
            "PN-31010",
            id="duplicate-entity-id",
        ),
        pytest.param(
            ("personas",),
            _world()["personas"] + [_world()["personas"][1]],
            "UEM",
            id="duplicate-persona-id",
        ),
        pytest.param(
            ("entities", 1, "aliases"),
            ["mechanical owner"],
            "PN-31010|UME",
            id="alias-across-kinds",
        ),
        pytest.param(
            ("entities", 0, "aliases"), ["drive part"], "drive|PN-31010", id="alias-across-entities"
        ),
        pytest.param(
            ("personas", 1, "aliases"), ["mechanical owner"], "UEM|UME", id="alias-across-personas"
        ),
        pytest.param(
            ("entities", 1, "aliases"),
            ["drive part", "drive part"],
            "PN-31010",
            id="alias-within-entity",
        ),
        pytest.param(
            ("personas", 0, "aliases"),
            ["mechanical owner", "mechanical owner"],
            "UME",
            id="alias-within-persona",
        ),
    ]
    + [
        pytest.param(
            ("events", 1, "claims", 0),
            {
                "entity": entity,
                "attribute": attribute,
                "value": "B",
                link: target if link == "supersedes" else [target],
            },
            "E2",
            id=f"{link}-{case}",
        )
        for link in ("supersedes", "contradicts")
        for case, target, entity, attribute in [
            ("missing-event", "missing", "PN-31010", "rev"),
            ("later-day", "E3", "PN-31010", "rev"),
            ("no-claim", "E0", "PN-31010", "rev"),
            ("different-entity", "E1", "drive", "rev"),
            ("different-attribute", "E1", "PN-31010", "qty"),
        ]
    ],
)
def test_world_rejects_broken_references(
    path: tuple[str | int, ...], value: Any, offender: str
) -> None:
    """Setup one broken reference; validate the world; assert the error names its owner."""
    raw = _world()
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = deepcopy(value)

    with pytest.raises(ValidationError, match=offender):
        World.model_validate(raw)


@pytest.mark.parametrize("link", ["supersedes", "contradicts"])
@pytest.mark.parametrize("day", [1, 2])
def test_world_accepts_claim_links_to_same_or_earlier_day(link: str, day: int) -> None:
    """Setup a claim on the same key; link it to a prior event; assert it validates."""
    raw = _world()
    raw["events"][1]["day"] = day
    raw["events"][1]["claims"][0][link] = "E1" if link == "supersedes" else ["E1"]
    world = World.model_validate(raw)
    claim = world.events[1].claims[0]
    assert getattr(claim, link) == ("E1" if link == "supersedes" else ("E1",))


def test_world_keeps_null_and_empty_fields() -> None:
    world = World.model_validate(_world())
    persona = world.personas[1]
    assert persona.owned_entities == persona.aliases == ()
    entity = world.entities[0]
    assert entity.subsystem is None
    assert entity.parent is None
    assert entity.aliases == ()
    event = world.events[3]
    assert event.claims == event.tasks == ()
    assert event.known_by == {}


@pytest.mark.parametrize("day", [1, 10])
def test_world_accepts_event_and_knowledge_day_bounds(day: int) -> None:
    """Setup a boundary day; validate the world; assert event and knowledge days survive."""
    raw = _world()
    raw["events"][1]["day"] = day
    raw["events"][1]["known_by"] = {"UME": day}
    event = World.model_validate(raw).events[1]
    assert event.day == day
    assert event.known_by == {"UME": day}


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (1, date(2026, 10, 12)),
        (5, date(2026, 10, 16)),
        (6, date(2026, 10, 19)),
        (10, date(2026, 10, 23)),
    ],
)
def test_date_of_skips_weekends(day: int, expected: date) -> None:
    assert World.model_validate(_world()).date_of(day) == expected


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("start_date",), "2026-10-13", id="start-not-monday"),
        pytest.param(
            ("project", "phases", 0, "from_date"), "2026-10-13", id="phase-starts-too-late"
        ),
        pytest.param(("project", "gates", 0, "date"), "2026-10-17", id="gate-on-weekend"),
        pytest.param(("project", "gates", 0, "date"), "2026-10-09", id="gate-before-world"),
        pytest.param(("project", "gates", 0, "date"), "2026-10-26", id="gate-after-world"),
    ],
)
def test_world_rejects_invalid_calendar(path: tuple[str | int, ...], value: str) -> None:
    """Setup an invalid calendar date; validate the world; assert a validation error."""
    raw = _world()
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        World.model_validate(raw)


@pytest.mark.parametrize("gate_date", ["2026-10-12", "2026-10-23"])
def test_world_accepts_calendar_boundaries(gate_date: str) -> None:
    """Setup an earlier phase and a boundary gate; validate the world; assert the gate date."""
    raw = _world()
    raw["project"]["phases"][0]["from_date"] = "2026-10-05"
    raw["project"]["gates"][0]["date"] = gate_date
    world = World.model_validate(raw)
    assert world.project.gates[0].date == date.fromisoformat(gate_date)


def test_load_world_reads_yaml(tmp_path: Path) -> None:
    """Setup a YAML world; load it; assert nested records and dates survive."""
    path = tmp_path / "world.yaml"
    path.write_text(yaml.safe_dump(_world()), encoding="utf-8")
    world = load_world(path)
    assert isinstance(world, World)
    assert world.name == "small"
    assert world.start_date == date(2026, 10, 12)
    assert len(world.events) == 4
    assert world.events[1].claims[0].entity == "PN-31010"
    assert world.project.gates[0].checklist[0].id == "G-DVT1.thermal"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param((), id="world"),
        pytest.param(("channels", 0), id="channel"),
        pytest.param(("personas", 0), id="persona"),
        pytest.param(("entities", 0), id="entity"),
        pytest.param(("events", 0), id="event"),
        pytest.param(("events", 0, "claims", 0), id="claim"),
        pytest.param(("events", 0, "tasks", 0), id="task-touch"),
        pytest.param(("project",), id="project"),
        pytest.param(("project", "phases", 0), id="phase-window"),
        pytest.param(("project", "gates", 0), id="gate"),
        pytest.param(("project", "gates", 0, "checklist", 0), id="checklist-task"),
    ],
)
def test_load_world_rejects_unknown_keys(tmp_path: Path, path: tuple[str | int, ...]) -> None:
    """Setup an extra key at each model level; load YAML; assert the key is rejected."""
    raw = _world()
    target: Any = raw
    for key in path:
        target = target[key]
    target["unexpected"] = True
    file = tmp_path / "world.yaml"
    file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="unexpected"):
        load_world(file)


@pytest.mark.parametrize("use_default", [False, True], ids=["option", "environment"])
@pytest.mark.parametrize("broken", [False, True], ids=["valid", "broken-reference"])
def test_world_check_reports_counts_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_default: bool, broken: bool
) -> None:
    """Setup a selected world; invoke the CLI; assert counts or the invalid file path."""
    raw = _world()
    if broken:
        raw["events"][1]["poster"] = "missing"
    path = tmp_path / "data/world/small/world.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_WORLD", "small" if use_default else "missing")
    assert Settings(_env_file=None).world == ("small" if use_default else "missing")
    args = ["world", "check"] + ([] if use_default else ["--world", "small"])

    result = runner.invoke(app, args)

    if broken:
        assert result.exit_code == 1
        assert "data/world/small/world.yaml" in result.output.replace("\\", "/")
        assert "E2" in result.output
    else:
        assert result.exit_code == 0, result.output
        for label, count in [(r"(?:CMAIN|kestrel-program)", 3), (r"(?:CFEED|plm-feed)", 1)]:
            assert re.search(rf"{label}[^\n]*\b{count}\b", result.output)
        for day, count in [(1, 2), (2, 1), (3, 1)]:
            label = rf"(?:\b[Dd]ay\s+{day}\b|\bD{day}\b|2026-10-{11 + day}|^\s*{day}\s*[:|])"
            assert re.search(rf"{label}[^\n]*\b{count}\b", result.output, re.MULTILINE)


def test_bundled_world_contains_planned_program() -> None:
    """Setup the bundled world path; load it; assert the program facts from B3.5."""
    path = Path(__file__).resolve().parents[1] / "data/world/kestrel/world.yaml"
    world = load_world(path)

    assert world.program == "Kestrel K2"
    assert (world.start_date, world.days, world.timezone) == (
        date(2026, 10, 12),
        10,
        "America/Los_Angeles",
    )
    channels = {channel.name: channel.id for channel in world.channels}
    assert len(world.channels) == 7
    assert set(channels) == {
        "kestrel-program",
        "eng-mech",
        "eng-elec-fw",
        "supply-chain",
        "dvt1-build",
        "ext-sunrise-cm",
        "plm-feed",
    }
    subsystems = {entity.id for entity in world.entities if entity.type == "subsystem"}
    assert subsystems == {"drive", "compute", "safety", "power", "chassis", "enclosure"}
    entities = {entity.id: entity for entity in world.entities}
    for entity_id in [
        "PN-11051",
        "PN-31010",
        "PN-21030",
        "PN-51010",
        "PN-61001",
        "ECO-0142",
        "DEV-0007",
        "PO-45002211",
        "G-DVT1",
        "G-TR1",
        "TST-THM-02",
        "BLD-DVT1",
    ]:
        assert entities[entity_id].subsystem in subsystems
    documents = [entity for entity in world.entities if entity.type == "document"]
    assert len(documents) == 3
    for document in documents:
        assert document.parent is not None or document.subsystem is not None
        assert {"rev", "status"} & document.state.keys()
    descriptions = [
        " ".join([document.id, document.description, document.parent or "", *document.aliases])
        for document in documents
    ]
    assert any("PN-11020" in description for description in descriptions)
    assert any(
        re.search(r"\bPRD\b|product requirements", text, re.IGNORECASE) for text in descriptions
    )
    assert any("schedule" in text.lower() for text in descriptions)

    humans = [persona for persona in world.personas if persona.kind == "human"]
    cm = [persona for persona in humans if persona.authority == "cm_stated"]
    assert len(cm) == 1
    assert channels["ext-sunrise-cm"] in cm[0].channels
    staff = [persona for persona in humans if persona.authority != "cm_stated"]
    assert len(staff) == 6
    by_role = {persona.role: persona for persona in staff}
    assert set(by_role) == {"ME", "EE", "FW", "supply_chain", "EM", "PM"}
    for persona in staff:
        assert persona.team
        assert persona.manager
    for role in ("ME", "EE", "FW"):
        assert by_role[role].manager == by_role["EM"].user_id
    assert by_role["supply_chain"].manager == by_role["PM"].user_id
    bots = [persona for persona in world.personas if persona.kind == "bot"]
    assert len(bots) == 3
    assert {bot.name for bot in bots} == {"plm_bot", "schedule_bot", "doc_control_bot"}
    for bot in bots:
        assert bot.role is None
        assert bot.authority == "plm_bot"
        assert bot.channels == (channels["plm-feed"],)
        posts = [event for event in world.events if event.poster == bot.user_id]
        assert posts
        assert {event.channel for event in posts} == {channels["plm-feed"]}

    gates = {gate.id: gate for gate in world.project.gates}
    assert set(gates) == {"G-EVT2-EXIT", "G-TR1", "G-DVT1"}
    assert {gate_id: gate.date for gate_id, gate in gates.items()} == {
        "G-EVT2-EXIT": date(2026, 10, 12),
        "G-TR1": date(2026, 10, 20),
        "G-DVT1": date(2026, 10, 22),
    }
    assert gates["G-DVT1"].buffer_days == 7
    assert gates["G-DVT1"].priority == "critical"
    touched = {touch.task for event in world.events for touch in event.tasks}
    for gate in gates.values():
        assert gate.checklist
        assert {task.id for task in gate.checklist} & touched
    assert [window.phase for window in world.project.phases] == ["EVT", "DVT"]
    scenarios = {event.scenario for event in world.events if event.scenario is not None}
    assert scenarios == {f"S{i}" for i in range(1, 16)} | {f"N{i}" for i in range(1, 6)}
    assert any(event.known_by for event in world.events)
    assert any(event.must_see_for for event in world.events)
    assert touched
    for scenario in ("S2", "S6", "N4", "S12"):
        claims = [
            claim for event in world.events if event.scenario == scenario for claim in event.claims
        ]
        assert any(claim.supersedes or claim.contradicts for claim in claims), scenario
        if scenario in ("S2", "S6"):
            assert any(
                claim.entity == "PN-31010"
                and claim.attribute == "rev"
                and (claim.supersedes or claim.contradicts)
                for claim in claims
            ), scenario
        elif scenario == "N4":
            assert any(
                entities[claim.entity].type == "harness"
                and claim.attribute == "qty"
                and (claim.supersedes or claim.contradicts)
                for claim in claims
            )
    events = {event.id: event for event in world.events}
    schedule_bot = next(bot for bot in bots if bot.name == "schedule_bot")
    assert any(
        events[target].poster == schedule_bot.user_id
        for event in world.events
        if event.scenario == "S12" and event.poster == cm[0].user_id
        for claim in event.claims
        for target in claim.contradicts
    )
    plm_bot = next(bot for bot in bots if bot.name == "plm_bot")
    assert not any(
        event.poster == plm_bot.user_id for event in world.events if event.scenario == "S1"
    )

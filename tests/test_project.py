from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from digest.project import ProjectConfig, load_project


def _project() -> dict[str, Any]:
    return {
        "project_id": "kestrel",
        "name": "Kestrel K2",
        "phases": [
            {"phase": "EVT", "from_date": "2026-10-12"},
            {"phase": "DVT", "from_date": "2026-10-19"},
        ],
        "gates": [
            {
                "id": "G-TR1",
                "name": "Tooling review",
                "date": "2026-10-20",
                "buffer_days": 0,
                "checklist": [
                    {
                        "id": "G-TR1.drawing",
                        "task": "Release the drawing",
                        "owner": "ME",
                        "expected_cadence_days": 1,
                    },
                ],
            },
            {
                "id": "G-DVT1",
                "name": "DVT build",
                "date": "2026-10-22",
                "buffer_days": 7,
                "priority": "critical",
                "checklist": [
                    {
                        "id": "G-DVT1.thermal",
                        "task": "Complete thermal testing",
                        "owner": "EE",
                        "expected_cadence_days": 2,
                        "priority": "high",
                    },
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    ("day", "phase"),
    [
        (date(2026, 10, 12), "EVT"),
        (date(2026, 10, 18), "EVT"),
        (date(2026, 10, 19), "DVT"),
        (date(2026, 11, 1), "DVT"),
    ],
)
def test_phase_on_uses_latest_window(day: date, phase: str) -> None:
    project = ProjectConfig.model_validate(_project())
    assert project.phase_on(day) == phase


def test_phase_on_rejects_day_before_first_window() -> None:
    project = ProjectConfig.model_validate(_project())
    with pytest.raises(ValueError):
        project.phase_on(date(2026, 10, 11))


@pytest.mark.parametrize(
    ("task_id", "owner", "cadence", "priority"),
    [("G-TR1.drawing", "ME", 1, "normal"), ("G-DVT1.thermal", "EE", 2, "high")],
)
def test_task_finds_task_across_gates(
    task_id: str, owner: str, cadence: int, priority: str
) -> None:
    project = ProjectConfig.model_validate(_project())
    task = project.task(task_id)
    assert (task.id, task.owner, task.expected_cadence_days, task.priority) == (
        task_id,
        owner,
        cadence,
        priority,
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("phases", 1, "from_date"), "2026-10-11", id="phases-out-of-order"),
        pytest.param(("phases", 1, "from_date"), "2026-10-12", id="duplicate-phase-date"),
        pytest.param(("gates", 0, "buffer_days"), -1, id="negative-buffer"),
        pytest.param(("gates", 0, "checklist", 0, "expected_cadence_days"), 0, id="zero-cadence"),
        pytest.param(
            ("gates", 0, "checklist", 0, "expected_cadence_days"), -1, id="negative-cadence"
        ),
        pytest.param(("gates", 1, "id"), "G-TR1", id="duplicate-gate-id"),
        pytest.param(
            ("gates", 1, "checklist", 0, "id"),
            "G-TR1.drawing",
            id="duplicate-task-across-gates",
        ),
        pytest.param(
            ("gates", 0, "checklist"),
            _project()["gates"][0]["checklist"] * 2,
            id="duplicate-task-within-gate",
        ),
    ],
)
def test_project_rejects_invalid_schedule(path: tuple[str | int, ...], value: Any) -> None:
    """Setup an invalid schedule; validate it; assert a validation error."""
    raw = _project()
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = deepcopy(value)

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(raw)


def test_gate_allows_empty_checklist() -> None:
    raw = _project()
    raw["gates"][0]["checklist"] = []
    project = ProjectConfig.model_validate(raw)
    assert project.gates[0].checklist == ()
    assert project.gates[0].buffer_days == 0


def test_load_project_reads_yaml(tmp_path: Path) -> None:
    """Setup a YAML file; load it; assert dates, phase windows and task fields."""
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(_project()), encoding="utf-8")

    project = load_project(path)

    assert isinstance(project, ProjectConfig)
    assert (project.project_id, project.name) == ("kestrel", "Kestrel K2")
    assert project.phases[1].from_date == date(2026, 10, 19)
    assert project.gates[1].date == date(2026, 10, 22)
    assert project.gates[1].priority == "critical"
    assert project.gates[1].checklist[0].id == "G-DVT1.thermal"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param((), id="project"),
        pytest.param(("phases", 0), id="phase-window"),
        pytest.param(("gates", 0), id="gate"),
        pytest.param(("gates", 0, "checklist", 0), id="checklist-task"),
    ],
)
def test_load_project_rejects_unknown_keys(tmp_path: Path, path: tuple[str | int, ...]) -> None:
    """Setup an extra key at each model level; load YAML; assert the key is rejected."""
    raw = _project()
    target: Any = raw
    for key in path:
        target = target[key]
    target["unexpected"] = True
    file = tmp_path / "project.yaml"
    file.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="unexpected"):
        load_project(file)


def test_bundled_live_project_validates() -> None:
    path = Path(__file__).resolve().parents[1] / "config/project.yaml"
    assert isinstance(load_project(path), ProjectConfig)

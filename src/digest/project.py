"""Project configuration: phase windows, gates and their checklists (SPEC section 4)."""

from __future__ import annotations

import datetime as dt
from itertools import pairwise
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, model_validator

from digest.models import Frozen, Phase, Priority, Role


class ChecklistTask(Frozen):
    id: str  # unique within the project, e.g. "G-DVT1.thermal"
    task: str
    owner: Role
    expected_cadence_days: int = Field(ge=1)  # working days between evidence
    priority: Priority = "normal"


class Gate(Frozen):
    id: str
    name: str
    date: dt.date
    buffer_days: int = Field(ge=0)
    priority: Priority = "normal"
    checklist: tuple[ChecklistTask, ...] = ()


class PhaseWindow(Frozen):
    phase: Phase
    from_date: dt.date


class ProjectConfig(Frozen):
    project_id: str
    name: str
    phases: tuple[PhaseWindow, ...] = Field(min_length=1)  # ascending from_date
    gates: tuple[Gate, ...]

    @model_validator(mode="after")
    def _check_schedule(self) -> Self:
        for earlier, later in pairwise(self.phases):
            if later.from_date <= earlier.from_date:
                raise ValueError(
                    f"phase {later.phase} starts {later.from_date}, "
                    f"not after {earlier.phase} ({earlier.from_date})"
                )
        _require_unique("gate", [gate.id for gate in self.gates])
        _require_unique(
            "checklist task", [task.id for gate in self.gates for task in gate.checklist]
        )
        return self

    def phase_on(self, day: dt.date) -> Phase:
        started = [window for window in self.phases if window.from_date <= day]
        if not started:
            raise ValueError(f"{day} is before the first phase window")
        return started[-1].phase

    def task(self, task_id: str) -> ChecklistTask:
        for gate in self.gates:
            for task in gate.checklist:
                if task.id == task_id:
                    return task
        raise KeyError(task_id)


def _require_unique(kind: str, ids: list[str]) -> None:
    seen: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            raise ValueError(f"duplicate {kind} id {item_id}")
        seen.add(item_id)


def load_project(path: Path) -> ProjectConfig:
    with path.open(encoding="utf-8") as f:
        return ProjectConfig.model_validate(yaml.safe_load(f))

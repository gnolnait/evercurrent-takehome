"""Project configuration: phase windows, gates and their checklists (SPEC section 4)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from digest.models import Frozen, Phase, Priority, Role


class ChecklistTask(Frozen):
    id: str  # unique within the project, e.g. "G-DVT1.thermal"
    task: str
    owner: Role
    expected_cadence_days: int  # working days between evidence
    priority: Priority = "normal"


class Gate(Frozen):
    id: str
    name: str
    date: dt.date
    buffer_days: int
    priority: Priority = "normal"
    checklist: tuple[ChecklistTask, ...] = ()


class PhaseWindow(Frozen):
    phase: Phase
    from_date: dt.date


class ProjectConfig(Frozen):
    project_id: str
    name: str
    phases: tuple[PhaseWindow, ...]  # ascending from_date
    gates: tuple[Gate, ...]

    def phase_on(self, day: dt.date) -> Phase:
        raise NotImplementedError

    def task(self, task_id: str) -> ChecklistTask:
        raise NotImplementedError


def load_project(path: Path) -> ProjectConfig:
    raise NotImplementedError

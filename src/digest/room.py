"""The demo room: the recorded world with edits applied, and the readers' digests.

See docs/plans/m3-ui.md. The edits change only the world that selection reads. Replay and
extraction read the recorded world, so every edit replays from the recorded cassettes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, NoReturn, Protocol, assert_never

from pydantic import Field, SkipValidation

from digest.daily.profile import _owned_set
from digest.daily.render import line_parts, select_digest
from digest.daily.select import Selection
from digest.driver import ReaderDiff, diff_selections
from digest.models import Frozen, Role
from digest.priors import Priors
from digest.project import ChecklistTask, Gate, add_working_days
from digest.store import Store
from digest.world.schema import Persona, World

_READERS = ("U0ME01", "U0SC01", "U0EM01")
_DAY = 6  # the world day of the room


class SetRole(Frozen):
    kind: Literal["set_role"]
    user_id: str
    role: Role


class SetMembership(Frozen):
    kind: Literal["set_membership"]
    user_id: str
    channel_id: str
    member: bool


class SetPrivate(Frozen):
    kind: Literal["set_private"]
    channel_id: str
    private: bool


class SetOwner(Frozen):
    kind: Literal["set_owner"]
    entity_id: str
    role: Role | None


class SetGateDate(Frozen):
    kind: Literal["set_gate_date"]
    gate_id: str
    date: dt.date


class SetTaskOwner(Frozen):
    kind: Literal["set_task_owner"]
    task_id: str
    role: Role


Edit = Annotated[
    SetRole | SetMembership | SetPrivate | SetOwner | SetGateDate | SetTaskOwner,
    Field(discriminator="kind"),
]


class RoomBase(Frozen):
    """Built once by `digest serve` from a store replayed and extracted through `day`."""

    world: World  # the recorded world
    priors: Priors
    day: dt.date
    store_path: Path
    readers: tuple[str, ...]
    recorded: tuple[Selection, ...]  # each reader's selection in the recorded world


class RoomState(Frozen):
    # The room world skips the `World` script checks: pydantic runs them again on a
    # `World` instance, so the field skips validation.
    world: SkipValidation[World]
    edits: tuple[Edit, ...]  # since start or the last reset
    selections: tuple[Selection, ...]  # same order as RoomBase.readers
    diffs: tuple[ReaderDiff, ...]  # each reader against RoomBase.recorded
    # Per reader: item ids in both selections whose LineView is equal but whose
    # `line_parts` differ from the recorded ones.
    reworded: tuple[tuple[str, ...], ...]
    # Per reader: lines the latest edit added, removed, changed or reworded.
    last_effect: tuple[int, ...]
    owners: dict[str, tuple[str, ...]]  # entity id -> sorted user ids whose owned set holds it


def apply_world_edits(world: World, edits: Sequence[Edit]) -> World:
    """The world with the edits applied in order; ValueError naming the edit on a refusal."""
    for edit in edits:
        world = _apply(world, edit)
    return world


def _apply(world: World, edit: Edit) -> World:
    """The world with one edit applied (plan R3 checks).

    The room world skips the `World` script checks, so this uses `model_copy` and never
    validates a new `World`.
    """
    match edit:
        case SetRole():
            persona = _human(world, edit, edit.user_id)
            return _with_persona(world, persona.model_copy(update={"role": edit.role}))
        case SetMembership():
            persona = _human(world, edit, edit.user_id)
            _require_channel(world, edit, edit.channel_id)
            if not edit.member:
                channels = tuple(c for c in persona.channels if c != edit.channel_id)
            elif edit.channel_id in persona.channels:
                channels = persona.channels
            else:
                channels = (*persona.channels, edit.channel_id)
            return _with_persona(world, persona.model_copy(update={"channels": channels}))
        case SetPrivate():
            _require_channel(world, edit, edit.channel_id)
            channels = tuple(
                c.model_copy(update={"private": edit.private}) if c.id == edit.channel_id else c
                for c in world.channels
            )
            return world.model_copy(update={"channels": channels})
        case SetOwner():
            if all(entity.id != edit.entity_id for entity in world.entities):
                _refuse(edit, f"{edit.entity_id} is not an entity")
            entities = tuple(
                e.model_copy(update={"owner": edit.role}) if e.id == edit.entity_id else e
                for e in world.entities
            )
            personas = tuple(
                p.model_copy(
                    update={
                        "owned_entities": tuple(
                            owned for owned in p.owned_entities if owned != edit.entity_id
                        )
                    }
                )
                for p in world.personas
            )
            return world.model_copy(update={"entities": entities, "personas": personas})
        case SetGateDate():
            if all(gate.id != edit.gate_id for gate in world.project.gates):
                _refuse(edit, f"{edit.gate_id} is not a gate")
            if edit.date.weekday() >= 5 or edit.date < world.start_date:
                _refuse(
                    edit,
                    f"gate {edit.gate_id} cannot move to {edit.date}: the date must be a "
                    f"weekday on or after {world.start_date}",
                )
            gates = tuple(
                g.model_copy(update={"date": edit.date}) if g.id == edit.gate_id else g
                for g in world.project.gates
            )
            return _with_gates(world, gates)
        case SetTaskOwner():
            if all(t.id != edit.task_id for g in world.project.gates for t in g.checklist):
                _refuse(edit, f"{edit.task_id} is not a checklist task")
            gates = tuple(
                g.model_copy(
                    update={
                        "checklist": tuple(
                            t.model_copy(update={"owner": edit.role}) if t.id == edit.task_id else t
                            for t in g.checklist
                        )
                    }
                )
                for g in world.project.gates
            )
            return _with_gates(world, gates)
        case _:
            assert_never(edit)


def _refuse(edit: Edit, reason: str) -> NoReturn:
    raise ValueError(f"edit {edit.kind} refused: {reason}")


def _human(world: World, edit: Edit, user_id: str) -> Persona:
    persona = next((p for p in world.personas if p.user_id == user_id), None)
    if persona is None:
        _refuse(edit, f"{user_id} is not a persona")
    if persona.kind != "human":
        _refuse(edit, f"{user_id} is a bot, not a person")
    return persona


def _require_channel(world: World, edit: Edit, channel_id: str) -> None:
    if all(channel.id != channel_id for channel in world.channels):
        _refuse(edit, f"{channel_id} is not a channel")


def _with_persona(world: World, persona: Persona) -> World:
    personas = tuple(persona if p.user_id == persona.user_id else p for p in world.personas)
    return world.model_copy(update={"personas": personas})


def _with_gates(world: World, gates: tuple[Gate, ...]) -> World:
    return world.model_copy(update={"project": world.project.model_copy(update={"gates": gates})})


def build_base(store: Store, world: World, priors: Priors) -> RoomBase:
    """The room base from a store already replayed and extracted through world day 6."""
    day = world.date_of(_DAY)
    return RoomBase(
        world=world,
        priors=priors,
        day=day,
        store_path=Path(store.path),
        readers=_READERS,
        recorded=_select(store, world, priors, _READERS, day),
    )


def build_state(base: RoomBase, store: Store, edits: Sequence[Edit]) -> RoomState:
    """The room state for the edits, read from `store`."""
    world = apply_world_edits(base.world, edits)
    selections = _select(store, world, base.priors, base.readers, base.day)
    diffs, reworded = _compare(base.world, base.recorded, world, selections)
    last_effect = tuple(0 for _ in base.readers)
    if edits:
        last_world = apply_world_edits(base.world, edits[:-1])
        last = _select(store, last_world, base.priors, base.readers, base.day)
        last_diffs, last_reworded = _compare(last_world, last, world, selections)
        last_effect = tuple(
            len(d.added) + len(d.removed) + len(d.changed) + len(r)
            for d, r in zip(last_diffs, last_reworded, strict=True)
        )
    owned = {
        p.user_id: set(_owned_set(world, p, p.role)) for p in world.personas if p.role is not None
    }
    return RoomState(
        world=world,
        edits=tuple(edits),
        selections=selections,
        diffs=diffs,
        reworded=reworded,
        last_effect=last_effect,
        owners={
            e.id: tuple(sorted(uid for uid, entities in owned.items() if e.id in entities))
            for e in world.entities
        },
    )


def _select(
    store: Store, world: World, priors: Priors, readers: Sequence[str], day: dt.date
) -> tuple[Selection, ...]:
    """Each reader's selection since the previous working day, the window of `digest demo`."""
    previous = add_working_days(day, -1)
    return tuple(
        select_digest(store, world, priors, uid, day, previous_digest=previous) for uid in readers
    )


def _compare(
    before_world: World,
    before: Sequence[Selection],
    after_world: World,
    after: Sequence[Selection],
) -> tuple[tuple[ReaderDiff, ...], tuple[tuple[str, ...], ...]]:
    """Per reader: the diff, and the ids whose `LineView` is equal but whose words differ."""
    before_names, before_tasks = _names(before_world), _tasks(before_world)
    after_names, after_tasks = _names(after_world), _tasks(after_world)
    diffs: list[ReaderDiff] = []
    reworded: list[tuple[str, ...]] = []
    for old, new in zip(before, after, strict=True):
        diff = diff_selections(old, new)
        changed = {change.item_id for change in diff.changed}
        old_lines = {line.item.item_id: line for line in old.lines}
        diffs.append(diff)
        reworded.append(
            tuple(
                line.item.item_id
                for line in new.lines
                if line.item.item_id in old_lines
                and line.item.item_id not in changed
                and line_parts(old_lines[line.item.item_id], before_names, before_tasks)
                != line_parts(line, after_names, after_tasks)
            )
        )
    return tuple(diffs), tuple(reworded)


def _names(world: World) -> dict[str, str]:
    return {persona.user_id: persona.name for persona in world.personas}


def _tasks(world: World) -> dict[tuple[str, str], ChecklistTask]:
    return {(gate.id, task.id): task for gate in world.project.gates for task in gate.checklist}


class RoomLike(Protocol):
    """What the web app needs from a room."""

    def apply(self, edit: Edit) -> RoomState: ...

    def reset(self) -> RoomState: ...

    def state(self) -> RoomState: ...


class Room:
    """Keeps the store path, never an open connection: each rebuild opens and closes it."""

    def __init__(self, base: RoomBase) -> None:
        """Build the first state; ValueError when `base.store_path` is not an existing file."""
        # Store.open creates a missing file, so check before the first build.
        if not base.store_path.is_file():
            raise ValueError(f"room store {base.store_path} is not an existing file")
        self._base = base
        self._state = self._build(())

    def apply(self, edit: Edit) -> RoomState:
        """Apply the edit; on a refusal raise ValueError and keep the state unchanged."""
        self._state = self._build((*self._state.edits, edit))
        return self._state

    def reset(self) -> RoomState:
        """Drop every edit."""
        self._state = self._build(())
        return self._state

    def state(self) -> RoomState:
        return self._state

    def _build(self, edits: Sequence[Edit]) -> RoomState:
        with Store.open(self._base.store_path) as store:
            return build_state(self._base, store, edits)

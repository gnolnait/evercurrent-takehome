"""The demo room: the recorded world with edits applied, and the readers' digests.

See docs/plans/m3-ui.md. The edits change only the world that selection reads. Replay and
extraction read the recorded world, so every edit replays from the recorded cassettes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field

from digest.daily.select import Selection
from digest.driver import ReaderDiff
from digest.models import Frozen, Role
from digest.priors import Priors
from digest.store import Store
from digest.world.schema import World


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
    world: World  # the room world
    edits: tuple[Edit, ...]  # since start or the last reset
    selections: tuple[Selection, ...]  # same order as RoomBase.readers
    diffs: tuple[ReaderDiff, ...]  # each reader against RoomBase.recorded
    # Per reader: item ids in both selections whose LineView is equal but whose
    # `line_parts` differ from the recorded ones.
    reworded: tuple[tuple[str, ...], ...]
    # Per reader: lines the latest edit added, removed, changed or reworded.
    last_effect: tuple[int, ...]
    owners: dict[str, tuple[str, ...]]  # entity id -> user ids whose owned set holds it


def apply_world_edits(world: World, edits: Sequence[Edit]) -> World:
    """The world with the edits applied in order; ValueError naming the edit on a refusal."""
    raise NotImplementedError


def build_base(store: Store, world: World, priors: Priors) -> RoomBase:
    """The room base from a store already replayed and extracted through world day 6."""
    raise NotImplementedError


def build_state(base: RoomBase, store: Store, edits: Sequence[Edit]) -> RoomState:
    """The room state for the edits, read from `store`."""
    raise NotImplementedError


class RoomLike(Protocol):
    """What the web app needs from a room."""

    def apply(self, edit: Edit) -> RoomState: ...

    def reset(self) -> RoomState: ...

    def state(self) -> RoomState: ...


class Room:
    """Keeps the store path, never an open connection: each rebuild opens and closes it."""

    def __init__(self, base: RoomBase) -> None:
        """Build the first state; ValueError when `base.store_path` is not an existing file."""
        raise NotImplementedError

    def apply(self, edit: Edit) -> RoomState:
        """Apply the edit; on a refusal raise ValueError and keep the state unchanged."""
        raise NotImplementedError

    def reset(self) -> RoomState:
        """Drop every edit."""
        raise NotImplementedError

    def state(self) -> RoomState:
        raise NotImplementedError

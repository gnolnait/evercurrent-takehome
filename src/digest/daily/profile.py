"""A reader's profile from the world persona, and the channel visibility rule (plan B5.1)."""

from __future__ import annotations

from digest.models import Frozen, Role
from digest.world.schema import World


class Profile(Frozen):
    user_id: str
    name: str
    role: Role
    team: str | None
    owned_entities: tuple[str, ...]
    channels: tuple[str, ...]  # channels the reader is a member of


def profile_for(world: World, user_id: str) -> Profile:
    raise NotImplementedError


def can_see(world: World, profile: Profile, channel_id: str) -> bool:
    raise NotImplementedError

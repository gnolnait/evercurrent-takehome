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
    """Build the reader's profile. Raise KeyError for an unknown id and ValueError for a bot."""
    persona = {persona.user_id: persona for persona in world.personas}[user_id]
    if persona.role is None:  # the world schema gives a role to each human and to no bot
        raise ValueError(f"persona {user_id} is a bot, not a reader")
    return Profile(
        user_id=persona.user_id,
        name=persona.name,
        role=persona.role,
        team=persona.team,
        owned_entities=persona.owned_entities,
        channels=persona.channels,
    )


def can_see(world: World, profile: Profile, channel_id: str) -> bool:
    """True for a public channel or a private channel the reader is in; false for an unknown id."""
    channel = next((channel for channel in world.channels if channel.id == channel_id), None)
    if channel is None:
        return False
    return not channel.private or channel_id in profile.channels

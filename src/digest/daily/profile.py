"""A reader's profile from the world persona, and the channel visibility rule (plan B5.1)."""

from __future__ import annotations

from digest.models import Frozen, Role
from digest.world.schema import Persona, World


class Profile(Frozen):
    user_id: str
    name: str
    role: Role
    team: str | None
    owned_entities: tuple[str, ...]  # the owned set of plan B5.1, sorted
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
        owned_entities=_owned_set(world, persona, persona.role),
        channels=persona.channels,
    )


def _owned_set(world: World, persona: Persona, role: Role) -> tuple[str, ...]:
    """The persona's entities, the entities its role owns, and their children, sorted (B5.1).

    A child is an entity whose `subsystem` or `parent` is in the set. The loop adds children
    until the set stops growing.
    """
    owned = set(persona.owned_entities)
    owned |= {entity.id for entity in world.entities if entity.owner == role}
    while True:
        children = {
            entity.id
            for entity in world.entities
            if entity.subsystem in owned or entity.parent in owned
        }
        if children <= owned:
            return tuple(sorted(owned))
        owned |= children


def can_see(world: World, profile: Profile, channel_id: str) -> bool:
    """True for a public channel or a private channel the reader is in; false for an unknown id."""
    channel = next((channel for channel in world.channels if channel.id == channel_id), None)
    if channel is None:
        return False
    return not channel.private or channel_id in profile.channels

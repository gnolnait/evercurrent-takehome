"""Render a reader's selection as plain text, and build a digest from a store (plan B6.1, B6.2)."""

from __future__ import annotations

from datetime import date

from digest.daily.select import Selection
from digest.priors import Priors
from digest.store import Store
from digest.world.schema import World


def render_digest(world: World, selection: Selection) -> str:
    raise NotImplementedError


def build_digest(store: Store, world: World, priors: Priors, user_id: str, day: date) -> str:
    raise NotImplementedError

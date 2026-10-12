"""Pure normalizers: whitespace, entity ids and people (plan B1.4)."""

from __future__ import annotations

from digest.models import Glossary


def normalize_space(text: str) -> str:
    raise NotImplementedError


def resolve_entity(text: str, glossary: Glossary) -> str | None:
    """The entity id for an id, an alias or an id with a revision suffix; the suffix is split off."""
    raise NotImplementedError


def resolve_person(text: str, glossary: Glossary) -> str | None:
    """The user id for a mention, a bare id, a persona alias or a persona name."""
    raise NotImplementedError

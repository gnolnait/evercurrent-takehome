"""Pure normalizers: whitespace, entity ids and people (plan B1.4)."""

from __future__ import annotations

import re

from digest.models import Glossary

# `<id>-C`, `<id> Rev C`, `<id> rev C` or `<id> Rev. C` (D24).
_REVISION = re.compile(r"(?P<base>.+?)(?:-| Rev\.? | rev )[A-Z]")
_MENTION = re.compile(r"<@(?P<user_id>[^>|]+)>")


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def resolve_entity(text: str, glossary: Glossary) -> str | None:
    """The entity id for an id, an alias or an id with a revision suffix; the suffix is split off."""
    exact = _entity_named(text, glossary)
    if exact is not None:
        return exact
    match = _REVISION.fullmatch(text)
    return None if match is None else _entity_named(match["base"], glossary)


def resolve_person(text: str, glossary: Glossary) -> str | None:
    """The user id for a mention, a bare id, a persona alias or a persona name."""
    mention = _MENTION.fullmatch(text)
    if mention is not None:
        text = mention["user_id"]
    key = text.casefold()
    for person in glossary.people:
        if key in {name.casefold() for name in (person.user_id, person.name, *person.aliases)}:
            return person.user_id
    return None


def _entity_named(text: str, glossary: Glossary) -> str | None:
    key = text.casefold()
    for entity in glossary.entities:
        if key in {name.casefold() for name in (entity.id, *entity.aliases)}:
            return entity.id
    return None

"""The negative-name list: names, ids and phrases the corpus must not reuse (SPEC section 6)."""

from __future__ import annotations

from pathlib import Path

from digest.models import Frozen

DEFAULT_AVOID_PATH = Path("data/world/avoid-names.yaml")


class AvoidNames(Frozen):
    """Matched case-insensitively: names as whole words, identifiers as whole tokens, phrases bounded."""

    names: tuple[str, ...]
    identifiers: tuple[str, ...]
    phrases: tuple[str, ...]


def load_avoid_names(path: Path = DEFAULT_AVOID_PATH) -> AvoidNames:
    raise NotImplementedError


def find_avoided(text: str, avoid: AvoidNames) -> list[str]:
    """The entries found in the text, in order of first appearance, no repeats."""
    raise NotImplementedError

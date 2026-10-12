"""The negative-name list: names, ids and phrases the corpus must not reuse (SPEC section 6)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from digest.models import Frozen

DEFAULT_AVOID_PATH = Path("data/world/avoid-names.yaml")
_LETTER = r"[^\W\d_]"
_ALNUM = r"[^\W_]"


class AvoidNames(Frozen):
    """Matched case-insensitively: names as whole words, identifiers as whole tokens, phrases bounded."""

    names: tuple[str, ...]
    identifiers: tuple[str, ...]
    phrases: tuple[str, ...]


def load_avoid_names(path: Path = DEFAULT_AVOID_PATH) -> AvoidNames:
    # A YAML error names the file and the line because the parser reads the open file.
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    try:
        return AvoidNames.model_validate(raw)
    except ValidationError as error:
        error.add_note(f"file: {path}")
        raise


def find_avoided(text: str, avoid: AvoidNames) -> list[str]:
    """The entries found in the text, in order of first appearance, no repeats."""
    patterns = [
        *((name, rf"(?<!{_LETTER}){re.escape(name)}(?!{_LETTER})") for name in avoid.names),
        *((ident, rf"(?<!{_ALNUM}){re.escape(ident)}(?!{_ALNUM})") for ident in avoid.identifiers),
        *((phrase, rf"\b{re.escape(phrase)}\b") for phrase in avoid.phrases),
    ]
    first: dict[str, int] = {}
    for entry, pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is not None and entry not in first:
            first[entry] = match.start()
    return sorted(first, key=first.__getitem__)

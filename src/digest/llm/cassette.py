"""Recorded responses keyed by the canonical hash of the request (SPEC section 6)."""

from __future__ import annotations

from pathlib import Path

from digest.llm import LLMRequest, LLMResponse


class CassetteMiss(LookupError):
    """Replay mode found no cassette for the request."""


def request_key(request: LLMRequest) -> str:
    raise NotImplementedError


class Cassettes:
    def __init__(self, root: Path) -> None:
        raise NotImplementedError

    def get(self, key: str) -> LLMResponse | None:
        raise NotImplementedError

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> Path:
        raise NotImplementedError

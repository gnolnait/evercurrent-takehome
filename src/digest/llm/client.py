"""The one client wrapper: cassette modes, output validation and the single retry (D15)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel

from digest.llm import Backend, LLMRequest, LLMResponse
from digest.llm.cassette import Cassettes
from digest.settings import Settings


class LLM:
    def __init__(
        self,
        backend: Backend | None,
        cassettes: Cassettes,
        mode: Literal["replay", "record", "live"],
    ) -> None:
        raise NotImplementedError

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def extract[T: BaseModel](self, request: LLMRequest, model_cls: type[T]) -> T:
        raise NotImplementedError

    def judge[E: Enum](self, request: LLMRequest, choices: type[E]) -> E:
        raise NotImplementedError


def from_settings(settings: Settings) -> LLM:
    raise NotImplementedError

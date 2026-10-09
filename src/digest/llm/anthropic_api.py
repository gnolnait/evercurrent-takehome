"""The production backend: the Anthropic API with schema-enforced output (D15)."""

from __future__ import annotations

import anthropic

from digest.llm import LLMRequest, LLMResponse, ModelIds


class AnthropicBackend:
    name = "anthropic_api"

    def __init__(self, client: anthropic.Anthropic, models: ModelIds) -> None:
        raise NotImplementedError

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

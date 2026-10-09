"""The development backend: one OpenAI-compatible gateway for every model family (D15)."""

from __future__ import annotations

import httpx2

from digest.llm import LLMRequest, LLMResponse, ModelIds


class OpenRouterBackend:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        models: ModelIds,
        client: httpx2.Client | None = None,
        timeout_s: float = 120,
    ) -> None:
        raise NotImplementedError

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

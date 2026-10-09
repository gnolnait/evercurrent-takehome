"""The development backend: one OpenAI-compatible gateway for every model family (D15)."""

from __future__ import annotations

from typing import Any

import httpx2

from digest.llm import LLMOutputError, LLMRequest, LLMResponse, ModelIds, parse_json_output

URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterBackend:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        models: ModelIds,
        client: httpx2.Client | None = None,
        timeout_s: float = 120,
    ) -> None:
        self.api_key = api_key
        self.models = models
        self.client = client if client is not None else httpx2.Client(timeout=timeout_s)

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self.models.resolve(request.model, self.name)
        # The configured models do not list `temperature` in supported_parameters, and
        # require_parameters would then exclude every endpoint, so it is not sent.
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                *(m.model_dump() for m in request.messages),
            ],
            "max_tokens": request.max_tokens,
        }
        if request.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "strict": True, "schema": request.output_schema},
            }
            # Route only to an endpoint that honours the schema.
            body["provider"] = {"require_parameters": True}
        response = self.client.post(
            URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        if not response.is_success:
            raise LLMOutputError(f"openrouter status {response.status_code}", raw=response.text)
        payload = response.json()
        choice = payload["choices"][0]
        text = choice["message"]["content"]
        if choice["finish_reason"] in ("length", "content_filter"):
            raise LLMOutputError(f"openrouter finish_reason {choice['finish_reason']}", raw=text)
        usage = payload.get("usage", {})
        return LLMResponse(
            text=text,
            data=parse_json_output(text) if request.output_schema is not None else None,
            backend=self.name,
            model=model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

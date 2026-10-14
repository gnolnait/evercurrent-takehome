"""The development backend: one OpenAI-compatible gateway for every model family (D15)."""

from __future__ import annotations

import logging
from typing import Any

import httpx2

from digest.llm import (
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    ModelIds,
    parse_json_output,
    strict_schema,
)

log = logging.getLogger(__name__)

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
        # The configured models do not list `temperature` in supported_parameters, so it
        # is not sent. With require_parameters it would also exclude every endpoint.
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
                "json_schema": {
                    "name": "out",
                    "strict": True,
                    "schema": strict_schema(request.output_schema),
                },
            }
            # Route only to an endpoint that honours the schema.
            body["provider"] = {"require_parameters": True}
        # A request error can hold the request and the key. The refusal and the log give
        # only its class, and `from None` drops the chain.
        try:
            response = self.client.post(
                URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx2.RequestError as error:
            error_class = type(error).__name__
            log.warning("llm_unreachable", extra={"backend": self.name, "error_class": error_class})
            raise LLMOutputError(f"openrouter unreachable: {error_class}", raw="") from None
        if not response.is_success:
            raise LLMOutputError(f"openrouter status {response.status_code}", raw=response.text)
        # Only this call is in the try statement. LLMOutputError is a ValueError, so a wider
        # try statement would catch the refusals above and below.
        try:
            payload = response.json()
        except ValueError:
            raise LLMOutputError("openrouter reply is not JSON", raw=response.text) from None
        # Check the shape explicitly. A gateway can send valid JSON of the wrong shape.
        # A try statement here would also catch the refusals below.
        if not _has_expected_shape(payload):
            raise LLMOutputError(
                "openrouter reply has an unexpected shape", raw=response.text
            ) from None
        choice = payload["choices"][0]
        text = choice["message"]["content"]
        if text is None:
            raise LLMOutputError(
                f"openrouter null content, finish_reason {choice['finish_reason']}", raw=""
            )
        if choice["finish_reason"] in ("length", "content_filter", "error"):
            raise LLMOutputError(f"openrouter finish_reason {choice['finish_reason']}", raw=text)
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            data=parse_json_output(text) if request.output_schema is not None else None,
            backend=self.name,
            model=model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


def _has_expected_shape(payload: object) -> bool:
    """Return True when the reply has the fields that `complete` reads."""
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    choice = choices[0]
    if not isinstance(choice, dict) or "finish_reason" not in choice:
        return False
    message = choice.get("message")
    if not isinstance(message, dict) or "content" not in message:
        return False
    # A null content is a refusal that `complete` reports with its finish_reason.
    if message["content"] is not None and not isinstance(message["content"], str):
        return False
    # A null usage counts as no usage. Any other value must be an object.
    usage = payload.get("usage")
    return usage is None or isinstance(usage, dict)

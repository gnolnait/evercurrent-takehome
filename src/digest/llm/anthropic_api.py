"""The production backend: the Anthropic API with schema-enforced output (D15)."""

from __future__ import annotations

import anthropic
from anthropic.types import TextBlock
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming

from digest.llm import LLMOutputError, LLMRequest, LLMResponse, ModelIds, parse_json_output


class AnthropicBackend:
    name = "anthropic_api"

    def __init__(self, client: anthropic.Anthropic, models: ModelIds) -> None:
        self.client = client
        self.models = models

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self.models.resolve(request.model, self.name)
        # anthropic 1.11.0 `messages.create` has no `temperature` parameter, so it is not sent.
        params: MessageCreateParamsNonStreaming = {
            "model": model,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
        }
        if request.output_schema is not None:
            params["output_config"] = {
                "format": {"type": "json_schema", "schema": request.output_schema}
            }
        message = self.client.messages.create(**params)
        text = next(block.text for block in message.content if isinstance(block, TextBlock))
        if message.stop_reason in ("max_tokens", "refusal"):
            raise LLMOutputError(f"anthropic stop_reason {message.stop_reason}", raw=text)
        return LLMResponse(
            text=text,
            data=parse_json_output(text) if request.output_schema is not None else None,
            backend=self.name,
            model=model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

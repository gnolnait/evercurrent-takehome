"""The one client wrapper: cassette modes, output validation and the single retry (D15)."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError, create_model

from digest.llm import Backend, LLMOutputError, LLMRequest, LLMResponse, load_model_ids
from digest.llm.anthropic_api import AnthropicBackend
from digest.llm.cassette import CassetteMiss, Cassettes, request_key
from digest.llm.openrouter import OpenRouterBackend
from digest.settings import Settings

log = logging.getLogger(__name__)


class LLM:
    def __init__(
        self,
        backend: Backend | None,
        cassettes: Cassettes,
        mode: Literal["replay", "record", "live"],
    ) -> None:
        if mode != "replay" and backend is None:
            raise ValueError(f"mode {mode} needs a backend")
        # Replay never calls a backend, so `backend is None` means replay from here on.
        self.backend = None if mode == "replay" else backend
        self.cassettes = cassettes
        self.mode = mode

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = request_key(request)
        response = None if self.mode == "live" else self.cassettes.get(key)
        hit = response is not None
        if response is None:
            if self.backend is None:
                last_user = next(
                    (m.content for m in reversed(request.messages) if m.role == "user"), ""
                )
                raise CassetteMiss(
                    f"no cassette for key {key}, model {request.model}: {last_user[:80]}"
                )
            response = self.backend.complete(request)
            if self.mode == "record":
                self.cassettes.put(key, request, response)
        self._log("llm_call", key, response, hit=hit)
        return response

    def extract[T: BaseModel](self, request: LLMRequest, model_cls: type[T]) -> T:
        if request.output_schema is None:
            request = request.model_copy(update={"output_schema": model_cls.model_json_schema()})
        return self._validated(request, model_cls)

    def judge[E: Enum](self, request: LLMRequest, choices: type[E]) -> E:
        schema = {
            "type": "object",
            "properties": {"choice": {"enum": [member.value for member in choices]}},
            "required": ["choice"],
            "additionalProperties": False,
        }
        request = request.model_copy(update={"output_schema": schema})
        judgement = create_model("Judgement", choice=(choices, ...))
        return self._validated(request, judgement).model_dump()["choice"]

    def _validated[T: BaseModel](self, request: LLMRequest, model_cls: type[T]) -> T:
        backend = self.backend
        try:
            response = self.complete(request)
            return model_cls.model_validate(response.data)
        except ValidationError as error:
            if backend is None:
                raise LLMOutputError("llm_output_invalid", raw=response.text) from error
        except LLMOutputError:
            if backend is None:
                raise
        # The retry bypasses the cassette read: a read would return the same invalid output.
        key = request_key(request)
        response = backend.complete(request)
        self._log("llm_retry", key, response)
        try:
            result = model_cls.model_validate(response.data)
        except ValidationError as error:
            raise LLMOutputError("llm_output_invalid_after_retry", raw=response.text) from error
        if self.mode == "record":
            self.cassettes.put(key, request, response)
        return result

    def _log(self, event: str, key: str, response: LLMResponse, **fields: object) -> None:
        log.info(
            event,
            extra={
                "key": key,
                "mode": self.mode,
                "backend": response.backend,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                **fields,
            },
        )


def from_settings(settings: Settings) -> LLM:
    cassettes = Cassettes(settings.cassettes)
    if settings.mode == "replay":
        return LLM(None, cassettes, "replay")
    if settings.mode == "live" and settings.backend != "anthropic_api":
        raise ValueError(
            "live mode needs DIGEST_BACKEND=anthropic_api; the gateway is for development"
        )
    models = load_model_ids()
    backend: Backend
    if settings.backend == "anthropic_api":
        if settings.anthropic_api_key is None:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        backend = AnthropicBackend(anthropic.Anthropic(api_key=settings.anthropic_api_key), models)
    else:
        if settings.openrouter_api_key is None:
            raise ValueError("OPENROUTER_API_KEY is not set")
        backend = OpenRouterBackend(settings.openrouter_api_key, models)
    return LLM(backend, cassettes, settings.mode)

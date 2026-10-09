"""LLM request and response types, the backend protocol and the logical model names.

Every model call in the package goes through this package, so cassettes capture all of
them (SPEC section 6). The client wrapper lives in `digest.llm.client`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from digest.models import Frozen

DEFAULT_MODEL_IDS_PATH = Path("config/models.yaml")
LogicalModel = Literal["extract", "adjudicate", "render", "judge", "classify"]


class Message(Frozen):
    role: Literal["user", "assistant"]
    content: str


class LLMRequest(Frozen):
    model: LogicalModel
    system: str
    messages: tuple[Message, ...]
    output_schema: dict[str, Any] | None = None
    temperature: float = 0.0
    max_tokens: int = 4096  # not part of the cassette key


class LLMResponse(Frozen):
    text: str
    data: dict[str, Any] | None  # parsed when output_schema was given
    backend: str
    model: str  # the backend's concrete model id
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMOutputError(ValueError):
    """The backend answered, but not with usable output. `raw` is the text it returned."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class Backend(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


class ModelIds(Frozen):
    """`config/models.yaml`: logical name to concrete model id, per backend."""

    extract: dict[str, str]
    adjudicate: dict[str, str]
    render: dict[str, str]
    judge: dict[str, str]
    classify: dict[str, str]

    def resolve(self, logical: LogicalModel, backend: str) -> str:
        raise NotImplementedError


def load_model_ids(path: Path = DEFAULT_MODEL_IDS_PATH) -> ModelIds:
    raise NotImplementedError

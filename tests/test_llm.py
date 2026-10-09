import hashlib
import json
import logging
import os
import socket
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

import anthropic
import httpx2
import pytest
from anthropic.types import Message as SDKMessage
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from digest.llm import (
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    LogicalModel,
    Message,
    ModelIds,
    load_model_ids,
)
from digest.llm.anthropic_api import AnthropicBackend
from digest.llm.cassette import CassetteMiss, Cassettes, request_key
from digest.llm.client import LLM, from_settings
from digest.llm.openrouter import OpenRouterBackend
from digest.settings import Settings

Mode = Literal["replay", "record", "live"]
Operation = Literal["extract", "judge"]
MODELS: tuple[LogicalModel, ...] = ("extract", "adjudicate", "render", "judge", "classify")


class FakeBackend:
    name = "fake"

    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return next(self.responses)


class FakeSDKClient:
    def __init__(self, response: SDKMessage) -> None:
        self.messages = self
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SDKMessage:
        self.calls.append(kwargs)
        return self.response


class Item(BaseModel):
    count: int


class Choice(Enum):
    KEEP = "keep"
    DROP = "drop"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_network(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A unit test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
def llm_request() -> LLMRequest:
    return LLMRequest(
        model="extract",
        system="Private system instruction: résumé",
        messages=(Message(role="user", content="Private user prompt: café"),),
        temperature=0.25,
        max_tokens=123,
    )


@pytest.fixture
def response() -> LLMResponse:
    return LLMResponse(
        text='{"count": 2}',
        data={"count": 2},
        backend="fake",
        model="fake-extractor",
        input_tokens=17,
        output_tokens=5,
    )


@pytest.fixture
def models() -> ModelIds:
    return ModelIds.model_validate(
        {name: {"anthropic_api": f"api-{name}", "openrouter": f"gateway/{name}"} for name in MODELS}
    )


@pytest.fixture
def models_path(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "models.yaml"
    path.parent.mkdir()
    path.write_text(
        "\n".join(
            f"{name}:\n  anthropic_api: api-{name}\n  openrouter: gateway/{name}" for name in MODELS
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("schema", [None, {"type": "object", "description": "café"}])
def test_request_key_matches_canonical_sha256(
    llm_request: LLMRequest, schema: dict[str, Any] | None
) -> None:
    request = llm_request.model_copy(update={"output_schema": schema})
    canonical = json.dumps(
        request.model_dump(exclude={"max_tokens"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert request_key(request) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        {"system": "Private system instruction: résumés"},
        {"model": "judge"},
        {"messages": (Message(role="user", content="A different prompt"),)},
        {"output_schema": {"type": "object"}},
        {"temperature": 0.5},
    ],
)
def test_request_key_changes_with_key_fields(
    llm_request: LLMRequest, change: dict[str, Any]
) -> None:
    assert request_key(llm_request) != request_key(llm_request.model_copy(update=change))


@settings(max_examples=25, database=None)
@given(
    properties=st.dictionaries(
        st.text(alphabet="abcé", min_size=1, max_size=5),
        st.sampled_from(["string", "integer", "boolean"]),
        min_size=2,
        max_size=5,
    ),
    max_tokens=st.integers(min_value=1, max_value=8192),
)
def test_request_key_ignores_schema_order_and_max_tokens(
    properties: dict[str, str], max_tokens: int
) -> None:
    """Setup reordered schemas / Action hash both requests / Assert keys stay equal."""
    schema = {
        "type": "object",
        "properties": {
            key: {"type": value, "description": "é"} for key, value in properties.items()
        },
    }
    reordered = {
        "properties": {
            key: {"description": "é", "type": value} for key, value in reversed(properties.items())
        },
        "type": "object",
    }
    request = LLMRequest(
        model="extract", system="é", messages=(), output_schema=schema, max_tokens=max_tokens
    )
    changed = request.model_copy(update={"output_schema": reordered, "max_tokens": max_tokens + 1})
    assert request_key(request) == request_key(changed)


def test_cassette_file_round_trip_and_atomic_overwrite(
    tmp_path: Path,
    llm_request: LLMRequest,
    response: LLMResponse,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup a cassette / Action overwrite through replace / Assert full files and old data."""
    root = tmp_path / "nested" / "cassettes"
    cassettes = Cassettes(root)
    key = request_key(llm_request)
    target = root / f"{key}.json"
    assert cassettes.get(key) is None
    replace = os.replace
    replacements: list[dict[str, Any]] = []
    old_bytes: bytes | None = None

    def observe_replace(src: Any, dst: Any) -> None:
        source, destination = Path(src), Path(dst)
        assert source.parent.resolve() == root.resolve()
        assert source != destination
        assert destination == target
        assert (target.read_bytes() if target.exists() else None) == old_bytes
        replacements.append(json.loads(source.read_text(encoding="utf-8")))
        replace(src, dst)

    monkeypatch.setattr(os, "replace", observe_replace)
    with caplog.at_level(logging.INFO):
        assert cassettes.put(key, llm_request, response) == target
        assert Cassettes(root).get(key) == response
        old_bytes = target.read_bytes()
        updated = response.model_copy(update={"text": "replacement", "data": None})
        assert cassettes.put(key, llm_request, updated) == target

    assert len(replacements) == 2
    for payload, expected in zip(replacements, (response, updated), strict=True):
        assert set(payload) == {"key", "request", "response", "recorded_at"}
        assert payload["key"] == key
        assert payload["request"] == llm_request.model_dump(mode="json")
        assert payload["response"] == expected.model_dump(mode="json")
        assert isinstance(payload["recorded_at"], str)
        datetime.fromisoformat(payload["recorded_at"].replace("Z", "+00:00"))
    assert Cassettes(root).get(key) == updated
    assert json.loads(target.read_text(encoding="utf-8")) == replacements[-1]
    assert list(root.iterdir()) == [target]
    assert any(
        Path(str(value)) == target for record in caplog.records for value in vars(record).values()
    )


@pytest.mark.parametrize(
    ("mode", "cached", "backend_present"),
    [
        ("replay", True, False),
        ("replay", True, True),
        ("record", True, True),
        ("record", False, True),
        ("live", True, True),
        ("live", False, True),
    ],
)
def test_complete_mode_controls_backend_and_cassettes(
    tmp_path: Path,
    llm_request: LLMRequest,
    response: LLMResponse,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    cached: bool,
    backend_present: bool,
) -> None:
    """Setup a mode and cache / Action complete / Assert calls, response, and disk state."""
    cassettes = Cassettes(tmp_path)
    key = request_key(llm_request)
    stored = response.model_copy(update={"text": "stored", "data": None})
    if cached:
        cassettes.put(key, llm_request, stored)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    backend = FakeBackend(response)

    def forbid_cassette(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Live mode touched a cassette")

    if mode == "live":
        monkeypatch.setattr(cassettes, "get", forbid_cassette)
        monkeypatch.setattr(cassettes, "put", forbid_cassette)
    llm = LLM(backend if backend_present else None, cassettes, mode)
    result = llm.complete(llm_request)

    calls_backend = mode == "live" or not cached
    assert result == (response if calls_backend else stored)
    assert backend.requests == ([llm_request] if calls_backend else [])
    if mode == "record" and not cached:
        assert Cassettes(tmp_path).get(key) == response
    else:
        assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("backend_present", [False, True])
def test_replay_miss_identifies_request_without_backend_call(
    tmp_path: Path, llm_request: LLMRequest, backend_present: bool
) -> None:
    """Setup a missing request / Action replay / Assert key and last user excerpt."""
    prompt = "last user " + "x" * 100 + "PRIVATE_SUFFIX"
    request = llm_request.model_copy(
        update={
            "messages": (
                Message(role="user", content="earlier user"),
                Message(role="assistant", content="earlier assistant"),
                Message(role="user", content=prompt),
                Message(role="assistant", content="final assistant"),
            )
        }
    )
    backend = FakeBackend()
    llm = LLM(backend if backend_present else None, Cassettes(tmp_path), "replay")
    with pytest.raises(CassetteMiss) as error:
        llm.complete(request)
    message = str(error.value)
    assert request_key(request) in message
    assert request.model in message
    assert prompt[:80] in message
    assert prompt[:81] not in message
    assert "PRIVATE_SUFFIX" not in message
    assert "final assistant" not in message
    assert backend.requests == []


@pytest.mark.parametrize("mode", ["record", "live"])
def test_nonreplay_requires_backend(tmp_path: Path, mode: Mode) -> None:
    with pytest.raises(ValueError):
        LLM(None, Cassettes(tmp_path), mode)


def test_complete_does_not_validate_schema(
    tmp_path: Path, llm_request: LLMRequest, response: LLMResponse
) -> None:
    request = llm_request.model_copy(update={"output_schema": Item.model_json_schema()})
    invalid = response.model_copy(update={"text": '{"count":"bad"}', "data": {"count": "bad"}})
    backend = FakeBackend(invalid)
    assert LLM(backend, Cassettes(tmp_path), "record").complete(request) == invalid
    assert backend.requests == [request]
    assert Cassettes(tmp_path).get(request_key(request)) == invalid


def validation_schema(operation: Operation) -> dict[str, Any]:
    if operation == "extract":
        return Item.model_json_schema()
    return {
        "type": "object",
        "properties": {"choice": {"enum": ["keep", "drop"]}},
        "required": ["choice"],
    }


def validate(llm: LLM, request: LLMRequest, operation: Operation) -> Item | Choice:
    if operation == "extract":
        return llm.extract(request, Item)
    return llm.judge(request, Choice)


@pytest.mark.parametrize("operation", ["extract", "judge"])
def test_validation_builds_schema(
    tmp_path: Path, llm_request: LLMRequest, response: LLMResponse, operation: Operation
) -> None:
    """Setup no output schema / Action validate / Assert schema and typed result."""
    data = {"count": 2} if operation == "extract" else {"choice": "keep"}
    backend = FakeBackend(response.model_copy(update={"text": json.dumps(data), "data": data}))
    request = llm_request.model_copy(update={"model": operation})
    result = validate(LLM(backend, Cassettes(tmp_path), "record"), request, operation)
    assert result == (Item(count=2) if operation == "extract" else Choice.KEEP)
    assert backend.requests == [
        request.model_copy(update={"output_schema": validation_schema(operation)})
    ]
    assert request.output_schema is None


def test_extract_preserves_supplied_schema(
    tmp_path: Path, llm_request: LLMRequest, response: LLMResponse
) -> None:
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
    request = llm_request.model_copy(update={"output_schema": schema})
    backend = FakeBackend(response)
    assert LLM(backend, Cassettes(tmp_path), "live").extract(request, Item) == Item(count=2)
    assert backend.requests == [request]


@pytest.mark.parametrize("operation", ["extract", "judge"])
@pytest.mark.parametrize(
    ("mode", "cached"), [("record", False), ("record", True), ("live", False), ("replay", True)]
)
@pytest.mark.parametrize("retry_valid", [True, False])
def test_validation_retries_once_and_overwrites_only_valid_output(
    tmp_path: Path,
    llm_request: LLMRequest,
    response: LLMResponse,
    operation: Operation,
    mode: Mode,
    cached: bool,
    retry_valid: bool,
) -> None:
    """Setup invalid output / Action validate / Assert retry limit and saved valid output."""
    request = llm_request.model_copy(update={"model": operation})
    sent = request.model_copy(update={"output_schema": validation_schema(operation)})
    key = request_key(sent)
    cassettes = Cassettes(tmp_path)
    bad_data = {"count": "bad"} if operation == "extract" else {"choice": "unknown"}
    good_data = {"count": 2} if operation == "extract" else {"choice": "drop"}
    first = response.model_copy(update={"text": "first invalid", "data": bad_data})
    second = response.model_copy(
        update={
            "text": "second valid" if retry_valid else "second invalid",
            "data": good_data if retry_valid else None,
        }
    )
    if cached:
        cassettes.put(key, sent, first)
    backend = FakeBackend(*([second] if cached else [first, second]))
    llm = LLM(backend, cassettes, mode)

    if mode == "replay" or not retry_valid:
        with pytest.raises(LLMOutputError) as error:
            validate(llm, request, operation)
        assert error.value.raw == (first.text if mode == "replay" else second.text)
    else:
        assert validate(llm, request, operation) == (
            Item(count=2) if operation == "extract" else Choice.DROP
        )
    expected_calls = 0 if mode == "replay" else (1 if cached else 2)
    assert backend.requests == [sent] * expected_calls
    if mode == "record" and retry_valid:
        assert Cassettes(tmp_path).get(key) == second
    if mode == "record" and not retry_valid:
        assert Cassettes(tmp_path).get(key) == first
    if mode == "replay":
        assert Cassettes(tmp_path).get(key) == first
    if mode == "live":
        assert list(tmp_path.iterdir()) == []


def sdk_message(text: str, stop_reason: str = "end_turn", model: str = "api-extract") -> SDKMessage:
    payload = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "thinking", "thinking": "internal", "signature": "test"},
            {"type": "text", "text": text},
            {"type": "text", "text": "Do not append this block"},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 17, "output_tokens": 5},
    }
    assert payload.keys() <= SDKMessage.model_fields.keys()
    return SDKMessage.model_validate(payload)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("logical", ["extract", "judge"])
def test_anthropic_maps_sdk_request_and_response(
    llm_request: LLMRequest,
    models: ModelIds,
    structured: bool,
    logical: LogicalModel,
) -> None:
    """Setup an SDK message / Action complete / Assert SDK kwargs and parsed fields."""
    schema = Item.model_json_schema() if structured else None
    request = llm_request.model_copy(update={"model": logical, "output_schema": schema})
    text = '{"count":2}' if structured else "plain text"
    model_id = f"api-{logical}"
    client = FakeSDKClient(sdk_message(text, model=model_id))
    result = AnthropicBackend(cast(anthropic.Anthropic, client), models).complete(request)
    expected: dict[str, Any] = {
        "model": model_id,
        "system": request.system,
        "messages": [message.model_dump() for message in request.messages],
        "temperature": 0.25,
        "max_tokens": 123,
    }
    if structured:
        expected["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    assert client.calls == [expected]
    assert result == LLMResponse(
        text=text,
        data={"count": 2} if structured else None,
        backend="anthropic_api",
        model=model_id,
        input_tokens=17,
        output_tokens=5,
    )


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_anthropic_rejects_incomplete_output(
    llm_request: LLMRequest, models: ModelIds, stop_reason: str
) -> None:
    client = FakeSDKClient(sdk_message("unfinished", stop_reason))
    backend = AnthropicBackend(cast(anthropic.Anthropic, client), models)
    with pytest.raises(LLMOutputError) as error:
        backend.complete(llm_request)
    assert error.value.raw == "unfinished"
    assert len(client.calls) == 1


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("with_usage", [False, True])
@pytest.mark.parametrize("logical", ["extract", "judge"])
def test_openrouter_maps_http_request_and_response(
    llm_request: LLMRequest,
    models: ModelIds,
    structured: bool,
    with_usage: bool,
    logical: LogicalModel,
) -> None:
    """Setup a gateway reply / Action complete / Assert HTTP fields and parsed response."""
    schema = Item.model_json_schema() if structured else None
    request = llm_request.model_copy(update={"model": logical, "output_schema": schema})
    text = '{"count":2}' if structured else "plain text"
    seen: list[httpx2.Request] = []

    def handler(incoming: httpx2.Request) -> httpx2.Response:
        seen.append(incoming)
        payload: dict[str, Any] = {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        }
        if with_usage:
            payload["usage"] = {"prompt_tokens": 17, "completion_tokens": 5}
        return httpx2.Response(200, json=payload)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        result = OpenRouterBackend("test-key", models, client=client).complete(request)
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer test-key"
    body = json.loads(seen[0].content)
    model_id = f"gateway/{logical}"
    assert body["model"] == model_id
    assert body["messages"] == [
        {"role": "system", "content": request.system},
        *[message.model_dump() for message in request.messages],
    ]
    assert body["temperature"] == 0.25
    assert body["max_tokens"] == 123
    if structured:
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "out", "strict": True, "schema": schema},
        }
        assert body["provider"] == {"require_parameters": True}
    else:
        assert "response_format" not in body
    assert result == LLMResponse(
        text=text,
        data={"count": 2} if structured else None,
        backend="openrouter",
        model=model_id,
        input_tokens=17 if with_usage else None,
        output_tokens=5 if with_usage else None,
    )


@pytest.mark.parametrize(
    ("status", "reason"), [(400, "stop"), (503, "stop"), (200, "length"), (200, "content_filter")]
)
def test_openrouter_rejects_http_errors_and_incomplete_output(
    llm_request: LLMRequest, models: ModelIds, status: int, reason: str
) -> None:
    calls: list[httpx2.Request] = []
    raw = "gateway error body" if status != 200 else "unfinished"

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if status != 200:
            return httpx2.Response(status, text=raw)
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": raw}, "finish_reason": reason}],
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        backend = OpenRouterBackend("test-key", models, client=client)
        with pytest.raises(LLMOutputError) as error:
            backend.complete(llm_request)
    assert error.value.raw == raw
    assert len(calls) == 1


@pytest.mark.parametrize("logical", MODELS)
@pytest.mark.parametrize("backend", ["anthropic_api", "openrouter"])
def test_load_model_ids_resolves_yaml(
    models_path: Path, logical: LogicalModel, backend: str
) -> None:
    models = load_model_ids(models_path)
    expected = f"api-{logical}" if backend == "anthropic_api" else f"gateway/{logical}"
    assert models.resolve(logical, backend) == expected


@pytest.mark.parametrize("logical", MODELS)
@pytest.mark.parametrize("backend", ["anthropic_api", "openrouter"])
def test_shipped_model_ids_resolve_default_path(
    monkeypatch: pytest.MonkeyPatch, logical: LogicalModel, backend: str
) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    assert load_model_ids().resolve(logical, backend)


@pytest.mark.parametrize(
    ("mode", "backend_name"),
    [
        ("replay", "openrouter"),
        ("replay", "anthropic_api"),
        ("record", "openrouter"),
        ("record", "anthropic_api"),
        ("live", "anthropic_api"),
    ],
)
def test_from_settings_selects_backend_and_cassette_root(
    tmp_path: Path,
    models_path: Path,
    llm_request: LLMRequest,
    response: LLMResponse,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    backend_name: Literal["openrouter", "anthropic_api"],
) -> None:
    """Setup explicit settings / Action build and complete / Assert selected backend and root."""
    monkeypatch.chdir(models_path.parent.parent)
    seen: list[str] = []
    constructor_keys: list[tuple[str, str]] = []

    def init_anthropic(
        backend: AnthropicBackend, client: anthropic.Anthropic, models: ModelIds
    ) -> None:
        assert client.api_key is not None
        constructor_keys.append((backend.name, client.api_key))

    def init_openrouter(
        backend: OpenRouterBackend,
        api_key: str,
        models: ModelIds,
        client: httpx2.Client | None = None,
        timeout_s: float = 120,
    ) -> None:
        constructor_keys.append((backend.name, api_key))

    def complete(backend: AnthropicBackend | OpenRouterBackend, request: LLMRequest) -> LLMResponse:
        seen.append(backend.name)
        assert request == llm_request
        return response

    def forbid_backend(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Replay constructed a backend")

    monkeypatch.setattr(AnthropicBackend, "complete", complete)
    monkeypatch.setattr(OpenRouterBackend, "complete", complete)
    if mode == "replay":
        monkeypatch.setattr(AnthropicBackend, "__init__", forbid_backend)
        monkeypatch.setattr(OpenRouterBackend, "__init__", forbid_backend)
    else:
        monkeypatch.setattr(AnthropicBackend, "__init__", init_anthropic)
        monkeypatch.setattr(OpenRouterBackend, "__init__", init_openrouter)
    root = tmp_path / "selected-cassettes"
    config = Settings(
        _env_file=None,
        mode=mode,
        backend=backend_name,
        world="kestrel",
        db=None,
        cassettes=root,
        ANTHROPIC_API_KEY="api-test-key" if mode != "replay" else None,
        OPENROUTER_API_KEY="gateway-test-key" if mode != "replay" else None,
    )
    if mode == "replay":
        Cassettes(root).put(request_key(llm_request), llm_request, response)
    assert from_settings(config).complete(llm_request) == response
    assert seen == ([] if mode == "replay" else [backend_name])
    expected_key = "api-test-key" if backend_name == "anthropic_api" else "gateway-test-key"
    assert constructor_keys == ([] if mode == "replay" else [(backend_name, expected_key)])
    if mode != "live":
        assert Cassettes(root).get(request_key(llm_request)) == response
    else:
        assert not list(root.glob("*.json"))


def test_from_settings_refuses_live_gateway(
    tmp_path: Path, models_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(models_path.parent.parent)
    config = Settings(
        _env_file=None,
        mode="live",
        backend="openrouter",
        world="kestrel",
        db=None,
        cassettes=tmp_path,
        ANTHROPIC_API_KEY="api-key",
        OPENROUTER_API_KEY="gateway-key",
    )
    with pytest.raises(ValueError):
        from_settings(config)


def test_blank_gateway_key_is_none_and_record_requires_key(
    tmp_path: Path, models_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup a blank dotenv key / Action load and build / Assert None and refusal."""
    monkeypatch.chdir(models_path.parent.parent)
    env = tmp_path / "blank.env"
    env.write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
    config = Settings(
        _env_file=env,
        mode="record",
        backend="openrouter",
        world="kestrel",
        db=None,
        cassettes=tmp_path / "cassettes",
        ANTHROPIC_API_KEY=None,
    )
    assert config.openrouter_api_key is None
    with pytest.raises(ValueError):
        from_settings(config)


@pytest.mark.parametrize(
    ("mode", "hit"), [("replay", True), ("record", True), ("record", False), ("live", False)]
)
def test_complete_logs_metadata_without_prompt(
    tmp_path: Path,
    llm_request: LLMRequest,
    response: LLMResponse,
    caplog: pytest.LogCaptureFixture,
    mode: Mode,
    hit: bool,
) -> None:
    """Setup a complete call / Action capture logs / Assert metadata without prompt text."""
    cassettes = Cassettes(tmp_path)
    key = request_key(llm_request)
    if hit:
        cassettes.put(key, llm_request, response)
    llm = LLM(None if mode == "replay" else FakeBackend(response), cassettes, mode)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert llm.complete(llm_request) == response
    records = [record for record in caplog.records if record.getMessage() == "llm_call"]
    assert len(records) == 1
    fields = vars(records[0])
    assert fields["key"] == key
    assert fields["mode"] == mode
    assert fields["hit"] is hit
    assert fields["backend"] == response.backend
    assert fields["model"] == response.model
    assert fields["input_tokens"] == 17
    assert fields["output_tokens"] == 5
    for secret in (llm_request.system, *(message.content for message in llm_request.messages)):
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert all(secret not in str(value) for value in vars(record).values())

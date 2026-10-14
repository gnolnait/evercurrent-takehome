"""M3 unit 11 adversarial pass: model-call failures become refusals."""

import httpx2
import pytest

from digest.llm import LLMOutputError, LLMRequest, Message, load_model_ids
from digest.llm.openrouter import OpenRouterBackend

KEY = "sk-test-SENTINEL-KEY"
CONTENT = '"message": {"content": "ok"}, "finish_reason": "stop"'


@pytest.mark.parametrize(
    "body",
    [
        "null",
        '{"choices": null}',
        '{"choices": []}',
        '{"choices": [{}]}',
        '{"choices": [{' + CONTENT + '}], "usage": 5}',
    ],
)
def test_openrouter_wrong_shape_json_reply_becomes_refusal(body: str) -> None:
    """Setup a 200 reply whose body is JSON of the wrong shape.

    Action complete a request.
    Assert an LLMOutputError, so run_extract counts a failed thread and does not crash.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=body)

    request = LLMRequest(
        model="extract",
        system="s",
        messages=(Message(role="user", content="u"),),
        max_tokens=16,
    )
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        backend = OpenRouterBackend(KEY, load_model_ids(), client=client)
        with pytest.raises(LLMOutputError) as caught:
            backend.complete(request)
    # Exactly LLMOutputError: the wrapper retries its subclass LLMOutputInvalid.
    assert type(caught.value) is LLMOutputError
    assert str(caught.value) == "openrouter reply has an unexpected shape"
    assert caught.value.raw == body

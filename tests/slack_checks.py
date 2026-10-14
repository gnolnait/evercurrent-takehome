"""Check the payload rules in plan A17."""

import re
from typing import Any

from digest.daily.select import Selection
from digest.daily.slack import slack_digest
from digest.seed import SeedMap
from digest.world.schema import World


def check_message(message: dict[str, Any], links: SeedMap | None, *, top: bool) -> None:
    assert set(message) == {"text", "blocks"}
    assert 1 <= len(message["text"]) <= 150
    assert len(message["blocks"]) <= 50
    texts = [message["text"]]
    for index, block in enumerate(message["blocks"]):
        assert block["type"] in {"header", "section", "context", "divider"}
        assert not {"accessory", "actions", "fields"} & block.keys()
        if block["type"] == "header":
            assert top and index == 0
            assert block["text"]["type"] == "plain_text"
            assert 1 <= len(block["text"]["text"]) <= 150
            continue
        objects = block.get("elements", []) if block["type"] == "context" else []
        if block["type"] == "context":
            assert 1 <= len(objects) <= 10
        if block["type"] == "section":
            objects = [block["text"]]
        for obj in objects:
            assert obj["type"] == "mrkdwn" and obj["verbatim"] is True
            assert 1 <= len(obj["text"]) <= 3000
            texts.append(obj["text"])
    for text in texts:
        assert "<@" not in text and "<!" not in text
        for url in re.findall(r"<(https?://[^|>]+)\|[^<>]*>", text):
            assert links is not None
            assert url.startswith(links.workspace_url.rstrip("/") + "/")
        remaining = re.sub(r"<https?://[^|>]+\|[^<>]*>|<#C[A-Z0-9]+>", "", text)
        remaining = re.sub(r"&amp;|&lt;|&gt;", "", remaining)
        assert not any(char in remaining for char in "<>&")


def build_digest(
    world: World,
    selection: Selection,
    *,
    links: SeedMap | None = None,
    authors: dict[str, str] | None = None,
    threaded: bool = True,
) -> list[dict[str, Any]]:
    messages = slack_digest(
        world,
        selection,
        links=links,
        authors=authors or {},
        threaded=threaded,
    )
    assert 1 <= len(messages) <= 2
    for index, message in enumerate(messages):
        check_message(message, links, top=index == 0)
    return messages

"""Recorded responses keyed by the canonical hash of the request (SPEC section 6)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from digest.llm import LLMRequest, LLMResponse

log = logging.getLogger(__name__)


class CassetteMiss(LookupError):
    """Replay mode found no cassette for the request."""


def request_key(request: LLMRequest) -> str:
    canonical = json.dumps(
        request.model_dump(exclude={"max_tokens"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Cassettes:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, key: str) -> LLMResponse | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        return LLMResponse.model_validate(json.loads(path.read_text(encoding="utf-8"))["response"])

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> Path:
        path = self.root / f"{key}.json"
        payload = {
            "key": key,
            "request": request.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        # The temp file sits in the same directory so os.replace stays an atomic rename.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.root, suffix=".tmp", delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, path)
        log.info("cassette_written", extra={"key": key, "path": path})
        return path

    def recorded_models(self) -> dict[str, set[str]]:
        """Logical model name to the concrete ids seen in the cassettes under the root."""
        models: dict[str, set[str]] = {}
        for path in self.root.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            models.setdefault(payload["request"]["model"], set()).add(payload["response"]["model"])
        return models

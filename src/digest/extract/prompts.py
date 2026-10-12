"""The triage and extraction prompts and their requests (plan B1.2, B1.3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from digest.llm import LLMRequest
from digest.models import Event, Glossary, Item
from digest.project import ProjectConfig
from digest.store import ThreadDelta

EXTRACT_PROMPT_VERSION = "m2.1"  # covers the triage and the extraction prompt
EXTRACT_SYSTEM = ""
TRIAGE_SYSTEM = ""


def extract_request(
    delta: ThreadDelta,
    glossary: Glossary,
    project: ProjectConfig,
    as_of: date,
    existing: Sequence[Item],
    citable: Sequence[Event],
) -> LLMRequest:
    raise NotImplementedError


def triage_request(
    delta: ThreadDelta, channel_purpose: str | None, citable: Sequence[Event]
) -> LLMRequest:
    raise NotImplementedError

"""Triage: a probability that a thread delta carries project information (plan B1.2)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from digest.llm.client import LLM
from digest.models import Event, TriageDecision
from digest.priors import Priors
from digest.store import ThreadDelta


def triage(
    llm: LLM,
    delta: ThreadDelta,
    citable: Sequence[Event],
    channel_purpose: str | None,
    priors: Priors,
    now: datetime,
) -> TriageDecision:
    raise NotImplementedError

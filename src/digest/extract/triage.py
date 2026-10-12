"""Triage: a probability that a thread delta carries project information (plan B1.2)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from digest.extract.prompts import EXTRACT_PROMPT_VERSION, triage_request
from digest.extract.schema import TriageOutput
from digest.llm.client import LLM
from digest.models import Event, TriageDecision
from digest.priors import Priors
from digest.store import ThreadDelta

log = logging.getLogger(__name__)


def triage(
    llm: LLM,
    delta: ThreadDelta,
    citable: Sequence[Event],
    channel_purpose: str | None,
    priors: Priors,
    now: datetime,
) -> TriageDecision:
    output = llm.extract(triage_request(delta, channel_purpose, citable), TriageOutput)
    threshold = priors.triage.keep_threshold
    decision = TriageDecision(
        thread_id=delta.thread_id,
        last_ts=delta.last_ts,
        prompt_version=EXTRACT_PROMPT_VERSION,
        model="classify",
        probability=output.probability,
        threshold=threshold,
        kept=output.probability >= threshold,
        rationale=output.rationale,
        decided_at=now,
    )
    log.info(
        "triage_decided",
        extra={
            "thread_id": decision.thread_id,
            "probability": decision.probability,
            "threshold": decision.threshold,
            "kept": decision.kept,
        },
    )
    return decision

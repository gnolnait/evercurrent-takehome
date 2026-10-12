"""The extract stage driver: pending thread deltas in, triage decisions and items out (plan B1.6)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from digest.llm.client import LLM
from digest.models import Frozen, Glossary
from digest.priors import Priors
from digest.project import ProjectConfig
from digest.store import Store


class ExtractReport(Frozen):
    threads: int  # pending thread deltas seen
    kept: int
    dropped: int  # by triage
    failed: int  # threads where triage or extraction raised LLMOutputError
    items_written: int
    drops: dict[str, int]  # summed verify drops


def run_extract(
    store: Store,
    llm: LLM,
    glossary: Glossary,
    project: ProjectConfig,
    priors: Priors,
    as_of: datetime,
    zone: ZoneInfo,
    channel_purposes: Mapping[str, str],
) -> ExtractReport:
    raise NotImplementedError

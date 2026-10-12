"""The extract stage driver: pending thread deltas in, triage decisions and items out (plan B1.6)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from digest.extract.prompts import EXTRACT_PROMPT_VERSION, extract_request
from digest.extract.schema import Extraction
from digest.extract.triage import triage
from digest.extract.verify import DROP_FIELDS, verify
from digest.llm import LLMOutputError
from digest.llm.cassette import CassetteMiss
from digest.llm.client import LLM
from digest.models import Event, Frozen, Glossary
from digest.priors import Priors
from digest.project import ProjectConfig
from digest.store import StageRun, Store, ThreadDelta

log = logging.getLogger(__name__)


class ExtractReport(Frozen):
    threads: int  # pending thread deltas seen
    kept: int
    dropped: int  # by triage
    failed: int  # threads where triage or extraction raised LLMOutputError
    items_written: int
    drops: dict[str, int]  # summed verify drops

    def plus(self, other: ExtractReport) -> ExtractReport:
        """The sum of two reports, field by field."""
        return ExtractReport(
            threads=self.threads + other.threads,
            kept=self.kept + other.kept,
            dropped=self.dropped + other.dropped,
            failed=self.failed + other.failed,
            items_written=self.items_written + other.items_written,
            drops={key: self.drops[key] + other.drops[key] for key in DROP_FIELDS},
        )


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
    _claim_prompt_version(store)
    day = as_of.astimezone(zone).date()
    # The prompt lists the tasks of gates on or after the day; a link may name only those.
    tasks = {task.id: task for gate in project.gates if gate.date >= day for task in gate.checklist}
    deltas = store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", as_of)
    kept = dropped = failed = items_written = 0
    drops = dict.fromkeys(DROP_FIELDS, 0)
    for delta in deltas:
        citable = _citable(delta)
        try:
            decision = store.triage_decision(
                delta.thread_id, delta.last_ts, EXTRACT_PROMPT_VERSION, "classify"
            )
            if decision is None:
                purpose = channel_purposes.get(delta.channel_id)
                decision = triage(llm, delta, citable, purpose, priors, as_of)
                store.record_triage(decision)
            if not decision.kept:
                dropped += 1
                _record_run(store, delta, as_of, "ok")
                continue
            kept += 1
            existing = store.items_for_thread(delta.thread_id)
            request = extract_request(delta, glossary, project, day, existing, citable)
            extraction = llm.extract(request, Extraction)
        except LLMOutputError as error:
            failed += 1
            _record_run(store, delta, as_of, "failed")
            log.warning(
                "extract_failed",
                extra={"thread_id": delta.thread_id, "error_class": type(error).__name__},
            )
            continue
        except CassetteMiss:
            log.error("extract_cassette_miss", extra={"thread_id": delta.thread_id})
            raise
        verified = verify(
            extraction,
            delta,
            {row.ts: row for row in citable},
            glossary,
            tasks,
            {item.item_id: item for item in existing},
        )
        written = store.upsert_items(
            verified.items, prompt_version=EXTRACT_PROMPT_VERSION, model="extract"
        )
        _record_run(store, delta, as_of, "ok")
        items_written += written
        drops = {key: drops[key] + verified.drops[key] for key in DROP_FIELDS}
        log.info(
            "extract_thread",
            extra={"thread_id": delta.thread_id, "items_written": written, "drops": verified.drops},
        )
    return ExtractReport(
        threads=len(deltas),
        kept=kept,
        dropped=dropped,
        failed=failed,
        items_written=items_written,
        drops=drops,
    )


def _claim_prompt_version(store: Store) -> None:
    """Tie the store to this prompt version, or refuse a store tied to another one."""
    stored = store.get_meta("extract_prompt_version")
    if stored is None:
        store.set_meta("extract_prompt_version", EXTRACT_PROMPT_VERSION)
    elif stored != EXTRACT_PROMPT_VERSION:
        raise ValueError(
            f"store {store.path} holds extract prompt version {stored}, this code runs"
            f" {EXTRACT_PROMPT_VERSION}; a new prompt version starts from a fresh store"
        )


def _citable(delta: ThreadDelta) -> tuple[Event, ...]:
    """The root and the changed rows, deleted rows excluded, one row per ts, in ts order."""
    rows = (*(() if delta.root is None else (delta.root,)), *delta.changed)
    by_ts = {row.ts: row for row in rows if not row.deleted}
    return tuple(sorted(by_ts.values(), key=lambda row: row.ts_us))


def _record_run(
    store: Store, delta: ThreadDelta, run_at: datetime, status: Literal["ok", "failed"]
) -> None:
    store.record_stage_run(
        StageRun(
            stage="extract",
            thread_id=delta.thread_id,
            last_ts=delta.last_ts,
            prompt_version=EXTRACT_PROMPT_VERSION,
            model="extract",
            run_at=run_at,
            status=status,
        )
    )

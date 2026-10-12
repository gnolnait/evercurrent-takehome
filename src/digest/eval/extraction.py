"""The extraction eval: replay day by day, extract, detect absences, score against golden (plan unit 4)."""

from __future__ import annotations

from pathlib import Path

from digest.llm.client import LLM
from digest.models import Frozen
from digest.priors import Priors


class TypeScores(Frozen):
    golden: int  # golden items of this golden type
    golden_matched: int  # of those, matched (plan B4.2)
    extracted: int  # extracted items of this extracted type in golden or distractor threads
    extracted_matched: int  # of those, paired with a golden item
    precision: float | None  # extracted_matched / extracted; None when extracted == 0
    recall: float | None  # golden_matched / golden; None when golden == 0


class ExtractionReport(Frozen):
    world: str
    days: int
    models: dict[str, str]  # logical name -> concrete ids seen in the cassettes
    by_type: dict[str, TypeScores]
    thread_recall: float | None  # golden items whose thread yielded any item
    type_agreement: float | None  # among matched golden items, share whose item has the same type
    objective_misses: dict[str, int]  # thread-matched golden items with a wrong objective field
    claims_golden: int
    claims_found: int
    claims_extracted: int  # extracted claims in golden threads
    claims_extracted_matched: int
    claim_recall: float | None  # claims_found / claims_golden
    claim_precision: float | None  # claims_extracted_matched / claims_extracted
    evidence_entries: int  # delivered evidence entries, the n of citation_validity
    entity_jaccard: float | None  # mean over matched items with golden entities
    entity_jaccard_n: int
    entities_golden: int
    entities_found: int
    entity_recall: float | None  # entities_found / entities_golden
    citation_validity: float  # verified entries over delivered entries; 1.0 by construction
    drops: dict[str, int]
    triage_golden_kept: int
    triage_golden_total: int
    triage_by_event: dict[str, float]  # max probability over the thread's revisions, per event
    triage_dropped_golden: tuple[str, ...]
    triage_distractors_kept: tuple[str, ...]
    missed: tuple[str, ...]  # golden event ids with no matched item
    unmatched: tuple[str, ...]  # extracted item ids matched to no golden item, with the thread kind
    absence_tp: int
    absence_fp: int
    absence_fn: int
    absence_false_alarm_rate: float | None
    absence_recall: float | None
    uncitable_tasks: tuple[str, ...]  # tasks inside a buffer with no evidence on any day
    realism: dict[str, float]  # messages per thread and chars per message: mean, median, p90
    not_run: dict[str, str]  # metric -> reason


def run_extraction_eval(
    world_dir: Path, llm: LLM, priors: Priors, store_path: Path
) -> ExtractionReport:
    raise NotImplementedError


def write_report(report: ExtractionReport, path: Path) -> None:
    raise NotImplementedError

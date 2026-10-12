"""The extraction eval: replay day by day, extract, detect absences, score against golden (plan unit 4)."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import get_args
from zoneinfo import ZoneInfo

from digest.absence import detect_absences
from digest.extract.normalize import normalize_space
from digest.extract.stage import run_extract
from digest.extract.verify import DROP_FIELDS
from digest.llm.client import LLM
from digest.models import Claim, Frozen, Item, ItemType, TriageDecision
from digest.priors import Priors
from digest.project import ProjectConfig, add_working_days
from digest.replay import read_events, replay_world
from digest.store import Store
from digest.world.golden import Golden, GoldenClaim, GoldenItem, derive_golden, read_golden
from digest.world.load import load_world
from digest.world.manifest import Manifest, load_manifest
from digest.world.schema import World

# (day, source, task id for a checklist absence or ask event id for an ask absence)
_AbsenceKey = tuple[int, str, str | None]

_NOT_RUN = {
    "objective.due": "world events carry no due date, so golden items have none",
    "objective.status": "world events carry no status, so golden items have none",
    "realism.reference": "no reference statistics in the repo",
}


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
    models: dict[str, str]  # logical name -> concrete ids seen in the cassettes, joined
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
    uncitable_tasks: tuple[str, ...]  # tasks with no link and no gate anchor on a day in a buffer
    realism: dict[str, float]  # messages per thread and chars per message: mean, median, p90
    not_run: dict[str, str]  # metric -> reason


def run_extraction_eval(
    world_dir: Path, llm: LLM, priors: Priors, store_path: Path
) -> ExtractionReport:
    """Run the world day by day in the store at `store_path` and score it against golden."""
    world = load_world(world_dir / "world.yaml")
    manifest = load_manifest(world_dir / "manifest.json")
    golden = _current_golden(world_dir / "golden.jsonl", derive_golden(world, manifest, priors))
    asks = {_thread_of(item): item.event_id for item in golden.items if item.type == "ask"}
    end = _end_of(world.date_of(world.days), ZoneInfo(world.timezone))
    with Store.open(store_path) as store:
        drops, detected, uncitable = _run_days(store, world, world_dir, llm, priors, asks)
        items = store.items(end)
        verified = _verified_entries(store, items, end)
        decisions = {
            event_id: _decisions(store, event_id, thread)
            for event_id, thread in _triaged_threads(golden).items()
        }
    golden_threads = {_thread_of(item) for item in golden.items}
    distractor_threads = {_thread(row.channel_id, row.thread_ts) for row in golden.distractors}
    by_thread: dict[str, list[Item]] = {}
    for item in items:
        by_thread.setdefault(item.thread_id, []).append(item)
    pairs = _pairs(golden.items, by_thread)
    paired = {item.item_id for item in pairs.values()}
    scored = [item for item in items if item.thread_id in golden_threads | distractor_threads]
    claims = _claim_counts(golden.items, by_thread)
    jaccard, jaccard_n, entities_golden, entities_found = _entity_counts(golden.items, pairs)
    kept = {event_id: any(row.kept for row in rows) for event_id, rows in decisions.items()}
    golden_keys = {
        (row.day, row.source, row.task if row.source == "checklist" else row.cites)
        for row in golden.absences
    }
    tp, fp, fn = (
        len(detected & golden_keys),
        len(detected - golden_keys),
        len(golden_keys - detected),
    )
    evidence_entries = sum(len(item.evidence) for item in items)
    return ExtractionReport(
        world=world.name,
        days=world.days,
        models={
            name: ", ".join(sorted(ids))
            for name, ids in sorted(llm.cassettes.recorded_models().items())
        },
        by_type=_by_type(golden.items, pairs, scored),
        thread_recall=_ratio(
            sum(1 for item in golden.items if by_thread.get(_thread_of(item))), len(golden.items)
        ),
        type_agreement=_ratio(sum(1 for item in golden.items if _agrees(item, pairs)), len(pairs)),
        objective_misses=_objective_misses(golden.items, by_thread, pairs),
        claims_golden=claims[0],
        claims_found=claims[1],
        claims_extracted=claims[2],
        claims_extracted_matched=claims[3],
        claim_recall=_ratio(claims[1], claims[0]),
        claim_precision=_ratio(claims[3], claims[2]),
        evidence_entries=evidence_entries,
        entity_jaccard=jaccard,
        entity_jaccard_n=jaccard_n,
        entities_golden=entities_golden,
        entities_found=entities_found,
        entity_recall=_ratio(entities_found, entities_golden),
        # No entry is wrong when no entry is delivered.
        citation_validity=verified / evidence_entries if evidence_entries else 1.0,
        drops=drops,
        triage_golden_kept=sum(1 for item in golden.items if kept[item.event_id]),
        triage_golden_total=len(golden.items),
        triage_by_event={
            event_id: max(row.probability for row in decisions[event_id])
            for event_id in sorted(decisions)
        },
        triage_dropped_golden=tuple(
            item.event_id for item in golden.items if not kept[item.event_id]
        ),
        triage_distractors_kept=tuple(
            row.event_id for row in golden.distractors if kept[row.event_id]
        ),
        missed=tuple(item.event_id for item in golden.items if item.event_id not in pairs),
        unmatched=tuple(
            f"{item.item_id} ({'golden' if item.thread_id in golden_threads else 'distractor'}"
            " thread)"
            for item in scored
            if item.item_id not in paired
        ),
        absence_tp=tp,
        absence_fp=fp,
        absence_fn=fn,
        absence_false_alarm_rate=_ratio(fp, tp + fp),
        absence_recall=_ratio(tp, tp + fn),
        uncitable_tasks=tuple(sorted(uncitable)),
        realism=_realism(world_dir, world, manifest),
        not_run=_NOT_RUN,
    )


def _current_golden(path: Path, derived: Golden) -> Golden:
    """The label file, when it equals the labels derived now; a stale file raises."""
    stored = read_golden(path)
    if stored != derived:
        raise ValueError(
            f"{path} differs from the labels derived from world.yaml, manifest.json and the"
            f" priors (ask_reply_cadence_days {stored.ask_reply_cadence_days} in the file,"
            f" {derived.ask_reply_cadence_days} in force); run digest world golden"
        )
    return stored


def _run_days(
    store: Store,
    world: World,
    world_dir: Path,
    llm: LLM,
    priors: Priors,
    asks: Mapping[str, str],
) -> tuple[dict[str, int], set[_AbsenceKey], set[str]]:
    """Replay, extract and detect absences for each day; the summed drops and the day keys."""
    zone = ZoneInfo(world.timezone)
    glossary = world.glossary()
    purposes = {channel.id: channel.purpose for channel in world.channels}
    drops = dict.fromkeys(DROP_FIELDS, 0)
    detected: set[_AbsenceKey] = set()
    uncitable: set[str] = set()
    for day in range(1, world.days + 1):
        today = world.date_of(day)
        replay_world(store, world_dir, today)
        as_of = _end_of(today, zone)
        report = run_extract(store, llm, glossary, world.project, priors, as_of, zone, purposes)
        drops = {key: drops[key] + report.drops[key] for key in DROP_FIELDS}
        found = detect_absences(store, world.project, priors, as_of, zone)
        detected |= {_absence_key(day, item, asks) for item in found}
        uncitable |= _uncitable(world.project, store.items(as_of), today)
    return drops, detected, uncitable


def _absence_key(day: int, item: Item, asks: Mapping[str, str]) -> _AbsenceKey:
    detail = item.absence
    assert detail is not None  # the Item validator sets it on every absence item
    if detail.source == "checklist":
        return (day, "checklist", detail.task)
    return (day, "ask", asks.get(item.thread_id, item.thread_id))


def _uncitable(project: ProjectConfig, items: Sequence[Item], day: date) -> set[str]:
    """Tasks of gates in their buffer on `day` with no task link and no claim on the gate."""
    linked = {link.task for item in items for link in item.tasks}
    anchored = {claim.entity for item in items for claim in item.claims}
    return {
        task.id
        for gate in project.gates
        if add_working_days(gate.date, -gate.buffer_days) <= day <= gate.date
        and gate.id not in anchored
        for task in gate.checklist
        if task.id not in linked
    }


def _verified_entries(store: Store, items: Iterable[Item], as_of: datetime) -> int:
    """Evidence entries whose quote is in the stored text of the cited message."""
    count = 0
    for item in items:
        texts = {event.ts: event.text for event in store.events_in_thread(item.thread_id, as_of)}
        for entry in item.evidence:
            span = normalize_space(entry.quote)
            if span and span in normalize_space(texts.get(entry.message_ts, "")):
                count += 1
    return count


def _triaged_threads(golden: Golden) -> dict[str, str]:
    """Event id to thread id, for each golden item and each distractor."""
    return {
        **{item.event_id: _thread_of(item) for item in golden.items},
        **{row.event_id: _thread(row.channel_id, row.thread_ts) for row in golden.distractors},
    }


def _decisions(store: Store, event_id: str, thread: str) -> list[TriageDecision]:
    decisions = store.triage_for_thread(thread)
    if not decisions:
        raise ValueError(
            f"thread {thread} of event {event_id} has no triage decision; the eval triages"
            " every thread in the store, so events.jsonl has no message in that thread"
        )
    return decisions


def _pairs(
    golden_items: Sequence[GoldenItem], by_thread: Mapping[str, list[Item]]
) -> dict[str, Item]:
    """Event id to item: each golden item takes the first free item that carries its fields."""
    taken: set[str] = set()
    pairs: dict[str, Item] = {}
    for golden in golden_items:
        match = next(
            (
                item
                for item in by_thread.get(_thread_of(golden), [])
                if item.item_id not in taken and _qualifies(golden, item)
            ),
            None,
        )
        if match is not None:
            taken.add(match.item_id)
            pairs[golden.event_id] = match
    return pairs


def _qualifies(golden: GoldenItem, item: Item) -> bool:
    carries = all(_carries(item, claim) for claim in golden.claims)
    return carries and (golden.owner is None or item.owner == golden.owner)


def _carries(item: Item, claim: GoldenClaim) -> bool:
    return _claim_key(claim) in {_claim_key(own) for own in item.claims}


def _claim_key(claim: GoldenClaim | Claim) -> tuple[str, str, str]:
    return (claim.entity, claim.attribute, normalize_space(claim.value).lower())


def _agrees(golden: GoldenItem, pairs: Mapping[str, Item]) -> bool:
    return golden.event_id in pairs and pairs[golden.event_id].type == golden.type


def _by_type(
    golden_items: Sequence[GoldenItem], pairs: Mapping[str, Item], scored: Sequence[Item]
) -> dict[str, TypeScores]:
    paired = {item.item_id for item in pairs.values()}
    scores: dict[str, TypeScores] = {}
    for kind in get_args(ItemType):
        if kind == "absence":
            continue
        golden = [item for item in golden_items if item.type == kind]
        golden_matched = sum(1 for item in golden if item.event_id in pairs)
        extracted = [item for item in scored if item.type == kind]
        extracted_matched = sum(1 for item in extracted if item.item_id in paired)
        scores[kind] = TypeScores(
            golden=len(golden),
            golden_matched=golden_matched,
            extracted=len(extracted),
            extracted_matched=extracted_matched,
            precision=_ratio(extracted_matched, len(extracted)),
            recall=_ratio(golden_matched, len(golden)),
        )
    return scores


def _objective_misses(
    golden_items: Sequence[GoldenItem],
    by_thread: Mapping[str, list[Item]],
    pairs: Mapping[str, Item],
) -> dict[str, int]:
    """Per field, the unmatched thread-matched golden items whose thread has no correct value."""
    misses = {"claims.value": 0, "owner": 0}
    for golden in golden_items:
        items = by_thread.get(_thread_of(golden), [])
        if not items or golden.event_id in pairs:
            continue
        if not all(any(_carries(item, claim) for item in items) for claim in golden.claims):
            misses["claims.value"] += 1
        if golden.owner is not None and all(item.owner != golden.owner for item in items):
            misses["owner"] += 1
    return misses


def _claim_counts(
    golden_items: Sequence[GoldenItem], by_thread: Mapping[str, list[Item]]
) -> tuple[int, int, int, int]:
    """Golden claims, those found in their thread, extracted claims in golden threads, matched."""
    golden_claims = [(_thread_of(item), claim) for item in golden_items for claim in item.claims]
    found = sum(
        1
        for thread, claim in golden_claims
        if any(_carries(item, claim) for item in by_thread.get(thread, []))
    )
    keys: dict[str, set[tuple[str, str, str]]] = {_thread_of(item): set() for item in golden_items}
    for thread, claim in golden_claims:
        keys[thread].add(_claim_key(claim))
    extracted = [
        (thread, claim)
        for thread in keys
        for item in by_thread.get(thread, [])
        for claim in item.claims
    ]
    matched = sum(1 for thread, claim in extracted if _claim_key(claim) in keys[thread])
    return len(golden_claims), found, len(extracted), matched


def _entity_counts(
    golden_items: Sequence[GoldenItem], pairs: Mapping[str, Item]
) -> tuple[float | None, int, int, int]:
    """Mean Jaccard, its n, golden entities and those found, over matched items with entities."""
    sets = [
        (set(item.entities), set(pairs[item.event_id].entities))
        for item in golden_items
        if item.event_id in pairs and item.entities
    ]
    jaccard = statistics.mean(len(g & e) / len(g | e) for g, e in sets) if sets else None
    return jaccard, len(sets), sum(len(g) for g, _ in sets), sum(len(g & e) for g, e in sets)


def _realism(world_dir: Path, world: World, manifest: Manifest) -> dict[str, float]:
    """Messages per rendered thread and characters per rendered message."""
    rendered = {
        (ref.channel_id, ref.thread_ts)
        for ref in manifest.threads.values()
        if ref.authored == "render"
    }
    threads: dict[tuple[str, str], dict[str, str]] = {}
    for event in read_events(world_dir / "events.jsonl", world.workspace_url):
        if (event.channel_id, event.thread_ts) in rendered:
            threads.setdefault((event.channel_id, event.thread_ts), {})[event.ts] = event.text
    return {
        **_stats("messages_per_thread", [len(texts) for texts in threads.values()]),
        **_stats(
            "chars_per_message",
            [len(text) for texts in threads.values() for text in texts.values()],
        ),
    }


def _stats(name: str, values: Sequence[int]) -> dict[str, float]:
    """Mean, median and nearest-rank p90."""
    ordered = sorted(values)
    return {
        f"{name}.mean": float(statistics.mean(ordered)),
        f"{name}.median": float(statistics.median(ordered)),
        f"{name}.p90": float(ordered[math.ceil(0.9 * len(ordered)) - 1]),
    }


def _thread_of(item: GoldenItem) -> str:
    return _thread(item.channel_id, item.thread_ts)


def _thread(channel_id: str, thread_ts: str) -> str:
    return f"{channel_id}:{thread_ts}"


def _end_of(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.max, zone).astimezone(UTC)


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def write_report(report: ExtractionReport, path: Path) -> None:
    """Write the report as markdown: one table per section, `n` beside every rate."""
    pairs = sum(scores.golden_matched for scores in report.by_type.values())
    golden = sum(scores.golden for scores in report.by_type.values())
    lines = [
        f"# Extraction eval: {report.world}",
        "",
        f"World `{report.world}`, {report.days} days. Golden items: {golden}.",
        "",
        *_table(
            "Extraction by type",
            (
                "type",
                "golden",
                "golden matched",
                "recall",
                "n",
                "extracted",
                "extracted matched",
                "precision",
                "n",
            ),
            [
                (
                    kind,
                    s.golden,
                    s.golden_matched,
                    s.recall,
                    s.golden,
                    s.extracted,
                    s.extracted_matched,
                    s.precision,
                    s.extracted,
                )
                for kind, s in report.by_type.items()
            ],
        ),
        *_table(
            "Matching",
            ("metric", "value", "n"),
            [
                ("thread recall", report.thread_recall, golden),
                ("type agreement (advisory)", report.type_agreement, pairs),
                ("citation validity", report.citation_validity, report.evidence_entries),
                *(
                    (f"objective misses: {field}", count, "")
                    for field, count in report.objective_misses.items()
                ),
                ("missed golden events", _listed(report.missed), ""),
                ("unmatched items", _listed(report.unmatched), ""),
            ],
        ),
        *_table(
            "Claims and entities",
            ("metric", "value", "n"),
            [
                ("claim recall", report.claim_recall, report.claims_golden),
                ("claim precision", report.claim_precision, report.claims_extracted),
                ("golden claims found", report.claims_found, ""),
                ("extracted claims matched", report.claims_extracted_matched, ""),
                ("entity jaccard", report.entity_jaccard, report.entity_jaccard_n),
                ("entity recall", report.entity_recall, report.entities_golden),
            ],
        ),
        *_table(
            "Triage",
            ("metric", "value", "n"),
            [
                (
                    "triage recall",
                    _ratio(report.triage_golden_kept, report.triage_golden_total),
                    report.triage_golden_total,
                ),
                ("golden events dropped", _listed(report.triage_dropped_golden), ""),
                ("distractor events kept", _listed(report.triage_distractors_kept), ""),
                *(
                    (f"max probability: {event_id}", probability, "")
                    for event_id, probability in report.triage_by_event.items()
                ),
            ],
        ),
        *_table(
            "Absences",
            ("metric", "value", "n"),
            [
                (
                    "absence false-alarm rate",
                    report.absence_false_alarm_rate,
                    report.absence_tp + report.absence_fp,
                ),
                ("absence recall", report.absence_recall, report.absence_tp + report.absence_fn),
                ("true positives", report.absence_tp, ""),
                ("false positives", report.absence_fp, ""),
                ("false negatives", report.absence_fn, ""),
                ("uncitable checklist items", _listed(report.uncitable_tasks), ""),
            ],
        ),
        *_table("Drops", ("field", "dropped"), list(report.drops.items())),
        *_table("Realism", ("statistic", "value"), list(report.realism.items())),
        *_table("Models", ("logical model", "concrete ids"), list(report.models.items())),
        *_table("Not run", ("metric", "reason"), list(report.not_run.items())),
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _table(
    title: str, header: Sequence[str], rows: Iterable[Sequence[str | int | float | None]]
) -> list[str]:
    return [
        f"## {title}",
        "",
        _row(header),
        _row(["---"] * len(header)),
        *(_row([_cell(value) for value in row]) for row in rows),
        "",
    ]


def _row(cells: Sequence[str]) -> str:
    return f"| {' | '.join(cells)} |"


def _cell(value: str | int | float | None) -> str:
    """A rate keeps its decimal point; None prints as n/a."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return str(round(value, 3))
    return str(value)


def _listed(values: Sequence[str]) -> str:
    return ", ".join(values) or "none"

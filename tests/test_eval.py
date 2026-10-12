import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import time_machine
import yaml
from typer.testing import CliRunner

import digest.cli as cli_module
from digest.cli import app
from digest.eval.extraction import ExtractionReport, run_extraction_eval, write_report
from digest.extract.prompts import EXTRACT_PROMPT_VERSION, extract_request, triage_request
from digest.extract.schema import Extraction, TriageOutput
from digest.extract.verify import DROP_FIELDS
from digest.llm import LLMRequest, LLMResponse, Message
from digest.llm.cassette import Cassettes, request_key
from digest.llm.client import LLM
from digest.models import Event
from digest.priors import AbsencePriors, Priors, TriagePriors
from digest.replay import replay_world
from digest.store import Store
from digest.world.golden import Golden, derive_golden, write_golden
from digest.world.load import load_world
from digest.world.manifest import Manifest, ThreadRef, write_manifest

runner = CliRunner()
WORKSPACE = "https://small.slack.com"
START = date(2026, 10, 12)
SHARED_ROOT = "1791907200.000001"
SHARED_REPLY = "1791907260.000002"
ASK_ROOT = "1791824400.000003"
ASK_REPLY = "1791824460.000008"
DISTRACTOR_ROOT = "1791828000.000004"
DISTRACTOR_REPLY = "1791828060.000005"
DISTRACTOR_REPLY_2 = "1791828120.000006"
OUTSIDE_ROOT = "1791825000.000007"
ABSENT_ROOT = "1791908400.000099"


@dataclass(frozen=True)
class EvalCase:
    root: Path
    world_dir: Path
    cassettes: Cassettes
    priors: Priors
    golden: Golden

    def llm(self, cassette_root: Path | None = None) -> LLM:
        return LLM(None, Cassettes(cassette_root or self.cassettes.root), "replay")


def _world(
    *,
    include_distractor: bool,
    absences: bool,
    scored_attribute: str = "status",
    scored_entity: str = "G-CITE",
    golden_value: str = "open",
    scored_claim_only: bool = False,
    ask_claim_value: str | None = None,
) -> dict[str, Any]:
    ask_day = 2 if absences else 1
    events: list[dict[str, Any]] = [
        {
            "id": "A-REV",
            "day": 1,
            "channel": "CENG",
            "poster": "UASKER",
            "kind": "risk",
            "summary": "The drive revision is C.",
            "claims": [{"entity": "PN-1", "attribute": "rev", "value": "C"}],
        },
        {
            "id": "B-QTY",
            "day": 1,
            "channel": "CENG",
            "poster": "UASKER",
            "kind": "spec_change",
            "summary": "The drive quantity is four and the gate is open.",
            "claims": [
                *(
                    []
                    if scored_claim_only
                    else [
                        {"entity": "PN-1", "attribute": "rev", "value": "B"},
                        {"entity": "PN-1", "attribute": "qty", "value": "4"},
                    ]
                ),
                {
                    "entity": scored_entity,
                    "attribute": scored_attribute,
                    "value": golden_value,
                },
            ],
        },
        {
            "id": "ASK-OWNER",
            "day": ask_day,
            "channel": "CENG",
            "poster": "UASKER",
            "kind": "ask",
            "summary": "When will the review finish?",
            "ask": {
                "of": "UOWNER",
                "answered_day": None if absences else 1,
            },
            "claims": (
                []
                if ask_claim_value is None
                else [{"entity": "G-CITE", "attribute": "status", "value": ask_claim_value}]
            ),
        },
    ]
    if include_distractor:
        events.append(
            {
                "id": "DIST-NOISE",
                "day": 1,
                "channel": "CNOISE",
                "poster": "UOWNER",
                "kind": "distractor",
                "summary": "Lunch moved to noon.",
            }
        )
    return {
        "name": "tiny",
        "program": "Kestrel K2",
        "workspace_url": WORKSPACE,
        "timezone": "America/Los_Angeles",
        "start_date": START.isoformat(),
        "days": 4,
        "project": {
            "project_id": "tiny",
            "name": "Kestrel K2",
            "phases": [{"phase": "DVT", "from_date": START.isoformat()}],
            "gates": [
                {
                    "id": "G-CITE",
                    "name": "Citable gate",
                    "date": date(2026, 10, 15).isoformat(),
                    "buffer_days": 3,
                    "checklist": [
                        {
                            "id": "G-CITE.task",
                            "task": "Run the cited test",
                            "owner": "ME",
                            "expected_cadence_days": 1 if absences else 10,
                        },
                        {
                            "id": "G-CITE.release",
                            "task": "Release the cited test report",
                            "owner": "ME",
                            "expected_cadence_days": 1 if absences else 10,
                        },
                    ],
                },
                {
                    "id": "G-UNCITE",
                    "name": "Uncitable gate",
                    "date": date(2026, 10, 15).isoformat(),
                    "buffer_days": 3,
                    "checklist": [
                        {
                            "id": "G-UNCITE.task",
                            "task": "Run the uncited test",
                            "owner": "EE",
                            "expected_cadence_days": 1 if absences else 10,
                        }
                    ],
                },
            ],
        },
        "channels": [
            {"id": "CENG", "name": "eng", "purpose": "Engineering work"},
            {"id": "CNOISE", "name": "social", "purpose": "Team conversation"},
        ],
        "personas": [
            {
                "user_id": "UASKER",
                "name": "Asker",
                "role": "EM",
                "channels": ["CENG"],
            },
            {
                "user_id": "UOWNER",
                "name": "Owner",
                "role": "ME",
                "channels": ["CENG", "CNOISE"],
            },
        ],
        "entities": [
            {"id": "PN-1", "type": "part", "description": "Drive part"},
            {"id": "CONTEXT", "type": "part", "description": "Context part"},
            {"id": "G-CITE", "type": "gate", "description": "Citable gate"},
            {"id": "G-UNCITE", "type": "gate", "description": "Uncitable gate"},
        ],
        "events": events,
    }


def _event(
    channel_id: str,
    channel_name: str,
    ts: str,
    text: str,
    user_id: str,
    *,
    thread_ts: str | None = None,
) -> Event:
    root = thread_ts or ts
    suffix = f"?thread_ts={root}&cid={channel_id}" if root != ts else ""
    return Event(
        source="replay",
        channel_id=channel_id,
        channel_name=channel_name,
        thread_ts=root,
        ts=ts,
        event_ts=ts,
        user_id=user_id,
        text=text,
        permalink=f"{WORKSPACE}/archives/{channel_id}/p{ts.replace('.', '')}{suffix}",
    )


def _events(include_distractor: bool, absences: bool) -> list[Event]:
    rows = [
        _event("CENG", "eng", SHARED_ROOT, "12345", "UASKER"),
        _event(
            "CENG",
            "eng",
            SHARED_REPLY,
            "abcdefghij",
            "UASKER",
            thread_ts=SHARED_ROOT,
        ),
        _event("CENG", "eng", ASK_ROOT, "When review?", "UASKER"),
        _event("CENG", "eng", OUTSIDE_ROOT, "x" * 20, "UASKER"),
    ]
    if not absences:
        rows.append(
            _event(
                "CENG",
                "eng",
                ASK_REPLY,
                "The review is complete.",
                "UOWNER",
                thread_ts=ASK_ROOT,
            )
        )
    if include_distractor:
        rows.extend(
            [
                _event("CNOISE", "social", DISTRACTOR_ROOT, "A" * 15, "UOWNER"),
                _event(
                    "CNOISE",
                    "social",
                    DISTRACTOR_REPLY,
                    "b" * 25,
                    "UOWNER",
                    thread_ts=DISTRACTOR_ROOT,
                ),
                _event(
                    "CNOISE",
                    "social",
                    DISTRACTOR_REPLY_2,
                    "c" * 40,
                    "UOWNER",
                    thread_ts=DISTRACTOR_ROOT,
                ),
            ]
        )
    return sorted(rows, key=lambda event: event.event_ts)


def _manifest(*, include_distractor: bool, absent_golden_thread: bool) -> Manifest:
    shared = ThreadRef(channel_id="CENG", thread_ts=SHARED_ROOT, authored="render")
    threads = {
        "A-REV": ThreadRef(
            channel_id="CENG",
            thread_ts=ABSENT_ROOT if absent_golden_thread else SHARED_ROOT,
            authored="render",
        ),
        "B-QTY": shared,
        "ASK-OWNER": ThreadRef(channel_id="CENG", thread_ts=ASK_ROOT, authored="human"),
        "RENDER-ONLY": ThreadRef(channel_id="CENG", thread_ts=OUTSIDE_ROOT, authored="render"),
    }
    if include_distractor:
        threads["DIST-NOISE"] = ThreadRef(
            channel_id="CNOISE", thread_ts=DISTRACTOR_ROOT, authored="render"
        )
    return Manifest(threads=threads)


def _extracted_item(
    item_type: str,
    evidence_ts: str,
    quote: str,
    *,
    entities: list[str] | None = None,
    owner: str | None = None,
    due: str | None = None,
    claims: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "rationale": "scripted test output",
        "item_id": None,
        "type": item_type,
        "title": "Extracted item",
        "summary": "Extracted summary.",
        "entities": entities or [],
        "owner": owner,
        "due": due,
        "affected_roles": [],
        "severity": "medium",
        "confidence": "high",
        "status": "open",
        "evidence": [{"message_ts": evidence_ts, "quote": quote}],
        "claims": claims or [],
        "tasks": [],
    }


def _claim(entity: str, attribute: str, value: str, message_ts: str) -> dict[str, str]:
    return {
        "entity": entity,
        "attribute": attribute,
        "value": value,
        "message_ts": message_ts,
    }


def _outputs(
    *,
    wrong_objectives: bool,
    scored_attribute: str = "status",
    scored_entity: str = "G-CITE",
    extracted_value: str = "closed",
    scored_claim_only: bool = False,
    ask_claim_value: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    quantity = "5" if wrong_objectives else "4"
    ask_owner = "UASKER" if wrong_objectives else "UOWNER"
    later = _extracted_item(
        "spec_change",
        SHARED_REPLY,
        "abcdefghij",
        entities=["PN-1"],
        claims=[_claim("PN-1", "rev", "C", SHARED_REPLY)],
    )
    earlier = _extracted_item(
        "decision",
        SHARED_ROOT,
        "12345",
        entities=list(dict.fromkeys(["PN-1", "G-CITE", "CONTEXT", scored_entity])),
        claims=[
            *(
                []
                if scored_claim_only
                else [
                    _claim("PN-1", "rev", " B ", SHARED_ROOT),
                    _claim("PN-1", "qty", quantity, SHARED_ROOT),
                ]
            ),
            _claim(scored_entity, scored_attribute, extracted_value, SHARED_ROOT),
        ],
    )
    noise = _extracted_item(
        "decision",
        SHARED_ROOT,
        "12345",
        entities=["PN-1"],
        due="next week",
        claims=[_claim("PN-1", "qty", "5", SHARED_ROOT)],
    )
    ask = _extracted_item(
        "ask",
        ASK_ROOT,
        "When review?",
        owner=ask_owner,
        claims=(
            []
            if ask_claim_value is None
            else [_claim("G-CITE", "status", ask_claim_value, ASK_ROOT)]
        ),
    )
    distractor = _extracted_item("fyi", DISTRACTOR_ROOT, "A" * 15, entities=["CONTEXT"])
    outside = _extracted_item(
        "fyi",
        OUTSIDE_ROOT,
        "x" * 20,
        entities=["UNKNOWN-ENTITY"],
        claims=[_claim("PN-1", "qty", "4", OUTSIDE_ROOT)],
    )
    return {
        f"CENG:{SHARED_ROOT}": [later, earlier, noise],
        f"CENG:{ASK_ROOT}": [ask],
        f"CENG:{OUTSIDE_ROOT}": [outside],
        f"CNOISE:{DISTRACTOR_ROOT}": [distractor],
    }


def _put_response(
    cassettes: Cassettes,
    request: LLMRequest,
    schema: dict[str, Any],
    data: dict[str, Any],
    model: str,
) -> None:
    sent = request.model_copy(update={"output_schema": schema})
    response = LLMResponse(
        text=json.dumps(data),
        data=data,
        backend="fake",
        model=model,
        input_tokens=10,
        output_tokens=5,
    )
    cassettes.put(request_key(sent), sent, response)


def _script_cassettes(
    case: EvalCase,
    *,
    include_distractor: bool,
    drop_shared: bool,
    wrong_objectives: bool,
    scored_attribute: str,
    scored_entity: str,
    extracted_value: str,
    scored_claim_only: bool,
    extracted_ask_claim_value: str | None,
) -> None:
    world = load_world(case.world_dir / "world.yaml")
    purposes = {channel.id: channel.purpose for channel in world.channels}
    outputs = _outputs(
        wrong_objectives=wrong_objectives,
        scored_attribute=scored_attribute,
        scored_entity=scored_entity,
        extracted_value=extracted_value,
        scored_claim_only=scored_claim_only,
        ask_claim_value=extracted_ask_claim_value,
    )
    request_store = case.root / "request.sqlite"
    with Store.open(request_store) as store:
        replay_world(store, case.world_dir, world.date_of(world.days))
        deltas = store.thread_deltas(
            "extract",
            EXTRACT_PROMPT_VERSION,
            "extract",
            datetime.max.replace(tzinfo=UTC),
        )
        for delta in deltas:
            by_ts = {
                event.ts: event
                for event in ((delta.root,) if delta.root is not None else ()) + delta.changed
                if not event.deleted
            }
            citable = tuple(by_ts[ts] for ts in sorted(by_ts))
            probability = 0.01 if drop_shared and delta.thread_ts == SHARED_ROOT else 0.9
            triage = triage_request(delta, purposes.get(delta.channel_id), citable)
            _put_response(
                case.cassettes,
                triage,
                TriageOutput.model_json_schema(),
                {"rationale": "scripted triage", "probability": probability},
                "triage-v1",
            )
            if probability < case.priors.triage.keep_threshold:
                continue
            extraction = extract_request(
                delta,
                world.glossary(),
                world.project,
                min(event.at.astimezone(ZoneInfo(world.timezone)).date() for event in citable),
                store.items_for_thread(delta.thread_id),
                citable,
            )
            _put_response(
                case.cassettes,
                extraction,
                Extraction.model_json_schema(),
                {"rationale": "scripted extraction", "items": outputs[delta.thread_id]},
                "extract-v2" if delta.thread_ts == OUTSIDE_ROOT else "extract-v1",
            )
    render_request = LLMRequest(
        model="render",
        system="Render the synthetic thread.",
        messages=(Message(role="user", content="render-only cassette"),),
    )
    _put_response(case.cassettes, render_request, {}, {}, "render-v1")
    if not include_distractor:
        assert f"CNOISE:{DISTRACTOR_ROOT}" not in {delta.thread_id for delta in deltas}


def _build_case(
    root: Path,
    *,
    include_distractor: bool = True,
    absences: bool = True,
    drop_shared: bool = False,
    wrong_objectives: bool = False,
    absent_golden_thread: bool = False,
    scored_attribute: str = "status",
    scored_entity: str = "G-CITE",
    golden_value: str = "open",
    extracted_value: str = "closed",
    scored_claim_only: bool = False,
    golden_ask_claim_value: str | None = None,
    extracted_ask_claim_value: str | None = None,
) -> EvalCase:
    world_dir = root / "data" / "world" / "tiny"
    world_dir.mkdir(parents=True)
    raw_world = _world(
        include_distractor=include_distractor,
        absences=absences,
        scored_attribute=scored_attribute,
        scored_entity=scored_entity,
        golden_value=golden_value,
        scored_claim_only=scored_claim_only,
        ask_claim_value=golden_ask_claim_value,
    )
    (world_dir / "world.yaml").write_text(yaml.safe_dump(raw_world), encoding="utf-8")
    rows = _events(include_distractor, absences)
    (world_dir / "events.jsonl").write_text(
        "".join(event.model_dump_json() + "\n" for event in rows), encoding="utf-8"
    )
    manifest = _manifest(
        include_distractor=include_distractor,
        absent_golden_thread=absent_golden_thread,
    )
    write_manifest(manifest, world_dir / "manifest.json")
    priors = Priors(
        triage=TriagePriors(miss_cost_ratio=10),
        absence=AbsencePriors(ask_reply_cadence_days=1),
    )
    config = root / "config"
    config.mkdir()
    (config / "priors.yaml").write_text(yaml.safe_dump(priors.model_dump()), encoding="utf-8")
    golden = derive_golden(load_world(world_dir / "world.yaml"), manifest, priors)
    write_golden(golden, world_dir / "golden.jsonl")
    case = EvalCase(
        root=root,
        world_dir=world_dir,
        cassettes=Cassettes(root / "data" / "cassettes"),
        priors=priors,
        golden=golden,
    )
    _script_cassettes(
        case,
        include_distractor=include_distractor,
        drop_shared=drop_shared,
        wrong_objectives=wrong_objectives,
        scored_attribute=scored_attribute,
        scored_entity=scored_entity,
        extracted_value=extracted_value,
        scored_claim_only=scored_claim_only,
        extracted_ask_claim_value=extracted_ask_claim_value,
    )
    return case


@pytest.fixture
def eval_case(tmp_path: Path) -> EvalCase:
    return _build_case(tmp_path)


def _run(case: EvalCase, name: str = "eval.sqlite") -> ExtractionReport:
    return run_extraction_eval(case.world_dir, case.llm(), case.priors, case.root / name)


@pytest.mark.parametrize("stale_part", ["meta", "item"])
def test_eval_rejects_stale_golden_file(eval_case: EvalCase, stale_part: str) -> None:
    if stale_part == "meta":
        stale = eval_case.golden.model_copy(update={"ask_reply_cadence_days": 2})
    else:
        items = list(eval_case.golden.items)
        claims = list(items[0].claims)
        claims[0] = claims[0].model_copy(update={"value": "D"})
        items[0] = items[0].model_copy(update={"claims": tuple(claims)})
        stale = eval_case.golden.model_copy(update={"items": tuple(items)})
    write_golden(stale, eval_case.world_dir / "golden.jsonl")

    with pytest.raises(ValueError, match="digest world golden"):
        _run(eval_case)


def test_eval_scores_matching_claims_absences_models_and_realism(eval_case: EvalCase) -> None:
    report = _run(eval_case)

    assert (report.world, report.days) == ("tiny", 4)
    assert report.models["classify"] == "triage-v1"
    assert {model.strip() for model in report.models["extract"].split(",")} == {
        "extract-v1",
        "extract-v2",
    }
    assert report.models["render"] == "render-v1"
    assert report.by_type["risk"].model_dump() == {
        "golden": 1,
        "golden_matched": 1,
        "extracted": 0,
        "extracted_matched": 0,
        "precision": None,
        "recall": 1.0,
    }
    assert report.by_type["decision"].model_dump() == {
        "golden": 0,
        "golden_matched": 0,
        "extracted": 2,
        "extracted_matched": 1,
        "precision": 0.5,
        "recall": None,
    }
    assert report.by_type["spec_change"].model_dump() == {
        "golden": 1,
        "golden_matched": 1,
        "extracted": 1,
        "extracted_matched": 1,
        "precision": 1.0,
        "recall": 1.0,
    }
    assert report.by_type["ask"].golden_matched == 1
    assert report.by_type["ask"].extracted == 1
    assert report.by_type["fyi"].model_dump() == {
        "golden": 0,
        "golden_matched": 0,
        "extracted": 1,
        "extracted_matched": 0,
        "precision": 0.0,
        "recall": None,
    }
    assert report.thread_recall == 1.0
    assert report.type_agreement == pytest.approx(1 / 3)
    assert report.objective_misses == {"claims.value": 0, "owner": 0}
    assert (
        report.claims_golden,
        report.claims_found,
        report.claims_extracted,
        report.claims_extracted_matched,
    ) == (4, 4, 5, 4)
    assert report.claim_recall == 1.0
    assert report.claim_precision == 0.8
    assert report.entity_jaccard == pytest.approx(5 / 6)
    assert report.entity_jaccard_n == 2
    assert (report.entities_golden, report.entities_found, report.entity_recall) == (3, 3, 1.0)
    assert report.evidence_entries == 6
    assert report.citation_validity == 1.0
    assert report.drops == {
        **dict.fromkeys(DROP_FIELDS, 0),
        "due.format": 1,
        "entities.unresolved": 1,
    }
    assert (report.triage_golden_kept, report.triage_golden_total) == (3, 3)
    assert report.triage_by_event == {
        "A-REV": 0.9,
        "ASK-OWNER": 0.9,
        "B-QTY": 0.9,
        "DIST-NOISE": 0.9,
    }
    assert report.triage_dropped_golden == ()
    assert report.triage_distractors_kept == ("DIST-NOISE",)
    assert report.missed == ()
    assert len(report.unmatched) == 2
    assert any(
        "distractor" in item and f"CNOISE:{DISTRACTOR_ROOT}" in item for item in report.unmatched
    )
    assert (report.absence_tp, report.absence_fp, report.absence_fn) == (3, 1, 2)
    assert report.absence_false_alarm_rate == pytest.approx(1 / 4)
    assert report.absence_recall == pytest.approx(3 / 5)
    assert report.uncitable_tasks == (
        "G-CITE.release",
        "G-CITE.task",
        "G-UNCITE.task",
    )
    assert report.realism == {
        "messages_per_thread.mean": 2.0,
        "messages_per_thread.median": 2.0,
        "messages_per_thread.p90": 3.0,
        "chars_per_message.mean": pytest.approx(115 / 6),
        "chars_per_message.median": 17.5,
        "chars_per_message.p90": 40.0,
    }
    assert any(
        reason == "no reference statistics in the repo" for reason in report.not_run.values()
    )
    assert any("due" in metric for metric in report.not_run)
    assert any("status" in metric for metric in report.not_run)


def test_eval_reports_each_objective_field_miss(tmp_path: Path) -> None:
    case = _build_case(tmp_path, wrong_objectives=True)

    report = _run(case)

    assert report.objective_misses == {"claims.value": 1, "owner": 1}
    assert report.missed == ("B-QTY", "ASK-OWNER")
    assert report.by_type["risk"].golden_matched == 1
    assert report.by_type["spec_change"].golden_matched == 0
    assert report.by_type["ask"].golden_matched == 0


@pytest.mark.parametrize("attribute", ["status", "spec_param"])
def test_eval_matches_non_discriminating_claims_without_equal_values(
    tmp_path: Path, attribute: str
) -> None:
    case = _build_case(
        tmp_path,
        scored_attribute=attribute,
        golden_value="expected",
        extracted_value="observed",
        scored_claim_only=True,
    )

    report = _run(case)

    assert report.by_type["spec_change"].golden_matched == 1
    assert report.claims_found == 2
    assert report.claims_extracted_matched == 2
    assert report.claim_precision == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("attribute", "entity", "golden_value", "equal_value", "different_value"),
    [
        ("rev", "PN-1", "B", " b ", "C"),
        ("gate_date", "G-CITE", "2026-10-15", " 2026-10-15 ", "2026-10-16"),
        ("qty", "PN-1", "4", " 4 ", "5"),
        ("owner", "PN-1", "UOWNER", " uowner ", "UASKER"),
    ],
)
def test_eval_matches_discriminating_claims_only_with_equal_normalized_values(
    tmp_path: Path,
    attribute: str,
    entity: str,
    golden_value: str,
    equal_value: str,
    different_value: str,
) -> None:
    equal_case = _build_case(
        tmp_path / "equal",
        scored_attribute=attribute,
        scored_entity=entity,
        golden_value=golden_value,
        extracted_value=equal_value,
        scored_claim_only=True,
        golden_ask_claim_value="expected",
        extracted_ask_claim_value="observed",
    )
    different_case = _build_case(
        tmp_path / "different",
        scored_attribute=attribute,
        scored_entity=entity,
        golden_value=golden_value,
        extracted_value=different_value,
        scored_claim_only=True,
        golden_ask_claim_value="expected",
        extracted_ask_claim_value="observed",
    )

    equal_report = _run(equal_case)
    different_report = _run(different_case)

    assert equal_report.by_type["spec_change"].golden_matched == 1
    assert equal_report.by_type["ask"].golden_matched == 1
    assert equal_report.claims_found == 3
    assert different_report.by_type["spec_change"].golden_matched == 0
    assert different_report.by_type["ask"].golden_matched == 1
    assert different_report.claims_found == 2


def test_eval_objective_claim_misses_ignore_non_discriminating_values(tmp_path: Path) -> None:
    case = _build_case(
        tmp_path,
        wrong_objectives=True,
        golden_ask_claim_value="expected",
        extracted_ask_claim_value="observed",
    )

    report = _run(case)

    assert report.objective_misses == {"claims.value": 1, "owner": 1}


def test_eval_counts_fully_dropped_golden_threads_as_missed(tmp_path: Path) -> None:
    case = _build_case(tmp_path, drop_shared=True)

    report = _run(case)

    assert (report.triage_golden_kept, report.triage_golden_total) == (1, 3)
    assert report.triage_dropped_golden == ("A-REV", "B-QTY")
    assert report.missed == ("A-REV", "B-QTY")


def test_eval_rejects_a_golden_thread_without_triage(tmp_path: Path) -> None:
    case = _build_case(tmp_path, absent_golden_thread=True)

    with pytest.raises(ValueError, match=rf"A-REV|CENG:{re.escape(ABSENT_ROOT)}"):
        _run(case)


def test_eval_handles_no_distractors_or_absences(tmp_path: Path) -> None:
    case = _build_case(tmp_path, include_distractor=False, absences=False)

    report = _run(case)

    assert report.triage_distractors_kept == ()
    assert not any("distractor" in item for item in report.unmatched)
    assert (report.absence_tp, report.absence_fp, report.absence_fn) == (0, 0, 0)
    assert report.absence_false_alarm_rate is None
    assert report.absence_recall is None
    assert report.by_type["fyi"].recall is None
    assert report.entity_jaccard_n == 1


def _section(text: str, name: str) -> str:
    match = re.search(
        rf"^##+\s+[^\n]*{re.escape(name)}[^\n]*$\n(.*?)(?=^##+\s+|\Z)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    assert match is not None, name
    return match.group(1)


def _assert_rate_n(text: str, labels: tuple[str, ...], n: int) -> None:
    normalized_labels = tuple(re.sub(r"[_-]+", " ", label.lower()) for label in labels)
    row = next(
        (
            line
            for line in text.splitlines()
            if all(label in re.sub(r"[_-]+", " ", line.lower()) for label in normalized_labels)
        ),
        "",
    )
    if row:
        assert re.search(rf"(?:\bn\s*=\s*{n}\b|\|\s*{n}\s*\|)", row, re.IGNORECASE), row
        return

    assert len(normalized_labels) == 2, labels
    item_type, metric = normalized_labels
    lines = text.splitlines()
    for header_index, line in enumerate(lines):
        headers = [
            re.sub(r"[_-]+", " ", cell.strip().lower()) for cell in line.strip("|").split("|")
        ]
        if "type" not in headers or metric not in headers:
            continue
        rate_columns = [
            index for index, header in enumerate(headers) if header in {"precision", "recall"}
        ]
        n_columns = [index for index, header in enumerate(headers) if header == "n"]
        if len(rate_columns) != len(n_columns):
            continue
        n_column = n_columns[rate_columns.index(headers.index(metric))]
        type_column = headers.index("type")
        for candidate in lines[header_index + 2 :]:
            if not candidate.startswith("|"):
                break
            cells = [cell.strip() for cell in candidate.strip("|").split("|")]
            if (
                len(cells) == len(headers)
                and re.sub(r"[_-]+", " ", cells[type_column].lower()) == item_type
            ):
                assert re.fullmatch(rf"(?:n\s*=\s*)?{n}", cells[n_column], re.IGNORECASE), candidate
                return
    raise AssertionError(labels)


def test_write_report_has_deterministic_tables_and_rate_counts(eval_case: EvalCase) -> None:
    report = _run(eval_case)
    first = eval_case.root / "first.md"
    second = eval_case.root / "second.md"

    with time_machine.travel("2031-05-06"):
        write_report(report, first)
    with time_machine.travel("2042-07-08"):
        write_report(report, second)

    assert first.read_bytes() == second.read_bytes()
    text = first.read_text(encoding="utf-8")
    section_names = [
        "Extraction",
        "Claims",
        "Triage",
        "Absences",
        "Drops",
        "Realism",
        "Models",
        "Not run",
    ]
    for name in section_names:
        section = _section(text, name)
        assert len(re.findall(r"^\|.*\|\s*$", section, re.MULTILINE)) >= 2
        assert len(re.findall(r"^\|\s*:?-+", section, re.MULTILINE)) == 1
    for labels, n in [
        (("thread recall",), 3),
        (("type agreement",), 3),
        (("claim recall",), 4),
        (("claim precision",), 5),
        (("citation validity",), 6),
        (("entity jaccard",), 2),
        (("entity recall",), 3),
        (("triage recall",), 3),
        (("false-alarm rate",), 4),
        (("absence recall",), 5),
    ]:
        _assert_rate_n(text, labels, n)
    extraction = _section(text, "extraction")
    for item_type, precision_n, recall_n in [
        ("risk", 0, 1),
        ("decision", 2, 0),
        ("spec_change", 1, 1),
        ("ask", 1, 1),
        ("fyi", 1, 0),
    ]:
        _assert_rate_n(extraction, (item_type, "precision"), precision_n)
        _assert_rate_n(extraction, (item_type, "recall"), recall_n)
    assert "2031-05-06" not in text
    assert "2042-07-08" not in text


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "missing-cassette"])
def test_cli_eval_writes_report_or_reports_cassette_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    case = _build_case(tmp_path)
    cassette_root = case.cassettes.root if complete else tmp_path / "empty-cassettes"
    out = tmp_path / "report.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_WORLD", "tiny")
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassette_root))
    monkeypatch.delenv("DIGEST_DB", raising=False)

    result = runner.invoke(app, ["eval", "--world", "tiny", "--out", str(out)])

    if complete:
        assert result.exit_code == 0, result.output
        assert out.is_file()
        assert str(out) in result.output
    else:
        assert result.exit_code == 1, result.output
        assert "no cassette for key" in result.output
        assert "model classify" in result.output
        assert not out.exists()


def test_cli_eval_default_report_name_uses_world_last_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _build_case(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_WORLD", "tiny")
    monkeypatch.setenv("DIGEST_CASSETTES", str(case.cassettes.root))
    monkeypatch.delenv("DIGEST_DB", raising=False)

    with time_machine.travel("2042-07-08"):
        result = runner.invoke(app, ["eval", "--world", "tiny"])

    expected = tmp_path / "reports" / "eval-2026-10-15.md"
    assert result.exit_code == 0, result.output
    assert expected.is_file()
    assert "2042-07-08" not in result.output


def _set_cli_eval_environment(
    monkeypatch: pytest.MonkeyPatch, case: EvalCase, cassette_root: Path | None = None
) -> None:
    monkeypatch.chdir(case.root)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_WORLD", "tiny")
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassette_root or case.cassettes.root))
    monkeypatch.delenv("DIGEST_DB", raising=False)


def _replace_recorded_data(
    case: EvalCase, *, model: str, prompt_text: str, data: dict[str, Any]
) -> None:
    matches: list[Path] = []
    for path in case.cassettes.root.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        request = payload["request"]
        if request["model"] != model or not any(
            prompt_text in message["content"] for message in request["messages"]
        ):
            continue
        payload["response"]["data"] = data
        payload["response"]["text"] = json.dumps(data)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        matches.append(path)
    assert len(matches) == 1


def test_eval_counts_failed_calls_and_marks_the_golden_item_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _build_case(tmp_path)
    _replace_recorded_data(
        case,
        model="classify",
        prompt_text="When review?",
        data={"rationale": "invalid probability", "probability": 2},
    )

    report = _run(case)
    report_path = tmp_path / "failed-report.md"
    write_report(report, report_path)

    assert report.extract_failed == 4
    assert "ASK-OWNER" in report.missed
    triage = _section(report_path.read_text(encoding="utf-8"), "triage")
    _assert_rate_n(triage, ("extract", "fail"), 4)

    _set_cli_eval_environment(monkeypatch, case)
    cli_report = tmp_path / "failed-cli-report.md"
    result = runner.invoke(app, ["eval", "--world", "tiny", "--out", str(cli_report)])

    assert result.exit_code == 0, result.output
    assert re.search(r"extract(?:ion)?[ _-]fail(?:ed|ures?):\s*4\b", result.output, re.IGNORECASE)


def test_eval_rejects_a_golden_thread_that_was_never_run(tmp_path: Path) -> None:
    case = _build_case(tmp_path, absent_golden_thread=True)

    with pytest.raises(ValueError) as raised:
        _run(case)

    message = str(raised.value)
    assert "never run" in message
    assert "A-REV" in message
    assert f"CENG:{ABSENT_ROOT}" in message


def test_cli_eval_reports_a_manifest_event_that_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _build_case(tmp_path)
    manifest = _manifest(include_distractor=True, absent_golden_thread=False)
    threads = dict(manifest.threads)
    del threads["ASK-OWNER"]
    write_manifest(Manifest(threads=threads), case.world_dir / "manifest.json")
    _set_cli_eval_environment(monkeypatch, case)

    result = runner.invoke(app, ["eval", "--world", "tiny", "--out", str(tmp_path / "x.md")])

    assert result.exit_code == 1
    assert "ASK-OWNER" in result.output
    assert "manifest" in result.output.lower()
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_eval_reports_no_rendered_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _build_case(tmp_path)
    manifest = _manifest(include_distractor=True, absent_golden_thread=False)
    human_threads = {
        event_id: ref.model_copy(update={"authored": "human"})
        for event_id, ref in manifest.threads.items()
    }
    write_manifest(Manifest(threads=human_threads), case.world_dir / "manifest.json")
    _set_cli_eval_environment(monkeypatch, case)
    report_path = tmp_path / "no-render.md"

    result = runner.invoke(app, ["eval", "--world", "tiny", "--out", str(report_path)])

    assert result.exit_code == 0, result.output
    text = report_path.read_text(encoding="utf-8")
    realism = _section(text, "realism")
    assert len(re.findall(r"^\|.*\|\s*$", realism, re.MULTILINE)) == 2
    assert any(
        "realism" in line.lower() and "no rendered threads" in line.lower()
        for line in _section(text, "not run").splitlines()
    )


def test_stale_golden_message_distinguishes_labels_from_cadence(eval_case: EvalCase) -> None:
    items = list(eval_case.golden.items)
    claims = list(items[0].claims)
    claims[0] = claims[0].model_copy(update={"value": "D"})
    items[0] = items[0].model_copy(update={"claims": tuple(claims)})
    stale = eval_case.golden.model_copy(update={"items": tuple(items)})
    write_golden(stale, eval_case.world_dir / "golden.jsonl")

    with pytest.raises(ValueError) as raised:
        _run(eval_case)

    message = str(raised.value).lower()
    assert "labels differ" in message or "differs from the labels derived" in message
    assert "cadence" not in message

    stale = eval_case.golden.model_copy(update={"ask_reply_cadence_days": 2})
    write_golden(stale, eval_case.world_dir / "golden.jsonl")

    with pytest.raises(ValueError) as raised:
        _run(eval_case)

    message = str(raised.value)
    assert re.search(r"ask_reply_cadence_days[^\n]*2[^\n]*1", message)


def test_cli_eval_removes_all_store_files_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _build_case(tmp_path)
    _set_cli_eval_environment(monkeypatch, case)
    store_path = tmp_path / "data" / "eval-tiny.sqlite"
    sidecars = [Path(f"{store_path}-wal"), Path(f"{store_path}-shm")]
    store_path.write_bytes(b"leftover store")
    for sidecar in sidecars:
        sidecar.write_bytes(b"leftover sidecar")

    original_run = cli_module.run_extraction_eval
    sidecars_existed_at_run_start: bool | None = None

    def run_after_cleanup(
        world_dir: Path, llm: LLM, priors: Priors, path: Path
    ) -> ExtractionReport:
        nonlocal sidecars_existed_at_run_start
        sidecars_existed_at_run_start = any(sidecar.exists() for sidecar in sidecars)
        return original_run(world_dir, llm, priors, path)

    monkeypatch.setattr(cli_module, "run_extraction_eval", run_after_cleanup)
    result = runner.invoke(app, ["eval", "--world", "tiny", "--out", str(tmp_path / "fresh.md")])

    assert result.exit_code == 0, result.output
    assert sidecars_existed_at_run_start is False
    assert not any(path.exists() for path in sidecars)

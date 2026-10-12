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

from digest.cli import app
from digest.eval.extraction import ExtractionReport, run_extraction_eval, write_report
from digest.extract.prompts import EXTRACT_PROMPT_VERSION, extract_request, triage_request
from digest.extract.schema import Extraction, TriageOutput
from digest.extract.verify import DROP_FIELDS
from digest.llm import LLMRequest, LLMResponse
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
DISTRACTOR_ROOT = "1791828000.000004"
DISTRACTOR_REPLY = "1791828060.000005"
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


def _world(*, include_distractor: bool, absences: bool) -> dict[str, Any]:
    ask_day = 2 if absences else 1
    events: list[dict[str, Any]] = [
        {
            "id": "A-REV",
            "day": 1,
            "channel": "CENG",
            "poster": "UASKER",
            "kind": "risk",
            "summary": "The drive revision is B.",
            "claims": [{"entity": "PN-1", "attribute": "rev", "value": "B"}],
        },
        {
            "id": "B-QTY",
            "day": 1,
            "channel": "CENG",
            "poster": "UASKER",
            "kind": "spec_change",
            "summary": "The drive quantity is four and the gate is open.",
            "claims": [
                {"entity": "PN-1", "attribute": "rev", "value": "B"},
                {"entity": "PN-1", "attribute": "qty", "value": "4"},
                {"entity": "G-CITE", "attribute": "status", "value": "open"},
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
                        }
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


def _events(include_distractor: bool) -> list[Event]:
    rows = [
        _event("CENG", "eng", SHARED_ROOT, "1234567890", "UASKER"),
        _event(
            "CENG",
            "eng",
            SHARED_REPLY,
            "abcdefghij",
            "UASKER",
            thread_ts=SHARED_ROOT,
        ),
        _event("CENG", "eng", ASK_ROOT, "When review?", "UASKER"),
    ]
    if include_distractor:
        rows.extend(
            [
                _event("CNOISE", "social", DISTRACTOR_ROOT, "ABCDEFGHIJ", "UOWNER"),
                _event(
                    "CNOISE",
                    "social",
                    DISTRACTOR_REPLY,
                    "klmnopqrst",
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
        "due": None,
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


def _outputs(*, wrong_objectives: bool) -> dict[str, list[dict[str, Any]]]:
    quantity = "5" if wrong_objectives else "4"
    ask_owner = "UASKER" if wrong_objectives else "UOWNER"
    later = _extracted_item(
        "spec_change",
        SHARED_REPLY,
        "abcdefghij",
        entities=["PN-1", "G-CITE"],
        claims=[
            _claim("PN-1", "rev", "B", SHARED_REPLY),
            _claim("PN-1", "qty", quantity, SHARED_REPLY),
            _claim("G-CITE", "status", "open", SHARED_REPLY),
        ],
    )
    earlier = _extracted_item(
        "risk",
        SHARED_ROOT,
        "1234567890",
        entities=["PN-1", "CONTEXT"],
        claims=[_claim("PN-1", "rev", " B ", SHARED_ROOT)],
    )
    noise = _extracted_item(
        "decision",
        SHARED_ROOT,
        "1234567890",
        entities=["PN-1"],
        claims=[_claim("PN-1", "qty", "5", SHARED_ROOT)],
    )
    ask = _extracted_item(
        "fyi",
        ASK_ROOT,
        "When review?",
        owner=ask_owner,
    )
    distractor = _extracted_item("fyi", DISTRACTOR_ROOT, "ABCDEFGHIJ", entities=["CONTEXT"])
    return {
        f"CENG:{SHARED_ROOT}": [later, earlier, noise],
        f"CENG:{ASK_ROOT}": [ask],
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
) -> None:
    world = load_world(case.world_dir / "world.yaml")
    purposes = {channel.id: channel.purpose for channel in world.channels}
    outputs = _outputs(wrong_objectives=wrong_objectives)
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
                "extract-v1",
            )
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
) -> EvalCase:
    world_dir = root / "data" / "world" / "tiny"
    world_dir.mkdir(parents=True)
    raw_world = _world(include_distractor=include_distractor, absences=absences)
    (world_dir / "world.yaml").write_text(yaml.safe_dump(raw_world), encoding="utf-8")
    rows = _events(include_distractor)
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
    )
    return case


@pytest.fixture
def eval_case(tmp_path: Path) -> EvalCase:
    return _build_case(tmp_path)


def _run(case: EvalCase, name: str = "eval.sqlite") -> ExtractionReport:
    return run_extraction_eval(case.world_dir, case.llm(), case.priors, case.root / name)


def test_eval_rejects_stale_golden_file(eval_case: EvalCase) -> None:
    stale = eval_case.golden.model_copy(update={"ask_reply_cadence_days": 2})
    write_golden(stale, eval_case.world_dir / "golden.jsonl")

    with pytest.raises(ValueError, match="digest world golden"):
        _run(eval_case)


def test_eval_scores_matching_claims_absences_models_and_realism(eval_case: EvalCase) -> None:
    report = _run(eval_case)

    assert (report.world, report.days) == ("tiny", 4)
    assert report.models == {"classify": "triage-v1", "extract": "extract-v1"}
    assert report.by_type["risk"].model_dump() == {
        "golden": 1,
        "golden_matched": 1,
        "extracted": 1,
        "extracted_matched": 1,
        "precision": 1.0,
        "recall": 1.0,
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
    assert report.by_type["ask"].extracted == 0
    assert report.by_type["fyi"].model_dump() == {
        "golden": 0,
        "golden_matched": 0,
        "extracted": 2,
        "extracted_matched": 1,
        "precision": 0.5,
        "recall": None,
    }
    assert report.thread_recall == 1.0
    assert report.type_agreement == pytest.approx(2 / 3)
    assert report.objective_misses == {"claims.value": 0, "owner": 0}
    assert (
        report.claims_golden,
        report.claims_found,
        report.claims_extracted,
        report.claims_extracted_matched,
    ) == (4, 4, 5, 4)
    assert report.claim_recall == 1.0
    assert report.claim_precision == 0.8
    assert report.entity_jaccard == 0.75
    assert report.entity_jaccard_n == 2
    assert (report.entities_golden, report.entities_found, report.entity_recall) == (3, 3, 1.0)
    assert report.evidence_entries == 5
    assert report.citation_validity == 1.0
    assert report.drops == dict.fromkeys(DROP_FIELDS, 0)
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
    assert any("golden" in item and "#2" in item for item in report.unmatched)
    assert any(
        "distractor" in item and f"CNOISE:{DISTRACTOR_ROOT}" in item for item in report.unmatched
    )
    assert (report.absence_tp, report.absence_fp, report.absence_fn) == (2, 1, 1)
    assert report.absence_false_alarm_rate == pytest.approx(1 / 3)
    assert report.absence_recall == pytest.approx(2 / 3)
    assert report.uncitable_tasks == ("G-UNCITE.task",)
    assert len(report.realism) == 6
    assert sorted(report.realism.values()) == [2.0, 2.0, 2.0, 10.0, 10.0, 10.0]
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
    assert report.entity_jaccard_n == 2


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##+\s+{re.escape(heading)}\s*$\n(.*?)(?=^##+\s+|\Z)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    assert match is not None, heading
    return match.group(1)


def _assert_rate_n(text: str, label: str, n: int) -> None:
    row = next((line for line in text.splitlines() if label.lower() in line.lower()), "")
    assert row, label
    assert re.search(rf"(?:\bn\s*=\s*{n}\b|\|\s*{n}\s*\|)", row, re.IGNORECASE), row


def test_write_report_has_deterministic_tables_and_rate_counts(eval_case: EvalCase) -> None:
    report = _run(eval_case)
    first = eval_case.root / "first.md"
    second = eval_case.root / "second.md"

    with time_machine.travel("2031-05-06"):
        write_report(report, first)
        write_report(report, second)

    assert first.read_bytes() == second.read_bytes()
    text = first.read_text(encoding="utf-8")
    headings = [
        "Extraction by type",
        "Claims",
        "Triage",
        "Absences",
        "Drops",
        "Realism",
        "Models",
        "Not run",
    ]
    for heading in headings:
        section = _section(text, heading)
        assert len(re.findall(r"^\|.*\|\s*$", section, re.MULTILINE)) >= 2
        assert len(re.findall(r"^\|\s*:?-+", section, re.MULTILINE)) == 1
    for label, n in [
        ("Thread recall", 3),
        ("Type agreement", 3),
        ("Claim recall", 4),
        ("Claim precision", 5),
        ("Citation validity", 5),
        ("Entity Jaccard", 2),
        ("Entity recall", 3),
        ("Triage recall", 3),
        ("False-alarm rate", 3),
        ("Absence recall", 3),
    ]:
        _assert_rate_n(text, label, n)
    assert "2031-05-06" not in text


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

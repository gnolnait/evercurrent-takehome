import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from functools import partialmethod
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from typer.testing import CliRunner

from digest.cli import app
from digest.extract.normalize import normalize_space, resolve_entity, resolve_person
from digest.extract.prompts import (
    EXTRACT_PROMPT_VERSION,
    EXTRACT_SYSTEM,
    TRIAGE_SYSTEM,
    extract_request,
    triage_request,
)
from digest.extract.schema import (
    ExtractedClaim,
    ExtractedEvidence,
    ExtractedItem,
    ExtractedTask,
    Extraction,
    TriageOutput,
)
from digest.extract.stage import run_extract
from digest.extract.triage import triage
from digest.extract.verify import DROP_FIELDS, verify
from digest.llm import LLMOutputError, LLMRequest, LLMResponse, strict_schema
from digest.llm.cassette import CassetteMiss, Cassettes, request_key
from digest.llm.client import LLM
from digest.models import (
    Claim,
    Event,
    Evidence,
    Glossary,
    GlossaryEntity,
    GlossaryPerson,
    Item,
    TaskLink,
    TriageDecision,
)
from digest.priors import AbsencePriors, Priors, TriagePriors
from digest.project import ChecklistTask, Gate, PhaseWindow, ProjectConfig
from digest.settings import Settings
from digest.store import Store, ThreadDelta

ROOT = "1760000000.000001"
REPLY = "1760000000.000002"
LATER = "1760000001.000002"
AS_OF = datetime(2026, 10, 14, 6, 59, 59, tzinfo=UTC)
ZONE = ZoneInfo("America/Los_Angeles")
CHANNEL_PURPOSES = {"C1": "Mechanical", "C2": "Program"}
runner = CliRunner()


def event(ts: str = ROOT, **changes: Any) -> Event:
    values: dict[str, Any] = {
        "source": "replay",
        "channel_id": "C1",
        "channel_name": "eng-mech",
        "thread_ts": ROOT,
        "ts": ts,
        "event_ts": ts,
        "user_id": "U1",
        "text": "PN-31010 Rev C released complete by Friday.",
        "files": ("PN-31010-C.pdf",),
        "permalink": f"https://kestrel.test/C1/{ts}",
    }
    return Event(**(values | changes))


def delta(*changed: Event, root: Event | None = None, last_ts: str | None = None) -> ThreadDelta:
    rows = changed or ((root,) if root is not None else (event(REPLY),))
    return ThreadDelta(
        thread_id=f"C1:{ROOT}",
        channel_id="C1",
        thread_ts=ROOT,
        last_ts=last_ts or max(row.event_ts for row in rows),
        since_ts=None,
        root=root,
        changed=tuple(rows),
    )


def glossary(
    entity_id: str = "PN-31010", alias: str = "main harness", person_alias: str = "Nadia"
) -> Glossary:
    return Glossary(
        entities=(
            GlossaryEntity(
                id=entity_id,
                type="harness",
                description="Main power harness",
                aliases=(alias,),
            ),
        ),
        people=(
            GlossaryPerson(
                user_id="U1",
                name="Nadia Brenner",
                role="ME",
                aliases=(person_alias, "@nadia"),
                authority="owner_stated",
            ),
            GlossaryPerson(
                user_id="B1",
                name="release_bot",
                role=None,
                aliases=("releases",),
                authority="plm_bot",
            ),
        ),
    )


def project() -> ProjectConfig:
    return ProjectConfig(
        project_id="k2",
        name="Kestrel K2",
        phases=(PhaseWindow(phase="EVT", from_date=date(2026, 1, 1)),),
        gates=(
            Gate(
                id="G-OLD",
                name="Old gate",
                date=date(2026, 10, 12),
                buffer_days=0,
                checklist=(
                    ChecklistTask(
                        id="old.task", task="Old task", owner="ME", expected_cadence_days=1
                    ),
                ),
            ),
            Gate(
                id="G-TODAY",
                name="Today's gate",
                date=date(2026, 10, 13),
                buffer_days=0,
                checklist=(
                    ChecklistTask(
                        id="today.task",
                        task="Complete today's review",
                        owner="EE",
                        expected_cadence_days=1,
                    ),
                ),
            ),
            Gate(
                id="G-NEXT",
                name="Next gate",
                date=date(2026, 10, 14),
                buffer_days=0,
                checklist=(
                    ChecklistTask(
                        id="next.task",
                        task="Complete next review",
                        owner="EM",
                        expected_cadence_days=1,
                    ),
                ),
            ),
        ),
    )


def priors(ratio: float = 3) -> Priors:
    return Priors(
        triage=TriagePriors(miss_cost_ratio=ratio),
        absence=AbsencePriors(ask_reply_cadence_days=2),
    )


def extracted_item(**changes: Any) -> ExtractedItem:
    values: dict[str, Any] = {
        "rationale": "The message reports a released revision.",
        "item_id": None,
        "type": "bom_change",
        "title": "Harness revision released",
        "summary": "PN-31010 moved to Rev C.",
        "entities": ["main harness"],
        "owner": None,
        "due": None,
        "affected_roles": ["EE", "supply_chain"],
        "severity": "high",
        "confidence": "high",
        "status": "open",
        "evidence": [ExtractedEvidence(message_ts=ROOT, quote="PN-31010 Rev C released")],
        "claims": [],
        "tasks": [],
    }
    return ExtractedItem(**(values | changes))


def stored_item(**changes: Any) -> Item:
    values: dict[str, Any] = {
        "item_id": "I1",
        "thread_id": f"C1:{ROOT}",
        "type": "risk",
        "title": "Old title",
        "summary": "Old summary",
        "entities": ("PN-31010",),
        "owner": "U1",
        "due": date(2026, 10, 20),
        "affected_roles": ("ME",),
        "severity": "low",
        "confidence": "medium",
        "evidence": (Evidence(message_ts=ROOT, quote="old quote", permalink="https://old"),),
        "first_seen": ROOT,
        "last_updated": ROOT,
        "claims": (),
        "tasks": (),
        "status": "open",
        "resolved_by": None,
    }
    return Item(**(values | changes))


def replay_llm(path: Path) -> LLM:
    return LLM(None, Cassettes(path), "replay")


def response(data: dict[str, Any]) -> LLMResponse:
    return LLMResponse(text=json.dumps(data), data=data, backend="test", model="test-model")


def schema_request(request: LLMRequest, model: type[Extraction] | type[TriageOutput]) -> LLMRequest:
    if request.output_schema is not None:
        return request
    return request.model_copy(update={"output_schema": model.model_json_schema()})


def citable_for(delta_row: ThreadDelta) -> tuple[Event, ...]:
    rows = ([delta_row.root] if delta_row.root is not None else []) + list(delta_row.changed)
    by_ts = {row.ts: row for row in rows if not row.deleted}
    return tuple(by_ts[key] for key in sorted(by_ts))


def put_stage_cassettes(
    root: Path,
    store: Store,
    *,
    triage_data: dict[str, Any],
    extraction_data: dict[str, Any] | None = None,
    as_of: datetime = AS_OF,
    channel_purposes: dict[str, str] | None = None,
) -> list[LLMRequest]:
    requests: list[LLMRequest] = []
    cassettes = Cassettes(root)
    for delta_row in store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", as_of):
        citable = citable_for(delta_row)
        triage_sent = schema_request(
            triage_request(
                delta_row,
                (channel_purposes or {}).get(delta_row.channel_id),
                citable,
            ),
            TriageOutput,
        )
        cassettes.put(request_key(triage_sent), triage_sent, response(triage_data))
        requests.append(triage_sent)
        if extraction_data is not None:
            extraction_sent = schema_request(
                extract_request(
                    delta_row,
                    glossary(),
                    project(),
                    as_of.astimezone(ZONE).date(),
                    store.items_for_thread(delta_row.thread_id),
                    citable,
                ),
                Extraction,
            )
            cassettes.put(request_key(extraction_sent), extraction_sent, response(extraction_data))
            requests.append(extraction_sent)
    return requests


def extraction_data(*items: ExtractedItem) -> dict[str, Any]:
    return Extraction(rationale="Extract the supported change.", items=list(items)).model_dump(
        mode="json"
    )


def walk_schema(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_schema(child)


@pytest.mark.parametrize("model", [Extraction, TriageOutput])
def test_output_schema_obeys_strict_provider_limits(
    model: type[Extraction] | type[TriageOutput],
) -> None:
    schema = strict_schema(model.model_json_schema())
    for node in walk_schema(schema):
        if "properties" in node:
            assert set(node["required"]) == set(node["properties"])
        assert not isinstance(node.get("additionalProperties"), dict)
        assert node.get("additionalProperties") is not True
        assert node.get("minItems", 0) <= 1
        assert "minimum" not in node
        assert "maximum" not in node
        assert "exclusiveMinimum" not in node
        assert "exclusiveMaximum" not in node
        assert node.get("format") not in {"date", "date-time"}


@pytest.mark.parametrize("probability", [-0.01, 1.01])
def test_triage_output_rejects_probability_outside_unit_interval(probability: float) -> None:
    with pytest.raises(ValidationError, match="probability"):
        TriageOutput(rationale="Boundary check", probability=probability)


@pytest.mark.parametrize("probability", [0.0, 1.0])
def test_triage_output_accepts_probability_boundaries(probability: float) -> None:
    assert (
        TriageOutput(rationale="Boundary check", probability=probability).probability == probability
    )


@pytest.mark.parametrize(
    ("ratio", "probability", "threshold", "kept"),
    [(3, 0.24, 0.25, False), (3, 0.25, 0.25, True), (9, 0.1, 0.1, True)],
)
def test_triage_uses_classify_threshold_and_decision_metadata(
    tmp_path: Path, ratio: float, probability: float, threshold: float, kept: bool
) -> None:
    row = event()
    delta_row = delta(row, root=row)
    request = schema_request(triage_request(delta_row, "Mechanical work", (row,)), TriageOutput)
    Cassettes(tmp_path).put(
        request_key(request),
        request,
        response({"rationale": "Contains a release.", "probability": probability}),
    )
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)

    decision = triage(
        replay_llm(tmp_path), delta_row, (row,), "Mechanical work", priors(ratio), now
    )

    assert request.model == "classify"
    assert decision == TriageDecision(
        thread_id=delta_row.thread_id,
        last_ts=delta_row.last_ts,
        prompt_version=EXTRACT_PROMPT_VERSION,
        model="classify",
        probability=probability,
        threshold=threshold,
        kept=kept,
        rationale="Contains a release.",
        decided_at=now,
    )


def test_triage_request_carries_only_citable_thread_content_and_probability_rule() -> None:
    root = event(text="ROOT-PROJECT-CHANGE", user_id="U-ROOT")
    changed = event(REPLY, text="CHANGED-PROJECT-ASK", user_id="U-CHANGED")
    deleted = event(LATER, text="DELETED-PRIVATE-TEXT", user_id="U-DELETED", deleted=True)
    request = triage_request(
        delta(changed, deleted, root=root),
        "PURPOSE-MECHANICAL-WORK",
        (root, changed),
    )

    assert request.system == TRIAGE_SYSTEM
    assert len(request.messages) == 1
    text = request.messages[0].content
    for value in (
        "PURPOSE-MECHANICAL-WORK",
        "ROOT-PROJECT-CHANGE",
        ROOT,
        "U-ROOT",
        "CHANGED-PROJECT-ASK",
        REPLY,
        "U-CHANGED",
    ):
        assert value in text
    assert "DELETED-PRIVATE-TEXT" not in text
    assert LATER not in text
    assert "U-DELETED" not in text
    assert re.search(r"probabil", f"{request.system}\n{text}", re.IGNORECASE)


def test_triage_replay_rejects_invalid_probability_without_retry(tmp_path: Path) -> None:
    row = event()
    delta_row = delta(row, root=row)
    request = schema_request(triage_request(delta_row, None, (row,)), TriageOutput)
    Cassettes(tmp_path).put(
        request_key(request),
        request,
        response({"rationale": "Invalid recording.", "probability": 1.5}),
    )
    with pytest.raises(LLMOutputError, match="llm_output_invalid"):
        triage(replay_llm(tmp_path), delta_row, (row,), None, priors(), AS_OF)


def test_extract_request_orders_sections_filters_rows_and_states_rules() -> None:
    root = event(text="ROOT-MARKER", user_id="U-ROOT", files=("root.step",))
    later = event("1760000000.000003", text="LATER-MARKER", user_id="U-LATER", files=("later.txt",))
    earlier = event(REPLY, text="EARLIER-MARKER", user_id="U-EARLIER", files=("early.csv",))
    deleted = event("1760000000.000004", text="DELETED-MARKER", deleted=True)
    existing = stored_item(
        item_id="ITEM-EXISTING-MARKER",
        owner=None,
        due=None,
        claims=(),
        tasks=(),
        title="EXISTING-MARKER",
    )
    request = extract_request(
        delta(later, earlier, deleted, root=root),
        glossary(alias="GLOSSARY-MARKER"),
        project(),
        date(2026, 10, 13),
        (existing,),
        (deleted, later, root, earlier),
    )

    assert request.model == "extract"
    assert request.system == EXTRACT_SYSTEM
    assert len(request.messages) == 1
    text = request.messages[0].content
    assert text.index("GLOSSARY-MARKER") < min(text.index("today.task"), text.index("next.task"))
    assert max(text.index("today.task"), text.index("next.task")) < text.index("EXISTING-MARKER")
    ordered_markers = ["EXISTING-MARKER", "ROOT-MARKER", "EARLIER-MARKER", "LATER-MARKER"]
    assert [text.index(marker) for marker in ordered_markers] == sorted(
        text.index(marker) for marker in ordered_markers
    )
    for value in (
        "ITEM-EXISTING-MARKER",
        ROOT,
        "U-ROOT",
        "root.step",
        REPLY,
        "U-EARLIER",
        "early.csv",
        "1760000000.000003",
        "U-LATER",
        "later.txt",
    ):
        assert value in text
    assert "old.task" not in text
    assert "DELETED-MARKER" not in text
    assert "G-OLD" not in text
    assert text.count("ROOT-MARKER") == 1
    system = request.system.lower()
    combined = f"{system}\n{text.lower()}"
    assert "golden" not in combined
    assert "scenario" not in combined
    for word in ("new", "item_id", "verbatim", "null", "owner", "due", "rationale"):
        assert word in system
    assert re.search(r"ask.{0,120}(owner|asked)|(owner|asked).{0,120}ask", system, re.DOTALL)
    assert re.search(r"done.*complete|complete.*done", system, re.DOTALL)
    assert re.search(r"(question|roll-up).{0,160}\bnot\b|\bnot\b.{0,160}(question|roll-up)", system)
    assert re.search(r"tasks?.*list", system, re.DOTALL)
    assert any(word in system for word in ("run", "result", "review", "sign-off", "received"))


def test_extract_request_handles_missing_root_and_omits_world_event_ids() -> None:
    reply = event(REPLY, text="Only changed message")
    request = extract_request(
        delta(reply, root=None), glossary(), project(), date(2026, 10, 13), (), (reply,)
    )
    text = request.messages[0].content
    assert "Only changed message" in text


def test_extract_request_lists_root_once_when_first_revision_includes_it() -> None:
    root = event(text="FIRST-REVISION-ROOT")
    request = extract_request(
        delta(root, root=root), glossary(), project(), date(2026, 10, 13), (), (root,)
    )
    assert request.messages[0].content.count("FIRST-REVISION-ROOT") == 1


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("  alpha\t beta\n gamma  ", "alpha beta gamma"),
        ("\r\n", ""),
        ("already single", "already single"),
    ],
)
def test_normalize_space_examples(raw: str, normalized: str) -> None:
    assert normalize_space(raw) == normalized


@pytest.mark.parametrize(
    ("raw", "resolved"),
    [
        ("pn-31010", "PN-31010"),
        ("MAIN HARNESS", "PN-31010"),
        ("PN-31010-C", "PN-31010"),
        ("PN-31010 Rev C", "PN-31010"),
        ("PN-31010 rev C", "PN-31010"),
        ("PN-31010 Rev. C", "PN-31010"),
        ("PN-31010 Rev cc", None),
        ("prefix PN-31010", None),
        ("unknown", None),
    ],
)
def test_resolve_entity_examples(raw: str, resolved: str | None) -> None:
    assert resolve_entity(raw, glossary()) == resolved


@pytest.mark.parametrize(
    ("raw", "resolved"),
    [
        ("<@U1>", "U1"),
        ("u1", "U1"),
        ("NADIA", "U1"),
        ("nadia brenner", "U1"),
        ("<@U999>", None),
        ("Nadia and Tobias", None),
    ],
)
def test_resolve_person_examples(raw: str, resolved: str | None) -> None:
    assert resolve_person(raw, glossary()) == resolved


@st.composite
def glossary_cases(draw: st.DrawFn) -> tuple[Glossary, str, tuple[str, ...]]:
    token = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=2, max_size=8)
    entity_id = draw(token.map(lambda value: f"PN-{value}"))
    aliases = draw(
        st.lists(
            token.map(lambda value: f"alias {value}"),
            min_size=1,
            max_size=3,
            unique_by=str.casefold,
        )
    )
    return glossary(entity_id=entity_id, alias=aliases[0]), entity_id, tuple(aliases)


@settings(max_examples=30, derandomize=True)
@given(glossary_cases())
def test_every_entity_id_and_alias_resolves(case: tuple[Glossary, str, tuple[str, ...]]) -> None:
    generated, entity_id, aliases = case
    generated = generated.model_copy(
        update={"entities": (generated.entities[0].model_copy(update={"aliases": aliases}),)}
    )
    for value in (entity_id, *aliases):
        assert resolve_entity(value.swapcase(), generated) == entity_id


@settings(max_examples=30, derandomize=True)
@given(
    glossary_cases(),
    st.sampled_from(["-", " Rev ", " rev ", " Rev. "]),
    st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
)
def test_every_entity_revision_form_resolves(
    case: tuple[Glossary, str, tuple[str, ...]], separator: str, revision: str
) -> None:
    generated, entity_id, _ = case
    assert resolve_entity(f"{entity_id}{separator}{revision}", generated) == entity_id


@settings(max_examples=30, derandomize=True)
@given(glossary_cases(), st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=20))
def test_non_entity_strings_do_not_resolve(
    case: tuple[Glossary, str, tuple[str, ...]], candidate: str
) -> None:
    generated, entity_id, aliases = case
    forbidden = {entity_id.casefold(), *(alias.casefold() for alias in aliases)}
    for separator in ("-", " rev ", " rev. "):
        forbidden.update(
            f"{value}{separator}{letter.casefold()}"
            for value in tuple(forbidden)
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )
    if candidate.casefold() not in forbidden:
        assert resolve_entity(candidate, generated) is None


def verify_context() -> tuple[ThreadDelta, dict[str, Event], dict[str, ChecklistTask]]:
    root = event()
    reply = event(REPLY, user_id="UNKNOWN", text="Second evidence says review complete.")
    return (
        delta(root, reply, root=root),
        {ROOT: root, REPLY: reply},
        {
            "today.task": ChecklistTask(
                id="today.task",
                task="Complete today's review",
                owner="EE",
                expected_cadence_days=1,
            )
        },
    )


def drop_case(
    field: str,
) -> tuple[Extraction, ThreadDelta, dict[str, Event], dict[str, ChecklistTask], dict[str, Item]]:
    delta_row, citable, tasks = verify_context()
    item = extracted_item()
    existing: dict[str, Item] = {}
    if field == "evidence.unknown_message":
        item = extracted_item(evidence=[ExtractedEvidence(message_ts=LATER, quote="missing")])
    elif field == "evidence.quote":
        item = extracted_item(evidence=[ExtractedEvidence(message_ts=ROOT, quote="not present")])
    elif field == "item_id.unknown":
        item = extracted_item(item_id="missing")
    elif field == "entities.unresolved":
        item = extracted_item(entities=["not an entity"])
    elif field == "claims.entity":
        item = extracted_item(
            claims=[ExtractedClaim(entity="missing", attribute="rev", value="C", message_ts=ROOT)]
        )
    elif field == "claims.message":
        item = extracted_item(
            claims=[ExtractedClaim(entity="PN-31010", attribute="rev", value="C", message_ts=REPLY)]
        )
    elif field == "owner.unresolved":
        item = extracted_item(owner="Nobody")
    elif field == "due.format":
        item = extracted_item(due="Friday")
    elif field == "tasks.unknown":
        item = extracted_item(tasks=[ExtractedTask(task="missing", effect="done", message_ts=ROOT)])
    elif field == "tasks.message":
        item = extracted_item(
            tasks=[ExtractedTask(task="today.task", effect="done", message_ts=REPLY)]
        )
    else:
        old_claims = (
            (
                Claim(
                    entity="PN-31010",
                    attribute="rev",
                    value="B",
                    message_ts=ROOT,
                    authority="owner_stated",
                ),
            )
            if field == "claims.stale"
            else ()
        )
        old_tasks = (
            (TaskLink(task="today.task", effect="evidence", message_ts=ROOT),)
            if field == "tasks.stale"
            else ()
        )
        existing_item = stored_item(claims=old_claims, tasks=old_tasks)
        existing = {existing_item.item_id: existing_item}
        item = extracted_item(
            item_id=existing_item.item_id,
            evidence=[ExtractedEvidence(message_ts=REPLY, quote="Second evidence")],
        )
    extraction = Extraction(rationale="Verify fields.", items=[item])
    return extraction, delta_row, citable, tasks, existing


@pytest.mark.parametrize(
    ("quote", "message_ts", "kept"),
    [
        ("", ROOT, False),
        (" \t\n ", ROOT, False),
        ("PN-31010   Rev C\nreleased", ROOT, True),
        ("Second evidence", ROOT, False),
    ],
)
def test_verify_quote_matches_only_its_cited_message_after_space_normalization(
    quote: str, message_ts: str, kept: bool
) -> None:
    delta_row, citable, tasks = verify_context()
    result = verify(
        Extraction(
            rationale="Check quote.",
            items=[
                extracted_item(evidence=[ExtractedEvidence(message_ts=message_ts, quote=quote)])
            ],
        ),
        delta_row,
        citable,
        glossary(),
        tasks,
        {},
    )
    assert bool(result.items) is kept
    assert result.drops == dict.fromkeys(DROP_FIELDS, 0) | {"evidence.quote": 0 if kept else 1}


@pytest.mark.parametrize("field", DROP_FIELDS)
def test_verify_counts_each_drop_and_preserves_valid_items(field: str) -> None:
    extraction, delta_row, citable, tasks, existing = drop_case(field)
    result = verify(extraction, delta_row, citable, glossary(), tasks, existing)
    assert result.drops == dict.fromkeys(DROP_FIELDS, 0) | {field: 1}
    if field in {"evidence.unknown_message", "evidence.quote", "item_id.unknown"}:
        assert result.items == ()
    else:
        (item,) = result.items
        projections = {
            "entities.unresolved": item.entities == (),
            "claims.entity": item.claims == (),
            "claims.message": item.claims == (),
            "owner.unresolved": item.owner is None,
            "due.format": item.due is None,
            "tasks.unknown": item.tasks == (),
            "tasks.message": item.tasks == (),
            "evidence.stale": all(entry.message_ts != ROOT for entry in item.evidence),
            "claims.stale": item.claims == (),
            "tasks.stale": item.tasks == (),
        }
        assert projections[field]


def test_verify_new_item_sets_ids_times_permalink_authority_and_nulls() -> None:
    delta_row, citable, tasks = verify_context()
    delta_row = delta_row.model_copy(update={"last_ts": LATER})
    raw = extracted_item(
        entities=["main harness", "PN-31010", "main harness"],
        owner=None,
        due=None,
        evidence=[
            ExtractedEvidence(message_ts=REPLY, quote="Second evidence"),
            ExtractedEvidence(message_ts=ROOT, quote="PN-31010 Rev C"),
        ],
        claims=[
            ExtractedClaim(entity="main harness", attribute="rev", value="C", message_ts=ROOT),
            ExtractedClaim(
                entity="PN-31010", attribute="status", value="complete", message_ts=REPLY
            ),
        ],
        tasks=[],
        status="resolved",
    )
    second_raw = extracted_item(
        title="Second new item",
        evidence=[ExtractedEvidence(message_ts=ROOT, quote="released complete")],
    )
    result = verify(
        Extraction(rationale="Supported.", items=[raw, second_raw]),
        delta_row,
        citable,
        glossary(),
        tasks,
        {},
    )
    item, second_item = result.items
    assert item.item_id == f"{delta_row.thread_id}#{delta_row.last_ts}#0"
    assert second_item.item_id == f"{delta_row.thread_id}#{delta_row.last_ts}#1"
    assert delta_row.last_ts != max(entry.message_ts for entry in item.evidence)
    assert item.entities == ("PN-31010",)
    assert item.owner is None and item.due is None
    assert item.first_seen == ROOT
    assert item.last_updated == REPLY
    assert item.resolved_by == REPLY
    assert [entry.permalink for entry in item.evidence] == [
        citable[REPLY].permalink,
        citable[ROOT].permalink,
    ]
    assert [claim.entity for claim in item.claims] == ["PN-31010", "PN-31010"]
    assert [claim.authority for claim in item.claims] == ["owner_stated", "hearsay"]
    assert result.drops == dict.fromkeys(DROP_FIELDS, 0)


def test_verify_update_merges_evidence_unions_links_and_replaces_scalars() -> None:
    old_uncited_ts = "1759999999.000001"
    stale_ts = "1760000000.000003"
    root = event(text="replacement quote")
    reply = event(REPLY, text="new quote review complete")
    stale = event(stale_ts, text="edited text no longer supports the old quote")
    delta_row = delta(root, reply, stale, root=root)
    kept_old_claim = Claim(
        entity="PN-31010",
        attribute="rev",
        value="B",
        message_ts=old_uncited_ts,
        authority="owner_stated",
    )
    stale_claim = Claim(
        entity="PN-31010",
        attribute="status",
        value="old status",
        message_ts=stale_ts,
        authority="owner_stated",
    )
    kept_done_task = TaskLink(task="today.task", effect="done", message_ts=old_uncited_ts)
    stale_task = TaskLink(task="today.task", effect="evidence", message_ts=stale_ts)
    existing_item = stored_item(
        type="risk",
        entities=(),
        owner=None,
        affected_roles=("ME",),
        evidence=(
            Evidence(message_ts=old_uncited_ts, quote="uncited old quote", permalink="old-link"),
            Evidence(message_ts=ROOT, quote="old root quote", permalink="old-root-link"),
            Evidence(message_ts=stale_ts, quote="stale quote", permalink="stale-link"),
        ),
        first_seen=old_uncited_ts,
        last_updated=stale_ts,
        claims=(kept_old_claim, stale_claim),
        tasks=(kept_done_task, stale_task),
    )
    incoming = extracted_item(
        item_id="I1",
        type="decision",
        title="New title",
        summary="New summary",
        entities=["main harness"],
        owner="<@U1>",
        due="2026-10-22",
        affected_roles=["EE"],
        severity="high",
        confidence="high",
        status="resolved",
        evidence=[
            ExtractedEvidence(message_ts=ROOT, quote="replacement quote"),
            ExtractedEvidence(message_ts=REPLY, quote="new quote"),
            ExtractedEvidence(message_ts=REPLY, quote="new quote"),
        ],
        claims=[
            ExtractedClaim(
                entity="PN-31010", attribute="status", value="complete", message_ts=REPLY
            ),
            ExtractedClaim(
                entity="main harness", attribute="status", value="complete", message_ts=REPLY
            ),
        ],
        tasks=[
            ExtractedTask(task="today.task", effect="done", message_ts=REPLY),
            ExtractedTask(task="today.task", effect="done", message_ts=REPLY),
        ],
    )
    result = verify(
        Extraction(rationale="Update.", items=[incoming]),
        delta_row,
        {ROOT: root, REPLY: reply, stale_ts: stale},
        glossary(),
        {"today.task": project().task("today.task")},
        {"I1": existing_item},
    )
    (updated,) = result.items
    assert [(e.message_ts, e.quote) for e in updated.evidence] == [
        (old_uncited_ts, "uncited old quote"),
        (ROOT, "replacement quote"),
        (REPLY, "new quote"),
    ]
    assert updated.evidence[0] == existing_item.evidence[0]
    assert [(c.attribute, c.value, c.message_ts) for c in updated.claims] == [
        ("rev", "B", old_uncited_ts),
        ("status", "complete", REPLY),
    ]
    assert [(link.effect, link.message_ts) for link in updated.tasks] == [
        ("done", old_uncited_ts),
        ("done", REPLY),
    ]
    assert updated.model_dump(
        exclude={"evidence", "claims", "tasks", "first_seen", "last_updated", "resolved_by"}
    ) == {
        "item_id": "I1",
        "thread_id": delta_row.thread_id,
        "type": "decision",
        "title": "New title",
        "summary": "New summary",
        "entities": ("PN-31010",),
        "owner": "U1",
        "due": date(2026, 10, 22),
        "affected_roles": ("EE",),
        "severity": "high",
        "confidence": "high",
        "status": "resolved",
        "absence": None,
    }
    assert (updated.first_seen, updated.last_updated, updated.resolved_by) == (
        old_uncited_ts,
        REPLY,
        REPLY,
    )
    assert result.drops == dict.fromkeys(DROP_FIELDS, 0) | {
        "evidence.stale": 1,
        "claims.stale": 1,
        "tasks.stale": 1,
    }


def test_verify_empty_extraction_has_zero_drops() -> None:
    result = verify(
        Extraction(rationale="Nothing new.", items=[]), delta(root=None), {}, glossary(), {}, {}
    )
    assert result.items == ()
    assert result.drops == dict.fromkeys(DROP_FIELDS, 0)


def test_run_extract_sets_version_records_triage_and_writes_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    cassette_path = tmp_path / "cassettes"
    with Store.open(db_path) as store:
        store.upsert_events([event()])
        requests = put_stage_cassettes(
            cassette_path,
            store,
            triage_data={"rationale": "Keep.", "probability": 1.0},
            extraction_data=extraction_data(extracted_item()),
            channel_purposes=CHANNEL_PURPOSES,
        )
        report = run_extract(
            store,
            replay_llm(cassette_path),
            glossary(),
            project(),
            priors(),
            AS_OF,
            ZONE,
            CHANNEL_PURPOSES,
        )
        decision = store.triage_decision(f"C1:{ROOT}", ROOT, EXTRACT_PROMPT_VERSION, "classify")
        assert store.get_meta("extract_prompt_version") == EXTRACT_PROMPT_VERSION
        assert decision is not None and decision.kept
        assert report.model_dump() == {
            "threads": 1,
            "kept": 1,
            "dropped": 0,
            "failed": 0,
            "items_written": 1,
            "drops": dict.fromkeys(DROP_FIELDS, 0),
        }
        assert len(store.items_for_thread(f"C1:{ROOT}")) == 1
        assert len(store.triage_for_thread(f"C1:{ROOT}")) == 1
        extraction_request = next(request for request in requests if request.model == "extract")
        assert "today.task" in extraction_request.messages[0].content
        assert "old.task" not in extraction_request.messages[0].content
        assert AS_OF.date() != AS_OF.astimezone(ZONE).date()
        assert (
            run_extract(
                store,
                replay_llm(cassette_path),
                glossary(),
                project(),
                priors(),
                AS_OF,
                ZONE,
                CHANNEL_PURPOSES,
            ).threads
            == report.failed
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT prompt_version, model FROM items").fetchall() == [
            (EXTRACT_PROMPT_VERSION, "extract")
        ]


def test_run_extract_refuses_different_store_prompt_version(tmp_path: Path) -> None:
    db_path = tmp_path / "version.sqlite"
    with Store.open(db_path) as store:
        store.set_meta("extract_prompt_version", "m1.old")
        with pytest.raises(ValueError) as error:
            run_extract(
                store,
                replay_llm(tmp_path),
                glossary(),
                project(),
                priors(),
                AS_OF,
                ZONE,
                CHANNEL_PURPOSES,
            )
    message = str(error.value)
    assert "m1.old" in message
    assert EXTRACT_PROMPT_VERSION in message
    assert str(db_path) in message


def test_run_extract_reuses_triage_and_dropped_thread_writes_no_items(tmp_path: Path) -> None:
    db_path = tmp_path / "dropped.sqlite"
    with Store.open(db_path) as store:
        store.upsert_events([event()])
        store.record_triage(
            TriageDecision(
                thread_id=f"C1:{ROOT}",
                last_ts=ROOT,
                prompt_version=EXTRACT_PROMPT_VERSION,
                model="classify",
                probability=0,
                threshold=0.25,
                kept=False,
                rationale="No project information.",
                decided_at=AS_OF,
            )
        )
        report = run_extract(
            store,
            replay_llm(tmp_path),
            glossary(),
            project(),
            priors(),
            AS_OF,
            ZONE,
            CHANNEL_PURPOSES,
        )
        assert (report.threads, report.kept, report.dropped, report.items_written) == (1, 0, 1, 0)
        assert store.items_for_thread(f"C1:{ROOT}") == []
        assert store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", AS_OF) == []


@pytest.mark.parametrize("failure_at", ["triage", "extraction"])
def test_run_extract_counts_output_failure_records_failed_and_continues(
    tmp_path: Path, failure_at: str
) -> None:
    db_path = tmp_path / "failed.sqlite"
    cassette_path = tmp_path / "cassettes"
    with Store.open(db_path) as store:
        first = event(channel_id="C1")
        second = event(channel_id="C2", channel_name="program")
        store.upsert_events([first, second])
        for delta_row in store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", AS_OF):
            triage_sent = schema_request(
                triage_request(
                    delta_row,
                    CHANNEL_PURPOSES.get(delta_row.channel_id),
                    citable_for(delta_row),
                ),
                TriageOutput,
            )
            probability = 1.5 if delta_row.channel_id == "C1" and failure_at == "triage" else 0.0
            if delta_row.channel_id == "C1" and failure_at == "extraction":
                probability = 1.0
            Cassettes(cassette_path).put(
                request_key(triage_sent),
                triage_sent,
                response({"rationale": "Result.", "probability": probability}),
            )
            if delta_row.channel_id == "C1" and failure_at == "extraction":
                extract_sent = schema_request(
                    extract_request(
                        delta_row,
                        glossary(),
                        project(),
                        AS_OF.astimezone(ZONE).date(),
                        (),
                        citable_for(delta_row),
                    ),
                    Extraction,
                )
                Cassettes(cassette_path).put(request_key(extract_sent), extract_sent, response({}))
        report = run_extract(
            store,
            replay_llm(cassette_path),
            glossary(),
            project(),
            priors(),
            AS_OF,
            ZONE,
            CHANNEL_PURPOSES,
        )
        assert (report.threads, report.failed, report.dropped) == (2, 1, 1)
        pending = store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", AS_OF)
        assert [row.channel_id for row in pending] == ["C1"]
        second_report = run_extract(
            store,
            replay_llm(cassette_path),
            glossary(),
            project(),
            priors(),
            AS_OF,
            ZONE,
            CHANNEL_PURPOSES,
        )
        assert second_report.threads == report.failed
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT thread_id, status FROM stage_runs ORDER BY thread_id"
        ).fetchall() == [(f"C1:{ROOT}", "failed"), (f"C2:{ROOT}", "ok")]


def test_run_extract_reraises_cassette_miss(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with Store.open(tmp_path / "miss.sqlite") as store:
        store.upsert_events([event(text="CASSETTE-MISS-SECRET")])
        with caplog.at_level(logging.INFO), pytest.raises(CassetteMiss):
            run_extract(
                store,
                replay_llm(tmp_path / "empty"),
                glossary(),
                project(),
                priors(),
                AS_OF,
                ZONE,
                CHANNEL_PURPOSES,
            )
    records = [
        record for record in caplog.records if record.getMessage() == "extract_cassette_miss"
    ]
    assert len(records) == 1
    assert vars(records[0])["thread_id"] == f"C1:{ROOT}"
    assert "CASSETTE-MISS-SECRET" not in str(vars(records[0]))


def test_run_extract_after_drop_uses_only_new_revision_and_root(tmp_path: Path) -> None:
    cassette_path = tmp_path / "cassettes"
    with Store.open(tmp_path / "revision.sqlite") as store:
        root = event(text="ROOT-ONLY")
        first = event(REPLY, text="REVISION-ONE")
        store.upsert_events([root, first])
        put_stage_cassettes(
            cassette_path,
            store,
            triage_data={"rationale": "Drop.", "probability": 0.0},
            channel_purposes=CHANNEL_PURPOSES,
        )
        first_report = run_extract(
            store,
            replay_llm(cassette_path),
            glossary(),
            project(),
            priors(),
            AS_OF,
            ZONE,
            CHANNEL_PURPOSES,
        )
        second = event(LATER, text="REVISION-TWO")
        store.upsert_events([second])
        requests = put_stage_cassettes(
            cassette_path,
            store,
            triage_data={"rationale": "Keep.", "probability": 1.0},
            extraction_data=extraction_data(
                extracted_item(evidence=[ExtractedEvidence(message_ts=LATER, quote="REVISION-TWO")])
            ),
            channel_purposes=CHANNEL_PURPOSES,
        )
        second_report = run_extract(
            store,
            replay_llm(cassette_path),
            glossary(),
            project(),
            priors(),
            AS_OF,
            ZONE,
            CHANNEL_PURPOSES,
        )
    assert (first_report.dropped, second_report.kept) == (1, 1)
    extraction_request = next(request for request in requests if request.model == "extract")
    content = extraction_request.messages[0].content
    assert "ROOT-ONLY" in content
    assert "REVISION-TWO" in content
    assert "REVISION-ONE" not in content


def write_cli_world(root: Path) -> None:
    world = {
        "name": "tiny",
        "program": "Kestrel K2",
        "workspace_url": "https://kestrel.test",
        "timezone": "America/Los_Angeles",
        "start_date": "2026-10-12",
        "days": 3,
        "project": project().model_dump(mode="json"),
        "channels": [{"id": "C1", "name": "eng-mech", "purpose": "Mechanical"}],
        "personas": [
            {
                "user_id": "U1",
                "name": "Nadia Brenner",
                "role": "ME",
                "channels": ["C1"],
                "aliases": ["Nadia", "@nadia"],
            },
            {
                "user_id": "B1",
                "name": "release_bot",
                "kind": "bot",
                "role": None,
                "channels": ["C1"],
                "aliases": ["releases"],
                "authority": "plm_bot",
            },
        ],
        "entities": [
            {
                "id": "PN-31010",
                "type": "harness",
                "description": "Main power harness",
                "aliases": ["main harness"],
            }
        ],
        "events": [],
    }
    world_dir = root / "data" / "world" / "tiny"
    world_dir.mkdir(parents=True)
    (world_dir / "world.yaml").write_text(yaml.safe_dump(world), encoding="utf-8")
    config = root / "config"
    config.mkdir()
    (config / "priors.yaml").write_text(
        "triage:\n  miss_cost_ratio: 3\nabsence:\n  ask_reply_cadence_days: 2\n",
        encoding="utf-8",
    )


@pytest.fixture
def cli_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    write_cli_world(tmp_path)
    db_path = tmp_path / "store.sqlite"
    cassette_path = tmp_path / "cassettes"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_WORLD", "tiny")
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassette_path))
    monkeypatch.delenv("DIGEST_DB", raising=False)
    monkeypatch.setattr(Settings, "__init__", partialmethod(Settings.__init__, _env_file=None))
    return db_path, cassette_path


def prepare_cli_store(db_path: Path, *, until: bool = True, world: bool = True) -> None:
    first_ts = "1791831600.000001"
    second_ts = "1791918000.000001"
    with Store.open(db_path) as store:
        store.upsert_events(
            [
                event(first_ts, thread_ts=first_ts, event_ts=first_ts, text="FIRST-DAY-THREAD"),
                event(second_ts, thread_ts=second_ts, event_ts=second_ts, text="SECOND-DAY-THREAD"),
            ]
        )
        if world:
            store.set_meta("world", "tiny")
        store.set_meta("timezone", "America/Los_Angeles")
        if until:
            store.set_meta("until", "2026-10-14T06:59:59.999999+00:00")


def test_cli_extract_prints_json_report(cli_case: tuple[Path, Path]) -> None:
    db_path, cassette_path = cli_case
    prepare_cli_store(db_path)
    with Store.open(db_path) as store:
        for as_of in (
            datetime(2026, 10, 13, 6, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 10, 14, 6, 59, 59, 999999, tzinfo=UTC),
        ):
            put_stage_cassettes(
                cassette_path,
                store,
                triage_data={"rationale": "Drop.", "probability": 0.0},
                as_of=as_of,
                channel_purposes=CHANNEL_PURPOSES,
            )
    result = runner.invoke(app, ["extract", "--world", "tiny", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["threads"] == 2
    assert report["dropped"] == 2


def test_cli_extract_refuses_store_without_until(cli_case: tuple[Path, Path]) -> None:
    db_path, _ = cli_case
    prepare_cli_store(db_path, until=False)
    result = runner.invoke(app, ["extract", "--db", str(db_path)])
    assert result.exit_code == 1, result.output
    assert "until" in result.output.lower()


def test_cli_extract_refuses_store_without_world(cli_case: tuple[Path, Path]) -> None:
    db_path, _ = cli_case
    prepare_cli_store(db_path, world=False)
    result = runner.invoke(app, ["extract", "--db", str(db_path)])
    assert result.exit_code == 1, result.output
    assert "world" in result.output.lower()


def test_cli_extract_refuses_world_that_differs_from_store(cli_case: tuple[Path, Path]) -> None:
    db_path, _ = cli_case
    prepare_cli_store(db_path)
    result = runner.invoke(app, ["extract", "--world", "other", "--db", str(db_path)])
    assert result.exit_code == 1, result.output
    output = result.output.lower()
    assert "world" in output
    assert "tiny" in output
    assert "other" in output


def test_cli_extract_reports_cassette_key_on_miss(cli_case: tuple[Path, Path]) -> None:
    db_path, _ = cli_case
    prepare_cli_store(db_path)
    result = runner.invoke(app, ["extract", "--db", str(db_path)])
    assert result.exit_code == 1, result.output
    assert "cassette" in result.output.lower()
    assert re.search(r"\b[0-9a-f]{64}\b", result.output)


def test_extract_logs_structured_success_without_message_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cassette_path = tmp_path / "cassettes"
    root = event(text="SECRET MESSAGE TEXT PN-31010 Rev C released complete by Friday.")
    with Store.open(tmp_path / "logs.sqlite") as store:
        store.upsert_events([root])
        put_stage_cassettes(
            cassette_path,
            store,
            triage_data={"rationale": "Keep.", "probability": 1.0},
            extraction_data=extraction_data(extracted_item(entities=["missing entity"])),
            channel_purposes=CHANNEL_PURPOSES,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            report = run_extract(
                store,
                replay_llm(cassette_path),
                glossary(),
                project(),
                priors(),
                AS_OF,
                ZONE,
                CHANNEL_PURPOSES,
            )
    records = {record.getMessage(): vars(record) for record in caplog.records}
    assert {"triage_decided", "extract_thread"} <= records.keys()
    assert {
        key: records["triage_decided"][key]
        for key in ("thread_id", "probability", "threshold", "kept")
    } == {
        "thread_id": f"C1:{ROOT}",
        "probability": 1.0,
        "threshold": 0.25,
        "kept": True,
    }
    assert records["extract_thread"]["thread_id"] == f"C1:{ROOT}"
    assert records["extract_thread"]["items_written"] == 1
    expected_drops = dict.fromkeys(DROP_FIELDS, 0) | {"entities.unresolved": 1}
    assert report.drops == expected_drops
    assert records["extract_thread"]["drops"] == expected_drops
    assert all("SECRET MESSAGE TEXT" not in record.getMessage() for record in caplog.records)
    assert all("SECRET MESSAGE TEXT" not in str(vars(record)) for record in caplog.records)


def test_extract_logs_structured_failure_without_message_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cassette_path = tmp_path / "cassettes"
    root = event(text="ANOTHER SECRET")
    with Store.open(tmp_path / "failed-log.sqlite") as store:
        store.upsert_events([root])
        put_stage_cassettes(
            cassette_path,
            store,
            triage_data={"rationale": "Invalid.", "probability": 2.0},
            channel_purposes=CHANNEL_PURPOSES,
        )
        caplog.clear()
        with caplog.at_level(logging.INFO):
            run_extract(
                store,
                replay_llm(cassette_path),
                glossary(),
                project(),
                priors(),
                AS_OF,
                ZONE,
                CHANNEL_PURPOSES,
            )
    failures = [record for record in caplog.records if record.getMessage() == "extract_failed"]
    assert len(failures) == 1
    fields = vars(failures[0])
    assert fields["thread_id"] == f"C1:{ROOT}"
    assert fields["error_class"] in {"LLMOutputError", "LLMOutputInvalid"}
    assert "ANOTHER SECRET" not in str(fields)

import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from digest.absence import ask_absences, checklist_absences
from digest.cli import app
from digest.models import (
    Claim,
    Event,
    Evidence,
    Item,
    ItemStatus,
    ItemType,
    Role,
    TaskEffect,
    TaskLink,
)
from digest.project import ProjectConfig, add_working_days, load_project, working_days_between
from digest.store import StageRun, Store

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("America/Los_Angeles")
START = date(2026, 10, 12)
runner = CliRunner()


def _ts(day: date, second: int = 0) -> str:
    moment = datetime.combine(day, time(12, 0, second), tzinfo=ZONE).astimezone(UTC)
    return f"{int(moment.timestamp()):010d}.000000"


def _day_ts(day: int, second: int = 0) -> str:
    return _ts(_day_date(day), second)


def _day_date(day: int) -> date:
    weeks, weekday = divmod(day - 1, 5)
    return START + timedelta(weeks=weeks, days=weekday)


def _evidence(day: int, label: str, second: int = 0) -> Evidence:
    message_ts = _day_ts(day, second)
    return Evidence(
        message_ts=message_ts,
        quote=label,
        permalink=f"https://kestrel.slack.com/archives/C1/p{message_ts.replace('.', '')}",
    )


def _item(
    item_id: str,
    day: int,
    *,
    item_type: ItemType = "fyi",
    owner: str | None = None,
    entities: tuple[str, ...] = (),
    affected_roles: tuple[Role, ...] = (),
    claims: tuple[Claim, ...] = (),
    tasks: tuple[TaskLink, ...] = (),
    evidence: tuple[Evidence, ...] | None = None,
    status: ItemStatus = "open",
    first_seen: str | None = None,
    last_updated: str | None = None,
    thread_id: str | None = None,
) -> Item:
    cited = evidence or (_evidence(day, item_id),)
    return Item(
        item_id=item_id,
        thread_id=thread_id or f"C1:{cited[0].message_ts}",
        type=item_type,
        title=item_id,
        summary=f"Summary for {item_id}.",
        entities=entities,
        owner=owner,
        due=None,
        affected_roles=affected_roles,
        severity="medium",
        confidence="high",
        evidence=cited,
        first_seen=first_seen or cited[0].message_ts,
        last_updated=last_updated or cited[-1].message_ts,
        claims=claims,
        tasks=tasks,
        status=status,
        resolved_by=None,
        absence=None,
    )


def _event(item: Item, user_id: str, day: int, *, deleted: bool = False) -> Event:
    ts = _day_ts(day)
    channel_id, thread_ts = item.thread_id.split(":", 1)
    return Event(
        source="replay",
        channel_id=channel_id,
        channel_name="eng",
        thread_ts=thread_ts,
        ts=ts,
        event_ts=ts,
        user_id=user_id,
        text=f"Message from {user_id}",
        permalink=f"https://kestrel.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
        deleted=deleted,
    )


def _two_gate_project() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project_id": "two-gates",
            "name": "Two gates",
            "phases": [{"phase": "DVT", "from_date": "2026-10-12"}],
            "gates": [
                {
                    "id": "G-A",
                    "name": "Alpha gate",
                    "date": "2026-10-22",
                    "buffer_days": 7,
                    "checklist": [
                        {
                            "id": "G-A.critical",
                            "task": "Critical task",
                            "owner": "EE",
                            "expected_cadence_days": 1,
                            "priority": "critical",
                        },
                        {
                            "id": "G-A.high",
                            "task": "High task",
                            "owner": "EE",
                            "expected_cadence_days": 1,
                            "priority": "high",
                        },
                        {
                            "id": "G-A.normal",
                            "task": "Normal task",
                            "owner": "ME",
                            "expected_cadence_days": 1,
                        },
                    ],
                },
                {
                    "id": "G-B",
                    "name": "Beta gate",
                    "date": "2026-10-23",
                    "buffer_days": 2,
                    "checklist": [
                        {
                            "id": "G-B.task",
                            "task": "Beta task",
                            "owner": "PM",
                            "expected_cadence_days": 1,
                        }
                    ],
                },
            ],
        }
    )


@pytest.mark.parametrize(
    ("day", "offset", "expected"),
    [
        (date(2026, 10, 22), -7, date(2026, 10, 13)),
        (date(2026, 10, 16), 1, date(2026, 10, 19)),
    ],
)
def test_add_working_days_examples(day: date, offset: int, expected: date) -> None:
    assert add_working_days(day, offset) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 10, 16), date(2026, 10, 19), 1),
        (date(2026, 10, 16), date(2026, 10, 16), 0),
        (date(2026, 10, 15), date(2026, 10, 17), 1),
        (date(2026, 10, 19), date(2026, 10, 18), 0),
    ],
)
def test_working_days_between_examples(start: date, end: date, expected: int) -> None:
    assert working_days_between(start, end) == expected


@given(
    st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)).filter(
        lambda value: value.weekday() < 5
    ),
    st.integers(min_value=0, max_value=100),
)
def test_working_day_helpers_are_inverse_for_nonnegative_offsets(day: date, count: int) -> None:
    assert working_days_between(day, add_working_days(day, count)) == count


def test_checklist_absence_uses_latest_link_and_emits_all_contract_fields() -> None:
    project = _two_gate_project()
    merged_root = _evidence(1, "merged root")
    old = _evidence(2, "old")
    latest = _evidence(3, "latest")
    items = [
        _item(
            "latest",
            3,
            entities=("PART",),
            tasks=(TaskLink(task="G-A.high", effect="evidence", message_ts=latest.message_ts),),
            evidence=(merged_root, latest),
            first_seen=merged_root.message_ts,
            last_updated=_day_ts(6),
        ),
        _item(
            "old",
            2,
            entities=("OLD",),
            tasks=(TaskLink(task="G-A.high", effect="evidence", message_ts=old.message_ts),),
        ),
    ]

    (absence,) = checklist_absences(project, items, date(2026, 10, 19), ZONE)

    assert absence.item_id == "absence:G-A.high"
    assert absence.thread_id == items[0].thread_id
    assert "Alpha gate" in absence.title
    assert "High task" in absence.title
    assert absence.entities == ("G-A", "PART")
    assert absence.owner is None
    assert absence.affected_roles == ("EE",)
    assert (absence.severity, absence.confidence) == ("high", "high")
    assert absence.evidence == (latest,)
    assert absence.first_seen == absence.last_updated == latest.message_ts
    assert absence.absence is not None
    assert absence.absence.model_dump() == {
        "source": "checklist",
        "gate": "G-A",
        "task": "G-A.high",
        "asker": None,
        "days_silent": 3,
        "buffer_consumed": 4,
        "buffer_days": 7,
    }


def test_checklist_done_task_yields_nothing() -> None:
    project = _two_gate_project()
    cited = _evidence(2, "done")
    done = _item(
        "done",
        2,
        tasks=(TaskLink(task="G-A.high", effect="done", message_ts=cited.message_ts),),
    )
    assert checklist_absences(project, [done], date(2026, 10, 20), ZONE) == []


def test_checklist_gate_anchor_uses_earliest_claim_and_item_id_tiebreak() -> None:
    project = _two_gate_project()
    first_b = _evidence(1, "B")
    first_a = _evidence(1, "A")
    later = _evidence(2, "later")
    items = [
        _item(
            "B-anchor",
            1,
            entities=("B",),
            claims=(
                Claim(
                    entity="G-A",
                    attribute="status",
                    value="open",
                    message_ts=first_b.message_ts,
                    authority="owner_stated",
                ),
            ),
            evidence=(first_b,),
            thread_id="C1:thread-b",
        ),
        _item(
            "A-anchor",
            1,
            entities=("A",),
            claims=(
                Claim(
                    entity="G-A",
                    attribute="status",
                    value="open",
                    message_ts=first_a.message_ts,
                    authority="owner_stated",
                ),
            ),
            evidence=(first_a,),
            thread_id="C1:thread-a",
        ),
        _item(
            "later",
            2,
            claims=(
                Claim(
                    entity="G-A",
                    attribute="status",
                    value="open",
                    message_ts=later.message_ts,
                    authority="owner_stated",
                ),
            ),
            evidence=(later,),
        ),
    ]

    absences = checklist_absences(project, items, date(2026, 10, 19), ZONE)

    assert [item.item_id for item in absences] == [
        "absence:G-A.critical",
        "absence:G-A.high",
        "absence:G-A.normal",
    ]
    assert all(item.evidence == (first_a,) for item in absences)
    assert all(item.thread_id == "C1:thread-a" for item in absences)
    assert [item.severity for item in absences] == ["high", "high", "medium"]


def test_checklist_skips_mentions_without_claims_and_unanchored_tasks() -> None:
    project = _two_gate_project()
    mention = _item("mention", 1, entities=("G-A",), tasks=())
    unrelated = _item("unrelated", 1, entities=("OTHER",), claims=(), tasks=())
    assert checklist_absences(project, [mention, unrelated], date(2026, 10, 19), ZONE) == []


def test_checklist_fires_on_buffer_opening_but_not_day_before() -> None:
    project = _two_gate_project()
    anchor_ts = _ts(date(2026, 10, 8))
    anchor = Evidence(message_ts=anchor_ts, quote="anchor", permalink="https://example.test/anchor")
    item = _item(
        "anchor",
        1,
        claims=(
            Claim(
                entity="G-A",
                attribute="status",
                value="open",
                message_ts=anchor.message_ts,
                authority="owner_stated",
            ),
        ),
        evidence=(anchor,),
    )
    assert checklist_absences(project, [item], date(2026, 10, 12), ZONE) == []
    opened = checklist_absences(project, [item], date(2026, 10, 13), ZONE)
    assert [row.absence.task for row in opened if row.absence] == [
        "G-A.critical",
        "G-A.high",
        "G-A.normal",
    ]


def test_checklist_emits_nothing_after_gate() -> None:
    project = _two_gate_project()
    anchor = _evidence(1, "anchor")
    item = _item(
        "anchor",
        1,
        claims=(
            Claim(
                entity="G-A",
                attribute="status",
                value="open",
                message_ts=anchor.message_ts,
                authority="owner_stated",
            ),
        ),
    )
    assert checklist_absences(project, [item], date(2026, 10, 23), ZONE) == []


def test_checklist_ignores_links_and_gate_claims_after_as_of() -> None:
    project = _two_gate_project()
    earlier = _evidence(2, "earlier link and claim")
    future = _evidence(7, "future link and claim")
    future_item = _item(
        "future",
        7,
        claims=(
            Claim(
                entity="G-A",
                attribute="status",
                value="open",
                message_ts=future.message_ts,
                authority="owner_stated",
            ),
        ),
        tasks=(TaskLink(task="G-A.high", effect="evidence", message_ts=future.message_ts),),
        evidence=(future,),
    )
    earlier_item = _item(
        "earlier",
        2,
        claims=(
            Claim(
                entity="G-A",
                attribute="status",
                value="open",
                message_ts=earlier.message_ts,
                authority="owner_stated",
            ),
        ),
        tasks=(TaskLink(task="G-A.high", effect="evidence", message_ts=earlier.message_ts),),
        evidence=(earlier,),
    )

    absences = checklist_absences(project, [future_item, earlier_item], date(2026, 10, 19), ZONE)

    assert [row.item_id for row in absences] == [
        "absence:G-A.critical",
        "absence:G-A.high",
        "absence:G-A.normal",
    ]
    assert all(row.evidence == (earlier,) for row in absences)


def test_checklist_counts_silence_from_local_evidence_date() -> None:
    project = _two_gate_project()
    local_evening = datetime(2026, 10, 12, 18, tzinfo=ZONE).astimezone(UTC)
    timestamp = f"{int(local_evening.timestamp()):010d}.000000"
    evidence = Evidence(
        message_ts=timestamp, quote="local evening", permalink="https://example.test"
    )
    item = _item(
        "anchor",
        1,
        claims=(
            Claim(
                entity="G-A",
                attribute="status",
                value="open",
                message_ts=timestamp,
                authority="owner_stated",
            ),
        ),
        evidence=(evidence,),
    )

    absences = checklist_absences(project, [item], date(2026, 10, 16), ZONE)

    assert [row.absence.days_silent for row in absences if row.absence] == [4, 4, 4]


def test_checklist_weekend_as_of_counts_only_through_friday() -> None:
    project = _two_gate_project()
    anchor = _evidence(1, "anchor")
    item = _item(
        "anchor",
        1,
        claims=(
            Claim(
                entity="G-A",
                attribute="status",
                value="open",
                message_ts=anchor.message_ts,
                authority="owner_stated",
            ),
        ),
    )
    absences = checklist_absences(project, [item], date(2026, 10, 17), ZONE)
    assert [absence.absence.days_silent for absence in absences if absence.absence] == [4, 4, 4]


def test_ask_absence_finds_asker_and_emits_all_contract_fields() -> None:
    owner_evidence = _evidence(2, "owner listed first", 1)
    ask_evidence = _evidence(2, "ask message", 2)
    ask = _item(
        "ASK-1",
        2,
        item_type="ask",
        owner="UOWNER",
        entities=("PART",),
        affected_roles=("EE", "EM"),
        evidence=(owner_evidence, ask_evidence),
    )
    events = [
        _event(ask, "UOWNER", 2).model_copy(
            update={"ts": owner_evidence.message_ts, "event_ts": owner_evidence.message_ts}
        ),
        _event(ask, "UASKER", 2).model_copy(
            update={"ts": ask_evidence.message_ts, "event_ts": ask_evidence.message_ts}
        ),
    ]

    (absence,) = ask_absences([ask], lambda _: events, date(2026, 10, 19), 2, ZONE)

    assert absence.item_id == "absence:ask:ASK-1"
    assert absence.thread_id == ask.thread_id
    assert "UASKER" in absence.title and "UOWNER" in absence.title
    assert absence.summary == ask.summary
    assert absence.entities == ask.entities
    assert absence.owner == "UOWNER"
    assert absence.affected_roles == ask.affected_roles
    assert (absence.severity, absence.confidence) == ("medium", "high")
    assert absence.evidence == (ask_evidence,)
    assert absence.first_seen == absence.last_updated == ask_evidence.message_ts
    assert absence.absence is not None
    assert absence.absence.model_dump() == {
        "source": "ask",
        "gate": None,
        "task": None,
        "asker": "UASKER",
        "days_silent": 4,
        "buffer_consumed": None,
        "buffer_days": None,
    }


@pytest.mark.parametrize(
    ("reply_user", "deleted", "expected_count"),
    [
        pytest.param("UOWNER", False, 0, id="owner-reply"),
        pytest.param("UOTHER", False, 1, id="other-reply"),
        pytest.param("UOWNER", True, 1, id="deleted-owner-reply"),
    ],
)
def test_ask_absence_only_live_owner_reply_closes(
    reply_user: str, deleted: bool, expected_count: int
) -> None:
    ask = _item("ASK-1", 1, item_type="ask", owner="UOWNER")
    ask_event = _event(ask, "UASKER", 1)
    reply = _event(ask, reply_user, 2, deleted=deleted)
    folded = ask.model_copy(
        update={
            "evidence": (ask.evidence[0], _evidence(2, "folded reply")),
            "last_updated": reply.ts,
        }
    )
    assert (
        len(ask_absences([folded], lambda _: [ask_event, reply], date(2026, 10, 16), 2, ZONE))
        == expected_count
    )


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(
            _item("resolved", 1, item_type="ask", owner="UOWNER", status="resolved"),
            id="resolved-ask",
        ),
        pytest.param(_item("owned-fyi", 1, owner="UOWNER"), id="owned-non-ask"),
    ],
)
def test_ask_absence_filters_source_items(item: Item) -> None:
    assert (
        ask_absences([item], lambda _: [_event(item, "UASKER", 1)], date(2026, 10, 16), 2, ZONE)
        == []
    )


def test_ask_absence_clock_uses_ask_message_not_last_updated() -> None:
    ask = _item(
        "ASK-1",
        1,
        item_type="ask",
        owner="UOWNER",
        first_seen=_day_ts(0),
        last_updated=_day_ts(3),
    )

    (absence,) = ask_absences(
        [ask], lambda _: [_event(ask, "UASKER", 1)], date(2026, 10, 16), 2, ZONE
    )

    assert absence.absence is not None
    assert absence.absence.days_silent == 4


def test_ask_absence_counts_silence_from_local_ask_date() -> None:
    ask = _item("ASK-1", 1, item_type="ask", owner="UOWNER")
    local_evening = datetime(2026, 10, 12, 18, tzinfo=ZONE).astimezone(UTC)
    timestamp = f"{int(local_evening.timestamp()):010d}.000000"
    evidence = Evidence(message_ts=timestamp, quote="late ask", permalink="https://example.test")
    ask = ask.model_copy(
        update={"evidence": (evidence,), "first_seen": timestamp, "last_updated": timestamp}
    )
    event = _event(ask, "UASKER", 1).model_copy(update={"ts": timestamp, "event_ts": timestamp})

    (absence,) = ask_absences([ask], lambda _: [event], date(2026, 10, 16), 2, ZONE)

    assert absence.absence is not None
    assert absence.absence.days_silent == 4


def test_ask_without_owner_is_skipped() -> None:
    ask = _item("ASK-1", 1, item_type="ask", owner=None)
    assert (
        ask_absences([ask], lambda _: [_event(ask, "UASKER", 1)], date(2026, 10, 16), 2, ZONE) == []
    )


def test_ask_without_asking_evidence_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    ask = _item("ASK-1", 1, item_type="ask", owner="UOWNER")
    caplog.set_level(logging.DEBUG, logger="digest.absence")
    assert (
        ask_absences([ask], lambda _: [_event(ask, "UOWNER", 1)], date(2026, 10, 16), 2, ZONE) == []
    )
    assert "absence_ask_without_message" in caplog.messages


def _kestrel_items() -> tuple[list[Item], dict[str, list[Event]]]:
    rows: list[tuple[str, int, tuple[tuple[str, TaskEffect], ...], tuple[str, ...]]] = [
        ("SCHED-1", 1, (), ("G-DVT1",)),
        ("EVT2-EXIT", 1, (("G-EVT2-EXIT.review", "done"), ("G-EVT2-EXIT.actions", "evidence")), ()),
        ("S3-THERMAL-RUN1", 2, (("G-DVT1.thermal", "evidence"),), ()),
        ("S2-PO", 3, (("G-DVT1.ctb", "evidence"),), ()),
        ("S6-EMC", 3, (("G-DVT1.emc", "evidence"),), ()),
        ("S5-MLCC", 4, (("G-DVT1.ctb", "evidence"),), ()),
        ("S4-FAI", 5, (("G-DVT1.fai", "evidence"),), ()),
        ("S3-BUFFER", 6, (), ()),
        ("S10-UN383", 6, (("G-DVT1.battery", "evidence"),), ()),
        ("S3-TEXTURE", 7, (("G-TR1.dfm", "evidence"),), ("G-TR1",)),
        ("S15-GO", 9, (("G-DVT1.review", "done"),), ("G-DVT1",)),
        ("BUILD-D1", 10, (("G-DVT1.ctb", "evidence"),), ()),
    ]
    items: list[Item] = []
    for event_id, day, touches, gate_claims in rows:
        cited = _evidence(day, event_id)
        items.append(
            _item(
                event_id,
                day,
                entities=(event_id,),
                evidence=(cited,),
                tasks=tuple(
                    TaskLink(task=task, effect=effect, message_ts=cited.message_ts)
                    for task, effect in touches
                ),
                claims=tuple(
                    Claim(
                        entity=gate,
                        attribute="status",
                        value="open",
                        message_ts=cited.message_ts,
                        authority="owner_stated",
                    )
                    for gate in gate_claims
                ),
            )
        )
    safety = _item("ASK-SAF", 3, item_type="ask", owner="U0FW01", affected_roles=("FW", "EE", "EM"))
    purchase = _item(
        "ASK-PO", 4, item_type="ask", owner="U0SC01", affected_roles=("supply_chain", "EE")
    )
    items.extend([safety, purchase])
    threads = {
        safety.thread_id: [_event(safety, "U0EM01", 3)],
        purchase.thread_id: [_event(purchase, "U0EE01", 4), _event(purchase, "U0SC01", 5)],
    }
    return items, threads


def test_bundled_project_day_table_from_hand_built_items() -> None:
    project = load_project(ROOT / "config/project.yaml")
    items, threads = _kestrel_items()
    expected = {
        1: set(),
        2: set(),
        3: set(),
        4: {"G-DVT1.fai", "G-DVT1.battery"},
        5: {"G-DVT1.thermal", "G-DVT1.battery", "G-DVT1.safety"},
        6: {"G-DVT1.thermal", "G-DVT1.emc", "G-DVT1.ctb", "G-DVT1.safety", "ASK-SAF"},
        7: {
            "G-DVT1.thermal",
            "G-DVT1.emc",
            "G-DVT1.ctb",
            "G-DVT1.safety",
            "G-DVT1.review",
            "ASK-SAF",
        },
        8: {
            "G-DVT1.thermal",
            "G-DVT1.emc",
            "G-DVT1.fai",
            "G-DVT1.ctb",
            "G-DVT1.safety",
            "G-DVT1.review",
            "ASK-SAF",
        },
        9: {
            "G-DVT1.thermal",
            "G-DVT1.emc",
            "G-DVT1.fai",
            "G-DVT1.ctb",
            "G-DVT1.battery",
            "G-DVT1.safety",
            "ASK-SAF",
        },
        10: {"ASK-SAF"},
    }
    for day in range(1, 11):
        as_of = _day_date(day)
        visible = [item for item in items if item.first_seen <= _day_ts(day, 59)]
        checklist = checklist_absences(project, visible, as_of, ZONE)

        def callback(item: Item, bound: date = as_of) -> list[Event]:
            return [
                event
                for event in threads.get(item.thread_id, [])
                if event.at.astimezone(ZONE).date() <= bound
            ]

        asks = ask_absences(visible, callback, as_of, 2, ZONE)
        actual: set[str] = set()
        for absence in [*checklist, *asks]:
            assert absence.absence is not None
            actual.add(absence.absence.task or absence.item_id.removeprefix("absence:ask:"))
        assert actual == expected[day], day


def test_absence_output_order_is_deterministic() -> None:
    project = _two_gate_project()
    project = project.model_copy(
        update={
            "gates": tuple(
                gate.model_copy(
                    update={
                        "date": date(2026, 10, 22),
                        "buffer_days": 7,
                        "checklist": tuple(reversed(gate.checklist)),
                    }
                )
                for gate in reversed(project.gates)
            )
        }
    )
    anchor = _evidence(1, "anchors")
    anchor_item = _item(
        "anchors",
        1,
        claims=tuple(
            Claim(
                entity=gate,
                attribute="status",
                value="open",
                message_ts=anchor.message_ts,
                authority="owner_stated",
            )
            for gate in ("G-B", "G-A")
        ),
    )
    checklist = checklist_absences(project, [anchor_item], date(2026, 10, 19), ZONE)
    task_ids: list[str] = []
    for item in checklist:
        assert item.absence is not None
        assert item.absence.task is not None
        task_ids.append(item.absence.task)
    assert task_ids == ["G-A.critical", "G-A.high", "G-A.normal", "G-B.task"]

    ask_z = _item("Z-ASK", 1, item_type="ask", owner="UOWNER")
    ask_a = _item("A-ASK", 1, item_type="ask", owner="UOWNER")
    events = {
        ask_z.thread_id: [_event(ask_z, "UASKER", 1)],
        ask_a.thread_id: [_event(ask_a, "UASKER", 1)],
    }
    asks = ask_absences(
        [ask_z, ask_a], lambda item: events[item.thread_id], date(2026, 10, 16), 2, ZONE
    )
    assert [item.item_id for item in asks] == ["absence:ask:A-ASK", "absence:ask:Z-ASK"]


def _copy_world_files(tmp_path: Path) -> None:
    world_dir = tmp_path / "data/world/kestrel"
    world_dir.mkdir(parents=True)
    world_dir.joinpath("world.yaml").write_bytes(
        (ROOT / "data/world/kestrel/world.yaml").read_bytes()
    )
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("priors.yaml").write_bytes((ROOT / "config/priors.yaml").read_bytes())


def _set_replay_meta(store: Store) -> None:
    for key, value in {
        "world": "kestrel",
        "timezone": "America/Los_Angeles",
        "workspace_url": "https://kestrel-k2.slack.com",
        "until": "2026-10-16T06:59:59.999999+00:00",
    }.items():
        store.set_meta(key, value)


def _write_ready_cli_store(store: Store) -> None:
    _set_replay_meta(store)
    anchor = _evidence(1, "schedule")
    ask = _item("ASK-CLI", 1, item_type="ask", owner="U0FW01", affected_roles=("FW", "EM"))
    ask_event = _event(ask, "U0EM01", 1)
    store.upsert_events([ask_event])
    store.record_stage_run(
        StageRun(
            stage="extract",
            thread_id=ask_event.thread_id,
            last_ts=ask_event.event_ts,
            prompt_version="m2.1",
            model="extract",
            run_at=datetime.now(UTC),
            status="ok",
        )
    )
    store.upsert_items(
        [
            _item(
                "SCHED-1",
                1,
                claims=(
                    Claim(
                        entity="G-DVT1",
                        attribute="gate_date",
                        value="2026-10-22",
                        message_ts=anchor.message_ts,
                        authority="plm_bot",
                    ),
                ),
            ),
            ask,
        ],
        prompt_version="m2.1",
        model="extract",
    )


def test_absences_cli_prints_json_lines_after_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy_world_files(tmp_path)
    store_path = tmp_path / "store.sqlite"
    with Store.open(store_path) as store:
        _write_ready_cli_store(store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["absences", "--world", "kestrel", "--db", str(store_path)])

    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines()]
    assert [row["item_id"] for row in rows] == [
        "absence:G-DVT1.battery",
        "absence:G-DVT1.ctb",
        "absence:G-DVT1.emc",
        "absence:G-DVT1.fai",
        "absence:G-DVT1.thermal",
        "absence:ask:ASK-CLI",
    ]
    assert all(row["type"] == "absence" for row in rows)


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        pytest.param("missing-until", "until", id="no-until"),
        pytest.param("pending", "run digest extract first", id="pending-delta"),
    ],
)
def test_absences_cli_refuses_unready_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, reason: str
) -> None:
    _copy_world_files(tmp_path)
    store_path = tmp_path / "store.sqlite"
    with Store.open(store_path) as store:
        if state == "pending":
            _write_ready_cli_store(store)
            pending = _item("pending", 1)
            store.upsert_events([_event(pending, "U0EM01", 1)])
        else:
            for key, value in {
                "world": "kestrel",
                "timezone": "America/Los_Angeles",
                "workspace_url": "https://kestrel-k2.slack.com",
            }.items():
                store.set_meta(key, value)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["absences", "--world", "kestrel", "--db", str(store_path)])

    assert result.exit_code == 1
    assert reason in result.output

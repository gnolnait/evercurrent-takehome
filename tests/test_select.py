from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from digest.daily.profile import Profile, can_see, profile_for
from digest.daily.select import SECTION_CAPS, Selection, select
from digest.models import AbsenceDetail, Event, Evidence, Item, ItemType, Role, Severity, ts_micros
from digest.priors import AbsencePriors, DailyPriors, Priors, TriagePriors, load_priors
from digest.world.schema import World

DAY = date(2026, 10, 14)
BASE_TS = "1791993600.000001"


def world() -> World:
    """Build a small world with public, joined private, and hidden private channels."""
    return World.model_validate(
        {
            "name": "selection",
            "program": "Kestrel K2",
            "workspace_url": "https://kestrel.slack.com",
            "timezone": "America/Los_Angeles",
            "start_date": "2026-10-12",
            "days": 10,
            "project": {
                "project_id": "kestrel",
                "name": "Kestrel K2",
                "phases": [{"phase": "EVT", "from_date": "2026-10-12"}],
                "gates": [
                    {
                        "id": "G-DVT1",
                        "name": "DVT build",
                        "date": "2026-10-22",
                        "buffer_days": 7,
                        "checklist": [
                            {
                                "id": "G-DVT1.thermal",
                                "task": "Complete thermal testing",
                                "owner": "ME",
                                "expected_cadence_days": 1,
                            }
                        ],
                    },
                    {
                        "id": "G-OTHER",
                        "name": "Unowned gate",
                        "date": "2026-10-23",
                        "buffer_days": 3,
                    },
                ],
            },
            "channels": [
                {"id": "CPUBLIC", "name": "program", "purpose": "Program updates"},
                {
                    "id": "CPRIVATE",
                    "name": "mechanical",
                    "purpose": "Mechanical work",
                    "private": True,
                },
                {
                    "id": "CHIDDEN",
                    "name": "leads",
                    "purpose": "Leadership work",
                    "private": True,
                },
            ],
            "personas": [
                {
                    "user_id": "UREADER",
                    "name": "Reader",
                    "role": "ME",
                    "team": "hardware",
                    "channels": ["CPUBLIC", "CPRIVATE"],
                    "owned_entities": ["PN-OWNED", "G-DVT1"],
                },
                {
                    "user_id": "UEMPTY",
                    "name": "Firmware reader",
                    "role": "FW",
                    "channels": ["CPUBLIC"],
                },
                {
                    "user_id": "UASKER",
                    "name": "Asker",
                    "role": "EM",
                    "channels": ["CPUBLIC", "CHIDDEN"],
                },
                {
                    "user_id": "BBOT",
                    "name": "record_bot",
                    "kind": "bot",
                    "channels": ["CPUBLIC"],
                    "authority": "plm_bot",
                },
            ],
            "entities": [
                {"id": "PN-OWNED", "type": "part", "description": "Owned part"},
                {
                    "id": "SUB-ROLE-OWNED",
                    "type": "subsystem",
                    "description": "Subsystem owned by the reader role",
                    "owner": "ME",
                },
                {
                    "id": "ASM-CHILD",
                    "type": "assembly",
                    "description": "Child of the role-owned subsystem",
                    "subsystem": "SUB-ROLE-OWNED",
                },
                {
                    "id": "PN-GRANDCHILD",
                    "type": "part",
                    "description": "Child of the child assembly",
                    "parent": "ASM-CHILD",
                },
                {"id": "PN-OTHER", "type": "part", "description": "Other part"},
                {
                    "id": "PN-OTHER-ROLE",
                    "type": "part",
                    "description": "Part owned by another role",
                    "owner": "EE",
                },
                {"id": "G-DVT1", "type": "gate", "description": "DVT build"},
                {"id": "G-OTHER", "type": "gate", "description": "Other gate"},
            ],
            "events": [],
        }
    )


def reader(*, owned_entities: tuple[str, ...] = ("PN-OWNED", "G-DVT1")) -> Profile:
    return Profile(
        user_id="UREADER",
        name="Reader",
        role="ME",
        team="hardware",
        owned_entities=owned_entities,
        channels=("CPUBLIC", "CPRIVATE"),
    )


def priors(
    *,
    role_priors: dict[Role, dict[ItemType, float]] | None = None,
    min_score: float = 0.0,
) -> Priors:
    return Priors(
        triage=TriagePriors(miss_cost_ratio=10),
        absence=AbsencePriors(ask_reply_cadence_days=2),
        daily=DailyPriors(role_priors=role_priors or {}, min_score=min_score),
    )


def evidence(channel_id: str, suffix: int = 1) -> Evidence:
    ts = f"1791993600.{suffix:06d}"
    return Evidence(
        channel_id=channel_id,
        message_ts=ts,
        quote=f"Evidence in {channel_id}",
        permalink=f"https://kestrel.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
    )


def item(item_id: str, **changes: Any) -> Item:
    channel_ids = changes.pop("channel_ids", ("CPUBLIC",))
    values: dict[str, Any] = {
        "item_id": item_id,
        "thread_id": f"{channel_ids[0]}:{BASE_TS}",
        "type": "decision",
        "title": f"Title {item_id}",
        "summary": f"Summary {item_id}",
        "entities": ("PN-OTHER",),
        "owner": "UASKER",
        "due": None,
        "affected_roles": (),
        "severity": "medium",
        "confidence": "high",
        "evidence": tuple(
            evidence(channel_id, index) for index, channel_id in enumerate(channel_ids, 1)
        ),
        "first_seen": BASE_TS,
        "last_updated": BASE_TS,
    }
    return Item(**(values | changes))


def absence(
    item_id: str,
    *,
    source: Literal["checklist", "ask"] = "checklist",
    channel_ids: tuple[str, ...] = ("CPUBLIC",),
    asker: str | None = None,
    owner: str | None = "UASKER",
    affected_roles: tuple[Role, ...] = ("FW",),
) -> Item:
    detail = AbsenceDetail(
        source=source,
        gate="G-DVT1" if source == "checklist" else None,
        task="Complete thermal testing" if source == "checklist" else None,
        asker=asker if source == "ask" else None,
        days_silent=3,
        buffer_consumed=2 if source == "checklist" else None,
        buffer_days=7 if source == "checklist" else None,
    )
    return item(
        item_id,
        type="absence",
        absence=detail,
        channel_ids=channel_ids,
        owner=owner,
        affected_roles=affected_roles,
    )


def choose(
    items: list[Item] | tuple[Item, ...] = (),
    absences: list[Item] | tuple[Item, ...] = (),
    *,
    profile: Profile | None = None,
    table: Priors | None = None,
    day: date = DAY,
    previous_digest: date | None = None,
    newest_evidence: Mapping[str, int] | None = None,
) -> Selection:
    return select(
        world(),
        profile or reader(),
        items,
        absences,
        table or priors(),
        day,
        previous_digest=previous_digest,
        newest_evidence=newest_evidence if newest_evidence is not None else {},
    )


def slack_ts(moment: datetime) -> str:
    elapsed = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    micros = elapsed // timedelta(microseconds=1)
    seconds, fraction = divmod(micros, 1_000_000)
    return f"{seconds:010d}.{fraction:06d}"


def test_profile_for_builds_profile_from_human_persona() -> None:
    profile = profile_for(world(), "UREADER")

    assert profile.model_dump(exclude={"owned_entities"}) == reader().model_dump(
        exclude={"owned_entities"}
    )


def test_profile_for_closes_owned_entities_over_role_and_containment() -> None:
    profile = profile_for(world(), "UREADER")

    assert profile.owned_entities == (
        "ASM-CHILD",
        "G-DVT1",
        "PN-GRANDCHILD",
        "PN-OWNED",
        "SUB-ROLE-OWNED",
    )


@pytest.mark.parametrize(
    ("user_id", "error"),
    [("BBOT", ValueError), ("UNKNOWN", KeyError)],
    ids=["bot", "unknown"],
)
def test_profile_for_refuses_invalid_reader(user_id: str, error: type[Exception]) -> None:
    match = "bot" if user_id == "BBOT" else None
    with pytest.raises(error, match=match):
        profile_for(world(), user_id)


@pytest.mark.parametrize(
    ("channel_id", "expected"),
    [
        ("CPUBLIC", True),
        ("CPRIVATE", True),
        ("CHIDDEN", False),
        ("UNKNOWN", False),
    ],
    ids=["public", "private-member", "private-non-member", "unknown"],
)
def test_can_see_applies_channel_visibility(channel_id: str, expected: bool) -> None:
    profile = reader()
    if channel_id == "CPUBLIC":
        profile = profile.model_copy(update={"channels": ("CPRIVATE",)})
    assert can_see(world(), profile, channel_id) is expected


def test_visibility_keeps_public_channel_for_non_member() -> None:
    profile = reader().model_copy(update={"channels": ("CPRIVATE",)})

    selection = choose([item("public", channel_ids=("CPUBLIC",))], profile=profile)

    assert [line.item.item_id for line in selection.lines] == ["public"]
    assert selection.hidden == 0


def test_visibility_drops_item_in_hidden_thread() -> None:
    row = item("hidden", channel_ids=("CHIDDEN",))

    selection = choose([row])

    assert selection.lines == ()
    assert selection.hidden == 1


def test_visibility_keeps_only_evidence_from_visible_thread_channel() -> None:
    original = item("mixed", channel_ids=("CPUBLIC", "CPRIVATE"))

    selection = choose([original])

    assert len(selection.lines) == 1
    assert selection.lines[0].item.evidence == (original.evidence[0],)
    assert selection.lines[0].item is not original
    assert len(original.evidence) == 2
    assert selection.hidden == 0


def test_visibility_drops_item_with_no_evidence_from_thread_channel() -> None:
    row = item("no-thread-evidence").model_copy(update={"evidence": (evidence("CPRIVATE"),)})

    selection = choose([row])

    assert selection.lines == ()
    assert selection.hidden == 1


def test_visibility_uses_evidence_channel_id_instead_of_permalink() -> None:
    row = item("mismatched-evidence").model_copy(
        update={
            "evidence": (
                evidence("CPRIVATE").model_copy(
                    update={
                        "permalink": (
                            "https://kestrel.slack.com/archives/CPUBLIC/p1791993600000001"
                        )
                    }
                ),
            )
        }
    )

    selection = choose([row])

    assert selection.lines == ()
    assert selection.hidden == 1


def test_visibility_applies_thread_channel_to_absences() -> None:
    hidden = absence("absence-hidden", channel_ids=("CHIDDEN",), affected_roles=("ME",))
    visible = absence(
        "absence-visible",
        channel_ids=("CPUBLIC", "CPRIVATE"),
        affected_roles=("ME",),
    )

    selection = choose(absences=[hidden, visible])

    assert [line.item.item_id for line in selection.lines] == ["absence-visible"]
    assert selection.lines[0].item.evidence == (visible.evidence[0],)
    assert visible.evidence[1] not in selection.lines[0].item.evidence
    assert selection.hidden == 1


@pytest.mark.parametrize(
    ("row", "reason_terms"),
    [
        (item("ask", type="ask", owner="UREADER"), ("ask",)),
        (item("blocker", type="blocker", entities=("PN-OWNED",)), ("blocker", "owned")),
        (
            item("schedule", type="schedule_change", entities=("G-DVT1",)),
            ("schedule", "gate"),
        ),
        (absence("checklist", affected_roles=("ME",)), ("checklist",)),
        (
            absence("asker", source="ask", asker="UREADER", affected_roles=()),
            ("ask",),
        ),
        (
            absence("asked", source="ask", asker="UASKER", owner="UREADER", affected_roles=()),
            ("ask",),
        ),
    ],
    ids=["owned-ask", "owned-blocker", "owned-gate", "checklist", "asker", "asked"],
)
def test_each_must_see_rule_is_kept(row: Item, reason_terms: tuple[str, ...]) -> None:
    selection = choose(
        absences=[row] if row.type == "absence" else [],
        items=[] if row.type == "absence" else [row],
        table=priors(min_score=1.1),
    )

    assert len(selection.lines) == 1
    line = selection.lines[0]
    assert line.must_see is True
    assert line.score == 0
    assert all(term in line.reason.lower() for term in reason_terms)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("EE", (("ee-checklist", True),)),
        ("EM", (("ee-checklist", True),)),
        ("ME", ()),
    ],
    ids=["task-owner-role", "engineering-manager", "other-role"],
)
def test_checklist_absence_reaches_only_task_role_and_em(
    role: Role, expected: tuple[tuple[str, bool], ...]
) -> None:
    missing = absence("ee-checklist", affected_roles=("EE",))
    profile = reader().model_copy(update={"role": role})
    table = priors(role_priors={role: {"absence": 1.0}}, min_score=0.01)

    selection = choose(absences=[missing], profile=profile, table=table)

    assert tuple((line.item.item_id, line.must_see) for line in selection.lines) == expected


def test_role_owned_entity_makes_blocker_must_see_when_persona_omits_it() -> None:
    profile = profile_for(world(), "UREADER")
    blocker = item("role-owned-blocker", type="blocker", entities=("SUB-ROLE-OWNED",))

    selection = choose([blocker], profile=profile, table=priors(min_score=1.1))

    assert len(selection.lines) == 1
    assert selection.lines[0].must_see is True


@pytest.mark.parametrize(
    ("row", "profile"),
    [
        (item("blocker-other", type="blocker", entities=("PN-OTHER",)), reader()),
        (item("schedule-other-gate", type="schedule_change", entities=("G-OTHER",)), reader()),
        (
            item("schedule-owned-part", type="schedule_change", entities=("PN-OWNED",)),
            reader(owned_entities=("PN-OWNED",)),
        ),
        (
            absence(
                "ask-third-party",
                source="ask",
                asker="UASKER",
                owner="UEMPTY",
                affected_roles=(),
            ),
            reader(),
        ),
    ],
    ids=["blocker-not-owned", "gate-not-owned", "owned-part-not-gate", "ask-third-party"],
)
def test_items_outside_must_see_rules_are_ranked(row: Item, profile: Profile) -> None:
    selection = choose(
        absences=[row] if row.type == "absence" else [],
        items=[] if row.type == "absence" else [row],
        profile=profile,
    )

    assert selection.lines[0].must_see is False


def test_must_see_line_is_kept_above_section_cap() -> None:
    must_see = item("must", type="blocker", entities=("PN-OWNED",))
    ranked = [item(f"risk-{index}", type="risk") for index in range(SECTION_CAPS["risks"] + 2)]

    selection = choose([*ranked, must_see])
    risks = [line for line in selection.lines if line.section == "risks"]

    assert len(risks) == SECTION_CAPS["risks"] + 1
    assert [line.item.item_id for line in risks if line.must_see] == ["must"]


@pytest.mark.parametrize(
    ("entities", "affected_roles", "relevance"),
    [
        (("PN-OWNED",), (), 1.0),
        (("PN-OTHER",), ("ME",), 0.8),
        (("PN-OTHER",), (), 0.4),
    ],
    ids=["owned", "affected-role", "neither"],
)
def test_ranked_score_names_all_terms(
    entities: tuple[str, ...], affected_roles: tuple[Role, ...], relevance: float
) -> None:
    table = priors(role_priors={"ME": {"decision": 0.6}})
    row = item(
        "scored",
        severity="high",
        entities=entities,
        affected_roles=affected_roles,
    )

    line = choose([row], table=table).lines[0]

    assert line.score == pytest.approx(0.9 * 0.6 * relevance)
    assert "importance=0.9" in line.reason
    assert "role_prior=0.6" in line.reason
    assert f"personal_relevance={relevance}" in line.reason


def test_transitive_child_of_owned_entity_has_full_personal_relevance() -> None:
    profile = profile_for(world(), "UREADER")
    row = item("owned-grandchild", severity="high", entities=("PN-GRANDCHILD",))

    line = choose(
        [row],
        profile=profile,
        table=priors(role_priors={"ME": {"decision": 1.0}}),
    ).lines[0]

    assert line.score == pytest.approx(0.9)
    assert "personal_relevance=1.0" in line.reason


@pytest.mark.parametrize(
    ("item_type", "type_weight"),
    [
        ("blocker", 1.0),
        ("decision", 0.9),
        ("schedule_change", 0.9),
        ("spec_change", 0.8),
        ("bom_change", 0.8),
        ("risk", 0.7),
        ("ask", 0.6),
        ("absence", 0.9),
        ("fyi", 0.3),
    ],
)
@pytest.mark.parametrize(
    ("severity", "severity_weight"),
    [("high", 1.0), ("medium", 0.7), ("low", 0.4)],
)
def test_ranked_score_uses_type_and_severity_weights(
    item_type: ItemType,
    type_weight: float,
    severity: Severity,
    severity_weight: float,
) -> None:
    row = (
        absence(
            "weighted",
            source="ask",
            asker="UASKER",
            owner="UEMPTY",
            affected_roles=(),
        ).model_copy(update={"severity": severity})
        if item_type == "absence"
        else item("weighted", type=item_type, severity=severity)
    )

    selection = choose(
        absences=[row] if item_type == "absence" else [],
        items=[] if item_type == "absence" else [row],
        table=priors(role_priors={"ME": {}}),
    )

    line = selection.lines[0]
    importance = type_weight * severity_weight
    assert line.score == pytest.approx(importance * 0.5 * 0.4)


def test_ranked_score_uses_default_role_prior() -> None:
    line = choose([item("default", severity="high")]).lines[0]

    assert line.score == pytest.approx(0.9 * 0.5 * 0.4)
    assert "role_prior=0.5" in line.reason


def test_ranked_item_below_minimum_score_is_left_out() -> None:
    table = priors(role_priors={"ME": {"fyi": 0.1}}, min_score=0.02)
    row = item("small", type="fyi", severity="low")

    assert choose([row], table=table).lines == ()


def test_bundled_daily_priors_are_used_for_ranking() -> None:
    table = load_priors(Path("config/priors.yaml"))
    row = item("bundled", severity="medium", affected_roles=("ME",))

    line = choose([row], table=table).lines[0]

    assert line.score == pytest.approx(0.9 * 0.7 * 0.9 * 0.8)
    assert "role_prior=0.9" in line.reason


@pytest.mark.parametrize(
    ("item_type", "section"),
    [
        ("ask", "fyi"),
        ("absence", "not_happened"),
        ("decision", "decisions"),
        ("schedule_change", "decisions"),
        ("spec_change", "decisions"),
        ("bom_change", "decisions"),
        ("blocker", "risks"),
        ("risk", "risks"),
        ("fyi", "fyi"),
    ],
)
def test_each_item_type_is_assigned_to_its_section(item_type: ItemType, section: str) -> None:
    row = (
        absence("row", affected_roles=("ME",))
        if item_type == "absence"
        else item("row", type=item_type)
    )

    selection = choose(
        absences=[row] if item_type == "absence" else [],
        items=[] if item_type == "absence" else [row],
    )

    assert selection.lines[0].section == section


def test_ask_owned_by_reader_is_must_see_in_needs_you() -> None:
    ask = item("ask-for-reader", type="ask", owner="UREADER")

    selection = choose([ask], table=priors(min_score=1.1))

    assert [(line.item.item_id, line.section, line.must_see) for line in selection.lines] == [
        ("ask-for-reader", "needs_you", True)
    ]


def test_ask_by_reader_for_someone_else_is_ranked_in_fyi() -> None:
    ask = item(
        "ask-by-reader",
        type="ask",
        owner="UEMPTY",
        severity="high",
        title="Reader asks the firmware owner for an update",
    )
    table = priors(role_priors={"ME": {"ask": 1.0}}, min_score=0.2)

    selection = choose([ask], table=table)

    assert [(line.item.item_id, line.section, line.must_see) for line in selection.lines] == [
        ("ask-by-reader", "fyi", False)
    ]
    assert selection.lines[0].score > table.daily.min_score


def test_ask_absence_removes_only_ask_named_in_id() -> None:
    first_ask = item("ask-first", type="ask", owner="UREADER")
    second_ask = item("ask-second", type="ask", owner="UREADER")
    missing = absence(
        f"absence:ask:{first_ask.item_id}",
        source="ask",
        asker="UASKER",
        owner="UREADER",
        affected_roles=(),
    ).model_copy(update={"thread_id": first_ask.thread_id})

    selection = choose([first_ask, second_ask], [missing])

    assert [(line.item.item_id, line.section) for line in selection.lines] == [
        ("absence:ask:ask-first", "needs_you"),
        ("ask-second", "needs_you"),
    ]


def test_ask_in_another_thread_keeps_line_beside_absence() -> None:
    ask = item("ask", type="ask", owner="UREADER")
    other_ask = item(
        "ask-other-thread",
        type="ask",
        owner="UREADER",
        thread_id="CPUBLIC:1791993601.000001",
    )
    missing = absence(
        f"absence:ask:{ask.item_id}",
        source="ask",
        asker="UASKER",
        owner="UREADER",
        affected_roles=(),
    ).model_copy(update={"thread_id": ask.thread_id})

    selection = choose([ask, other_ask], [missing])

    assert [(line.item.item_id, line.section) for line in selection.lines] == [
        ("absence:ask:ask", "needs_you"),
        ("ask-other-thread", "needs_you"),
    ]


def test_ask_absence_for_asker_goes_to_not_happened() -> None:
    ask = item("ask", type="ask", owner="UEMPTY")
    missing = absence(
        f"absence:ask:{ask.item_id}",
        source="ask",
        asker="UREADER",
        owner="UEMPTY",
        affected_roles=(),
    ).model_copy(update={"thread_id": ask.thread_id})

    selection = choose([ask], [missing])

    assert [(line.item.item_id, line.section) for line in selection.lines] == [
        ("absence:ask:ask", "not_happened")
    ]


def test_ranked_ask_absence_replaces_ask_without_fyi_copy_for_third_party_reader() -> None:
    ask = item("ask", type="ask", owner="UREADER")
    missing = absence(
        f"absence:ask:{ask.item_id}",
        source="ask",
        asker="UASKER",
        owner="UREADER",
        affected_roles=(),
    ).model_copy(update={"thread_id": ask.thread_id})

    selection = choose([ask], [missing], profile=profile_for(world(), "UEMPTY"))

    assert [(line.item.item_id, line.section) for line in selection.lines] == [
        ("absence:ask:ask", "not_happened")
    ]
    assert all(line.section != "fyi" for line in selection.lines)
    assert selection.lines[0].must_see is False


def test_ranked_ask_stays_when_ask_absence_is_below_minimum_score() -> None:
    ask = item("ask", type="ask", owner="UEMPTY")
    missing = absence(
        f"absence:ask:{ask.item_id}",
        source="ask",
        asker="UASKER",
        owner="UEMPTY",
        affected_roles=(),
    ).model_copy(update={"thread_id": ask.thread_id})
    table = priors(
        role_priors={"ME": {"ask": 1.0, "absence": 0.1}},
        min_score=0.03,
    )

    selection = choose([ask], [missing], table=table)

    assert [(line.item.item_id, line.section) for line in selection.lines] == [("ask", "fyi")]
    assert selection.lines[0].must_see is False


def test_ranked_ties_use_newest_evidence_then_item_id() -> None:
    table = priors(
        role_priors={
            "ME": {
                "decision": 1.0,
                "schedule_change": 0.9999995,
            }
        }
    )
    rows = [
        item("ranked-a-old", evidence=(evidence("CPUBLIC", 1),)),
        item(
            "ranked-z-new",
            type="schedule_change",
            evidence=(evidence("CPUBLIC", 4),),
        ),
        item("ranked-c-equal", evidence=(evidence("CPUBLIC", 3),)),
        item(
            "ranked-b-equal",
            type="schedule_change",
            evidence=(evidence("CPUBLIC", 3),),
        ),
    ]

    selection = choose(rows, table=table)

    assert len({format(line.score, ".6g") for line in selection.lines}) == 1
    assert [line.item.item_id for line in selection.lines] == [
        "ranked-z-new",
        "ranked-b-equal",
        "ranked-c-equal",
        "ranked-a-old",
    ]


def test_must_see_ties_stay_in_item_id_order() -> None:
    rows = [
        item(
            "must-z-new",
            type="ask",
            owner="UREADER",
            evidence=(evidence("CPUBLIC", 4),),
        ),
        item(
            "must-a-old",
            type="ask",
            owner="UREADER",
            evidence=(evidence("CPUBLIC", 1),),
        ),
    ]

    selection = choose(rows)

    assert [line.item.item_id for line in selection.lines] == ["must-a-old", "must-z-new"]


def test_each_section_caps_only_ranked_lines() -> None:
    rows = [item(f"ask-{index}", type="ask") for index in range(7)]
    rows += [item(f"ask-owned-{index}", type="ask", owner="UREADER") for index in range(7)]
    rows += [item(f"decision-{index}") for index in range(7)]
    rows += [
        item("risk-low-c", type="risk", severity="low"),
        item("risk-high-b", type="risk", severity="high"),
        item("risk-medium-b", type="risk", severity="medium"),
        item("risk-low-b", type="risk", severity="low"),
        item("risk-high-a", type="risk", severity="high"),
        item("risk-medium-a", type="risk", severity="medium"),
        item("risk-low-a", type="risk", severity="low"),
    ]
    rows += [item(f"fyi-{index}", type="fyi") for index in range(7)]
    missing = [
        absence(
            f"absence-{index}",
            source="ask",
            asker="UASKER",
            owner="UEMPTY",
            affected_roles=(),
        )
        for index in range(5)
    ]
    rows.append(item("risk-must", type="blocker", entities=("PN-OWNED",)))

    selection = choose(rows, missing)
    counts = Counter(line.section for line in selection.lines)

    assert counts == {
        "needs_you": 7,
        "not_happened": 3,
        "decisions": 5,
        "risks": 6,
        "fyi": 5,
    }
    risk_lines = [line for line in selection.lines if line.section == "risks"]
    assert [line.item.item_id for line in risk_lines] == [
        "risk-must",
        "risk-high-a",
        "risk-high-b",
        "risk-medium-a",
        "risk-medium-b",
        "risk-low-a",
    ]


def test_resolved_and_after_day_items_are_left_out() -> None:
    end = datetime.combine(DAY, time.max, ZoneInfo(world().timezone))
    boundary = slack_ts(end)
    after = slack_ts(end + timedelta(microseconds=1))
    rows = [
        item("open-boundary", last_updated=boundary),
        item("resolved", status="resolved"),
        item("future", last_updated=after),
    ]

    selection = choose(rows)

    assert [line.item.item_id for line in selection.lines] == ["open-boundary"]


def test_lines_have_stable_section_and_rank_order() -> None:
    rows = [
        item("needs-must", type="ask", owner="UREADER"),
        item("decision-low-b", severity="low"),
        item("decision-high", severity="high"),
        item("decision-low-a", severity="low"),
        item("risk-must", type="blocker", entities=("PN-OWNED",)),
        item("risk-ranked", type="risk", severity="high"),
        item("fyi-ranked", type="fyi", severity="high"),
    ]
    missing = [absence("absence-must", affected_roles=("ME",))]

    first = choose(rows, missing)
    second = choose(rows, missing)

    assert [line.item.item_id for line in first.lines] == [
        "needs-must",
        "absence-must",
        "decision-high",
        "decision-low-a",
        "decision-low-b",
        "risk-must",
        "risk-ranked",
        "fyi-ranked",
    ]
    assert first == second


def test_reader_who_owns_nothing_gets_ranked_lines() -> None:
    profile = reader(owned_entities=())

    selection = choose([item("unowned", severity="high")], profile=profile)

    assert [line.item.item_id for line in selection.lines] == ["unowned"]
    assert selection.lines[0].must_see is False
    assert "personal_relevance=0.4" in selection.lines[0].reason


def test_no_items_returns_empty_selection() -> None:
    selection = choose()

    assert selection.profile == reader()
    assert selection.day == DAY
    assert selection.lines == ()
    assert selection.hidden == 0


def test_all_evidence_hidden_returns_only_hidden_count() -> None:
    selection = choose([item("private", channel_ids=("CHIDDEN",))])

    assert selection.lines == ()
    assert selection.hidden == 1


@pytest.mark.parametrize(
    "item_type", ["decision", "schedule_change", "spec_change", "bom_change", "risk", "fyi"]
)
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "at", "after"])
def test_window_uses_newest_evidence_after_previous_day_end(
    item_type: ItemType, offset: int
) -> None:
    """W1: compare newest evidence even when last_updated is in the window."""
    previous = date(2026, 10, 13)
    boundary = datetime.combine(previous, time.max, ZoneInfo(world().timezone))
    row = item(
        "updated",
        type=item_type,
        first_seen=slack_ts(boundary - timedelta(days=30)),
        last_updated=BASE_TS,
    )

    selected = choose(
        [row],
        previous_digest=previous,
        newest_evidence={
            row.item_id: ts_micros(slack_ts(boundary + timedelta(microseconds=offset)))
        },
    )

    assert [line.item.item_id for line in selected.lines] == (["updated"] if offset > 0 else [])
    assert selected.hidden == 0


@pytest.mark.parametrize("kind", ["blocker", "ask", "ask-absence", "checklist-absence", "decision"])
def test_window_keeps_only_open_ranked_exceptions(kind: str) -> None:
    """W3: only old blockers and ask absences remain ranked candidates."""
    old = slack_ts(datetime(2026, 9, 1, tzinfo=UTC))
    row = (
        absence(
            "old",
            source="ask" if kind == "ask-absence" else "checklist",
            asker="UASKER",
            owner="UEMPTY",
            affected_roles=(),
        )
        if kind.endswith("absence")
        else item("old", type=kind, owner="UEMPTY")
    ).model_copy(update={"first_seen": old, "last_updated": old})

    selected = choose(
        [] if row.type == "absence" else [row],
        [row] if row.type == "absence" else [],
        previous_digest=date(2026, 10, 13),
    )

    assert [(line.item.item_id, line.must_see) for line in selected.lines] == (
        [("old", False)] if kind in {"blocker", "ask-absence"} else []
    )


@pytest.mark.parametrize("item_type", ["blocker", "ask"])
def test_window_does_not_restore_resolved_exceptions(item_type: ItemType) -> None:
    """W3: an age exception does not reopen a resolved item."""
    row = item("resolved", type=item_type, status="resolved")

    assert choose([row], previous_digest=date(2026, 10, 13)).lines == ()


@pytest.mark.parametrize(
    "row",
    [
        item("ask", type="ask", owner="UREADER"),
        item("blocker", type="blocker", entities=("PN-OWNED",)),
        item("gate", type="schedule_change", entities=("G-DVT1",)),
        absence("checklist", affected_roles=("ME",)),
        absence("asker", source="ask", asker="UREADER", owner="UEMPTY"),
        absence("asked", source="ask", asker="UASKER", owner="UREADER"),
    ],
    ids=["owned-ask", "owned-blocker", "owned-gate", "checklist", "asker", "asked"],
)
def test_window_preserves_old_must_see_lines(row: Item) -> None:
    """W4: every current must-see rule bypasses the window and score threshold."""
    old = slack_ts(datetime(2026, 9, 1, tzinfo=UTC))
    row = row.model_copy(update={"first_seen": old, "last_updated": old})
    items = [] if row.type == "absence" else [row]
    absences = [row] if row.type == "absence" else []
    table = priors(min_score=1.1)

    first = choose(items, absences, table=table, previous_digest=None)
    later = choose(items, absences, table=table, previous_digest=date(2026, 10, 13))

    assert [(line.item.item_id, line.must_see) for line in later.lines] == [(row.item_id, True)]
    assert later.lines == first.lines


@pytest.mark.parametrize(
    ("previous", "boundary_day"),
    [
        (None, date(2026, 10, 12)),
        (date(2026, 9, 1), date(2026, 10, 5)),
        (date(2026, 10, 5), date(2026, 10, 5)),
        (date(2026, 10, 15), date(2026, 10, 15)),
    ],
    ids=["first-five-weekdays", "cap-ten-weekdays", "exact-cap", "recent-previous"],
)
def test_window_default_working_day_boundaries(previous: date | None, boundary_day: date) -> None:
    """W5: count weekdays back from Monday, without clamping to world start."""
    boundary = datetime.combine(boundary_day, time.max, ZoneInfo(world().timezone))
    rows = [
        item("at", last_updated=slack_ts(boundary)),
        item("after", last_updated=slack_ts(boundary + timedelta(microseconds=1))),
    ]

    selected = choose(rows, day=date(2026, 10, 19), previous_digest=previous)

    assert [line.item.item_id for line in selected.lines] == ["after"]


@pytest.mark.parametrize(
    "previous",
    [None, date(2026, 9, 1), date(2026, 10, 16)],
    ids=["first", "capped", "friday-to-monday"],
)
def test_window_reads_configured_working_days(previous: date | None) -> None:
    """W5: a configured one-day window on Monday starts at Friday's end."""
    data = priors().model_dump()
    data["daily"]["window"] = {"first_working_days": 1, "max_working_days": 1}
    table = Priors.model_validate(data)
    boundary = datetime(2026, 10, 16, 23, 59, 59, 999999, ZoneInfo(world().timezone))
    rows = [
        item("at", last_updated=slack_ts(boundary)),
        item("weekend", last_updated=slack_ts(boundary + timedelta(microseconds=1))),
    ]

    selected = choose(rows, table=table, day=date(2026, 10, 19), previous_digest=previous)

    assert [line.item.item_id for line in selected.lines] == ["weekend"]


@pytest.mark.parametrize("edited", [False, True], ids=["old-message", "edited-message"])
def test_window_uses_cited_message_event_time(edited: bool) -> None:
    """W2: an edit admits evidence whose message time is before the window."""
    previous = date(2026, 10, 13)
    boundary = datetime.combine(previous, time.max, ZoneInfo(world().timezone))
    old = slack_ts(boundary - timedelta(days=1))
    event = Event(
        source="replay",
        channel_id="CPUBLIC",
        channel_name="program",
        thread_ts=old,
        ts=old,
        event_ts=slack_ts(boundary + timedelta(microseconds=1)) if edited else old,
        user_id="UASKER",
        text="Drawing released.",
        edited=edited,
        permalink="https://kestrel.slack.com/archives/CPUBLIC/p" + old.replace(".", ""),
    )
    row = item(
        "edited",
        first_seen=old,
        last_updated=old,
        evidence=(
            Evidence(
                channel_id=event.channel_id,
                message_ts=event.ts,
                quote=event.text,
                permalink=event.permalink,
            ),
        ),
    )

    selected = choose(
        [row],
        previous_digest=previous,
        newest_evidence={row.item_id: ts_micros(event.event_ts)},
    )

    assert [line.item.item_id for line in selected.lines] == (["edited"] if edited else [])


@pytest.mark.parametrize("offset", [0, 1], ids=["day-end", "after-day"])
def test_window_excludes_evidence_after_digest_day(offset: int) -> None:
    boundary = datetime.combine(DAY, time.max, ZoneInfo(world().timezone))
    row = item("future")

    selected = choose(
        [row],
        previous_digest=date(2026, 10, 13),
        newest_evidence={"future": ts_micros(slack_ts(boundary + timedelta(microseconds=offset)))},
    )

    assert [line.item.item_id for line in selected.lines] == ([] if offset else ["future"])

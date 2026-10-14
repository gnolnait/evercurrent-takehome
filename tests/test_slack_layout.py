"""Examples from the v0.4 layout contract. A22 needs a live manual check."""

from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from slack_sdk.errors import SlackApiError
from typer.testing import CliRunner

import digest.cli as cli
from digest.daily.profile import Profile
from digest.daily.render import context_lines, render_digest, select_digest
from digest.daily.select import Line, Section, Selection
from digest.models import AbsenceDetail, Evidence, Item
from digest.project import ChecklistTask, Gate, PhaseWindow, ProjectConfig
from digest.seed import MappedFile, MappedMessage, SeedMap
from digest.world.schema import Channel, Entity, Persona, World
from slack_checks import build_digest, check_message

DAY = date(2026, 10, 19)
ROOT = Path(__file__).parents[1]
TS = "1792407600.000001"


def source(ts: str = TS, *, quote: str = "Can you give an ETA?", channel: str = "CENG") -> Evidence:
    return Evidence(
        channel_id=channel,
        message_ts=ts,
        quote=quote,
        permalink=f"https://kestrel-k2.slack.com/archives/{channel}/p{ts.replace('.', '')}",
    )


def row(
    number: int = 1,
    *,
    section: Section = "risks",
    must: bool = True,
    score: float = 0.5,
    **changes: Any,
) -> Line:
    evidence = source(f"1792407600.{number:06d}")
    values = dict(
        item_id=f"I-{number}",
        thread_id=f"CENG:{evidence.message_ts}",
        type="risk",
        title=f"Fact {number}",
        summary="First sentence. Second sentence!",
        entities=(),
        owner=None,
        due=None,
        affected_roles=("ME",),
        severity="high",
        confidence="high",
        evidence=(evidence,),
        first_seen=evidence.message_ts,
        last_updated=evidence.message_ts,
    )
    return Line(
        item=Item(**(values | changes)),
        section=section,
        must_see=must,
        reason="contract example",
        score=score,
    )


def absence(
    number: int = 1,
    *,
    task: str = "thermal",
    gate: str = "G-A",
    days: int = 3,
    used: int | None = 2,
    asker: str | None = None,
    **changes: Any,
) -> Line:
    detail = AbsenceDetail(
        source="ask" if asker is not None else "checklist",
        gate=None if asker else gate,
        task=None if asker else task,
        asker=asker,
        days_silent=days,
        buffer_consumed=None if asker else used,
        buffer_days=None if asker else 7,
    )
    return row(number, section="not_happened", type="absence", absence=detail, **changes)


def chosen(profile: Profile, *lines: Line, **changes: Any) -> Selection:
    return Selection(
        **(
            dict(
                profile=profile, day=DAY, lines=lines, hidden=0, previous_digest=date(2026, 10, 16)
            )
            | changes
        )
    )


def mrkdwn(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": mrkdwn(text)}


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [mrkdwn(text)]}


def pairs(message: dict[str, Any]) -> list[tuple[str, str]]:
    blocks = message["blocks"]
    return [
        (block["text"]["text"], blocks[index + 1]["elements"][0]["text"])
        for index, block in enumerate(blocks[:-1])
        if block["type"] == "section"
        and blocks[index + 1]["type"] == "context"
        and block["text"]["text"].startswith("*")
        and not block["text"]["text"].startswith("*G")
    ]


@pytest.fixture
def profile() -> Profile:
    return Profile(
        user_id="UREADER",
        name="Nadia Brenner",
        role="ME",
        team="mechanical",
        owned_entities=(),
        channels=("CENG", "CPROG"),
    )


@pytest.fixture
def world() -> World:
    return World(
        name="kestrel",
        program="Kestrel K2",
        workspace_url="https://kestrel-k2.slack.com",
        timezone="America/Los_Angeles",
        start_date=date(2026, 10, 12),
        days=20,
        project=ProjectConfig(
            project_id="K2",
            name="Kestrel K2",
            phases=(PhaseWindow(phase="DVT", from_date=date(2026, 10, 12)),),
            gates=(),
        ),
        channels=(
            Channel(id="CPROG", name="program", purpose="program"),
            Channel(id="CENG", name="eng-me", purpose="engineering"),
        ),
        personas=(
            Persona(user_id="UASKER", name="Grace Chen", role="EM", channels=("CPROG", "CENG")),
            Persona(user_id="UREADER", name="Nadia Brenner", role="ME", channels=("CENG",)),
            Persona(
                user_id="UOTHER", name="Hamid Rostami", role="supply_chain", channels=("CENG",)
            ),
            Persona(user_id="UBOT", name="Parts bot", kind="bot", channels=("CENG",)),
        ),
        entities=(
            Entity(id="PN-1", type="part", description="part"),
            Entity(id="G-TYPE", type="gate", description="gate without date"),
            Entity(id="V-1", type="vendor", description="vendor"),
            Entity(id="SUB-1", type="subsystem", description="subsystem"),
            Entity(id="BUILD-1", type="build", description="build"),
        ),
        events=(),
    )


def gates(world: World, *values: Gate) -> World:
    return world.model_copy(update={"project": world.project.model_copy(update={"gates": values})})


def gate(code: str = "G-A", *, offset: int = 1, buffer: int = 7, owner: str = "EE") -> Gate:
    return Gate(
        id=code,
        name="Review (PN-1)",
        date=DAY + timedelta(days=offset),
        buffer_days=buffer,
        checklist=(
            ChecklistTask(
                id="thermal" if code == "G-A" else code + ".thermal",
                task="Close the TST-THM-02 thermal review",
                owner=owner,
                expected_cadence_days=2,
            ),
        ),
    )


def mapping(*entries: Evidence, files: dict[str, MappedFile] | None = None) -> SeedMap:
    return SeedMap(
        team_id="TTEST",
        workspace_url="https://scratch.slack.com",
        world="kestrel",
        channels={"CENG": "C111", "CPROG": "C222"},
        files=files or {},
        messages={
            f"{entry.channel_id}:{entry.message_ts}": MappedMessage(
                channel="C111" if entry.channel_id == "CENG" else "C222",
                ts=entry.message_ts,
                thread_ts=TS,
            )
            for entry in entries
        },
    )


@pytest.mark.parametrize("hidden", [0, 1, 2])
@pytest.mark.parametrize("has_item", [False, True])
@pytest.mark.parametrize("phase", [False, True])
def test_top_block_order_and_context(
    world: World, profile: Profile, hidden: int, has_item: bool, phase: bool
) -> None:
    """A1, A4: exact top blocks, including the empty form and the phase twin."""
    if not phase:
        world = world.model_copy(
            update={
                "project": world.project.model_copy(
                    update={
                        "phases": (PhaseWindow(phase="DVT", from_date=DAY + timedelta(days=1)),)
                    }
                )
            }
        )
    selected = chosen(profile, *((row(),) if has_item else ()), hidden=hidden)
    messages = build_digest(world, selected)
    expected = [
        {"type": "header", "text": {"type": "plain_text", "text": "Kestrel K2 · Mon 19 Oct"}},
        context("\n".join(context_lines(world, selected))),
        section("*1 to read first*. 1 of 1 are new." if has_item else "Nothing new for you."),
    ]
    if has_item:
        expected += [
            {"type": "divider"},
            section("*Fact 1*\nFirst sentence."),
            context("*Risk* · New · In #eng-me"),
        ]
    if hidden:
        noun = "item" if hidden == 1 else "items"
        expected += [
            {"type": "divider"},
            context(f"{hidden} {noun} left out: from channels you cannot see."),
        ]
    assert messages == [
        {
            "text": "1 to read first, starting with Fact 1."
            if has_item
            else "Nothing new for you.",
            "blocks": expected,
        }
    ]


@pytest.mark.parametrize(
    ("owned", "role", "entities", "expected"),
    [
        (("G-B", "G-PAST"), "ME", ("G-C",), "G-B"),
        ((), "ME", ("G-C",), "G-A"),
        ((), "PM", ("G-C", "G-B"), "G-C"),
        ((), "PM", (), None),
    ],
)
def test_gate_rule_priority_and_past_gate(
    world: World,
    profile: Profile,
    owned: tuple[str, ...],
    role: str,
    entities: tuple[str, ...],
    expected: str | None,
) -> None:
    """A2: each rule beats later rules. Past dates cannot win."""
    world = gates(
        world,
        gate("G-B", offset=3, owner="FW"),
        gate("G-C", offset=0, owner="EE"),
        gate("G-PAST", offset=-1, owner="ME"),
        gate(owner="ME"),
    )
    profile = profile.model_copy(update={"owned_entities": owned, "role": role})
    top = build_digest(world, chosen(profile, row(entities=entities)))[0]
    when = {
        "G-A": "tomorrow, Tue 20 Oct",
        "G-B": "in 3 days, Thu 22 Oct",
        "G-C": "today, Mon 19 Oct",
    }
    count = "*1 to read first*. 1 of 1 are new."
    assert top["blocks"][2] == section(
        count if expected is None else f"*Review ({expected}) is {when[expected]}.*\n{count}"
    )


@pytest.mark.parametrize(
    ("buffer", "used", "clause"),
    [
        (0, (9, 4), " No buffer days."),
        (7, (2, 5, 3), " 5 of 7 buffer days are used."),
        (7, (None,), ""),
        (7, (), ""),
    ],
)
def test_gate_tie_and_buffer(
    world: World, profile: Profile, buffer: int, used: tuple[int | None, ...], clause: str
) -> None:
    """A2: lower id wins equal dates. The buffer uses the largest absence value."""
    world = gates(world, gate("G-Z", buffer=buffer), gate(buffer=buffer))
    profile = profile.model_copy(update={"owned_entities": ("G-Z", "G-A")})
    lines = [absence(i + 1, used=value) for i, value in enumerate(used)] or [row()]
    top = build_digest(world, chosen(profile, *lines))[0]
    assert top["blocks"][2]["text"]["text"].splitlines()[0] == (
        "*Review (G-A) is tomorrow, Tue 20 Oct.*" + clause
    )


@pytest.mark.parametrize(
    ("must", "ranked", "count", "foot"),
    [
        (1, 0, "*1 to read first*", None),
        (5, 2, "*5 to read first*, 2 more in the thread", "Risks 2"),
        (
            7,
            2,
            "*7 to read first*: 5 below, 2 at the top of the thread, 2 more in the thread",
            "Read first 2, Risks 2",
        ),
        (6, 0, "*6 to read first*: 5 below, 1 at the top of the thread", "Read first 1"),
        (0, 1, "Nothing needs you today. *1 worth a look*", None),
        (0, 2, "Nothing needs you today. *2 worth a look*", None),
        (0, 3, "Nothing needs you today. *3 worth a look*", None),
        (0, 5, "Nothing needs you today. *3 worth a look*, 2 more in the thread", "Risks 2"),
        (0, 0, "Nothing new for you", None),
    ],
)
@pytest.mark.parametrize("first", [False, True])
@pytest.mark.parametrize("threaded", [False, True])
def test_count_templates_and_foot(
    world: World,
    profile: Profile,
    must: int,
    ranked: int,
    count: str,
    foot: str | None,
    first: bool,
    threaded: bool,
) -> None:
    """A3, A4: all count forms and both delivery vocabularies."""
    selected = chosen(
        profile,
        *(row(i + 1, must=i < must) for i in range(must + ranked)),
        previous_digest=None if first else date(2026, 10, 16),
    )
    messages = build_digest(world, selected, threaded=threaded)
    if not threaded:
        count = count.replace("more in the thread", "more below").replace(
            "at the top of the thread", "at the top of the next message"
        )
    suffix = f" {must + ranked} of {must + ranked} are new." if not first and must + ranked else ""
    assert messages[0]["blocks"][2] == section(count + "." + suffix)
    if foot is None:
        assert messages[0]["blocks"][-1]["type"] != "divider"
        assert len(messages) == 1
    else:
        prefix = "In the thread: " if threaded else "Next message: "
        assert messages[0]["blocks"][-2:] == [{"type": "divider"}, context(prefix + foot)]
        assert messages[1]["text"] == "More from your digest: " + foot


@pytest.mark.parametrize("threaded", [False, True])
def test_foot_combines_groups_and_hidden(world: World, profile: Profile, threaded: bool) -> None:
    """A4: the hidden part follows the complete list, in section order."""
    lines = [row(i, section="needs_you") for i in range(1, 7)]
    lines += [row(9, section="decisions", must=False), row(8, must=False)]
    top = build_digest(world, chosen(profile, *lines, hidden=1), threaded=threaded)[0]
    prefix = "In the thread" if threaded else "Next message"
    assert top["blocks"][-1] == context(
        f"{prefix}: Read first 1, Decisions 1, Risks 1. "
        "1 item left out: from channels you cannot see."
    )


@pytest.mark.parametrize(
    ("must", "title", "expected"),
    [
        (True, "Ready.", "1 to read first, starting with Ready."),
        (True, "Ready?", "1 to read first, starting with Ready?"),
        (True, "Ready!", "1 to read first, starting with Ready!"),
        (False, "Ready", "Worth a look: Ready."),
        (None, "", "Nothing new for you."),
    ],
)
@pytest.mark.parametrize("with_gate", [False, True])
def test_notification_forms(
    world: World, profile: Profile, must: bool | None, title: str, expected: str, with_gate: bool
) -> None:
    """A5: notification punctuation and the optional gate prefix."""
    if with_gate:
        world = gates(world, gate(owner="ME"))
        expected = "G-A tomorrow. " + expected
    selected = chosen(profile, *((row(title=title, must=must),) if must is not None else ()))
    assert build_digest(world, selected)[0]["text"] == expected


def test_notification_cut_counts_escaped_characters(world: World, profile: Profile) -> None:
    """A5, A16: escaped width sets the phone limit and preserves word boundaries."""
    top = build_digest(world, chosen(profile, row(title="& " * 60)))[0]
    assert top["text"] == "1 to read first, starting with " + "&amp; " * 19 + "&amp;…"


def test_top_sort_keys_and_overflow_labels(world: World, profile: Profile) -> None:
    """A6: each top key changes a pair. Overflow keeps top order and labels."""
    world = gates(world, gate(), gate("G-B", offset=3))
    lines = [
        row(9, section="fyi", entities=("G-A",)),
        row(8),
        row(6, entities=("G-B",)),
        row(7, entities=("G-A",)),
        absence(2, days=1, gate="G-B", task="G-B.thermal"),
        absence(3, days=2, thread_id="CPROG:1792407600.000003"),
        absence(4, days=5),
        absence(5, days=2),
        row(1, section="needs_you"),
    ]
    messages = build_digest(world, chosen(profile, *lines))
    top_titles = [text.splitlines()[0] for text, _ in pairs(messages[0])]
    assert top_titles == [
        "*Fact 1*",
        "*TST-THM-02 thermal review: no progress evidence for 5 working days*",
        "*TST-THM-02 thermal review: no progress evidence for 2 working days*",
        "*TST-THM-02 thermal review: no progress evidence for 2 working days*",
        "*TST-THM-02 thermal review: no progress evidence for 1 working day*",
    ]
    assert [meta.split(" · ")[-2:] for _, meta in pairs(messages[0])[2:4]] == [
        ["In #program", "checked: #program threads"],
        ["In #eng-me", "checked: #eng-me threads"],
    ]
    assert messages[1]["blocks"][0] == section("*Read first* · 4")
    assert [text.splitlines()[0] for text, _ in pairs(messages[1])] == [
        "*Fact 7*",
        "*Fact 6*",
        "*Fact 8*",
        "*Fact 9*",
    ]
    assert [meta.split(" · ")[0] for _, meta in pairs(messages[1])] == [
        "*Risk*",
        "*Risk*",
        "*Risk*",
        "*FYI*",
    ]


def test_ranked_top_and_thread_sort(world: World, profile: Profile) -> None:
    """A6, A15: ranked top uses score, while reply uses section then score."""
    lines = [
        row(7, section="fyi", must=False, score=3),
        row(8, section="fyi", must=False, score=6),
        row(9, section="fyi", must=False, score=3),
        row(3, must=False, score=8),
        row(4, must=False, score=9),
        row(5, must=False, score=8),
        row(6, must=False, score=7),
        row(2, section="decisions", must=False, score=2),
        row(1, section="needs_you", must=False, score=1),
    ]
    top, reply = build_digest(world, chosen(profile, *lines))
    assert [text for text, _ in pairs(top)] == [
        "*Fact 4*\nFirst sentence.",
        "*Fact 3*\nFirst sentence.",
        "*Fact 5*\nFirst sentence.",
    ]
    assert reply["text"] == "More from your digest: Needs you 1, Decisions 1, Risks 1, FYI 3"
    assert [block["type"] for block in reply["blocks"]] == [
        "section",
        "section",
        "context",
        "divider",
        "section",
        "section",
        "context",
        "divider",
        "section",
        "section",
        "context",
        "divider",
        "section",
        "section",
        "context",
        "section",
        "context",
        "section",
        "context",
    ]
    assert [text for text, _ in pairs(reply)] == [
        f"*Fact {i}*\nFirst sentence." for i in (1, 2, 6, 8, 7, 9)
    ]
    assert all(not meta.startswith("*") for _, meta in pairs(reply))


@pytest.mark.parametrize(
    ("kind", "same_thread", "shared", "fold"),
    [
        ("ask", True, ("PN-1",), True),
        ("ask", True, ("G-TYPE",), False),
        ("ask", False, ("PN-1",), False),
        ("risk", True, ("PN-1",), False),
        ("ask-absence", True, ("PN-1",), True),
    ],
)
def test_ask_fold_and_negative_twins(
    world: World,
    profile: Profile,
    kind: str,
    same_thread: bool,
    shared: tuple[str, ...],
    fold: bool,
) -> None:
    """A7: shared thread and a non-gate code are both required for an ask fold."""
    ask = source(quote="Please send the ETA.")
    authors = {f"CENG:{TS}": "UASKER"}
    changes = dict(entities=shared, owner="UREADER", evidence=(ask,))
    absorber = (
        absence(1, asker="UASKER", **changes)
        if kind == "ask-absence"
        else row(1, section="needs_you", type=kind, **changes)
    )
    member = row(
        2,
        must=True,
        title="Shortage",
        entities=shared,
        thread_id=absorber.item.thread_id if same_thread else "CENG:1792407600.000002",
    )
    messages = build_digest(
        world,
        chosen(profile, absorber.model_copy(update={"must_see": False}), member),
        authors=authors,
    )
    rendered = [pair for message in messages for pair in pairs(message)]
    assert len(rendered) == (1 if fold else 2)
    if fold:
        title = (
            "Grace has had no reply from you in this thread for 3 working days"
            if (kind == "ask-absence")
            else "Shortage"
        )
        assert rendered[0][0] == f"*{title}*\nGrace asked you: “Please send the ETA.”"
        assert rendered[0][1].startswith("*Asked of you*")
        assert messages[0]["blocks"][2] == section(
            "*1 to read first*. " + ("0" if kind == "ask-absence" else "1") + " of 1 are new."
        )
    else:
        assert any(text.startswith("*Shortage*") for text, _ in rendered)


@pytest.mark.parametrize(
    ("task_text", "fold"),
    [
        ("Close the TST-THM-02 thermal review", True),
        ("Close XTST-THM-02 thermal review", False),
        ("Close TST-THM-02-X thermal review", False),
        ("Close (TST-THM-02) thermal review", True),
    ],
)
@pytest.mark.parametrize("best_score", [1, 2])
def test_checklist_fold_boundaries_and_best_member(
    world: World, profile: Profile, task_text: str, fold: bool, best_score: int
) -> None:
    """A7, A9: task tokens decide a fold. Must-see beats score for the body."""
    task = gate().checklist[0].model_copy(update={"task": task_text})
    world = gates(world, gate().model_copy(update={"checklist": (task,)}))
    lines = [
        absence(1, must=False),
        absence(2, must=False),
        row(3, must=False, score=10, entities=("TST-THM-02",), summary="Ranked body."),
        row(4, score=1, entities=("TST-THM-02",), summary="Chosen body. More."),
        row(5, score=best_score, entities=("TST-THM-02",), summary="Higher score body."),
    ]
    links = mapping(*(entry for line in lines for entry in line.item.evidence))
    messages = build_digest(world, chosen(profile, *lines), links=links)
    rendered = [pair for message in messages for pair in pairs(message)]
    assert len(rendered) == (2 if fold else 5)
    if fold:
        assert rendered[0][0].endswith(
            "\nChosen body." if best_score == 1 else "\nHigher score body."
        )
        assert rendered[1][0].endswith("\nProgress is expected every 2 working days.")
        assert all("checked: #eng-me threads" in meta for _, meta in rendered)
        assert f"p{TS.replace('.', '')}|Thread>" in rendered[0][1]
        assert "p1792407600000004" not in rendered[0][1]
        assert messages[0]["blocks"][2] == section(
            "*1 to read first*, 1 more in the thread. 0 of 2 are new."
        )


def test_two_asks_and_two_absences_do_not_fold(world: World, profile: Profile) -> None:
    """A7: absorbers remain separate even when the thread and codes match."""
    lines = [
        row(i, section="needs_you", type="ask", entities=("PN-1",), thread_id=f"CENG:{TS}")
        for i in (2, 1)
    ]
    lines += [absence(i, entities=("PN-1",), thread_id=f"CENG:{TS}") for i in (4, 3)]
    lines += [
        row(6, entities=("PN-1",), title="First member", score=0.1, thread_id=f"CENG:{TS}"),
        row(5, entities=("PN-1",), title="Second member", score=10, thread_id=f"CENG:{TS}"),
    ]
    messages = build_digest(world, chosen(profile, *lines))
    assert len(pairs(messages[0])) == 4
    assert [text for text, _ in pairs(messages[0])][:2] == [
        "*First member*\nFirst sentence.",
        "*Fact 1*\nFirst sentence.",
    ]
    assert messages[0]["blocks"][2] == section("*4 to read first*. 2 of 4 are new.")


@pytest.mark.parametrize("known", [False, True])
def test_ask_resolver_uses_first_known_non_owner(
    world: World, profile: Profile, known: bool
) -> None:
    """A8, A13, A14: body, source and tags use the same evidence entry."""
    entries = (
        source("1792140000.000001", quote="Unknown quote"),
        source("1792140000.000002", quote="Owner quote"),
        source("1792140000.000003", quote="Selected quote"),
        source("1792140000.000004", quote="Later asker quote"),
        source("1792140000.000005", quote="Reader reply"),
    )
    authors = {
        f"CENG:{entries[1].message_ts}": "UREADER",
        f"CENG:{entries[4].message_ts}": "UREADER",
    }
    if known:
        authors |= {f"CENG:{entry.message_ts}": "UASKER" for entry in entries[2:4]}
    links = mapping(*entries)
    line = row(
        type="ask",
        owner="UREADER",
        section="needs_you",
        evidence=entries,
        first_seen=entries[0].message_ts,
    )
    text, meta = pairs(build_digest(world, chosen(profile, line), links=links, authors=authors)[0])[
        0
    ]
    if known:
        assert text == "*Fact 1*\nGrace asked you: “Selected quote”"
        assert meta == (
            "*Asked of you* · Asked Fri 16 Oct · You replied · "
            "<https://scratch.slack.com/archives/C111/p1792140000000003?"
            f"thread_ts={TS}&cid=C111|Message> in <#C111>"
        )
    else:
        assert text == "*Fact 1*\nFirst sentence."
        assert meta == "*Asked of you* · Open since Fri 16 Oct · In <#C111>"


@pytest.mark.parametrize(
    ("asker", "owner", "title", "prefix"),
    [
        ("UREADER", "UOTHER", "No reply from Hamid in this thread for 1 working day", "You asked"),
        (
            "UASKER",
            "UREADER",
            "Grace has had no reply from you in this thread for 1 working day",
            "Grace asked you",
        ),
        (
            "UMISSING",
            "UREADER",
            "Someone has had no reply from you in this thread for 1 working day",
            "Someone asked you",
        ),
    ],
)
@pytest.mark.parametrize("known", [False, True])
def test_ask_absence_titles(
    world: World, profile: Profile, asker: str, owner: str, title: str, prefix: str, known: bool
) -> None:
    """A9, A10: titles state who has no reply and where the system looked."""
    line = absence(asker=asker, days=1, owner=owner)
    authors = {f"CENG:{TS}": asker} if known else {}
    text, meta = pairs(build_digest(world, chosen(profile, line), authors=authors)[0])[0]
    assert text == f"*{title}*\n" + (
        f"{prefix}: “Can you give an ETA?”" if known else "First sentence."
    )
    assert "New" not in meta and "Asked Mon" not in meta
    assert meta.split(" · ")[0] == ("*Your question*" if asker == "UREADER" else "*Asked of you*")


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("Close the TST-THM-02 thermal review", "TST-THM-02 thermal review"),
        ("Pass TST-THM-02 thermal review", "TST-THM-02 thermal review"),
        (None, "missing"),
    ],
)
def test_checklist_noun_and_missing_task(
    world: World, profile: Profile, task: str | None, expected: str
) -> None:
    """A9: remove the verb and optional article. Unknown tasks have no body or owner."""
    if task:
        original = gate()
        world = gates(
            world,
            original.model_copy(
                update={"checklist": (original.checklist[0].model_copy(update={"task": task}),)}
            ),
        )
    line = absence(task="thermal" if task else "missing", days=1)
    text, meta = pairs(build_digest(world, chosen(profile, line))[0])[0]
    body = "\nProgress is expected every 2 working days." if task else ""
    assert text == f"*{expected}: no progress evidence for 1 working day*{body}"
    assert "checked: #eng-me threads" in meta
    assert not {"New", "You replied", "Updated"} & set(meta.split(" · "))
    if not task:
        assert "Owner:" not in meta


@pytest.mark.parametrize(
    ("summary", "body"),
    [
        (" \n First. Second?", "First."),
        ("Ready? Yes.", "Ready?"),
        ("Stop! Later.", "Stop!"),
        ("Use 3.5 and v1.2. Next.", "Use 3.5 and v1.2."),
        ("No punctuation\n here", "No punctuation here"),
        ("", ""),
        ("No.space then end.", "No.space then end."),
    ],
)
def test_body_first_sentence(world: World, profile: Profile, summary: str, body: str) -> None:
    """A10: sentence marks split only before whitespace or the end."""
    text, _ = pairs(build_digest(world, chosen(profile, row(summary=summary)))[0])[0]
    assert text == "*Fact 1*" + ("\n" + body if body else "")


@pytest.mark.parametrize(
    ("owner", "prefix"),
    [
        ("UREADER", "Grace asked you"),
        ("UOTHER", "Grace asked"),
    ],
)
def test_quote_cleanup_and_prefix(world: World, profile: Profile, owner: str, prefix: str) -> None:
    """A10: clean all Slack quote tokens before escaping the remaining text."""
    quote = (
        " <@UREADER>,:;.!?-  <@UASKER|label> <@UUNKNOWN> <#CENG|eng-me> <https://x|ETA> <raw> & "
    )
    entry = source(quote=quote)
    line = row(type="ask", owner=owner, evidence=(entry,))
    text, _ = pairs(
        build_digest(world, chosen(profile, line), authors={f"CENG:{TS}": "UASKER"})[0]
    )[0]
    assert text == f"*Fact 1*\n{prefix}: “@Grace @someone #eng-me ETA &lt;raw&gt; &amp;”"


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("word " * 30, "word " * 19 + "word…"),
        ("x" * 101, "x" * 99 + "…"),
    ],
)
def test_quote_cut_excludes_prefix(
    world: World, profile: Profile, quote: str, expected: str
) -> None:
    """A10, A16: the quote owns a separate 100-character limit."""
    entry = source(quote=quote)
    line = row(type="ask", owner="UREADER", evidence=(entry,))
    text, _ = pairs(
        build_digest(world, chosen(profile, line), authors={f"CENG:{TS}": "UASKER"})[0]
    )[0]
    assert text == f"*Fact 1*\nGrace asked you: “{expected}”"


@pytest.mark.parametrize(
    ("section_name", "label"),
    [
        ("needs_you", "Needs you"),
        ("not_happened", "Not happened"),
        ("decisions", "Decision"),
        ("risks", "Risk"),
        ("fyi", "FYI"),
    ],
)
@pytest.mark.parametrize("must", [False, True])
def test_fallback_labels(
    world: World, profile: Profile, section_name: Section, label: str, must: bool
) -> None:
    """A11: both ranked and must-see top items use the section fallback."""
    _, meta = pairs(build_digest(world, chosen(profile, row(section=section_name, must=must)))[0])[
        0
    ]
    assert meta == f"*{label}* · New · In #eng-me"


@pytest.mark.parametrize(
    ("owned", "entities", "title", "label"),
    [
        (("G-TYPE", "PN-1"), ("G-TYPE", "PN-1"), "PN-1 failed", "You own this"),
        (("PN-1",), ("PN-1",), "Failure", "On your PN-1"),
        (("G-TYPE",), ("G-TYPE",), "G-TYPE slipped", "On your gate"),
        (("G-TYPE",), ("G-TYPE",), "Slip", "On your gate G-TYPE"),
    ],
)
def test_owned_labels(
    world: World,
    profile: Profile,
    owned: tuple[str, ...],
    entities: tuple[str, ...],
    title: str,
    label: str,
) -> None:
    """A11: non-gate ownership wins. The displayed title decides the label form."""
    profile = profile.model_copy(update={"owned_entities": owned})
    _, meta = pairs(build_digest(world, chosen(profile, row(entities=entities, title=title)))[0])[0]
    assert meta.split(" · ")[0] == f"*{label}*"


@pytest.mark.parametrize(
    ("owner", "section_name", "expected"),
    [
        ("UREADER", "risks", "Owner: you"),
        ("UOTHER", "risks", "Owner: Hamid (supply chain)"),
        ("UBOT", "risks", None),
        ("UMISSING", "risks", None),
        (None, "risks", None),
        ("UOTHER", "fyi", None),
    ],
)
@pytest.mark.parametrize("top", [False, True])
def test_owner_cases(
    world: World,
    profile: Profile,
    owner: str | None,
    section_name: Section,
    expected: str | None,
    top: bool,
) -> None:
    """A11: owner display depends on persona kind, section and label presence."""
    target = row(8, section=section_name, owner=owner, must=top)
    lines = [target] if top else [row(1, section="needs_you"), target]
    messages = build_digest(world, chosen(profile, *lines))
    _, meta = next(
        pair for message in messages for pair in pairs(message) if pair[0].startswith("*Fact 8*")
    )
    if top and owner == "UREADER":
        expected = None
    if expected is None:
        assert "Owner:" not in meta
    else:
        assert expected in meta.split(" · ")


@pytest.mark.parametrize(
    ("role", "duplicate", "label", "owner"),
    [
        ("ME", False, "Your gate task", None),
        ("EM", False, "Gate task", "Owner: Nadia (ME)"),
        ("EM", True, "Gate task", "Owner: ME"),
    ],
)
def test_checklist_role_labels_and_owners(
    world: World, profile: Profile, role: str, duplicate: bool, label: str, owner: str | None
) -> None:
    """A11: the role owns the task. EM needs no gate ownership to see Gate task."""
    world = gates(world, gate(owner="ME"))
    if duplicate:
        world = world.model_copy(
            update={
                "personas": (
                    *world.personas,
                    Persona(user_id="USECOND", name="Ana Bell", role="ME", channels=("CENG",)),
                )
            }
        )
    profile = profile.model_copy(update={"role": role})
    _, meta = pairs(build_digest(world, chosen(profile, absence()))[0])[0]
    assert meta.split(" · ")[0] == f"*{label}*"
    assert (owner in meta.split(" · ")) if owner else "Owner:" not in meta


def test_codes_filter_order_deduplicate_and_members(world: World, profile: Profile) -> None:
    """A12: codes from members follow owned codes, with type and substring filters."""
    world = gates(world, gate())
    profile = profile.model_copy(update={"owned_entities": ("PN-8",)})
    ask = row(
        1,
        section="needs_you",
        type="ask",
        owner="UREADER",
        must=False,
        entities=("G-TYPE", "V-1", "SUB-1", "BUILD-1", "PN-2", "PN-3", "PN-2"),
        summary="PN-2 is quoted.",
    )
    member = row(
        2,
        title="PN-3 shortage",
        thread_id=ask.item.thread_id,
        entities=("PN-2", "G-A", "PN-8", "PN-9"),
    )
    top = build_digest(world, chosen(profile, ask, member))[0]
    text, meta = pairs(top)[0]
    assert text == "*PN-3 shortage*\nPN-2 is quoted."
    assert meta == "*Asked of you* · New · PN-8, PN-9 · In #eng-me"
    assert top["blocks"][2] == section("*1 to read first*. 1 of 1 are new.")


@pytest.mark.parametrize(
    ("same", "buffer", "used", "part"),
    [
        (True, 7, 2, ""),
        (False, 7, 2, "G-A: 2 of 7 buffer days used · "),
        (False, 7, None, ""),
        (False, 0, 9, "G-A: no buffer days · "),
    ],
)
def test_checklist_buffer_scope(
    world: World, profile: Profile, same: bool, buffer: int, used: int | None, part: str
) -> None:
    """A2, A11, A13: another gate's buffer stays with its task. Absences have no age tag."""
    world = gates(world, gate(buffer=buffer), gate("G-B", offset=0))
    profile = profile.model_copy(update={"owned_entities": ("G-A" if same else "G-B",)})
    _, meta = pairs(build_digest(world, chosen(profile, absence(used=used)))[0])[0]
    assert meta == f"*Gate task* · Owner: EE · {part}In #eng-me · checked: #eng-me threads"


@pytest.mark.parametrize(
    ("title", "entities", "expected"),
    [
        ("Fact", ("PN-1", "PN-2", "PN-3"), "PN-1, PN-2"),
        ("PN-1 " + "x " * 60 + "PN-2", ("PN-1", "PN-2"), "PN-2"),
        ("Fact", ("PN-1",), None),
    ],
)
def test_codes_use_cut_text_and_label(
    world: World, profile: Profile, title: str, entities: tuple[str, ...], expected: str | None
) -> None:
    """A12, A16: omitted title suffixes do not suppress codes. Labels do."""
    if expected is None:
        profile = profile.model_copy(update={"owned_entities": ("PN-1",)})
    _, meta = pairs(build_digest(world, chosen(profile, row(title=title, entities=entities)))[0])[0]
    if expected:
        assert expected in meta.split(" · ")
    else:
        assert meta == "*On your PN-1* · New · In #eng-me"


@pytest.mark.parametrize(
    ("first", "seen", "ask", "reply", "tags"),
    [
        (False, "1792220399.999999", False, False, "Open since Fri 16 Oct"),
        (False, "1792220400.000000", False, False, "New"),
        (True, TS, False, False, ""),
        (True, "1792140000.000001", False, False, "Open since Fri 16 Oct"),
        (False, "1792140000.000001", True, True, "Asked Fri 16 Oct · You replied"),
        (False, TS, True, True, "New · You replied"),
        (False, "1792140000.000001", True, False, "Asked Fri 16 Oct"),
        (True, TS, True, True, "Asked Fri 16 Oct · You replied"),
    ],
)
def test_age_tags(
    world: World, profile: Profile, first: bool, seen: str, ask: bool, reply: bool, tags: str
) -> None:
    """A13: the previous local day boundary, first digest and reply ordering."""
    entries = (
        source("1792139999.000001"),
        source("1792140000.000001"),
        source("1792140001.000001"),
    )
    authors = {
        f"CENG:{entries[0].message_ts}": "UREADER",
        f"CENG:{entries[1].message_ts}": "UASKER",
    }
    if reply:
        authors[f"CENG:{entries[2].message_ts}"] = "UREADER"
    line = row(
        type="ask" if ask else "risk",
        owner="UREADER" if ask else None,
        evidence=entries,
        first_seen=seen,
    )
    _, meta = pairs(
        build_digest(
            world,
            chosen(profile, line, previous_digest=None if first else date(2026, 10, 16)),
            authors=authors,
        )[0]
    )[0]
    label = "Asked of you" if ask else "Risk"
    assert meta == f"*{label}* · " + (tags + " · " if tags else "") + "In #eng-me"
    assert "Updated" not in meta


@pytest.mark.parametrize(
    ("target", "ask", "expected"),
    [
        (
            TS,
            False,
            f"<https://scratch.slack.com/archives/C111/p{TS.replace('.', '')}|Thread> in <#C111>",
        ),
        (
            "1792407600.000002",
            False,
            "<https://scratch.slack.com/archives/C111/p1792407600000002?"
            f"thread_ts={TS}&cid=C111|Thread> in <#C111>",
        ),
        (
            TS,
            True,
            "<https://scratch.slack.com/archives/C111/p1792407600000002?"
            f"thread_ts={TS}&cid=C111|Message> in <#C111>",
        ),
    ],
)
def test_source_target(
    world: World, profile: Profile, target: str, ask: bool, expected: str
) -> None:
    """A14: root, reply root and resolved ask targets have exact link labels."""
    entries = (source(), source("1792407600.000002"))
    line = row(
        type="ask" if ask else "risk",
        owner="UREADER" if ask else None,
        thread_id=f"CENG:{target}",
        evidence=entries,
    )
    authors = {f"CENG:{TS}": "UREADER", "CENG:1792407600.000002": "UASKER"}
    _, meta = pairs(
        build_digest(world, chosen(profile, line), links=mapping(*entries), authors=authors)[0]
    )[0]
    assert meta.endswith(expected)


@pytest.mark.parametrize("mapped_channel", [False, True])
@pytest.mark.parametrize("has_map", [False, True])
def test_unmapped_sources_have_no_world_link(
    world: World, profile: Profile, mapped_channel: bool, has_map: bool
) -> None:
    """A14: missing message keys use only the mapped channel or the world name."""
    links = mapping() if has_map else None
    if links and not mapped_channel:
        links.channels.clear()
    _, meta = pairs(build_digest(world, chosen(profile, row()), links=links)[0])[0]
    assert meta == "*Risk* · New · " + (
        "In <#C111>" if has_map and mapped_channel else "In #eng-me"
    )


@pytest.mark.parametrize("extra", [0, 1, 2])
def test_files_union_in_map_order(world: World, profile: Profile, extra: int) -> None:
    """A14: files from members keep map order, not evidence order, and unique names."""
    ask = row(1, section="needs_you", type="ask", owner="UREADER", entities=("PN-1",))
    member = row(2, title="Shortage", entities=("PN-1",), thread_id=ask.item.thread_id)
    files = {
        "CENG:1792407600.000002/z&last.pdf": MappedFile(
            permalink="https://scratch.slack.com/files/z"
        ),
        f"CENG:{TS}/a<first>.pdf": MappedFile(permalink="https://scratch.slack.com/files/a"),
        f"CENG:{TS}/z&last.pdf": MappedFile(permalink="https://scratch.slack.com/files/duplicate"),
        "CENG:1792407600.000099/unrelated.pdf": MappedFile(
            permalink="https://scratch.slack.com/files/no"
        ),
    }
    files |= {
        f"CENG:{TS}/extra{i}.pdf": MappedFile(permalink=f"https://scratch.slack.com/files/{i}")
        for i in range(extra)
    }
    links = mapping(*ask.item.evidence, *member.item.evidence, files=files)
    _, meta = pairs(build_digest(world, chosen(profile, ask, member), links=links)[0])[0]
    expected = (
        ":paperclip: <https://scratch.slack.com/files/z|z&amp;last.pdf> "
        "<https://scratch.slack.com/files/a|a&lt;first&gt;.pdf>"
    )
    if extra:
        expected += f" +{extra} " + ("file" if extra == 1 else "files")
    assert meta.endswith(expected)
    _, no_map = pairs(build_digest(world, chosen(profile, ask, member))[0])[0]
    assert ":paperclip:" not in no_map


@pytest.mark.parametrize(
    ("count", "shown", "omitted"), [(26, 23, 0), (27, 24, 0), (28, 24, 1), (35, 24, 8)]
)
def test_reply_cap_packs_whole_items(
    world: World, profile: Profile, count: int, shown: int, omitted: int
) -> None:
    """A15: reserve the omission block only when the uncapped reply exceeds 50."""
    lines = [row(i, must=False, title=f"Fact {i}") for i in range(1, count + 1)]
    top, reply = build_digest(world, chosen(profile, *lines))
    assert [text for text, _ in pairs(top)] == [f"*Fact {i}*\nFirst sentence." for i in (1, 2, 3)]
    assert reply["blocks"][0] == section(f"*Risks* · {count - 3}")
    assert [text for text, _ in pairs(reply)] == [
        f"*Fact {i}*\nFirst sentence." for i in range(4, shown + 4)
    ]
    if omitted:
        assert reply["blocks"][-1] == context(
            f"{omitted} more " + ("item not shown" if omitted == 1 else "items not shown")
        )
    else:
        assert reply["blocks"][-1] == context("New · In #eng-me")


def test_reply_cap_never_leaves_empty_heading(world: World, profile: Profile) -> None:
    """A15: the next group's divider and heading must fit with its first item."""
    lines = [row(1, section="needs_you")]
    lines += [row(i, section="decisions", must=False) for i in range(2, 25)]
    lines += [row(25, section="fyi", must=False), row(26, section="fyi", must=False)]
    _, reply = build_digest(world, chosen(profile, *lines))
    assert len(reply["blocks"]) == 48
    assert reply["blocks"][0] == section("*Decisions* · 23")
    assert reply["blocks"][-1] == context("2 more items not shown")
    assert not any(block == section("*FYI* · 2") for block in reply["blocks"])


@pytest.mark.parametrize(
    ("title", "summary", "expected_title", "expected_body"),
    [
        ("word " * 30, "word " * 50, "word " * 17 + "word…", "word " * 31 + "word…"),
        ("x" * 100, "y" * 180, "x" * 89 + "…", "y" * 159 + "…"),
        ("x" * 88 + "&", "a" * 158 + "&", "x" * 88 + "&amp;", "a" * 158 + "&amp;"),
        ("x" * 88 + "& Z", "a" * 158 + "& Z", "x" * 88 + "&amp;…", "a" * 158 + "&amp;…"),
    ],
)
def test_raw_cuts_then_escape(
    world: World,
    profile: Profile,
    title: str,
    summary: str,
    expected_title: str,
    expected_body: str,
) -> None:
    """A16: the limits count raw text and include the ellipsis."""
    text, _ = pairs(build_digest(world, chosen(profile, row(title=title, summary=summary)))[0])[0]
    assert text == f"*{expected_title}*\n{expected_body}"


def test_long_meta_drops_partial_link_and_header_is_plain(world: World, profile: Profile) -> None:
    """A16: _cut drops a partial file link. The header keeps raw angle brackets."""
    world = world.model_copy(update={"program": "Kestrel & <K2> " + "x" * 160})
    links = mapping(
        source(),
        files={
            f"CENG:{TS}/long.pdf": MappedFile(
                permalink="https://scratch.slack.com/files/" + "x" * 3100
            )
        },
    )
    top = build_digest(world, chosen(profile, row()), links=links)[0]
    assert top["blocks"][0]["text"] == {
        "type": "plain_text",
        "text": ("Kestrel & <K2> " + "x" * 160 + " · Mon 19 Oct")[:149] + "…",
    }
    _, meta = pairs(top)[0]
    assert meta.endswith(" · :paperclip: …")
    assert "<https://scratch.slack.com/files/" not in meta


@pytest.mark.parametrize("defect", ["cut-link", "raw-text", "accessory", "header", "host"])
def test_safety_helper_rejects_each_broken_payload(defect: str) -> None:
    """A17: a valid twin passes before each payload mutation fails."""
    links = mapping()
    valid = {
        "text": "Worth a look: Fact.",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Kestrel & <K2>"}},
            section("<https://scratch.slack.com/archives/C111/p123|Thread> in <#C111>"),
        ],
    }
    check_message(valid, links, top=True)
    broken = deepcopy(valid)
    if defect == "cut-link":
        broken["blocks"][1] = section("<https://scratch.slack.com/archives/C111/p123|Thr…")
    elif defect == "raw-text":
        broken["blocks"][1] = section("A & B < C > D")
    elif defect == "accessory":
        broken["blocks"][1]["accessory"] = {"type": "button"}
    elif defect == "header":
        broken["blocks"][0]["text"]["text"] = "x" * 151
    else:
        broken["blocks"][1] = section("<https://scratch.slack.com.evil/archives/C111/p123|Thread>")
    with pytest.raises(AssertionError):
        check_message(broken, links, top=True)


SECRET = "xox" + "b-layout-secret"
HOOK = "https://hooks.slack.com/services/layout-secret"


class WebhookResponse:
    def __init__(self, status: int = 200, body: str = "ok") -> None:
        self.status_code = status
        self.body = body


class DeliveryClient:
    def __init__(
        self, *, fail_at: int = 0, response: Any = None, missing_at: int = 0, dm: bool = False
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_at, self.response, self.missing_at, self.dm = fail_at, response, missing_at, dm

    def conversations_open(self, *, users: str) -> dict[str, Any]:
        self.calls.append({"method": "conversations_open", "users": users})
        return {"channel": {"id": "D111"}}

    def send(self, **payload: Any) -> Any:
        number = sum(call["method"] != "conversations_open" for call in self.calls) + 1
        self.calls.append({"method": "chat_postMessage" if self.dm else "send", **payload})
        if number == self.fail_at:
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        if self.dm:
            return {"ts": None if number == self.missing_at else f"1800000000.{number:06d}"}
        return WebhookResponse()

    def chat_postMessage(self, **payload: Any) -> Any:
        return self.send(**payload)


def delivery_setup(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    profile: Profile,
    client: DeliveryClient,
    *,
    reply: bool,
    prior: int = 0,
) -> list[Selection]:
    selections = []
    for index in range(3):
        reader = profile.model_copy(update={"name": f"Reader {index}"})
        lines = [row(10 * index + 1)]
        if reply and index >= prior:
            lines.append(row(10 * index + 2, must=False))
        selections.append(chosen(reader, *lines))
    monkeypatch.setattr(cli, "_demo_selections", lambda day: (world, selections, {}))
    monkeypatch.setattr(cli, "_env_value", lambda key: SECRET if key == "SLACK_BOT_TOKEN" else None)
    monkeypatch.setattr(cli, "_webhook_url", lambda: HOOK)
    monkeypatch.setattr(cli, "_slack_web_client", lambda token: client)
    monkeypatch.setattr(cli, "_webhook_client", lambda url: client)
    return selections


def expected_calls(world: World, selections: list[Selection], *, dm: bool) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = [{"method": "conversations_open", "users": "U111"}] if dm else []
    number = 0
    for selected in selections:
        messages = build_digest(world, selected, threaded=dm)
        number += 1
        calls.append(
            {
                "method": "chat_postMessage" if dm else "send",
                **({"channel": "D111"} if dm else {}),
                **messages[0],
            }
        )
        if len(messages) == 2:
            calls.append(
                {
                    "method": "chat_postMessage" if dm else "send",
                    **({"channel": "D111", "thread_ts": f"1800000000.{number:06d}"} if dm else {}),
                    **messages[1],
                }
            )
            number += 1
    return calls


@pytest.mark.parametrize("dm", [False, True])
@pytest.mark.parametrize("reply", [False, True])
def test_delivery_exact_calls(
    world: World, profile: Profile, monkeypatch: pytest.MonkeyPatch, dm: bool, reply: bool
) -> None:
    """A18, A19: each reply follows its top message and uses the returned ts."""
    client = DeliveryClient(dm=dm)
    selections = delivery_setup(monkeypatch, world, profile, client, reply=reply)
    result = CliRunner().invoke(cli.app, ["post", "--dm", "U111"] if dm else ["post"])
    assert result.exit_code == 0, result.output
    assert client.calls == expected_calls(world, selections, dm=dm)
    assert [
        line for line in result.output.splitlines() if line.startswith("posted the digest")
    ] == [f"posted the digest for Reader {index}" for index in range(3)]
    assert SECRET not in result.output and HOOK not in result.output


@pytest.mark.parametrize("prior", [0, 1])
@pytest.mark.parametrize("failure", ["missing-ts", "integer-ts", "api", "exception"])
def test_dm_reply_refusal(
    world: World, profile: Profile, monkeypatch: pytest.MonkeyPatch, prior: int, failure: str
) -> None:
    """A19: partial delivery reports the top message and completed reader count."""
    error = (
        SlackApiError(f"failure {SECRET}", {"error": "missing_scope"})
        if failure == "api"
        else LookupError(SECRET)
    )
    missing = failure in {"missing-ts", "integer-ts"}
    client = DeliveryClient(
        dm=True,
        fail_at=prior + 1 if failure == "integer-ts" else prior + 2 if not missing else 0,
        response={"ts": 123} if failure == "integer-ts" else error,
        missing_at=prior + 1 if failure == "missing-ts" else 0,
    )
    selections = delivery_setup(monkeypatch, world, profile, client, reply=True, prior=prior)
    result = CliRunner().invoke(cli.app, ["post", "--dm", "U111"])
    assert result.exit_code == 1, result.output
    reason = {
        "missing-ts": "the top message has no ts",
        "integer-ts": "the top message has no ts",
        "api": "missing_scope",
        "exception": "LookupError",
    }[failure]
    note = (
        ", and a rerun sends all 3 again"
        if prior == 0
        else "; 1 digest was already sent, and a rerun sends all 3 again"
    )
    assert (
        f"cannot post the thread reply of the digest for Reader {prior}: {reason}; "
        f"its top message was sent{note}"
    ) in result.output
    sent_calls = prior + (1 if missing else 2)
    assert client.calls == expected_calls(world, selections, dm=True)[: sent_calls + 1]
    assert SECRET not in result.output and SECRET not in str(result.exception)
    assert f"posted the digest for Reader {prior}" not in result.output


@pytest.mark.parametrize("response", [{}, {"ts": 123}, {"ts": None}])
def test_dm_no_reply_needs_no_ts(
    world: World, profile: Profile, monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    """A19: the no-reply twin succeeds when the top response has no ts."""
    client = DeliveryClient(dm=True, fail_at=1, response=response)
    selections = delivery_setup(monkeypatch, world, profile, client, reply=False)
    result = CliRunner().invoke(cli.app, ["post", "--dm", "U111"])
    assert result.exit_code == 0, result.output
    assert client.calls == expected_calls(world, selections, dm=True)
    assert "posted the digest for Reader 0" in result.output


@pytest.mark.parametrize("prior", [0, 1])
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            LookupError(HOOK),
            "cannot post the second message of the digest for Reader {reader}: LookupError",
        ),
        (
            WebhookResponse(400, "invalid_blocks"),
            "Slack refused the second message of the digest for Reader {reader}: status 400, invalid_blocks",
        ),
        (
            WebhookResponse(500, HOOK),
            "Slack refused the second message of the digest for Reader {reader}: status 500, body withheld",
        ),
    ],
)
def test_webhook_second_message_refusal(
    world: World,
    profile: Profile,
    monkeypatch: pytest.MonkeyPatch,
    prior: int,
    failure: Any,
    reason: str,
) -> None:
    """A18: second sends report partial delivery without exposing a credential."""
    client = DeliveryClient(fail_at=prior + 2, response=failure)
    selections = delivery_setup(monkeypatch, world, profile, client, reply=True, prior=prior)
    result = CliRunner().invoke(cli.app, ["post"])
    assert result.exit_code == 1, result.output
    note = (
        ", and a rerun sends all 3 again"
        if prior == 0
        else "; 1 digest was already sent, and a rerun sends all 3 again"
    )
    assert reason.format(reader=prior) + "; its first message was sent" + note in result.output
    assert client.calls == expected_calls(world, selections, dm=False)[: prior + 2]
    assert HOOK not in result.output and "layout-secret" not in result.output
    assert HOOK not in str(result.exception)
    assert f"posted the digest for Reader {prior}" not in result.output


@pytest.mark.parametrize("dm", [False, True])
def test_all_messages_built_before_first_send(
    world: World, profile: Profile, monkeypatch: pytest.MonkeyPatch, dm: bool
) -> None:
    """A18, A19: a later reader's render failure prevents every send."""
    client = DeliveryClient(dm=dm)
    delivery_setup(monkeypatch, world, profile, client, reply=True)
    builds: list[str] = []

    def fail_later(loaded: World, selected: Selection, **kwargs: Any) -> list[dict[str, Any]]:
        builds.append(selected.profile.name)
        if selected.profile.name == "Reader 1":
            raise ValueError("later render failed")
        return build_digest(loaded, selected, **kwargs)

    monkeypatch.setattr(cli, "slack_digest", fail_later, raising=False)
    result = CliRunner().invoke(cli.app, ["post", "--dm", "U111"] if dm else ["post"])
    assert result.exit_code == 1
    assert builds == ["Reader 0", "Reader 1"]
    assert not any(call["method"] in {"send", "chat_postMessage"} for call in client.calls)


def test_demo_authors_and_terminal_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A20: every stored evidence author is returned. Terminal text keeps its contract."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli.tempfile, "tempdir", str(tmp_path))
    with cli._demo_store(6) as (world, priors, store):
        as_of = datetime.combine(world.date_of(6), time.max, ZoneInfo(world.timezone))
        selections = [
            select_digest(
                store, world, priors, user, world.date_of(6), previous_digest=world.date_of(5)
            )
            for user in ("U0ME01", "U0SC01", "U0EM01")
        ]
        expected = {
            f"{entry.channel_id}:{entry.message_ts}": event.user_id
            for selected in selections
            for line in selected.lines
            for entry in line.item.evidence
            for event in store.events_in_thread(line.item.thread_id, as_of)
            if (event.channel_id, event.ts) == (entry.channel_id, entry.message_ts)
        }
    actual_world, actual_selections, authors = cli._demo_selections(6)
    assert actual_world == world
    assert actual_selections == selections
    assert authors == expected
    assert expected
    result = CliRunner().invoke(cli.app, ["demo", "--day", "6"])
    assert result.exit_code == 0, result.output
    shown = [{line.item.item_id for line in selected.lines} for selected in selections]
    roles = ("ME", "supply_chain", "EM")
    overlap = ", ".join(
        f"{roles[a]}/{roles[b]} {len(shown[a] & shown[b]) / len(shown[a] | shown[b]):.2f}"
        for a, b in ((0, 1), (0, 2), (1, 2))
    )
    assert (
        result.output
        == ("\n\n" + "-" * 40 + "\n\n").join(
            render_digest(world, selected) for selected in selections
        )
        + "\n\nItem overlap: "
        + overlap
        + "\n"
    )


def test_real_day_six_synthetic_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A21: real replay selections use the plan's deterministic synthetic map."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli.tempfile, "tempdir", str(tmp_path))
    with cli._demo_store(6) as (world, priors, store):
        day = world.date_of(6)
        as_of = datetime.combine(day, time.max, ZoneInfo(world.timezone)).astimezone(UTC)
        selections = [
            select_digest(store, world, priors, user, day, previous_digest=world.date_of(5))
            for user in ("U0ME01", "U0SC01", "U0EM01")
        ]
        events = {
            f"{event.channel_id}:{event.ts}": event
            for selected in selections
            for line in selected.lines
            for event in store.events_in_thread(line.item.thread_id, as_of)
        }
        evidence_keys = {
            f"{entry.channel_id}:{entry.message_ts}"
            for selected in selections
            for line in selected.lines
            for entry in line.item.evidence
        }
        links = SeedMap(
            team_id="TSYNTHETIC",
            workspace_url="https://scratch.slack.com",
            world="kestrel",
            channels={channel.id: channel.id for channel in reversed(world.channels)},
            messages={
                key: MappedMessage(channel=event.channel_id, ts=event.ts, thread_ts=event.thread_ts)
                for key, event in events.items()
                if key in evidence_keys
            },
            files={
                f"{key}/{name}": MappedFile(permalink=f"https://scratch.slack.com/files/F{i}")
                for i, (key, name) in enumerate(
                    (key, name) for key, event in events.items() for name in event.files
                )
            },
        )
        authors = {key: event.user_id for key, event in events.items() if key in evidence_keys}
        assert all(selected.lines for selected in selections)
        payloads = [
            build_digest(world, selected, links=links, authors=authors) for selected in selections
        ]
        assert [len(messages) for messages in payloads] == [2, 2, 2]

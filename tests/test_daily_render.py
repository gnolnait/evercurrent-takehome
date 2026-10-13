import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from functools import partialmethod
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

import digest.cli as cli
import digest.daily.render as daily_render
from digest.cli import app
from digest.daily.profile import Profile
from digest.daily.render import build_digest, render_digest
from digest.daily.select import Line, Section, Selection
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.extract.stage import ExtractReport
from digest.extract.verify import DROP_FIELDS
from digest.models import AbsenceDetail, Claim, Event, Evidence, Item
from digest.priors import load_priors
from digest.settings import Settings
from digest.store import StageRun, Store
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_PATH = ROOT / "data" / "world" / "kestrel" / "world.yaml"
CASSETTES = ROOT / "data" / "cassettes"
DAY = date(2026, 10, 14)
# Keep the gate evidence inside the first window and older than its update cadence.
TS = f"{int(datetime(2026, 10, 8, 12, tzinfo=ZoneInfo('America/Los_Angeles')).timestamp())}.000001"
WORKSPACE = "https://kestrel-k2.slack.com"
runner = CliRunner()


@pytest.fixture
def world() -> World:
    return load_world(WORLD_PATH)


@pytest.fixture
def profile() -> Profile:
    return Profile(
        user_id="U0ME01",
        name="Nadia Brenner",
        role="ME",
        team="mechanical",
        owned_entities=("PN-51010",),
        channels=("C0KPROG", "C0ENGME"),
    )


def evidence(
    number: int = 1,
    quote: str = "source text",
    channel_id: str = "C0ENGME",
    *,
    ts: str = TS,
) -> Evidence:
    message_ts = f"{ts.split('.')[0]}.{number:06d}"
    return Evidence(
        channel_id=channel_id,
        message_ts=message_ts,
        quote=quote,
        permalink=f"{WORKSPACE}/archives/{channel_id}/p{message_ts.replace('.', '')}",
    )


def item(**changes: Any) -> Item:
    values: dict[str, Any] = {
        "item_id": "ITEM-1",
        "thread_id": f"C0ENGME:{TS}",
        "type": "decision",
        "title": "Drawing released",
        "summary": "Release PN-51010 Rev C for tooling.",
        "entities": ("PN-51010", "G-TR1"),
        "owner": "U0SC01",
        "due": None,
        "affected_roles": ("ME",),
        "severity": "high",
        "confidence": "high",
        "evidence": (evidence(),),
        "first_seen": TS,
        "last_updated": TS,
    }
    return Item(**(values | changes))


def absence_item(source: str) -> Item:
    if source == "checklist":
        detail = AbsenceDetail(
            source="checklist",
            gate="G-DVT1",
            task="G-DVT1.fai",
            asker=None,
            days_silent=3,
            buffer_consumed=2,
            buffer_days=7,
        )
        return item(
            item_id="ABS-CHECKLIST",
            type="absence",
            title="Checklist absence",
            summary="No FAI update.",
            entities=("G-DVT1",),
            owner=None,
            evidence=(evidence(7),),
            absence=detail,
        )
    detail = AbsenceDetail(
        source="ask",
        gate=None,
        task=None,
        asker="U0ME01",
        days_silent=4,
        buffer_consumed=None,
        buffer_days=None,
    )
    return item(
        item_id="ABS-ASK",
        type="absence",
        title="Ask absence",
        summary="Can you approve the motor controller release?",
        entities=(),
        owner="U0EE01",
        evidence=(evidence(8),),
        absence=detail,
    )


def line(value: Item, section: Section, *, must_see: bool = False) -> Line:
    return Line(
        item=value,
        section=section,
        must_see=must_see,
        reason="owned entity",
        score=0 if must_see else 0.5,
    )


def selection(
    profile: Profile, *lines: Line, hidden: int = 0, previous_digest: date | None = None
) -> Selection:
    return Selection(
        profile=profile, day=DAY, lines=lines, hidden=hidden, previous_digest=previous_digest
    )


def test_render_digest_exact_normal_line(world: World, profile: Profile) -> None:
    """Set up one decision; render it; assert the complete plain-text digest."""
    selected = selection(
        profile,
        line(
            item(evidence=(evidence(1), evidence(2, "second source"))),
            "decisions",
            must_see=True,
        ),
    )

    rendered = render_digest(world, selected)

    assert rendered == (
        "Daily digest for Nadia Brenner (ME) - 2026-10-14\n"
        "First digest: the last 5 working days\n"
        "\n"
        "Decisions\n"
        "- Drawing released (PN-51010, G-TR1) - owner Hamid Rostami [must see]\n"
        "  Release PN-51010 Rev C for tooling.\n"
        f"  source: {evidence(1).permalink}\n"
        f"  source: {evidence(2).permalink}"
    )


def test_render_digest_orders_only_nonempty_sections(world: World, profile: Profile) -> None:
    """Set up every section; render; assert section order."""
    lines = (
        line(item(item_id="ASK", type="ask", title="Approve release"), "needs_you"),
        line(absence_item("checklist"), "not_happened", must_see=True),
        line(absence_item("ask"), "not_happened"),
        line(item(item_id="DECISION"), "decisions"),
        line(item(item_id="RISK", type="risk", title="Tooling risk"), "risks"),
        line(item(item_id="FYI", type="fyi", title="Build photos"), "fyi"),
    )

    rendered = render_digest(world, selection(profile, *lines))

    positions = [
        rendered.index(title)
        for title in ("Needs you", "Not happened", "Decisions", "Risks", "FYI")
    ]
    assert positions == sorted(positions)
    assert rendered.count("[must see]") == 1


def test_render_digest_checklist_absence_uses_world_task_text_and_includes_summary(
    world: World, profile: Profile
) -> None:
    selected = selection(
        profile,
        line(absence_item("checklist"), "not_happened", must_see=True),
    )

    rendered = render_digest(world, selected)

    assert (
        "- Not happened: Sign off FAI on DVT-1 mechanical parts (ME) for G-DVT1 - "
        "3 working days silent, 2/7 buffer days used [must see]\n"
        "  No FAI update.\n"
        f"  source: {evidence(7).permalink}"
    ) in rendered


def test_render_digest_ask_absence_includes_what_was_asked_before_source(
    world: World, profile: Profile
) -> None:
    selected = selection(profile, line(absence_item("ask"), "not_happened"))

    rendered = render_digest(world, selected)

    assert (
        "- Not happened: Nadia Brenner asked Tobias Lindqvist, no reply for 4 working days\n"
        "  Can you approve the motor controller release?\n"
        f"  source: {evidence(8).permalink}"
    ) in rendered


def test_render_digest_omits_empty_sections(world: World, profile: Profile) -> None:
    rendered = render_digest(world, selection(profile, line(item(), "decisions")))

    assert "Decisions" in rendered
    for absent_title in ("Needs you", "Not happened", "Risks", "FYI"):
        assert absent_title not in rendered


def test_render_digest_normal_line_without_owner_omits_owner(
    world: World, profile: Profile
) -> None:
    selected = selection(
        profile,
        line(item(item_id="UNOWNED", title="Unowned risk", owner=None), "risks"),
    )

    rendered = render_digest(world, selected)

    assert "- Unowned risk (PN-51010, G-TR1)" in rendered.splitlines()
    assert "owner none" not in rendered


def test_render_digest_empty_day_and_hidden_footer(world: World, profile: Profile) -> None:
    empty = render_digest(world, selection(profile))
    hidden = render_digest(world, selection(profile, hidden=2))

    assert empty == (
        "Daily digest for Nadia Brenner (ME) - 2026-10-14\n"
        "First digest: the last 5 working days\n\nNothing new for Nadia Brenner."
    )
    assert "hidden by the visibility filter" not in empty
    assert "Nothing new for Nadia Brenner." in hidden
    assert hidden.endswith("2 items hidden by the visibility filter.")


def test_render_digest_preserves_markdown_characters_as_plain_text(
    world: World, profile: Profile
) -> None:
    quoted = "Keep *literal* _markup_ and [x](y)."
    selected = selection(
        profile,
        line(item(title=quoted, summary=quoted, evidence=(evidence(3, quoted),)), "decisions"),
    )

    rendered = render_digest(world, selected)

    assert rendered.count(quoted) == 2
    assert "<y|x>" not in rendered
    assert re.findall(r"https?://\S+", rendered) == [evidence(3).permalink]


def set_replay_meta(store: Store, *, until: str = "2026-10-15T06:59:59.999999+00:00") -> None:
    for key, value in {
        "world": "kestrel",
        "timezone": "America/Los_Angeles",
        "workspace_url": WORKSPACE,
        "until": until,
    }.items():
        store.set_meta(key, value)


def pending_event() -> Event:
    return Event(
        source="replay",
        channel_id="C0ENGME",
        channel_name="eng-mech",
        thread_ts=TS,
        ts=TS,
        event_ts=TS,
        user_id="U0ME01",
        text="Release PN-51010 Rev C.",
        permalink=evidence().permalink,
    )


def test_build_digest_refuses_store_without_until(tmp_path: Path, world: World) -> None:
    with Store.open(tmp_path / "missing-until.sqlite") as store:
        for key, value in {
            "world": "kestrel",
            "timezone": "America/Los_Angeles",
            "workspace_url": WORKSPACE,
        }.items():
            store.set_meta(key, value)

        with pytest.raises(ValueError, match=r"(?i)until"):
            build_digest(store, world, load_priors(), "U0ME01", DAY)


def test_build_digest_refuses_pending_extract_deltas(tmp_path: Path, world: World) -> None:
    with Store.open(tmp_path / "pending.sqlite") as store:
        set_replay_meta(store)
        store.upsert_events([pending_event()])
        store.upsert_items([item()], prompt_version="m2.1", model="extract")

        with pytest.raises(ValueError, match=r"(?i)(pending.*extract|extract.*pending)"):
            build_digest(store, world, load_priors(), "U0ME01", DAY)


@pytest.mark.parametrize(
    "until",
    [
        pytest.param("2026-10-13T06:59:59.999999-07:00", id="earlier-local-date"),
        pytest.param("2026-10-15T06:59:58+00:00", id="utc-before-local-day-end"),
    ],
)
def test_build_digest_refuses_store_replayed_before_day(
    tmp_path: Path, world: World, until: str
) -> None:
    with Store.open(tmp_path / "early.sqlite") as store:
        set_replay_meta(store, until=until)
        store.upsert_items([item()], prompt_version="m2.1", model="extract")

        with pytest.raises(ValueError, match=r"(?i)(before|replay|until).*(day|2026-10-14)"):
            build_digest(store, world, load_priors(), "U0ME01", DAY)


def test_build_digest_refuses_store_replayed_after_day(tmp_path: Path, world: World) -> None:
    after_day = f"{int(datetime(2026, 10, 15, 7, tzinfo=UTC).timestamp())}.000000"
    future = item(item_id="FUTURE", title="Tomorrow's decision", last_updated=after_day)
    with Store.open(tmp_path / "future.sqlite") as store:
        set_replay_meta(store, until="2026-10-16T06:59:59.999999+00:00")
        store.upsert_items([future], prompt_version="m2.1", model="extract")

        with pytest.raises(ValueError) as raised:
            build_digest(store, world, load_priors(), "U0ME01", DAY)

    message = str(raised.value)
    assert DAY.isoformat() in message
    assert re.search(r"(?i)fresh\s+--db", message)
    assert "extract" in message.lower()
    assert "digest replay --until" not in message


def make_cli_store(
    path: Path,
    *,
    include_until: bool = True,
    include_item: bool = False,
    until: str = "2026-10-15T06:59:59.999999+00:00",
) -> None:
    with Store.open(path) as store:
        if include_until:
            set_replay_meta(store, until=until)
        else:
            for key, value in {
                "world": "kestrel",
                "timezone": "America/Los_Angeles",
                "workspace_url": WORKSPACE,
            }.items():
                store.set_meta(key, value)
        if include_item:
            gate_anchor = item(
                claims=(
                    Claim(
                        entity="G-DVT1",
                        attribute="status",
                        value="open",
                        channel_id="C0ENGME",
                        message_ts=TS,
                        authority="owner_stated",
                    ),
                )
            )
            store.upsert_items([gate_anchor], prompt_version="m2.1", model="extract")


def invoke_run(db_path: Path, user_id: str) -> Any:
    return runner.invoke(
        app,
        [
            "run",
            "--user",
            user_id,
            "--as-of",
            DAY.isoformat(),
            "--world",
            "kestrel",
            "--db",
            str(db_path),
        ],
    )


def test_cli_run_prints_digest_and_exits_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "ready.sqlite"
    make_cli_store(db_path, include_item=True)

    result = invoke_run(db_path, "U0ME01")

    assert result.exit_code == 0, result.output
    assert "Daily digest for Nadia Brenner (ME) - 2026-10-14" in result.stdout
    assert "Drawing released (PN-51010, G-TR1)" in result.stdout
    assert "- Not happened:" in result.stdout
    assert evidence().permalink in result.stdout


@pytest.mark.parametrize(
    ("user_id", "prepare", "reason"),
    [
        ("U-NOT-THERE", lambda path: make_cli_store(path), r"(?i)(unknown|no such).*U-NOT-THERE"),
        (
            "B0PLM01",
            lambda path: make_cli_store(path),
            r"(?i)(bot|human).*B0PLM01|B0PLM01.*(?i:bot|human)",
        ),
        ("U0ME01", lambda path: make_cli_store(path, include_until=False), r"(?i)until"),
        (
            "U0ME01",
            lambda path: make_cli_store(path, until="2026-10-16T06:59:59.999999+00:00"),
            r"(?i)(until|replay|cutoff).*2026-10-14",
        ),
    ],
    ids=["unknown-user", "bot", "missing-until", "after-day"],
)
def test_cli_run_exits_one_with_reason(
    tmp_path: Path,
    user_id: str,
    prepare: Callable[[Path], None],
    reason: str,
) -> None:
    db_path = tmp_path / "refused.sqlite"
    prepare(db_path)

    result = invoke_run(db_path, user_id)

    assert result.exit_code == 1, result.output
    assert re.search(reason, result.output), result.output


def has_extract_cassette(root: Path) -> bool:
    for path in root.rglob("*.json") if root.is_dir() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if payload.get("request", {}).get("model") == "extract":
            return True
    return False


def test_demo_overlap_line_reports_distinct_pairwise_jaccard_to_two_decimals() -> None:
    overlap = cli.__dict__["_overlap_line"](
        {"ITEM-1", "ITEM-2"},
        {"ITEM-2", "ITEM-3"},
        {"ITEM-1", "ITEM-2", "ITEM-4"},
    )

    assert overlap == ("Item overlap: ME/supply_chain 0.33, ME/EM 0.67, supply_chain/EM 0.25")


def test_demo_overlap_line_reports_zero_for_two_empty_digests() -> None:
    overlap = cli.__dict__["_overlap_line"](set(), set(), {"ITEM-1"})

    assert overlap == ("Item overlap: ME/supply_chain 0.00, ME/EM 0.00, supply_chain/EM 0.00")


@pytest.mark.parametrize(
    ("day", "label"),
    [(6, "Since your digest of 2026-10-16"), (1, "First digest: the last 5 working days")],
    ids=["previous-world-day", "first-world-day"],
)
def test_cli_demo_prints_item_overlap_last_without_cassettes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    day: int,
    label: str,
) -> None:
    today = world.date_of(day)
    zone = ZoneInfo(world.timezone)
    demo_ts = f"{int(datetime.combine(today, time(12), zone).timestamp())}.000001"
    item_inputs = (
        ("C0KPROG", ("ME", "supply_chain", "EM"), "high"),
        ("C0ENGME", ("ME",), "low"),
        ("C0SUPCH", ("supply_chain",), "low"),
        ("C0LEADS", ("EM",), "high"),
    )
    items = []
    for number, (channel, affected_roles, severity) in enumerate(item_inputs, start=1):
        items.append(
            item(
                item_id=f"ITEM-{number}",
                thread_id=f"{channel}:{demo_ts}",
                title=f"Decision {number}",
                entities=(),
                affected_roles=affected_roles,
                severity=severity,
                first_seen=demo_ts,
                last_updated=demo_ts,
                evidence=(evidence(number, channel_id=channel, ts=demo_ts),),
            )
        )

    def extract_known_items(store: Store, *args: object, **kwargs: object) -> ExtractReport:
        set_replay_meta(
            store, until=datetime.combine(today, time.max, zone).astimezone(UTC).isoformat()
        )
        store.upsert_items(items, prompt_version="m2.1", model="extract")
        return ExtractReport(
            threads=0,
            kept=0,
            dropped=0,
            failed=0,
            items_written=len(items),
            drops=dict.fromkeys(DROP_FIELDS, 0),
        )

    cassettes = tmp_path / "cassettes"
    cassettes.mkdir()
    # The replaced extract step does not read this cassette.
    (cassettes / "unused.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassettes))
    monkeypatch.setattr(cli, "_extract_days", extract_known_items)
    monkeypatch.setattr("digest.daily.render.detect_absences", lambda *args, **kwargs: [])

    result = runner.invoke(app, ["demo", "--day", str(day)])

    assert result.exit_code == 0, result.output
    headers = re.findall(r"Daily digest for .*\n([^\n]+)", result.stdout)
    assert headers == [label] * 3
    assert result.stdout.splitlines().count(label) == 3
    assert result.stdout.splitlines()[-1] == (
        "Item overlap: ME/supply_chain 0.33, ME/EM 0.33, supply_chain/EM 0.33"
    )


@pytest.mark.parametrize("exists", [False, True], ids=["missing-directory", "empty-directory"])
def test_cli_demo_refuses_missing_or_empty_cassette_directory_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    cassettes = tmp_path / "no-cassettes"
    if exists:
        cassettes.mkdir()
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassettes))
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-read")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-read")

    def refuse_settings(*args: object, **kwargs: object) -> None:
        raise AssertionError("digest demo constructed Settings")

    monkeypatch.setattr(Settings, "__init__", refuse_settings)
    monkeypatch.setattr(
        cli,
        "_extract_days",
        lambda *args, **kwargs: pytest.fail("digest demo replayed before cassette preflight"),
    )

    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 1, result.output
    assert str(cassettes) in result.output


def test_cli_demo_exits_one_and_reports_failed_extract_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = ExtractReport(
        threads=1,
        kept=0,
        dropped=0,
        failed=1,
        items_written=0,
        drops=dict.fromkeys(DROP_FIELDS, 0),
    )
    succeeded = failed.model_copy(update={"threads": 0, "failed": 0})
    reports = iter([succeeded] * 5 + [failed])
    cassettes = tmp_path / "cassettes"
    cassettes.mkdir()
    # The replaced extract step does not read this cassette.
    (cassettes / "unused.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DIGEST_CASSETTES", str(cassettes))
    monkeypatch.setattr(cli, "run_extract", lambda *args, **kwargs: next(reports))

    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 1, result.output
    assert re.search(r"1 failed thread", result.output, re.IGNORECASE), result.output
    assert "run digest extract first" not in result.output.lower()


@pytest.mark.skipif(
    not has_extract_cassette(CASSETTES),
    reason="data/cassettes holds no extract cassette",
)
def test_cli_demo_prints_three_offline_digests_under_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(Settings, "__init__", partialmethod(Settings.__init__, _env_file=None))

    started = perf_counter()
    result = runner.invoke(app, ["demo"])
    elapsed = perf_counter() - started

    assert result.exit_code == 0, result.output
    assert elapsed < 60
    for header in (
        "Daily digest for Nadia Brenner (ME)",
        "Daily digest for Hamid Rostami (supply_chain)",
        "Daily digest for Grace Okonkwo (EM)",
    ):
        assert header in result.stdout
    dates = re.findall(r"Daily digest for .* - (\d{4}-\d{2}-\d{2})", result.stdout)
    assert dates == ["2026-10-19"] * 3
    assert result.stdout.splitlines().count("Since your digest of 2026-10-16") == 3
    assert len(re.findall(r"(?m)^-{3,}$", result.stdout)) == 2


@pytest.mark.parametrize(
    "previous", [None, date(2026, 10, 13)], ids=["first-digest", "previous-digest"]
)
@pytest.mark.parametrize("edited", [False, True], ids=["original", "edited"])
def test_select_digest_uses_delivery_history_and_stored_event_time(
    world: World, profile: Profile, previous: date | None, edited: bool
) -> None:
    """W6: select stored evidence with omitted or explicit delivery history."""
    zone = ZoneInfo(world.timezone)
    until = datetime.combine(DAY, time.max, zone).astimezone(UTC)
    rows = []
    with Store.open(":memory:") as store:
        set_replay_meta(store, until=until.isoformat())
        for label, message_day in [
            ("before-first", date(2026, 10, 7)),
            ("at-previous", date(2026, 10, 13)),
            ("after-previous", DAY),
        ]:
            moment = datetime.combine(message_day, time(12), zone)
            if label == "at-previous":
                moment = datetime.combine(message_day, time.max, zone)
            ts = f"{int(moment.timestamp())}.{moment.microsecond:06d}"
            observed = (
                datetime.combine(DAY, time(9), zone)
                if edited and label == "before-first"
                else moment
            )
            event_ts = f"{int(observed.timestamp())}.{observed.microsecond:06d}"
            event = Event(
                source="replay",
                channel_id="C0ENGME",
                channel_name="eng-mech",
                thread_ts=ts,
                ts=ts,
                event_ts=event_ts,
                user_id="U0ME01",
                text="Drawing released.",
                edited=edited and label == "before-first",
                permalink=f"{WORKSPACE}/archives/C0ENGME/p{ts.replace('.', '')}",
            )
            if event.edited:
                store.upsert_events([event.model_copy(update={"event_ts": ts, "edited": False})])
            store.upsert_events([event])
            rows.append(
                item(
                    item_id=label,
                    thread_id=event.thread_id,
                    entities=(),
                    first_seen=ts,
                    last_updated=ts,
                    evidence=(
                        Evidence(
                            channel_id=event.channel_id,
                            message_ts=ts,
                            quote=event.text,
                            permalink=event.permalink,
                        ),
                    ),
                )
            )
            store.record_stage_run(
                StageRun(
                    stage="extract",
                    thread_id=event.thread_id,
                    last_ts=event.event_ts,
                    prompt_version=EXTRACT_PROMPT_VERSION,
                    model="extract",
                    run_at=until,
                    status="ok",
                )
            )
        store.upsert_items(rows, prompt_version=EXTRACT_PROMPT_VERSION, model="extract")
        table = load_priors()
        table = table.model_copy(
            update={"daily": table.daily.model_copy(update={"min_score": 0.0})}
        )

        if previous is None:
            selected = daily_render.select_digest(store, world, table, profile.user_id, DAY)
        else:
            selected = daily_render.select_digest(
                store, world, table, profile.user_id, DAY, previous_digest=previous
            )

    expected = {"after-previous"}
    if previous is None:
        expected.add("at-previous")
    if edited:
        expected.add("before-first")
    assert {line.item.item_id for line in selected.lines} == expected
    assert selected.previous_digest == previous


@pytest.mark.parametrize(
    ("previous", "label"),
    [
        (None, "First digest: the last 5 working days"),
        (date(2026, 10, 13), "Since your digest of 2026-10-13"),
    ],
)
def test_render_digest_window_line_under_header(
    world: World, profile: Profile, previous: date | None, label: str
) -> None:
    """W7: show delivery history directly below the digest header."""
    selected = selection(profile, previous_digest=previous)

    rendered = render_digest(world, selected)

    assert rendered.splitlines()[:2] == ["Daily digest for Nadia Brenner (ME) - 2026-10-14", label]


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        (None, "First digest: the last 3 working days"),
        (date(2026, 10, 13), "Since your digest of 2026-10-13"),
    ],
    ids=["first-digest", "previous-digest"],
)
def test_window_line_matches_render_digest(
    world: World, profile: Profile, previous: date | None, expected: str
) -> None:
    selected = Selection(
        profile=profile,
        day=DAY,
        lines=(),
        hidden=0,
        previous_digest=previous,
        first_working_days=3,
    )

    window_line = getattr(daily_render, "window_line", None)

    assert callable(window_line), "daily.render must expose window_line(selection)"
    assert window_line(selected) == render_digest(world, selected).splitlines()[1] == expected


@pytest.mark.parametrize("newest_first", [False, True], ids=["old-first", "new-first"])
def test_select_digest_uses_max_event_time_across_cited_messages(
    world: World, profile: Profile, newest_first: bool
) -> None:
    """Select an item when either cited message is after the window start."""
    until = datetime.combine(DAY, time.max, ZoneInfo(world.timezone)).astimezone(UTC)
    old = pending_event()
    recent_ts = (
        f"{int(datetime.combine(DAY, time(12), ZoneInfo(world.timezone)).timestamp())}.000001"
    )
    recent = old.model_copy(
        update={
            "ts": recent_ts,
            "event_ts": recent_ts,
            "permalink": evidence(ts=recent_ts).permalink,
        }
    )
    citations = (evidence(ts=old.ts), evidence(ts=recent.ts))
    row = item(
        entities=(),
        last_updated=recent.ts,
        evidence=tuple(reversed(citations)) if newest_first else citations,
    )
    with Store.open(":memory:") as store:
        set_replay_meta(store, until=until.isoformat())
        store.upsert_events([old, recent])
        store.upsert_items([row], prompt_version=EXTRACT_PROMPT_VERSION, model="extract")
        store.record_stage_run(
            StageRun(
                stage="extract",
                thread_id=old.thread_id,
                last_ts=recent.event_ts,
                prompt_version=EXTRACT_PROMPT_VERSION,
                model="extract",
                run_at=until,
                status="ok",
            )
        )

        selected = daily_render.select_digest(
            store,
            world,
            load_priors(),
            profile.user_id,
            DAY,
            previous_digest=date(2026, 10, 13),
        )

    assert [(line.item.item_id, line.must_see) for line in selected.lines] == [(row.item_id, False)]

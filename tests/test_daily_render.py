import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partialmethod
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.daily.profile import Profile
from digest.daily.render import build_digest, render_digest
from digest.daily.select import Line, Section, Selection
from digest.models import AbsenceDetail, Claim, Event, Evidence, Item
from digest.priors import load_priors
from digest.settings import Settings
from digest.store import Store
from digest.world.load import load_world
from digest.world.schema import World

ROOT = Path(__file__).parents[1]
WORLD_PATH = ROOT / "data" / "world" / "kestrel" / "world.yaml"
CASSETTES = ROOT / "data" / "cassettes"
DAY = date(2026, 10, 14)
TS = "1760000000.000001"
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


def evidence(number: int = 1, quote: str = "source text") -> Evidence:
    return Evidence(
        message_ts=f"1760000000.{number:06d}",
        quote=quote,
        permalink=f"{WORKSPACE}/archives/C0ENGME/p1760000000{number:06d}",
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
            task="Sign off FAI",
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
        summary="No reply.",
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


def selection(profile: Profile, *lines: Line, hidden: int = 0) -> Selection:
    return Selection(profile=profile, day=DAY, lines=lines, hidden=hidden)


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
        "\n"
        "Decisions\n"
        "- Drawing released (PN-51010, G-TR1) - owner Hamid Rostami [must see]\n"
        "  Release PN-51010 Rev C for tooling.\n"
        f"  source: {evidence(1).permalink}\n"
        f"  source: {evidence(2).permalink}"
    )


def test_render_digest_orders_only_nonempty_sections_and_absences(
    world: World, profile: Profile
) -> None:
    """Set up every section and both absences; render; assert order and absence wording."""
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
    assert (
        "- Not happened: Sign off FAI for G-DVT1 - 3 working days silent, 2/7 buffer days used"
        in rendered
    )
    assert (
        "- Not happened: Nadia Brenner asked Tobias Lindqvist, no reply for 4 working days"
        in rendered
    )
    assert rendered.count("[must see]") == 1
    assert f"source: {evidence(7).permalink}" in rendered
    assert f"source: {evidence(8).permalink}" in rendered


def test_render_digest_omits_empty_sections(world: World, profile: Profile) -> None:
    rendered = render_digest(world, selection(profile, line(item(), "decisions")))

    assert "Decisions" in rendered
    for absent_title in ("Needs you", "Not happened", "Risks", "FYI"):
        assert absent_title not in rendered


def test_render_digest_normal_line_without_owner_uses_none(world: World, profile: Profile) -> None:
    selected = selection(
        profile,
        line(item(item_id="UNOWNED", title="Unowned risk", owner=None), "risks"),
    )

    rendered = render_digest(world, selected)

    assert "- Unowned risk (PN-51010, G-TR1) - owner none" in rendered


def test_render_digest_empty_day_and_hidden_footer(world: World, profile: Profile) -> None:
    empty = render_digest(world, selection(profile))
    hidden = render_digest(world, selection(profile, hidden=2))

    assert empty == (
        "Daily digest for Nadia Brenner (ME) - 2026-10-14\n\nNothing new for Nadia Brenner."
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

        with pytest.raises(ValueError, match=r"(?i)(until|replay|cutoff).*2026-10-14"):
            build_digest(store, world, load_priors(), "U0ME01", DAY)


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


def test_cli_demo_reports_cassette_miss_without_api_key_or_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_cassettes = tmp_path / "empty-cassettes"
    empty_cassettes.mkdir()
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DIGEST_MODE", "replay")
    monkeypatch.setenv("DIGEST_CASSETTES", str(empty_cassettes))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(Settings, "__init__", partialmethod(Settings.__init__, _env_file=None))

    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 1, result.output
    assert re.search(r"no cassette for key", result.output, re.IGNORECASE), result.output


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
    assert len(re.findall(r"(?m)^-{3,}$", result.stdout)) == 2

"""Room model contract: M3 unit 8, excluding the scenario stretch."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.daily.profile import _owned_set
from digest.daily.render import line_parts, select_digest
from digest.daily.select import Section, Selection
from digest.driver import Change, LineView, ReaderDiff
from digest.models import Role
from digest.priors import Priors, load_priors
from digest.room import (
    Edit,
    Room,
    RoomBase,
    SetGateDate,
    SetMembership,
    SetOwner,
    SetPrivate,
    SetRole,
    SetTaskOwner,
    apply_world_edits,
    build_base,
    build_state,
)
from digest.store import Store
from digest.world.load import load_world
from digest.world.schema import World

WORLD_PATH = Path("data/world/kestrel/world.yaml")
DAY = date(2026, 10, 19)
PREVIOUS_DIGEST = date(2026, 10, 16)
READERS = ("U0ME01", "U0SC01", "U0EM01")
ABSENCES = ("absence:G-DVT1.emc", "absence:G-DVT1.thermal")
STALE_PACK = "C0EXTCM:1792164600.010061#1792164600.010061#0"
ANODIZE = "C0EXTCM:1792068738.000800#1792068738.000800#1"
DFM = "C0KPROG:1792423800.010077#1792423800.010077#1"
THERMAL = "C0KPROG:1792423800.010077#1792423800.010077#0"
ALL_EDITS: tuple[Edit, ...] = (
    SetRole(kind="set_role", user_id="U0SC01", role="EM"),
    SetPrivate(kind="set_private", channel_id="C0EXTCM", private=True),
    SetMembership(kind="set_membership", user_id="U0ME01", channel_id="C0LEADS", member=True),
    SetOwner(kind="set_owner", entity_id="PN-11051", role="EM"),
    SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 23)),
    SetTaskOwner(kind="set_task_owner", task_id="G-DVT1.emc", role="ME"),
)


@pytest.fixture(scope="session")
def world() -> World:
    return load_world(WORLD_PATH)


@pytest.fixture(scope="session")
def priors() -> Priors:
    return load_priors()


@pytest.fixture(scope="session")
def day6_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("room-day6") / "kestrel.sqlite"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DIGEST_MODE", "replay")
        patch.delenv("ANTHROPIC_API_KEY", raising=False)
        patch.delenv("OPENROUTER_API_KEY", raising=False)
        patch.delenv("OPENAI_API_KEY", raising=False)
        runner = CliRunner()
        for args in (
            ["replay", "--world", "kestrel", "--until", DAY.isoformat(), "--db", str(path)],
            ["extract", "--world", "kestrel", "--db", str(path)],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code == 0, (result.output, result.exception)
    return path


def base_for(path: Path, world: World, priors: Priors) -> RoomBase:
    # Call from test bodies so a stub failure is a test failure, not a fixture error.
    with Store.open(path) as store:
        return build_base(store, world, priors)


def empty_diff(user_id: str) -> ReaderDiff:
    return ReaderDiff(user_id=user_id, added=(), removed=(), changed=())


def item_ids(selection: Selection) -> set[str]:
    return {line.item.item_id for line in selection.lines}


def test_empty_edits_return_equal_world(world: World) -> None:
    assert apply_world_edits(world, []) == world


@pytest.mark.parametrize(
    "edit",
    [
        SetMembership(kind="set_membership", user_id="U0ME01", channel_id="C0DVT1B", member=True),
        SetPrivate(kind="set_private", channel_id="C0LEADS", private=True),
        SetRole(kind="set_role", user_id="U0ME01", role="ME"),
    ],
    ids=["existing-member", "already-private", "same-role"],
)
def test_setting_existing_value_leaves_world_equal(world: World, edit: Edit) -> None:
    once = apply_world_edits(world, [edit])

    assert once == world
    assert apply_world_edits(world, [edit, edit]) == once
    assert apply_world_edits(once, [edit]) == once


@pytest.mark.parametrize("edit", ALL_EDITS, ids=lambda edit: edit.kind)
def test_repeated_edit_equals_one_edit(world: World, edit: Edit) -> None:
    once = apply_world_edits(world, [edit])

    assert apply_world_edits(world, [edit, edit]) == once
    assert apply_world_edits(once, [edit]) == once


def test_role_changes_only_persona_role(world: World) -> None:
    expected = world.model_dump()
    next(p for p in expected["personas"] if p["user_id"] == "U0SC01")["role"] = "EM"
    before = world.model_dump()

    actual = apply_world_edits(world, [SetRole(kind="set_role", user_id="U0SC01", role="EM")])

    assert actual.model_dump() == expected
    assert world.model_dump() == before


@pytest.mark.parametrize("member", [True, False], ids=["add", "remove"])
def test_membership_changes_only_persona_channels(world: World, member: bool) -> None:
    channel_id = "C0LEADS" if member else "C0DVT1B"
    expected = world.model_dump()
    persona = next(p for p in expected["personas"] if p["user_id"] == "U0ME01")
    channels = set(persona["channels"])
    if member:
        channels.add(channel_id)
    else:
        channels.remove(channel_id)
    before = world.model_dump()

    actual = apply_world_edits(
        world,
        [
            SetMembership(
                kind="set_membership", user_id="U0ME01", channel_id=channel_id, member=member
            )
        ],
    ).model_dump()

    changed = next(p for p in actual["personas"] if p["user_id"] == "U0ME01")
    assert set(changed["channels"]) == channels
    assert len(changed["channels"]) == len(channels)
    changed["channels"] = persona["channels"]
    assert actual == expected
    assert world.model_dump() == before


@pytest.mark.parametrize("private", [True, False], ids=["private", "public"])
def test_private_changes_only_channel_flag(world: World, private: bool) -> None:
    channel_id = "C0EXTCM" if private else "C0LEADS"
    expected = world.model_dump()
    next(c for c in expected["channels"] if c["id"] == channel_id)["private"] = private
    before = world.model_dump()

    actual = apply_world_edits(
        world, [SetPrivate(kind="set_private", channel_id=channel_id, private=private)]
    )

    assert actual.model_dump() == expected
    assert world.model_dump() == before


@pytest.mark.parametrize("role", ["EM", None], ids=["transfer", "clear"])
def test_owner_changes_role_and_removes_every_direct_owner(world: World, role: Role | None) -> None:
    # Two direct owners expose an implementation that removes only the first one.
    world = world.model_copy(
        update={
            "personas": tuple(
                p.model_copy(update={"owned_entities": (*p.owned_entities, "PN-11051")})
                if p.user_id == "U0SC01"
                else p
                for p in world.personas
            )
        }
    )
    before = world.model_dump()
    expected = world.model_dump()
    next(e for e in expected["entities"] if e["id"] == "PN-11051")["owner"] = role
    for persona in expected["personas"]:
        persona["owned_entities"] = tuple(
            entity for entity in persona["owned_entities"] if entity != "PN-11051"
        )

    actual = apply_world_edits(world, [SetOwner(kind="set_owner", entity_id="PN-11051", role=role)])

    assert actual.model_dump() == expected
    assert world.model_dump() == before


@pytest.mark.parametrize(
    "day", [date(2026, 10, 12), date(2026, 10, 29)], ids=["start", "after-end"]
)
def test_gate_date_changes_only_gate_date(world: World, day: date) -> None:
    before = world.model_dump()
    expected = world.model_dump()
    next(g for g in expected["project"]["gates"] if g["id"] == "G-DVT1")["date"] = day

    actual = apply_world_edits(
        world, [SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=day)]
    )

    assert actual.model_dump() == expected
    assert world.model_dump() == before


def test_task_owner_changes_only_task_role(world: World) -> None:
    before = world.model_dump()
    expected = world.model_dump()
    gate = next(g for g in expected["project"]["gates"] if g["id"] == "G-DVT1")
    next(t for t in gate["checklist"] if t["id"] == "G-DVT1.emc")["owner"] = "ME"

    actual = apply_world_edits(
        world, [SetTaskOwner(kind="set_task_owner", task_id="G-DVT1.emc", role="ME")]
    )

    assert actual.model_dump() == expected
    assert world.model_dump() == before


@pytest.mark.parametrize(
    ("edit", "bad_id"),
    [
        (SetRole(kind="set_role", user_id="U-MISSING", role="EM"), "U-MISSING"),
        (SetRole(kind="set_role", user_id="B0PLM01", role="EM"), "B0PLM01"),
        (
            SetMembership(
                kind="set_membership", user_id="U-MISSING", channel_id="C0LEADS", member=True
            ),
            "U-MISSING",
        ),
        (
            SetMembership(
                kind="set_membership", user_id="B0PLM01", channel_id="C0LEADS", member=True
            ),
            "B0PLM01",
        ),
        (
            SetMembership(
                kind="set_membership", user_id="U0ME01", channel_id="C-MISSING", member=True
            ),
            "C-MISSING",
        ),
        (SetPrivate(kind="set_private", channel_id="C-MISSING", private=True), "C-MISSING"),
        (SetOwner(kind="set_owner", entity_id="PN-MISSING", role="EM"), "PN-MISSING"),
        (SetGateDate(kind="set_gate_date", gate_id="G-MISSING", date=DAY), "G-MISSING"),
        (SetTaskOwner(kind="set_task_owner", task_id="T-MISSING", role="ME"), "T-MISSING"),
        (SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 24)), "G-DVT1"),
        (SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 25)), "G-DVT1"),
        (SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 9)), "G-DVT1"),
    ],
    ids=[
        "role-unknown-persona",
        "role-bot",
        "membership-unknown-persona",
        "membership-bot",
        "membership-unknown-channel",
        "private-unknown-channel",
        "unknown-entity",
        "unknown-gate",
        "unknown-task",
        "saturday",
        "sunday",
        "before-start",
    ],
)
def test_r3_refusal_names_edit_and_id(world: World, edit: Edit, bad_id: str) -> None:
    before = world.model_dump()

    with pytest.raises(ValueError) as error:
        apply_world_edits(world, [edit])

    assert bad_id in str(error.value)
    assert edit.kind in str(error.value) or type(edit).__name__ in str(error.value)
    assert world.model_dump() == before


def test_remove_persona_from_channel_they_posted_in(world: World) -> None:
    assert any(e.poster == "U0ME01" and e.channel == "C0DVT1B" for e in world.events)

    actual = apply_world_edits(
        world,
        [
            SetMembership(
                kind="set_membership", user_id="U0ME01", channel_id="C0DVT1B", member=False
            )
        ],
    )

    assert "C0DVT1B" not in next(p for p in actual.personas if p.user_id == "U0ME01").channels
    assert actual.events == world.events


def test_build_base_records_day6_readers(day6_store: Path, world: World, priors: Priors) -> None:
    with Store.open(day6_store) as store:
        base = build_base(store, world, priors)
        expected = tuple(
            select_digest(store, world, priors, uid, DAY, previous_digest=PREVIOUS_DIGEST)
            for uid in READERS
        )

    assert base == RoomBase(
        world=world,
        priors=priors,
        day=DAY,
        store_path=day6_store,
        readers=READERS,
        recorded=expected,
    )


def test_build_state_without_edits_matches_recorded(
    day6_store: Path, world: World, priors: Priors
) -> None:
    with Store.open(day6_store) as store:
        recorded = tuple(
            select_digest(store, world, priors, uid, DAY, previous_digest=PREVIOUS_DIGEST)
            for uid in READERS
        )
        base = RoomBase(
            world=world,
            priors=priors,
            day=DAY,
            store_path=day6_store,
            readers=READERS,
            recorded=recorded,
        )
        state = build_state(base, store, [])

    assert state.world == world
    assert state.edits == ()
    assert state.selections == recorded
    assert state.diffs == tuple(empty_diff(uid) for uid in READERS)
    assert state.reworded == ((), (), ())
    assert state.last_effect == (0, 0, 0)


def test_edits_preserve_base_files_and_construct_no_llm(
    day6_store: Path, world: World, priors: Priors, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_llm(*args: Any, **kwargs: Any) -> None:
        pytest.fail("room constructed an LLM")

    wal = day6_store.with_name(day6_store.name + "-wal")
    before_db = day6_store.read_bytes()
    before_wal = wal.read_bytes() if wal.exists() else None
    before_world = load_world(WORLD_PATH)
    before_memory = world.model_dump()
    monkeypatch.setattr("digest.llm.client.LLM.__init__", forbid_llm)
    base = base_for(day6_store, world, priors)
    room = Room(base)

    for edit in ALL_EDITS:
        room.apply(edit)
    room.reset()

    assert day6_store.read_bytes() == before_db
    assert (wal.read_bytes() if wal.exists() else None) == before_wal
    assert load_world(WORLD_PATH) == before_world
    assert world.model_dump() == before_memory
    assert base.world == before_world


def test_equal_edit_sequences_give_equal_states(
    day6_store: Path, world: World, priors: Priors
) -> None:
    base = base_for(day6_store, world, priors)
    first, second = Room(base), Room(base)
    assert first.state() == second.state()

    for index, edit in enumerate(ALL_EDITS, 1):
        assert first.apply(edit) == second.apply(edit)
        assert first.state() == second.state()
        assert first.state().edits == ALL_EDITS[:index]


def test_reset_restores_recorded_state(day6_store: Path, world: World, priors: Priors) -> None:
    base = base_for(day6_store, world, priors)
    room = Room(base)
    initial = room.state()
    for edit in ALL_EDITS:
        room.apply(edit)
    assert room.state() != initial

    state = room.reset()

    assert state == initial == room.state()
    assert state.world == base.world
    assert state.edits == ()
    assert state.selections == base.recorded
    assert state.diffs == tuple(empty_diff(uid) for uid in base.readers)
    assert state.reworded == ((), (), ())
    assert state.last_effect == (0, 0, 0)


def test_sc_role_change_pins_whole_diff(day6_store: Path, world: World, priors: Priors) -> None:
    base = base_for(day6_store, world, priors)

    state = Room(base).apply(SetRole(kind="set_role", user_id="U0SC01", role="EM"))

    # world.yaml gives Hamid PO-45002211 directly and gives EM the gates.
    # B5.3 keeps his PO blocker and adds gate blockers and checklist absences.
    # B5.4 changes role priors and ranks; the stale drawing pack leaves the cap.
    def view(item_id: str, title: str, section: Section, rank: int | None, reason: str) -> LineView:
        return LineView(
            item_id=item_id,
            title=title,
            section=section,
            lane="must_see" if rank is None else "ranked",
            rank=rank,
            reason=reason,
        )

    ask = "absence:ask:C0ENGEF:1791997800.010040#1791997800.010040#0"
    mlcc = "C0SUPCH:1792079700.010041#1792079700.010041#0"
    expected = ReaderDiff(
        user_id="U0SC01",
        added=(
            view(
                ABSENCES[0],
                "DVT-1 go/no-go: no progress on Pass the TST-EMC-01 EMC pre-scan",
                "not_happened",
                None,
                "must see: a checklist absence, you run the gate",
            ),
            view(
                ABSENCES[1],
                "DVT-1 go/no-go: no progress on Close the TST-THM-02 thermal review",
                "not_happened",
                None,
                "must see: a checklist absence, you run the gate",
            ),
            view(
                DFM,
                "Top-cover DFM review open and blocking G-DVT1",
                "risks",
                None,
                "must see: an open blocker on an owned entity (G-DVT1)",
            ),
            view(
                "C0ENGEF:1791993300.010026#1791993300.010026#0",
                "TST-EMC-01 pre-scan failing on PN-31010 harness",
                "risks",
                4,
                "importance=1.0 role_prior=1.0 personal_relevance=0.8",
            ),
            view(
                "C0KPROG:1792423800.010077#1792423800.010077#2",
                "G-DVT1 schedule buffer eroding with two open reviews",
                "risks",
                5,
                "importance=0.49 role_prior=1.0 personal_relevance=1.0",
            ),
        ),
        removed=(
            view(
                STALE_PACK,
                "PN-11020 chassis base built to stale Rev B drawing pack",
                "risks",
                3,
                "importance=1.0 role_prior=1.0 personal_relevance=0.8",
            ),
        ),
        changed=(
            Change(
                item_id=ask,
                before=view(
                    ask,
                    "U0EM01 asked U0FW01; no reply for 3 working days",
                    "not_happened",
                    1,
                    "importance=0.63 role_prior=0.9 personal_relevance=0.4",
                ),
                after=view(
                    ask,
                    "U0EM01 asked U0FW01; no reply for 3 working days",
                    "not_happened",
                    1,
                    "importance=0.63 role_prior=1.0 personal_relevance=0.4",
                ),
                fields=("reason",),
            ),
            Change(
                item_id=THERMAL,
                before=view(
                    THERMAL,
                    "TST-THM-02 thermal review open and blocking G-DVT1",
                    "risks",
                    5,
                    "importance=1.0 role_prior=1.0 personal_relevance=0.4",
                ),
                after=view(
                    THERMAL,
                    "TST-THM-02 thermal review open and blocking G-DVT1",
                    "risks",
                    None,
                    "must see: an open blocker on an owned entity (G-DVT1)",
                ),
                fields=("lane", "rank", "reason"),
            ),
            Change(
                item_id=mlcc,
                before=view(
                    mlcc,
                    "C-MLCC-100V in allocation with no ship date on PN-21030",
                    "risks",
                    4,
                    "importance=1.0 role_prior=1.0 personal_relevance=0.8",
                ),
                after=view(
                    mlcc,
                    "C-MLCC-100V in allocation with no ship date on PN-21030",
                    "risks",
                    3,
                    "importance=1.0 role_prior=1.0 personal_relevance=0.8",
                ),
                fields=("rank",),
            ),
        ),
    )
    assert state.diffs[1] == expected
    assert "PO-45002211" in state.selections[1].profile.owned_entities
    assert state.diffs[0] == empty_diff("U0ME01")
    assert state.diffs[2] == empty_diff("U0EM01")


def test_private_cm_preserves_em_lines_and_increases_hidden(
    day6_store: Path, world: World, priors: Priors
) -> None:
    base = base_for(day6_store, world, priors)
    state = Room(base).apply(SetPrivate(kind="set_private", channel_id="C0EXTCM", private=True))

    assert tuple(s.hidden for s in base.recorded) == (0, 0, 0)
    assert tuple(s.hidden for s in state.selections) == (0, 0, 4)
    assert state.selections[:2] == base.recorded[:2]
    assert state.selections[2].lines == base.recorded[2].lines
    assert state.diffs[2] == empty_diff("U0EM01")
    assert state.diffs[:2] == (empty_diff("U0ME01"), empty_diff("U0SC01"))


def test_leaving_private_cm_changes_sc_lines(
    day6_store: Path, world: World, priors: Priors
) -> None:
    base = base_for(day6_store, world, priors)
    room = Room(base)
    before = room.apply(SetPrivate(kind="set_private", channel_id="C0EXTCM", private=True))

    state = room.apply(
        SetMembership(kind="set_membership", user_id="U0SC01", channel_id="C0EXTCM", member=False)
    )

    assert before.selections[1].hidden == 0
    assert state.selections[1].hidden == 4
    assert item_ids(state.selections[1]) == (
        item_ids(before.selections[1]) - {ANODIZE, STALE_PACK}
    ) | {DFM}
    assert next(
        line for line in before.selections[1].lines if line.item.item_id == ANODIZE
    ).must_see
    added = next(line for line in state.selections[1].lines if line.item.item_id == DFM)
    assert added.section == "risks"
    assert not added.must_see
    assert state.selections[0] == before.selections[0]
    assert state.selections[2] == before.selections[2]


@pytest.mark.parametrize("member", [True, False], ids=["add", "remove"])
def test_public_membership_changes_no_digest(
    day6_store: Path, world: World, priors: Priors, member: bool
) -> None:
    base = base_for(day6_store, world, priors)
    channel = "C0SUPCH" if member else "C0DVT1B"

    state = Room(base).apply(
        SetMembership(kind="set_membership", user_id="U0ME01", channel_id=channel, member=member)
    )

    for before, after in zip(base.recorded, state.selections, strict=True):
        assert after.lines == before.lines
        assert after.hidden == before.hidden
    assert state.diffs == tuple(empty_diff(uid) for uid in READERS)
    assert state.reworded == ((), (), ())
    assert state.last_effect == (0, 0, 0)


@pytest.mark.parametrize("user_id", ["U0ME01", "U0SC01"])
def test_joining_leads_changes_no_digest(
    day6_store: Path, world: World, priors: Priors, user_id: str
) -> None:
    base = base_for(day6_store, world, priors)

    state = Room(base).apply(
        SetMembership(kind="set_membership", user_id=user_id, channel_id="C0LEADS", member=True)
    )

    assert tuple(s.hidden for s in base.recorded) == (0, 0, 0)
    assert tuple(s.hidden for s in state.selections) == (0, 0, 0)
    assert tuple(s.lines for s in state.selections) == tuple(s.lines for s in base.recorded)
    assert state.diffs == tuple(empty_diff(uid) for uid in READERS)
    assert state.reworded == ((), (), ())
    assert state.last_effect == (0, 0, 0)


@pytest.mark.parametrize(
    ("day", "consumed"),
    [
        (date(2026, 10, 16), None),
        (date(2026, 10, 29), None),
        (date(2026, 10, 23), 3),
        (date(2026, 10, 28), 0),
    ],
    ids=["past-gate", "before-buffer", "three-days-used", "buffer-start"],
)
def test_gate_move_changes_em_absences(
    day6_store: Path, world: World, priors: Priors, day: date, consumed: int | None
) -> None:
    base = base_for(day6_store, world, priors)

    state = Room(base).apply(SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=day))

    before, after = base.recorded[2], state.selections[2]
    assert set(ABSENCES) <= item_ids(before)
    if consumed is None:
        assert item_ids(after) == item_ids(before) - set(ABSENCES)
        assert tuple(line.item_id for line in state.diffs[2].removed) == ABSENCES
        assert state.diffs[2].added == state.diffs[2].changed == ()
        assert state.reworded[2] == ()
    else:
        assert item_ids(after) == item_ids(before)
        assert state.diffs[2] == empty_diff("U0EM01")
        assert set(state.reworded[2]) == set(ABSENCES)
        assert len(state.reworded[2]) == 2
        names = {p.user_id: p.name for p in state.world.personas}
        tasks = {(g.id, t.id): t for g in state.world.project.gates for t in g.checklist}
        for line in after.lines:
            if line.item.item_id in ABSENCES:
                assert line.item.absence is not None
                assert line.item.absence.buffer_consumed == consumed
                assert line.item.absence.buffer_days == 7
                assert f"{consumed}/7 buffer days used" in line_parts(line, names, tasks)[0]
    assert state.last_effect[2] == 2


def test_last_effect_compares_latest_edit_not_recorded(
    day6_store: Path, world: World, priors: Priors
) -> None:
    room = Room(base_for(day6_store, world, priors))
    moved = room.apply(SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 23)))
    assert moved.last_effect[2] == 2

    state = room.apply(
        SetMembership(kind="set_membership", user_id="U0ME01", channel_id="C0LEADS", member=True)
    )

    assert state.last_effect == (0, 0, 0)
    assert state.reworded == moved.reworded
    assert state.diffs == moved.diffs


def test_select_exception_is_atomic_and_next_edit_succeeds(
    day6_store: Path, world: World, priors: Priors, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = Room(base_for(day6_store, world, priors))
    room.apply(SetOwner(kind="set_owner", entity_id="PN-11051", role="EM"))
    before = room.state()

    def fail_select(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("forced selection failure")

    with monkeypatch.context() as patch:
        patch.setattr("digest.daily.render.select", fail_select)
        with pytest.raises(RuntimeError, match=r"^forced selection failure$"):
            room.apply(SetRole(kind="set_role", user_id="U0SC01", role="EM"))
    assert room.state() == before

    edit = SetGateDate(kind="set_gate_date", gate_id="G-DVT1", date=date(2026, 10, 23))
    after = room.apply(edit)
    assert after == room.state()
    assert after.edits == (*before.edits, edit)
    assert next(p for p in after.world.personas if p.user_id == "U0SC01").role == "supply_chain"
    assert next(g for g in after.world.project.gates if g.id == "G-DVT1").date == edit.date
    assert after.last_effect[2] == 2


def test_owners_follow_effective_ownership(day6_store: Path, world: World, priors: Priors) -> None:
    room = Room(base_for(day6_store, world, priors))
    assert room.state().owners["PN-11051"] == ("U0ME01",)

    state = room.apply(SetOwner(kind="set_owner", entity_id="PN-11051", role="EM"))

    assert state.owners["PN-11051"] == ("U0EM01",)


def test_refused_edit_is_atomic_and_next_edit_succeeds(
    day6_store: Path, world: World, priors: Priors
) -> None:
    room = Room(base_for(day6_store, world, priors))
    room.apply(SetPrivate(kind="set_private", channel_id="C0EXTCM", private=True))
    before = room.state()

    with pytest.raises(ValueError, match="U-MISSING"):
        room.apply(SetRole(kind="set_role", user_id="U-MISSING", role="EM"))

    assert room.state() == before
    edit = SetRole(kind="set_role", user_id="U0SC01", role="EM")
    after = room.apply(edit)
    assert after == room.state()
    assert after.edits == (*before.edits, edit)
    assert next(p for p in after.world.personas if p.user_id == "U0SC01").role == "EM"


def test_owners_include_inherited_children(day6_store: Path, world: World, priors: Priors) -> None:
    child = next(e for e in world.entities if e.id == "PN-42010")
    assert child.parent == "PN-21020"
    assert child.owner is None
    assert all(child.id not in p.owned_entities for p in world.personas)
    room = Room(base_for(day6_store, world, priors))
    assert room.state().owners[child.id] == ("U0EE01",)

    state = room.apply(SetOwner(kind="set_owner", entity_id=child.id, role="EM"))

    expected = tuple(
        sorted(
            p.user_id
            for p in state.world.personas
            if p.role is not None and child.id in _owned_set(state.world, p, p.role)
        )
    )
    assert expected == ("U0EE01", "U0EM01")
    assert state.owners[child.id] == expected


@pytest.mark.parametrize("directory", [False, True], ids=["missing", "directory"])
def test_room_requires_existing_store_file(
    tmp_path: Path, world: World, priors: Priors, directory: bool
) -> None:
    path = tmp_path if directory else tmp_path / "missing.sqlite"
    base = RoomBase(
        world=world,
        priors=priors,
        day=DAY,
        store_path=path,
        readers=READERS,
        recorded=(),
    )

    with pytest.raises(ValueError):
        Room(base)

    if not directory:
        assert not path.exists()

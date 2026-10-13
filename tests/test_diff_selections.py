from datetime import date
from typing import Literal

import pytest

from digest.daily.profile import Profile
from digest.daily.select import Line, Section, Selection
from digest.driver import Change, DiffField, LineView, ReaderDiff, diff_selections
from digest.models import Evidence, Item

DAY = date(2026, 10, 19)
BASE_TS = "1792454400.000000"


def _profile(user_id: str = "U0ME01") -> Profile:
    return Profile(
        user_id=user_id,
        name="Maya Chen",
        role="ME",
        team="mechanical",
        owned_entities=(),
        channels=("C0ME",),
    )


def _item(item_id: str, title: str | None = None) -> Item:
    return Item(
        item_id=item_id,
        thread_id=f"C0ME:{BASE_TS}",
        type="fyi",
        title=title or f"Title {item_id}",
        summary=f"Summary {item_id}",
        entities=(),
        owner=None,
        due=None,
        affected_roles=(),
        severity="low",
        confidence="high",
        evidence=(
            Evidence(
                channel_id="C0ME",
                message_ts=BASE_TS,
                quote=f"Evidence {item_id}",
                permalink=f"https://kestrel-k2.slack.com/archives/C0ME/p{BASE_TS.replace('.', '')}",
            ),
        ),
        first_seen=BASE_TS,
        last_updated=BASE_TS,
    )


def _line(
    item_id: str,
    *,
    section: Section = "fyi",
    must_see: bool = False,
    title: str | None = None,
    reason: str = "because it matters",
    score: float = 0.5,
) -> Line:
    return Line(
        item=_item(item_id, title),
        section=section,
        must_see=must_see,
        reason=reason,
        score=score,
    )


def _selection(
    *lines: Line,
    user_id: str = "U0ME01",
    day: date = DAY,
    hidden: int = 0,
) -> Selection:
    return Selection(profile=_profile(user_id), day=day, lines=lines, hidden=hidden)


def _view(
    item_id: str,
    *,
    section: Section = "fyi",
    lane: Literal["must_see", "ranked"] = "ranked",
    rank: int | None = 1,
    title: str | None = None,
    reason: str = "because it matters",
) -> LineView:
    return LineView(
        item_id=item_id,
        title=title or f"Title {item_id}",
        section=section,
        lane=lane,
        rank=rank,
        reason=reason,
    )


def test_diff_selections_returns_empty_diff_for_equal_selections() -> None:
    selection = _selection(_line("same"))

    result = diff_selections(selection, selection)

    assert result == ReaderDiff(user_id="U0ME01", added=(), removed=(), changed=())


def test_diff_selections_reports_added_and_removed_full_views() -> None:
    before = _selection(_line("removed", section="risks", must_see=True))
    after = _selection(_line("added", section="decisions", reason="new reason"))

    result = diff_selections(before, after)

    assert result.added == (_view("added", section="decisions", reason="new reason"),)
    assert result.removed == (_view("removed", section="risks", lane="must_see", rank=None),)
    assert result.changed == ()


@pytest.mark.parametrize(
    ("field", "before_lines", "after_lines", "before_view", "after_view"),
    [
        (
            "section",
            (_line("target", section="decisions"),),
            (_line("target", section="risks"),),
            _view("target", section="decisions"),
            _view("target", section="risks"),
        ),
        (
            "rank",
            (_line("target"),),
            (_line("zz-ahead"), _line("target")),
            _view("target"),
            _view("target", rank=2),
        ),
        (
            "title",
            (_line("target", title="Old title"),),
            (_line("target", title="New title"),),
            _view("target", title="Old title"),
            _view("target", title="New title"),
        ),
        (
            "reason",
            (_line("target", reason="old reason"),),
            (_line("target", reason="new reason"),),
            _view("target", reason="old reason"),
            _view("target", reason="new reason"),
        ),
    ],
    ids=["section", "rank", "title", "reason"],
)
def test_diff_selections_reports_each_change_field_alone(
    field: DiffField,
    before_lines: tuple[Line, ...],
    after_lines: tuple[Line, ...],
    before_view: LineView,
    after_view: LineView,
) -> None:
    result = diff_selections(_selection(*before_lines), _selection(*after_lines))

    assert result.changed == (
        Change(item_id="target", before=before_view, after=after_view, fields=(field,)),
    )


def test_diff_selections_lists_combined_fields_in_contract_order() -> None:
    before = _selection(
        _line("target", section="risks", must_see=True, title="Old title", reason="old reason")
    )
    after = _selection(
        _line("ahead", section="fyi"),
        _line("target", section="fyi", title="New title", reason="new reason"),
    )

    result = diff_selections(before, after)

    assert result.changed == (
        Change(
            item_id="target",
            before=_view(
                "target",
                section="risks",
                lane="must_see",
                rank=None,
                title="Old title",
                reason="old reason",
            ),
            after=_view("target", rank=2, title="New title", reason="new reason"),
            fields=("section", "lane", "rank", "title", "reason"),
        ),
    )


def test_diff_selections_lane_switch_reports_lane_rank_and_changed_reason() -> None:
    before = _selection(_line("target", section="risks", must_see=True, reason="urgent"))
    after = _selection(_line("target", section="risks", reason="ranked evidence"))

    result = diff_selections(before, after)

    assert result.changed == (
        Change(
            item_id="target",
            before=_view("target", section="risks", lane="must_see", rank=None, reason="urgent"),
            after=_view("target", section="risks", reason="ranked evidence"),
            fields=("lane", "rank", "reason"),
        ),
    )


def test_diff_selections_rank_uses_ranked_lines_in_same_section_only() -> None:
    before = _selection(
        _line("must-see", section="risks", must_see=True),
        _line("other-section", section="decisions"),
        _line("target", section="risks"),
    )
    after = _selection(
        _line("must-see", section="risks", must_see=True),
        _line("other-section", section="decisions"),
        _line("zz-ahead", section="risks"),
        _line("target", section="risks"),
    )

    result = diff_selections(before, after)

    assert result.changed == (
        Change(
            item_id="target",
            before=_view("target", section="risks"),
            after=_view("target", section="risks", rank=2),
            fields=("rank",),
        ),
    )


def test_diff_selections_ignores_score_change_without_order_or_reason_change() -> None:
    before = _selection(_line("same", score=0.1))
    after = _selection(_line("same", score=0.9))

    result = diff_selections(before, after)

    assert result == ReaderDiff(user_id="U0ME01", added=(), removed=(), changed=())


def test_diff_selections_orders_all_result_tuples_by_line_view() -> None:
    before = _selection(
        _line("removed-fyi", section="fyi"),
        _line("change-z", section="fyi", must_see=True, reason="old"),
        _line("removed-risk-ranked", section="risks"),
        _line("removed-risk-must-z", section="risks", must_see=True),
        _line("removed-risk-must-a", section="risks", must_see=True),
        _line("change-ranked-z", section="decisions", reason="old"),
        _line("change-ranked-a", section="decisions", reason="old"),
    )
    after = _selection(
        _line("added-fyi", section="fyi"),
        _line("change-z", section="needs_you", must_see=True, reason="new"),
        _line("added-needs-you", section="needs_you"),
        _line("added-not-happened", section="not_happened"),
        _line("added-decision", section="decisions"),
        _line("change-ranked-z", section="decisions", reason="new"),
        _line("change-ranked-a", section="decisions", reason="new"),
        _line("added-risk-ranked-z", section="risks"),
        _line("added-risk-ranked-a", section="risks"),
        _line("added-risk-must-z", section="risks", must_see=True),
        _line("added-risk-must-a", section="risks", must_see=True),
    )

    result = diff_selections(before, after)

    assert tuple(view.item_id for view in result.added) == (
        "added-needs-you",
        "added-not-happened",
        "added-decision",
        "added-risk-must-a",
        "added-risk-must-z",
        "added-risk-ranked-z",
        "added-risk-ranked-a",
        "added-fyi",
    )
    assert tuple(view.item_id for view in result.removed) == (
        "removed-risk-must-a",
        "removed-risk-must-z",
        "removed-risk-ranked",
        "removed-fyi",
    )
    assert tuple(change.item_id for change in result.changed) == (
        "change-z",
        "change-ranked-z",
        "change-ranked-a",
    )


def test_diff_selections_refuses_different_readers() -> None:
    with pytest.raises(ValueError, match=r"U0ME01.*U0EE01"):
        diff_selections(
            _selection(_line("before"), user_id="U0ME01"),
            _selection(_line("after"), user_id="U0EE01"),
        )


@pytest.mark.parametrize("duplicate_side", ["before", "after"])
def test_diff_selections_refuses_duplicate_item_id(duplicate_side: str) -> None:
    duplicate = _selection(_line("same"), _line("same", section="risks"))
    unique = _selection(_line("other"))
    before, after = (duplicate, unique) if duplicate_side == "before" else (unique, duplicate)

    with pytest.raises(ValueError, match=r"item_id same"):
        diff_selections(before, after)


def test_diff_selections_ignores_day_and_hidden() -> None:
    before = _selection(_line("same"), day=date(2026, 10, 18), hidden=1)
    after = _selection(_line("same"), day=date(2026, 10, 19), hidden=99)

    result = diff_selections(before, after)

    assert result == ReaderDiff(user_id="U0ME01", added=(), removed=(), changed=())


def test_diff_selections_is_repeatable_and_does_not_change_inputs() -> None:
    before = _selection(_line("removed"), _line("changed", reason="old"))
    after = _selection(_line("added"), _line("changed", reason="new"))
    before_dump = before.model_dump()
    after_dump = after.model_dump()

    first = diff_selections(before, after)
    second = diff_selections(before, after)

    assert second == first
    assert before.model_dump() == before_dump
    assert after.model_dump() == after_dump

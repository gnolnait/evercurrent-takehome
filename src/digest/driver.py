"""The what-if driver (docs/plans/m3-driver.md): compare two selections of one reader."""

from __future__ import annotations

from typing import Literal, get_args

from digest.daily.select import SECTION_CAPS, Section, Selection
from digest.models import Frozen

DiffField = Literal["section", "lane", "rank", "title", "reason"]


class LineView(Frozen):
    """One line of a selection as the diff compares it."""

    item_id: str
    title: str
    section: Section
    lane: Literal["must_see", "ranked"]
    # 1-based position among the ranked lines of its section; None in the must-see lane
    rank: int | None
    reason: str


class Change(Frozen):
    """An item in both selections whose view differs."""

    item_id: str
    before: LineView
    after: LineView
    fields: tuple[DiffField, ...]  # every differing field, in the order of `DiffField`


class ReaderDiff(Frozen):
    """What changed for one reader between two selections."""

    user_id: str
    added: tuple[LineView, ...]
    removed: tuple[LineView, ...]
    changed: tuple[Change, ...]


def diff_selections(before: Selection, after: Selection) -> ReaderDiff:
    """Compare two selections of one reader by `item_id` (plan m3-driver.md, `digest diff`).

    The caller passes two selections of the same reader; `user_id` is `after.profile.user_id`.

    Each tuple is ordered by section (as `Selection.lines`), then lane (must-see first),
    then rank, then `item_id`; a `Change` sorts by its `after` view.
    """
    user_id = after.profile.user_id
    if before.profile.user_id != user_id:
        raise ValueError(f"cannot diff readers {before.profile.user_id} and {user_id}")
    before_views = _views(before)
    after_views = _views(after)
    added = [view for item_id, view in after_views.items() if item_id not in before_views]
    removed = [view for item_id, view in before_views.items() if item_id not in after_views]
    changed: list[Change] = []
    for item_id, after_view in after_views.items():
        before_view = before_views.get(item_id)
        if before_view is None or before_view == after_view:
            continue
        fields = tuple(
            field
            for field in get_args(DiffField)
            if getattr(before_view, field) != getattr(after_view, field)
        )
        changed.append(Change(item_id=item_id, before=before_view, after=after_view, fields=fields))
    return ReaderDiff(
        user_id=user_id,
        added=tuple(sorted(added, key=_order)),
        removed=tuple(sorted(removed, key=_order)),
        changed=tuple(sorted(changed, key=_change_order)),
    )


_SECTION_ORDER: dict[Section, int] = {section: index for index, section in enumerate(SECTION_CAPS)}


def _views(selection: Selection) -> dict[str, LineView]:
    """Map each `item_id` of the selection to its view; refuse a duplicate `item_id`."""
    views: dict[str, LineView] = {}
    ranks: dict[Section, int] = {}
    for line in selection.lines:
        item_id = line.item.item_id
        if item_id in views:
            raise ValueError(f"item_id {item_id} occurs twice in one selection")
        rank = None
        if not line.must_see:
            rank = ranks.get(line.section, 0) + 1
            ranks[line.section] = rank
        views[item_id] = LineView(
            item_id=item_id,
            title=line.item.title,
            section=line.section,
            lane="must_see" if line.must_see else "ranked",
            rank=rank,
            reason=line.reason,
        )
    return views


def _change_order(change: Change) -> tuple[int, int, int, str]:
    """Sort key of a `Change`: the key of its `after` view."""
    return _order(change.after)


def _order(view: LineView) -> tuple[int, int, int, str]:
    """Sort key: section, then lane (must-see first), then rank, then `item_id`."""
    return (
        _SECTION_ORDER[view.section],
        0 if view.lane == "must_see" else 1,
        view.rank or 0,
        view.item_id,
    )

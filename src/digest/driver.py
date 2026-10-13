"""The what-if driver (docs/plans/m3-driver.md): compare two selections of one reader."""

from __future__ import annotations

from typing import Literal

from digest.daily.select import Section, Selection
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
    raise NotImplementedError

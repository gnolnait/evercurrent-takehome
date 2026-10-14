"""The read half of the D20 function layer (plan docs/plans/m4-queries.md).

Plain functions over a replayed and extracted store. Each one returns a frozen view of
plain fields, so the MCP server and the web preview serialize it as it is. No function
writes the store.
"""

from datetime import date
from typing import Literal

from pydantic import SkipValidation

from digest.daily.select import Section
from digest.models import Attribute, Authority, Frozen, Item, Role
from digest.priors import Priors
from digest.store import Store
from digest.world.schema import World

Cell = str | int | float | None
TaskState = Literal["done", "in progress", "no evidence"]


class QueryContext(Frozen):
    """The store, the world and the window that every query function reads (plan Q0)."""

    store: SkipValidation[Store]
    world: SkipValidation[World]
    priors: Priors
    day: date  # the store's day; open_context sets it from meta.until
    previous_digest: date | None  # None: a first digest, as in select_digest


class EvidenceView(Frozen):
    channel_id: str
    channel_name: str | None
    message_ts: str
    quote: str
    permalink: str
    author_id: str | None  # None when the message is not stored
    author_name: str | None


class ItemView(Frozen):
    item: Item
    owner_name: str | None
    evidence: tuple[EvidenceView, ...]  # in the order of item.evidence


class ClaimView(Frozen):
    item_id: str
    attribute: Attribute
    value: str
    channel_id: str
    message_ts: str
    authority: Authority
    author_id: str | None  # None when the message is not stored
    author_name: str | None


class PersonView(Frozen):
    user_id: str
    name: str
    role: Role | None
    kind: Literal["human", "bot"]


class EntityView(Frozen):
    entity_id: str
    type: str
    description: str
    owner_role: Role | None
    owners: tuple[PersonView, ...]  # by user_id
    claims: tuple[ClaimView, ...]  # by message ts, then item_id
    note: str  # says the claims are as stated, with no supersession


class RankedLineView(Frozen):
    user_id: str
    user_name: str
    item_id: str
    title: str
    section: Section
    lane: Literal["must_see", "ranked"]
    rank: int | None  # 1-based among the ranked lines of its section; None when must-see
    score: float  # 0 for a must-see line
    reason: str


class DigestView(Frozen):
    user_id: str
    user_name: str
    day: date
    previous_digest: date | None
    lines: tuple[RankedLineView, ...]  # in the selection's order
    text: str  # render_digest


class GateView(Frozen):
    gate_id: str
    name: str
    date: date
    buffer_start: date
    in_buffer: bool


class TaskView(Frozen):
    gate_id: str
    task_id: str
    task: str
    owner_role: Role
    state: TaskState
    latest_link_day: date | None  # the newest task link day; None without a link


class ProjectStateView(Frozen):
    day: date
    phase_line: str | None
    gates: tuple[GateView, ...]  # by date, then gate_id
    tasks: tuple[TaskView, ...]  # by gate, then task_id
    people: tuple[PersonView, ...]  # by user_id


class QueryResult(Frozen):
    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]  # a BLOB cell is its hex text
    truncated: bool


def open_context(
    store: Store,
    world: World,
    priors: Priors,
    *,
    previous_digest: date | Literal["auto"] | None = "auto",
) -> QueryContext:
    """The context for the store's day; refuse a store as select_digest does (plan Q0)."""
    raise NotImplementedError


def search_items(
    ctx: QueryContext,
    text: str = "",
    *,
    type: str | None = None,
    status: str | None = None,
    entity: str | None = None,
    owner: str | None = None,
    updated_since: date | None = None,
    limit: int = 20,
) -> tuple[ItemView, ...]:
    """The items that match every given filter (plan Q1)."""
    raise NotImplementedError


def get_item(ctx: QueryContext, item_id: str) -> ItemView | None:
    """One stored or absence item; None for an unknown id (plan Q2)."""
    raise NotImplementedError


def get_entity_history(ctx: QueryContext, entity: str) -> EntityView | None:
    """The entity, its owners and every claim on it; None for an unknown entity (plan Q3)."""
    raise NotImplementedError


def list_open_blockers(ctx: QueryContext) -> tuple[ItemView, ...]:
    """Open blocker items, newest evidence first (plan Q4)."""
    raise NotImplementedError


def list_absences(ctx: QueryContext) -> tuple[ItemView, ...]:
    """The absence items for the day, in detect_absences order (plan Q5)."""
    raise NotImplementedError


def why_ranked(ctx: QueryContext, user_id: str, item_id: str) -> RankedLineView | None:
    """The reader's line for the item; None when the digest does not hold it (plan Q6)."""
    raise NotImplementedError


def get_digest(ctx: QueryContext, user_id: str) -> DigestView:
    """The reader's digest as lines and as rendered text (plan Q7)."""
    raise NotImplementedError


def get_project_state(ctx: QueryContext) -> ProjectStateView:
    """The phase, the gates, the checklist task states and the people on the day (plan Q8)."""
    raise NotImplementedError


def query(
    ctx: QueryContext, sql: str, *, row_cap: int = 200, timeout_s: float = 2.0
) -> QueryResult:
    """Run one read-only SQL statement under an authorizer, a row cap and a timeout (plan Q9)."""
    raise NotImplementedError


def schema_text() -> str:
    """The text of src/digest/schema.sql (plan Q10)."""
    raise NotImplementedError

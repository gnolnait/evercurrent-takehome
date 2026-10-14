"""The read half of the D20 function layer (plan docs/plans/m4-queries.md).

Plain functions over a replayed and extracted store. Each one returns a frozen view of
plain fields, so the MCP server and the web preview serialize it as it is. No function
writes the store.
"""

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from time import perf_counter
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, SkipValidation

from digest.absence import detect_absences, task_citations
from digest.daily.profile import profile_for
from digest.daily.render import (
    check_store_day,
    phase_line,
    render_digest,
    replayed_until,
    select_digest,
)
from digest.daily.select import Section, Selection
from digest.models import (
    Attribute,
    Authority,
    Event,
    Evidence,
    Frozen,
    Item,
    ItemStatus,
    ItemType,
    Role,
    ts_datetime,
    ts_micros,
)
from digest.priors import Priors
from digest.project import add_working_days
from digest.store import Store
from digest.world.schema import Entity, Persona, World

Cell = str | int | float | None
TaskState = Literal["done", "in progress", "no evidence"]


class QueryContext(Frozen):
    """The store, the world and the window that every query function reads (plan Q0)."""

    # Store is a plain class; pydantic needs leave to hold it.
    model_config = ConfigDict(arbitrary_types_allowed=True)

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


_NOTE = (
    "The claims are as stated. The claim ledger is not built, so there is no supersession: "
    "a later claim does not replace an earlier one."
)
# The limit on the text and BLOB cells of a query result, in lengths as SQLite returns them.
_RESULT_BYTES = 8_000_000
# The authorizer actions that a read needs. FTS5 also reads PRAGMA data_version.
_READ_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)


def open_context(
    store: Store,
    world: World,
    priors: Priors,
    *,
    previous_digest: date | Literal["auto"] | None = "auto",
) -> QueryContext:
    """The context for the store's day; refuse a store as select_digest does (plan Q0).

    "auto" gives the previous working day, or None (a first digest) on the world's first day.
    """
    day = replayed_until(store).astimezone(ZoneInfo(world.timezone)).date()
    check_store_day(store, world, day)
    previous = previous_digest
    if previous == "auto":
        previous = add_working_days(day, -1) if day > world.start_date else None
    return QueryContext(store=store, world=world, priors=priors, day=day, previous_digest=previous)


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
    if not 1 <= limit <= 100:
        raise ValueError(f"limit {limit} is not from 1 to 100")
    if type is not None and type not in get_args(ItemType):
        raise ValueError(f"type {type!r} is not one of {get_args(ItemType)}")
    if status is not None and status not in get_args(ItemStatus):
        raise ValueError(f"status {status!r} is not one of {get_args(ItemStatus)}")
    needle = text.strip().lower()
    if not needle and all(f is None for f in (type, status, entity, owner, updated_since)):
        raise ValueError("give a text or at least one filter")
    wanted = None
    if entity is not None:
        wanted = _entity(ctx.world, entity)
        if wanted is None:
            raise ValueError(f"entity {entity!r} is not an entity id or alias")
    if owner is not None and owner not in _names(ctx.world):
        raise ValueError(f"owner {owner!r} is not a persona user id")
    zone = ZoneInfo(ctx.world.timezone)
    names = {e.id: (e.id, *e.aliases) for e in ctx.world.entities}
    hits: dict[tuple[str, str], int] = {}
    if needle:
        # SQLite reads LIMIT -1 as no limit, so the search returns every hit.
        found = ctx.store.search_events(text, _as_of(ctx), limit=-1)
        for position, event in enumerate(found):
            hits.setdefault((event.channel_id, event.ts), position)
    matches: list[tuple[tuple[int, int, int, str], Item]] = []
    for item in _items(ctx):
        if (
            (type is not None and item.type != type)
            or (status is not None and item.status != status)
            or (wanted is not None and wanted.id not in item.entities)
            or (owner is not None and item.owner != owner)
            or (
                updated_since is not None
                and ts_datetime(item.last_updated).astimezone(zone).date() < updated_since
            )
        ):
            continue
        cited = [hits[key] for key in map(_message, item.evidence) if key in hits]
        position = min(cited) if cited else None
        terms = [term for e in item.entities for term in names.get(e, (e,))]
        if needle and not (
            position is not None
            or needle in item.title.lower()
            or needle in item.summary.lower()
            or any(needle in term.lower() for term in terms)
        ):
            continue
        rank = (0, position) if position is not None else (1, 0)
        matches.append(((*rank, -ts_micros(item.last_updated), item.item_id), item))
    matches.sort(key=lambda match: match[0])
    return _views(ctx, [item for _, item in matches[:limit]])


def get_item(ctx: QueryContext, item_id: str) -> ItemView | None:
    """One stored or absence item; None for an unknown id (plan Q2)."""
    item = next((item for item in _items(ctx) if item.item_id == item_id), None)
    return None if item is None else _views(ctx, [item])[0]


def get_entity_history(ctx: QueryContext, entity: str) -> EntityView | None:
    """The entity, its owners and every claim on it; None for an unknown entity (plan Q3)."""
    found = _entity(ctx.world, entity)
    if found is None:
        return None
    owners = tuple(
        _person(persona)
        for persona in sorted(ctx.world.personas, key=lambda p: p.user_id)
        if persona.kind == "human"
        and found.id in profile_for(ctx.world, persona.user_id).owned_entities
    )
    cited = [
        (item, claim)
        for item in ctx.store.items(_as_of(ctx))
        for claim in item.claims
        if claim.entity == found.id
    ]
    events = _events(ctx, [item for item, _ in cited])
    people = _names(ctx.world)
    claims: list[ClaimView] = []
    for item, claim in cited:
        event = events.get((claim.channel_id, claim.message_ts))
        author = None if event is None else event.user_id
        claims.append(
            ClaimView(
                item_id=item.item_id,
                **claim.model_dump(exclude={"entity"}),
                author_id=author,
                author_name=None if author is None else people.get(author),
            )
        )
    claims.sort(key=lambda claim: (claim.message_ts, claim.item_id))
    return EntityView(
        entity_id=found.id,
        type=found.type,
        description=found.description,
        owner_role=found.owner,
        owners=owners,
        claims=tuple(claims),
        note=_NOTE,
    )


def list_open_blockers(ctx: QueryContext) -> tuple[ItemView, ...]:
    """Open blocker items, newest evidence first (plan Q4).

    The newest evidence is the greatest `event_ts` of the cited messages, as select_digest
    counts it. A cited message the store does not hold counts at its `message_ts`.
    """
    blockers = [
        item
        for item in ctx.store.items(_as_of(ctx))
        if item.type == "blocker" and item.status == "open"
    ]
    events = _events(ctx, blockers)

    def newest(item: Item) -> int:
        return max(
            ts_micros(events[key].event_ts) if key in events else ts_micros(key[1])
            for key in map(_message, item.evidence)
        )

    return _views(ctx, sorted(blockers, key=lambda item: (-newest(item), item.item_id)))


def list_absences(ctx: QueryContext) -> tuple[ItemView, ...]:
    """The absence items for the day, in detect_absences order (plan Q5)."""
    return _views(ctx, _absences(ctx))


def why_ranked(ctx: QueryContext, user_id: str, item_id: str) -> RankedLineView | None:
    """The reader's line for the item; None when the digest does not hold it (plan Q6)."""
    lines = _line_views(_selection(ctx, user_id))
    return next((line for line in lines if line.item_id == item_id), None)


def get_digest(ctx: QueryContext, user_id: str) -> DigestView:
    """The reader's digest as lines and as rendered text (plan Q7)."""
    selection = _selection(ctx, user_id)
    return DigestView(
        user_id=selection.profile.user_id,
        user_name=selection.profile.name,
        day=ctx.day,
        previous_digest=ctx.previous_digest,
        lines=_line_views(selection),
        text=render_digest(ctx.world, selection),
    )


def get_project_state(ctx: QueryContext) -> ProjectStateView:
    """The phase, the gates, the checklist task states and the people on the day (plan Q8).

    A gate's buffer starts `buffer_days` working days before the gate, as for the
    checklist absences. The task state reads the task links as `checklist_absences` does.
    """
    zone = ZoneInfo(ctx.world.timezone)
    items = ctx.store.items(_as_of(ctx))
    gates: list[GateView] = []
    tasks: list[TaskView] = []
    for gate in sorted(ctx.world.project.gates, key=lambda gate: (gate.date, gate.id)):
        start = add_working_days(gate.date, -gate.buffer_days)
        gates.append(
            GateView(
                gate_id=gate.id,
                name=gate.name,
                date=gate.date,
                buffer_start=start,
                in_buffer=start <= ctx.day <= gate.date,
            )
        )
        for task in sorted(gate.checklist, key=lambda task: task.id):
            links, _ = task_citations(gate, task, items, ctx.day, zone)
            state: TaskState = "no evidence"
            if any(link.effect == "done" for _, link in links):
                state = "done"
            elif links:
                state = "in progress"
            days = [ts_datetime(link.message_ts).astimezone(zone).date() for _, link in links]
            tasks.append(
                TaskView(
                    gate_id=gate.id,
                    task_id=task.id,
                    task=task.task,
                    owner_role=task.owner,
                    state=state,
                    latest_link_day=max(days, default=None),
                )
            )
    return ProjectStateView(
        day=ctx.day,
        phase_line=phase_line(ctx.world, ctx.day),
        gates=tuple(gates),
        tasks=tuple(tasks),
        people=tuple(_person(p) for p in sorted(ctx.world.personas, key=lambda p: p.user_id)),
    )


def query(
    ctx: QueryContext, sql: str, *, row_cap: int = 200, timeout_s: float = 2.0
) -> QueryResult:
    """Run one read-only SQL statement under an authorizer, a row cap and a timeout (plan Q9).

    Raise ValueError for a limit out of range, an empty statement, a refused or failed
    statement (with SQLite's message), a statement that runs past `timeout_s`, and a
    result whose text and BLOB cells pass `_RESULT_BYTES`. The timeout also applies
    while the rows are fetched and converted.
    """
    if not 1 <= row_cap <= 1000:
        raise ValueError(f"row_cap {row_cap} is not from 1 to 1000")
    if not 0.1 <= timeout_s <= 10:
        raise ValueError(f"timeout_s {timeout_s} is not from 0.1 to 10")
    path = ctx.store.path
    if not isinstance(path, Path):
        raise ValueError("cannot query an in-memory store")
    deadline = perf_counter() + timeout_s
    timed_out = False

    def stop() -> bool:
        nonlocal timed_out
        timed_out = perf_counter() > deadline
        return timed_out

    # Store hides its connection, so open one with the URI that Store.open_readonly uses.
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.set_authorizer(_authorize)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        connection.set_progress_handler(stop, 1000)
        cursor = connection.execute(sql)
        # The authorizer allows only reads, and each read has a column. A statement
        # without columns is blank or only a comment.
        if cursor.description is None:
            raise ValueError("one SQL statement is required")
        columns = tuple(column[0] for column in cursor.description)
        rows: list[tuple[Cell, ...]] = []
        size = 0
        truncated = False
        for row in cursor:  # one row at a time, so the size check comes before the next row
            if len(rows) == row_cap:
                truncated = True
                break
            size += sum(len(value) for value in row if isinstance(value, str | bytes))
            if size > _RESULT_BYTES:
                raise ValueError("query result is too large")
            rows.append(tuple(_cell(value) for value in row))
            # The progress handler stops SQLite only. This check stops the Python work.
            if perf_counter() > deadline:
                raise ValueError("query timed out")
    except sqlite3.Error as error:
        if timed_out:
            raise ValueError("query timed out") from error
        raise ValueError(str(error)) from error
    finally:
        connection.close()
    return QueryResult(columns=columns, rows=tuple(rows), truncated=truncated)


def schema_text() -> str:
    """The text of src/digest/schema.sql (plan Q10)."""
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _as_of(ctx: QueryContext) -> datetime:
    """The end of the context's day in the world's zone."""
    return datetime.combine(ctx.day, time.max, ZoneInfo(ctx.world.timezone))


def _absences(ctx: QueryContext) -> list[Item]:
    zone = ZoneInfo(ctx.world.timezone)
    return detect_absences(ctx.store, ctx.world.project, ctx.priors, _as_of(ctx), zone)


def _items(ctx: QueryContext) -> list[Item]:
    """The stored items as of the day, then the absence items of the day."""
    return [*ctx.store.items(_as_of(ctx)), *_absences(ctx)]


def _events(ctx: QueryContext, items: Sequence[Item]) -> dict[tuple[str, str], Event]:
    """The stored messages of the items' threads; an item cites only its own thread."""
    as_of = _as_of(ctx)
    return {
        _message(event): event
        for thread_id in {item.thread_id for item in items}
        for event in ctx.store.events_in_thread(thread_id, as_of)
    }


def _message(entry: Event | Evidence) -> tuple[str, str]:
    """The (channel_id, ts) key of a message; a Slack ts is unique only within a channel."""
    if isinstance(entry, Event):
        return entry.channel_id, entry.ts
    return entry.channel_id, entry.message_ts


def _names(world: World) -> dict[str, str]:
    return {persona.user_id: persona.name for persona in world.personas}


def _views(ctx: QueryContext, items: Sequence[Item]) -> tuple[ItemView, ...]:
    """Each item with its owner name and its evidence, with authors and channel names."""
    people = _names(ctx.world)
    channels = {channel.id: channel.name for channel in ctx.world.channels}
    events = _events(ctx, items)
    views = []
    for item in items:
        evidence = []
        for entry in item.evidence:
            event = events.get(_message(entry))
            author = None if event is None else event.user_id
            evidence.append(
                EvidenceView(
                    **entry.model_dump(),
                    channel_name=channels.get(entry.channel_id),
                    author_id=author,
                    author_name=None if author is None else people.get(author),
                )
            )
        owner = None if item.owner is None else people.get(item.owner)
        views.append(ItemView(item=item, owner_name=owner, evidence=tuple(evidence)))
    return tuple(views)


def _entity(world: World, name: str) -> Entity | None:
    """The entity with this id or alias, case-insensitive; None for no match."""
    wanted = name.strip().lower()
    return next(
        (
            entity
            for entity in world.entities
            if wanted in {term.lower() for term in (entity.id, *entity.aliases)}
        ),
        None,
    )


def _person(persona: Persona) -> PersonView:
    return PersonView(
        user_id=persona.user_id, name=persona.name, role=persona.role, kind=persona.kind
    )


def _selection(ctx: QueryContext, user_id: str) -> Selection:
    return select_digest(
        ctx.store, ctx.world, ctx.priors, user_id, ctx.day, previous_digest=ctx.previous_digest
    )


def _line_views(selection: Selection) -> tuple[RankedLineView, ...]:
    """The selection's lines; a ranked line counts its rank in its section, as digest.driver."""
    profile = selection.profile
    ranks: dict[Section, int] = {}
    views = []
    for line in selection.lines:
        rank = None
        if not line.must_see:
            rank = ranks[line.section] = ranks.get(line.section, 0) + 1
        views.append(
            RankedLineView(
                user_id=profile.user_id,
                user_name=profile.name,
                item_id=line.item.item_id,
                title=line.item.title,
                section=line.section,
                lane="must_see" if line.must_see else "ranked",
                rank=rank,
                score=line.score,
                reason=line.reason,
            )
        )
    return tuple(views)


def _authorize(
    action: int, arg1: str | None, arg2: str | None, database: str | None, source: str | None
) -> int:
    """Allow reads only. Allow PRAGMA data_version without an argument, because FTS5 reads it."""
    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() == "data_version" and arg2 is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _cell(value: Cell | bytes) -> Cell:
    """A BLOB cell becomes its hex text."""
    return value.hex() if isinstance(value, bytes) else value

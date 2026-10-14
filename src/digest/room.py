"""The demo room: the recorded world with edits applied, and the readers' digests.

See docs/plans/m3-ui.md and docs/plans/m3-room-edits.md. A world edit changes only the
world that selection reads and never reaches a model request. A message change writes the
room store, a copy of the base store, and runs extraction for the room's day on it.
Extraction reads the recorded world, so a recorded message change replays from cassettes.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Literal, NoReturn, Protocol, assert_never, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import Field, SkipValidation

from digest.daily.profile import _owned_set
from digest.daily.render import line_parts, select_digest
from digest.daily.select import Selection
from digest.driver import ReaderDiff, ScriptLine, StoreView, diff_selections, make_event
from digest.extract.stage import run_extract
from digest.llm.cassette import CassetteMiss, Cassettes
from digest.llm.client import LLM, from_settings
from digest.models import Event, Frozen, Role, micros_since_epoch
from digest.priors import Priors
from digest.project import ChecklistTask, Gate, add_working_days
from digest.queries import QueryContext, open_context
from digest.replay import read_events
from digest.settings import Settings
from digest.store import Store
from digest.world.schema import Persona, World

log = logging.getLogger(__name__)

_READERS = ("U0ME01", "U0SC01", "U0EM01")
_DAY = 6  # the world day of the room
_STAMP = dt.time(18, 0)  # the k-th message change is stamped k minutes later (plan M1)
NOT_RECORDED = (
    "this message change is not recorded; start digest serve --record with "
    "OPENROUTER_API_KEY to try it live"
)


class SetRole(Frozen):
    kind: Literal["set_role"]
    user_id: str
    role: Role


class SetMembership(Frozen):
    kind: Literal["set_membership"]
    user_id: str
    channel_id: str
    member: bool


class SetPrivate(Frozen):
    kind: Literal["set_private"]
    channel_id: str
    private: bool


class SetOwner(Frozen):
    kind: Literal["set_owner"]
    entity_id: str
    role: Role | None


class SetGateDate(Frozen):
    kind: Literal["set_gate_date"]
    gate_id: str
    date: dt.date


class SetTaskOwner(Frozen):
    kind: Literal["set_task_owner"]
    task_id: str
    role: Role


class EditMessage(Frozen):
    """Rewrite a stored message (plan m3-room-edits M2)."""

    kind: Literal["edit_message"]
    channel_id: str
    ts: str
    text: str
    files: tuple[str, ...] = ()


class PostMessage(Frozen):
    """Post a new message as a persona (plan m3-room-edits M3)."""

    kind: Literal["post_message"]
    user_id: str
    channel_id: str
    text: str
    thread_ts: str | None = None


class RunScenario(Frozen):
    """Post a recorded unit 7 script line (plan m3-room-edits M4)."""

    kind: Literal["run_scenario"]
    name: Literal["d2-bracket-delay", "d3-gate-delay"]


MessageChange = EditMessage | PostMessage | RunScenario
_MESSAGE_KINDS = (EditMessage, PostMessage, RunScenario)

Edit = Annotated[
    SetRole
    | SetMembership
    | SetPrivate
    | SetOwner
    | SetGateDate
    | SetTaskOwner
    | EditMessage
    | PostMessage
    | RunScenario,
    Field(discriminator="kind"),
]


class MessageView(Frozen):
    """One message in the room store up to the room's day, for the Messages panel."""

    channel_id: str
    channel_name: str
    thread_ts: str
    ts: str
    user_id: str
    author: str  # persona name
    text: str
    files: tuple[str, ...]
    edited: bool
    posted: bool  # true for a message the room added


class RoomBase(Frozen):
    """Built once by `digest serve` from a store replayed and extracted through `day`."""

    world: World  # the recorded world
    priors: Priors
    day: dt.date
    store_path: Path
    readers: tuple[str, ...]
    recorded: tuple[Selection, ...]  # each reader's selection in the recorded world


class RoomState(Frozen):
    # The room world skips the `World` script checks: pydantic runs them again on a
    # `World` instance, so the field skips validation.
    world: SkipValidation[World]
    edits: tuple[Edit, ...]  # since start or the last reset
    selections: tuple[Selection, ...]  # same order as RoomBase.readers
    diffs: tuple[ReaderDiff, ...]  # each reader against RoomBase.recorded
    # Per reader: item ids in both selections whose LineView is equal but whose
    # `line_parts` differ from the recorded ones.
    reworded: tuple[tuple[str, ...], ...]
    # Per reader: lines the latest edit added, removed, changed or reworded.
    last_effect: tuple[int, ...]
    owners: dict[str, tuple[str, ...]]  # entity id -> sorted user ids whose owned set holds it
    # Every message in the room store up to the room's day, after the message changes, by
    # channel, thread and ts (plan m3-room-edits). Empty for a hand-built state.
    messages: tuple[MessageView, ...] = ()
    message_changes: int = 0  # message changes since the last reset


def apply_world_edits(world: World, edits: Sequence[Edit]) -> World:
    """The world with the edits applied in order; ValueError naming the edit on a refusal."""
    for edit in edits:
        world = _apply(world, edit)
    return world


def _apply(world: World, edit: Edit) -> World:
    """The world with one edit applied (plan R3 checks).

    The room world skips the `World` script checks, so this uses `model_copy` and never
    validates a new `World`.
    """
    match edit:
        case SetRole():
            persona = _human(world, edit, edit.user_id)
            return _with_persona(world, persona.model_copy(update={"role": edit.role}))
        case SetMembership():
            persona = _human(world, edit, edit.user_id)
            _require_channel(world, edit, edit.channel_id)
            if not edit.member:
                channels = tuple(c for c in persona.channels if c != edit.channel_id)
            elif edit.channel_id in persona.channels:
                channels = persona.channels
            else:
                channels = (*persona.channels, edit.channel_id)
            return _with_persona(world, persona.model_copy(update={"channels": channels}))
        case SetPrivate():
            _require_channel(world, edit, edit.channel_id)
            channels = tuple(
                c.model_copy(update={"private": edit.private}) if c.id == edit.channel_id else c
                for c in world.channels
            )
            return world.model_copy(update={"channels": channels})
        case SetOwner():
            if all(entity.id != edit.entity_id for entity in world.entities):
                _refuse(edit, f"{edit.entity_id} is not an entity")
            entities = tuple(
                e.model_copy(update={"owner": edit.role}) if e.id == edit.entity_id else e
                for e in world.entities
            )
            personas = tuple(
                p.model_copy(
                    update={
                        "owned_entities": tuple(
                            owned for owned in p.owned_entities if owned != edit.entity_id
                        )
                    }
                )
                for p in world.personas
            )
            return world.model_copy(update={"entities": entities, "personas": personas})
        case SetGateDate():
            if all(gate.id != edit.gate_id for gate in world.project.gates):
                _refuse(edit, f"{edit.gate_id} is not a gate")
            if edit.date.weekday() >= 5 or edit.date < world.start_date:
                _refuse(
                    edit,
                    f"gate {edit.gate_id} cannot move to {edit.date}: the date must be a "
                    f"weekday on or after {world.start_date}",
                )
            gates = tuple(
                g.model_copy(update={"date": edit.date}) if g.id == edit.gate_id else g
                for g in world.project.gates
            )
            return _with_gates(world, gates)
        case SetTaskOwner():
            if all(t.id != edit.task_id for g in world.project.gates for t in g.checklist):
                _refuse(edit, f"{edit.task_id} is not a checklist task")
            gates = tuple(
                g.model_copy(
                    update={
                        "checklist": tuple(
                            t.model_copy(update={"owner": edit.role}) if t.id == edit.task_id else t
                            for t in g.checklist
                        )
                    }
                )
                for g in world.project.gates
            )
            return _with_gates(world, gates)
        case EditMessage() | PostMessage() | RunScenario():
            return world  # a message change changes the room store, never the world
        case _:
            assert_never(edit)


def _refuse(edit: Edit, reason: str) -> NoReturn:
    raise ValueError(f"edit {edit.kind} refused: {reason}")


def _human(world: World, edit: Edit, user_id: str) -> Persona:
    persona = next((p for p in world.personas if p.user_id == user_id), None)
    if persona is None:
        _refuse(edit, f"{user_id} is not a persona")
    if persona.kind != "human":
        _refuse(edit, f"{user_id} is a bot, not a person")
    return persona


def _require_channel(world: World, edit: Edit, channel_id: str) -> None:
    if all(channel.id != channel_id for channel in world.channels):
        _refuse(edit, f"{channel_id} is not a channel")


def _with_persona(world: World, persona: Persona) -> World:
    personas = tuple(persona if p.user_id == persona.user_id else p for p in world.personas)
    return world.model_copy(update={"personas": personas})


def _with_gates(world: World, gates: tuple[Gate, ...]) -> World:
    return world.model_copy(update={"project": world.project.model_copy(update={"gates": gates})})


def build_base(store: Store, world: World, priors: Priors) -> RoomBase:
    """The room base from a store already replayed and extracted through world day 6."""
    day = world.date_of(_DAY)
    return RoomBase(
        world=world,
        priors=priors,
        day=day,
        store_path=Path(store.path),
        readers=_READERS,
        recorded=_select(store, world, priors, _READERS, day),
    )


def build_state(
    base: RoomBase,
    store: Store,
    edits: Sequence[Edit],
    *,
    before: Store | None = None,
    messages: Sequence[MessageView] = (),
) -> RoomState:
    """The room state for the edits, read from `store`.

    `before` is the store that the edits without the latest one read; the default is
    `store`. `messages` fills the Messages panel of the state.
    """
    world = apply_world_edits(base.world, edits)
    selections = _select(store, world, base.priors, base.readers, base.day)
    diffs, reworded = _compare(base.world, base.recorded, world, selections)
    last_effect = tuple(0 for _ in base.readers)
    if edits:
        last_world = apply_world_edits(base.world, edits[:-1])
        last = _select(before or store, last_world, base.priors, base.readers, base.day)
        last_diffs, last_reworded = _compare(last_world, last, world, selections)
        last_effect = tuple(
            len(d.added) + len(d.removed) + len(d.changed) + len(r)
            for d, r in zip(last_diffs, last_reworded, strict=True)
        )
    owned = {
        p.user_id: set(_owned_set(world, p, p.role)) for p in world.personas if p.role is not None
    }
    return RoomState(
        world=world,
        edits=tuple(edits),
        selections=selections,
        diffs=diffs,
        reworded=reworded,
        last_effect=last_effect,
        owners={
            e.id: tuple(sorted(uid for uid, entities in owned.items() if e.id in entities))
            for e in world.entities
        },
        messages=tuple(messages),
        message_changes=sum(isinstance(edit, _MESSAGE_KINDS) for edit in edits),
    )


def _select(
    store: Store, world: World, priors: Priors, readers: Sequence[str], day: dt.date
) -> tuple[Selection, ...]:
    """Each reader's selection since the previous working day, the window of `digest demo`."""
    previous = add_working_days(day, -1)
    return tuple(
        select_digest(store, world, priors, uid, day, previous_digest=previous) for uid in readers
    )


def _compare(
    before_world: World,
    before: Sequence[Selection],
    after_world: World,
    after: Sequence[Selection],
) -> tuple[tuple[ReaderDiff, ...], tuple[tuple[str, ...], ...]]:
    """Per reader: the diff, and the ids whose `LineView` is equal but whose words differ."""
    before_names, before_tasks = _names(before_world), _tasks(before_world)
    after_names, after_tasks = _names(after_world), _tasks(after_world)
    diffs: list[ReaderDiff] = []
    reworded: list[tuple[str, ...]] = []
    for old, new in zip(before, after, strict=True):
        diff = diff_selections(old, new)
        changed = {change.item_id for change in diff.changed}
        old_lines = {line.item.item_id: line for line in old.lines}
        diffs.append(diff)
        reworded.append(
            tuple(
                line.item.item_id
                for line in new.lines
                if line.item.item_id in old_lines
                and line.item.item_id not in changed
                and line_parts(old_lines[line.item.item_id], before_names, before_tasks)
                != line_parts(line, after_names, after_tasks)
            )
        )
    return tuple(diffs), tuple(reworded)


def _names(world: World) -> dict[str, str]:
    return {persona.user_id: persona.name for persona in world.personas}


def _tasks(world: World) -> dict[tuple[str, str], ChecklistTask]:
    return {(gate.id, task.id): task for gate in world.project.gates for task in gate.checklist}


class RoomLike(Protocol):
    """What the web app needs from a room."""

    def apply(self, edit: Edit, *, fresh: bool = False) -> RoomState: ...

    def reset(self) -> RoomState: ...

    def state(self) -> RoomState: ...


@runtime_checkable
class SavingRoom(RoomLike, Protocol):
    """A room that writes its corpus copy (plan m3-room-edits S1)."""

    def save(self, path: Path) -> Path: ...


@runtime_checkable
class ItemsRoom(RoomLike, Protocol):
    """A room that the open-items page reads (plan m4-items I4)."""

    def items_context(self) -> AbstractContextManager[QueryContext]: ...


class ReseedLike(Protocol):
    """The seed owner's reseed of a saved corpus copy (plan m3-room-edits S2)."""

    def __call__(self, copy_dir: Path, *, dry_run: bool) -> str: ...


class Room:
    """Keeps store paths, never an open connection: each rebuild opens and closes them.

    The room store is a copy of the base store in a temporary directory that the room
    owns. It exists only while message changes exist.
    """

    def __init__(
        self, base: RoomBase, *, record: bool = False, cassettes: Path = Path("data/cassettes")
    ) -> None:
        """Build the first state; ValueError when `base.store_path` is not an existing file.

        `record` allows model calls for message changes with no cassette (plan M5).
        """
        # Store.open creates a missing file, so check before the first build.
        if not base.store_path.is_file():
            raise ValueError(f"room store {base.store_path} is not an existing file")
        self._base = base
        self._record = record
        # Resolve now, so a later change of the working directory changes no path.
        self._cassettes = cassettes.resolve()
        with Store.open_readonly(base.store_path) as store:
            name = store.get_meta("world")
        if name is None:
            raise ValueError(f"room store {base.store_path} has no meta key 'world'")
        self._world_dir = (Path("data") / "world" / name).resolve()
        corpus = read_events(self._world_dir / "events.jsonl", base.world.workspace_url)
        self._corpus = {(event.channel_id, event.ts): event for event in corpus}
        # Built on the first message change, so a world edit constructs no LLM.
        self._llm: LLM | None = None
        self._tmp = tempfile.TemporaryDirectory(prefix="digest-room-", ignore_cleanup_errors=True)
        self._stores = 0  # room store files made, for the next file name
        self._store: Path | None = None  # the room store; None with no message change
        self._changed: dict[tuple[str, str], Event] = {}  # events the message changes wrote
        self._state = self._build((), None, {}, None)

    def apply(self, edit: Edit, *, fresh: bool = False) -> RoomState:
        """Apply the edit; on a refusal raise ValueError and keep the state unchanged.

        With `fresh`, a message change drops every earlier message change since the last
        reset, keeps the world edits and applies as the first message change, so a
        curated edit replays from its recorded cassettes. A world edit ignores `fresh`.
        """
        if not isinstance(edit, _MESSAGE_KINDS):
            edits = (*self._state.edits, edit)
            self._state = self._build(edits, self._store, self._changed, self._store)
            return self._state
        earlier = self._state.edits
        if fresh:
            earlier = tuple(e for e in earlier if not isinstance(e, _MESSAGE_KINDS))
        edits = (*earlier, edit)
        # A scenario or a fresh change starts the room store again from the base store
        # (plan M4).
        restart = fresh or isinstance(edit, RunScenario)
        source = self._base.store_path if restart or self._store is None else self._store
        changed = {} if restart else dict(self._changed)
        stamp = dt.datetime.combine(self._base.day, _STAMP) + dt.timedelta(
            minutes=sum(isinstance(e, _MESSAGE_KINDS) for e in edits)
        )
        self._stores += 1
        path = Path(self._tmp.name) / f"room-{self._stores}.sqlite"
        try:
            with Store.open_readonly(source) as origin, origin.copy_to(path) as store:
                for event in self._write(store, edit, stamp, changed):
                    changed[(event.channel_id, event.ts)] = event
                self._extract(store)
            state = self._build(edits, path, changed, self._store)
        except BaseException:
            _remove_store(path)
            raise
        old, self._store, self._changed, self._state = self._store, path, changed, state
        if old is not None:
            _remove_store(old)
        return self._state

    def reset(self) -> RoomState:
        """Drop every edit; when the build fails, keep the state and the message changes."""
        # Build first. Change the room only after the build succeeds.
        state = self._build((), None, {}, None)
        old, self._store, self._changed, self._state = self._store, None, {}, state
        if old is not None:
            _remove_store(old)
        return self._state

    def state(self) -> RoomState:
        return self._state

    @contextlib.contextmanager
    def items_context(self) -> Iterator[QueryContext]:
        """Open the current store read-only and yield its context for the current world.

        The current store is the room store when message changes exist, else the base store.
        """
        with Store.open_readonly(self._store or self._base.store_path) as store:
            yield open_context(store, self._state.world, self._base.priors)

    def save(self, path: Path) -> Path:
        """Write the corpus copy with the room's message changes (plan S1); return `path`.

        The copy holds every file of the world directory. Its `events.jsonl` holds the
        committed events and the posted events, in `ts` order. An edited message has its
        new text and files, `edited` false and `event_ts` equal to its `ts`, so the copy
        is a clean corpus that `read_events` and `plan_seed` accept. The room store keeps
        the edit flag and the stamp.

        `path` must be a folder strictly inside `<working directory>/.room/`; any other
        path is refused with ValueError before any write, and so are a `.room` that is a
        link or a junction and a path inside `data/world`. The copy is built in a temporary
        folder beside `path`, which replaces `path` only when every file is written. On a
        failure the previous copy stays as it was and the temporary folder is removed.
        """
        cwd = Path.cwd().resolve()
        root = cwd / ".room"
        # A link at .room could point the copy at the committed corpus.
        if root.is_symlink() or root.is_junction() or root.resolve() != root:
            raise ValueError(f"cannot save the room: {root} is a link or resolves elsewhere")
        target = path.resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError(f"the room saves only into a folder inside {root}, not {path}")
        if target.is_relative_to(cwd / "data" / "world"):
            raise ValueError(f"cannot save the room inside the committed worlds: {path}")
        if target.exists() and not target.is_dir():
            raise ValueError(f"cannot save the room over {path}: it is not a folder")
        events = dict(self._corpus)
        for key, event in self._changed.items():
            committed = self._corpus.get(key)
            events[key] = (
                event
                if committed is None
                else committed.model_copy(update={"text": event.text, "files": event.files})
            )
        # Python sorts stably, so equal ts values keep the committed order.
        ordered = sorted(events.values(), key=lambda e: e.ts_us)
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{path.name}-", dir=path.parent))
        try:
            shutil.copytree(self._world_dir, staging, dirs_exist_ok=True)
            with (staging / "events.jsonl").open("w", encoding="utf-8", newline="\n") as file:
                file.writelines(event.model_dump_json() + "\n" for event in ordered)
        except BaseException:
            _remove_folder(staging)
            raise
        _replace_dir(staging, path)
        return path

    def _write(
        self,
        store: Store,
        edit: MessageChange,
        stamp: dt.datetime,
        changed: Mapping[tuple[str, str], Event],
    ) -> list[Event]:
        """Write the events of one message change to the room store (plan M2 to M4)."""
        world = self._base.world
        match edit:
            case EditMessage():
                key = (edit.channel_id, edit.ts)
                event = self._corpus.get(key)
                if event is None:
                    _refuse(edit, f"no recorded message has ts {edit.ts} in {edit.channel_id}")
                if _local(world, event.ts).date() > self._base.day:
                    _refuse(
                        edit,
                        f"message {edit.ts} is after day {_DAY} ({self._base.day}); only a "
                        "message on or before the room's day can change",
                    )
                updated = Event.model_validate(
                    changed.get(key, event).model_dump()
                    | {
                        "text": edit.text,
                        "files": edit.files,
                        "edited": True,
                        "event_ts": _ts(world, stamp),
                    }
                )
                store.upsert_events([updated])
                return [updated]
            case PostMessage():
                channel = next((c for c in world.channels if c.id == edit.channel_id), None)
                if channel is None:
                    _refuse(edit, f"{edit.channel_id} is not a channel")
                line = ScriptLine(
                    as_=edit.user_id,
                    channel=channel.name,
                    at=stamp.isoformat(),
                    text=edit.text,
                    thread=edit.thread_ts,
                )
                return [self._say(store, edit, line)]
            case RunScenario():
                script = self._world_dir / "scripts" / f"{edit.name}.jsonl"
                return [
                    self._say(store, edit, ScriptLine.model_validate_json(raw))
                    for raw in script.read_text(encoding="utf-8").split("\n")
                    if raw.strip()
                ]
            case _:
                assert_never(edit)

    def _say(self, store: Store, edit: Edit, line: ScriptLine) -> Event:
        """Write the event of one say line, as `digest say` does (unit 7 rules S1 to S3)."""
        world = self._base.world
        view = StoreView(day=self._base.day, latest_event_ts=store.latest_event_ts())
        at = dt.datetime.fromisoformat(line.at)
        try:
            event = make_event(world, view, line.as_, line.channel, at, line.text, line.thread)
        except ValueError as error:
            _refuse(edit, str(error))
        if line.thread is not None:  # rule S3
            found = store.events_in_thread(event.thread_id, _end_of_day(world, self._base.day))
            if not found or found[0].ts != line.thread:
                _refuse(
                    edit,
                    f"thread {line.thread} is not the ts of a top-level message in "
                    f"channel {line.channel}",
                )
        store.upsert_events([event])
        return event

    def _extract(self, store: Store) -> None:
        """Run extraction for the room's day, as `digest say` then `digest extract` do (M5)."""
        world, day = self._base.world, self._base.day
        store.set_meta("extract_until", (day - dt.timedelta(days=1)).isoformat())
        zone = ZoneInfo(world.timezone)
        glossary = world.glossary()
        purposes = {channel.id: channel.purpose for channel in world.channels}
        as_of = _end_of_day(world, day)
        if self._llm is None:
            self._llm = LLM(None, Cassettes(self._cassettes), "replay")
        try:
            report = run_extract(
                store, self._llm, glossary, world.project, self._base.priors, as_of, zone, purposes
            )
        except CassetteMiss:
            if not self._record:
                raise ValueError(NOT_RECORDED) from None
            # The gateway backend, built only when a miss needs it. The replay run keeps its
            # stage runs and triage decisions in the store, so this run resumes.
            # from_settings raises ValueError for a missing key and OSError for a missing
            # model ids file.
            try:
                self._llm = from_settings(
                    Settings(mode="record", backend="openrouter", cassettes=self._cassettes)
                )
            except OSError as error:
                raise ValueError(f"cannot start a live model call: {error}") from None
            # run_extract counts a backend failure as a failed thread, and the check below
            # refuses it. A backend failure is an LLMOutputError: a network failure, an HTTP
            # status such as an auth or rate-limit refusal, or output that does not validate.
            report = run_extract(
                store, self._llm, glossary, world.project, self._base.priors, as_of, zone, purposes
            )
        if report.failed:
            cause = (
                "the live model call failed (a network failure, an auth or rate-limit "
                "refusal, another HTTP error, or output that did not validate)"
                if self._llm.mode == "record"
                else "a recorded model output did not validate"
            )
            raise ValueError(
                f"extraction failed for {report.failed} thread(s) on {day}: {cause}; the room "
                "keeps its state"
            )
        store.set_meta("extract_until", day.isoformat())

    def _build(
        self,
        edits: Sequence[Edit],
        store_path: Path | None,
        changed: Mapping[tuple[str, str], Event],
        before_path: Path | None,
    ) -> RoomState:
        """The state for the edits, read from the room store or, with none, the base store."""
        messages = self._messages(changed)
        with Store.open(store_path or self._base.store_path) as store:
            if before_path == store_path:
                return build_state(self._base, store, edits, messages=messages)
            with Store.open(before_path or self._base.store_path) as before:
                return build_state(self._base, store, edits, before=before, messages=messages)

    def _messages(self, changed: Mapping[tuple[str, str], Event]) -> tuple[MessageView, ...]:
        """Every message up to the room's day after the message changes."""
        world = self._base.world
        names = _names(world)
        events = {**self._corpus, **changed}
        return tuple(
            MessageView(
                channel_id=event.channel_id,
                channel_name=event.channel_name,
                thread_ts=event.thread_ts,
                ts=event.ts,
                user_id=event.user_id,
                author=names.get(event.user_id, event.user_id),
                text=event.text,
                files=event.files,
                edited=event.edited,
                posted=key not in self._corpus,
            )
            for key, event in sorted(
                events.items(), key=lambda pair: (pair[0][0], pair[1].thread_ts, pair[0][1])
            )
            if _local(world, event.event_ts).date() <= self._base.day
        )


def _local(world: World, ts: str) -> dt.datetime:
    """The world-zone time of a Slack `ts`."""
    return dt.datetime.fromtimestamp(float(ts), ZoneInfo(world.timezone))


def _ts(world: World, at: dt.datetime) -> str:
    """The Slack `ts` of a naive world-zone time, as `make_event` makes it."""
    micros = micros_since_epoch(at.replace(tzinfo=ZoneInfo(world.timezone)))
    return f"{micros // 1_000_000:010d}.{micros % 1_000_000:06d}"


def _end_of_day(world: World, day: dt.date) -> dt.datetime:
    """The last instant of a world day, in UTC."""
    return dt.datetime.combine(day, dt.time.max, ZoneInfo(world.timezone)).astimezone(dt.UTC)


def _replace_dir(new: Path, path: Path) -> None:
    """Put the folder `new` at `path`; both are in the same parent folder.

    Windows cannot rename a folder over an existing one, so an existing `path` moves
    aside first, and it moves back when the second rename fails. A failure before the
    new folder is in place removes `new`. When the previous folder also cannot move
    back, the error names both folders, and both stay on disk.
    """
    try:
        if not path.exists():
            new.rename(path)
            return
        # mkdtemp reserves a free name; the rename below needs that name with no folder.
        old = Path(tempfile.mkdtemp(prefix=f".{path.name}-old-", dir=path.parent))
        old.rmdir()
        path.rename(old)
    except BaseException:
        _remove_folder(new)
        raise
    try:
        new.rename(path)
    except BaseException as error:
        try:
            old.rename(path)
        except OSError as restore_error:
            raise OSError(
                f"cannot put the room copy at {path} ({error}) or move the previous copy "
                f"back ({restore_error}); the new copy is in {new} and the previous copy "
                f"is in {old}"
            ) from error
        _remove_folder(new)
        raise
    _remove_folder(old)


def _remove_folder(path: Path) -> None:
    """Remove a folder; log a warning that names a folder left behind."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning(
            "room_folder_left", extra={"path": str(path), "error_class": type(error).__name__}
        )


def _remove_store(path: Path) -> None:
    """Remove a room store file with its `-wal` and `-shm` files.

    Windows can hold a handle on a closed store file. The temporary directory removes
    such a file when the room goes, so this ignores it.
    """
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(f"{path}{suffix}").unlink(missing_ok=True)

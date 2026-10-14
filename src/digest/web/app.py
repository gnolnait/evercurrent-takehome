"""The demo room page: routes over a `RoomLike` (docs/plans/m3-ui.md, unit 9, and
docs/plans/m3-room-edits.md, unit 10)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from itertools import groupby
from pathlib import Path
from typing import Any, get_args

import uvicorn
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import FormData

from digest.daily.render import SECTION_TITLES, context_lines, line_parts, window_line
from digest.driver import Change, LineView
from digest.models import Role
from digest.room import (
    Edit,
    EditMessage,
    PostMessage,
    ReseedLike,
    RoomLike,
    RoomState,
    RunScenario,
    SavingRoom,
    SetGateDate,
    SetMembership,
    SetOwner,
    SetPrivate,
    SetRole,
    SetTaskOwner,
)

log = logging.getLogger(__name__)

# Autoescape is on for every template; no template marks data as safe.
_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=True,
    undefined=StrictUndefined,
)
_EDIT: TypeAdapter[Edit] = TypeAdapter(Edit)
_ROLES: tuple[str, ...] = get_args(Role)
# The form fields of each edit kind, after `kind` (plan, "Interface").
_FIELDS: dict[str, tuple[str, ...]] = {
    "set_role": ("user_id", "role"),
    "set_membership": ("user_id", "channel_id", "member"),
    "set_private": ("channel_id", "private"),
    "set_owner": ("entity_id", "role"),
    "set_gate_date": ("gate_id", "date"),
    "set_task_owner": ("task_id", "role"),
    "edit_message": ("channel_id", "ts", "text", "files"),
    "post_message": ("user_id", "channel_id", "text", "thread_ts"),
    "run_scenario": ("name",),
}
_BOOLEANS = {"true": True, "false": False}
# Pydantic also reads a Unix time or a datetime as a date; the form allows YYYY-MM-DD only.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SCENARIOS: tuple[str, ...] = get_args(RunScenario.model_fields["name"].annotation)
_MESSAGE_KINDS = (EditMessage, PostMessage, RunScenario)
_SAVED_NOTE = (
    "golden.jsonl is copied unchanged, so it no longer describes the copy when the copy "
    "holds message changes."
)


def create_app(room: RoomLike, *, reseed: ReseedLike | None = None) -> FastAPI:
    """Routes: GET /, POST /edit, POST /reset, POST /save; with `reseed`, POST /reseed."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    curated = _curated(Path("data") / "world" / room.state().world.name / "room-edits.yaml")
    # The message changes at the last save; empty before the first save (plan S3).
    saved: list[tuple[Edit, ...]] = []

    def render(
        refusal: str | None = None, notice: str | None = None, *, status: int = 400
    ) -> HTMLResponse:
        return _page(
            room.state(),
            refusal,
            notice=notice,
            curated=curated,
            seed=reseed is not None,
            status=status,
        )

    def failed(route: str, error: Exception) -> HTMLResponse:
        """The 500 page for an unexpected room error (plan m3-failures F4 and F5).

        The page and the log fields name only the error class. The message can hold a
        path, a token or a model reply. The traceback goes only to the local server log.
        """
        error_class = type(error).__name__
        log.error(
            "room_rebuild_failed",
            extra={"route": route, "error_class": error_class},
            exc_info=True,
        )
        return render(
            f"The room could not apply this change ({error_class}); the room is unchanged.",
            status=500,
        )

    @app.get("/")
    async def page() -> HTMLResponse:
        return render()

    @app.post("/edit")
    async def edit(request: Request) -> Response:
        # Each refusal is a ValueError: a bad form field here, an R3 check in the room.
        form = await request.form()
        # A curated or scenario form sends fresh=true; any other value or none is false.
        fresh = form.get("fresh") == "true"
        try:
            room.apply(_edit_from(form), fresh=fresh)
        except ValueError as error:
            return render(str(error))
        except Exception as error:
            return failed("/edit", error)
        return RedirectResponse("/", status_code=303)

    @app.post("/reset")
    async def reset() -> Response:
        try:
            room.reset()
        except ValueError as error:
            return render(str(error))
        except Exception as error:
            return failed("/reset", error)
        return RedirectResponse("/", status_code=303)

    @app.post("/save")
    async def save() -> HTMLResponse:
        if not isinstance(room, SavingRoom):
            return render("This room cannot save a corpus copy.")
        path = _copy_dir(room.state())
        try:
            room.save(path)
        except (OSError, ValueError) as error:
            saved.clear()  # a real reseed waits for a save that succeeds
            return render(f"The save failed: {error}")
        saved[:] = [_message_changes(room.state())]
        return render(
            notice=f"Saved the room's message changes to {path.as_posix()}. {_SAVED_NOTE}"
        )

    if reseed is not None:

        @app.post("/reseed")
        async def run_reseed(dry_run: str) -> HTMLResponse:
            if dry_run not in _BOOLEANS:
                return render(f"dry_run must be true or false, got {dry_run!r}.")
            state = room.state()
            if not saved:
                return render("Save the room's message changes before a reseed.")
            if saved[0] != _message_changes(state):
                return render(
                    "The room has message changes that are not saved. Save them before a reseed."
                )
            try:
                report = reseed(_copy_dir(state), dry_run=_BOOLEANS[dry_run])
            except (OSError, ValueError) as error:
                return render(f"The reseed was refused: {error}")
            title = "Reseed dry run" if _BOOLEANS[dry_run] else "Reseed"
            return render(notice=f"{title}:\n{report}")

    return app


def _copy_dir(state: RoomState) -> Path:
    """The corpus copy of the room's world (plan S1)."""
    return Path(".room") / state.world.name


def _message_changes(state: RoomState) -> tuple[Edit, ...]:
    return tuple(edit for edit in state.edits if isinstance(edit, _MESSAGE_KINDS))


def _curated(path: Path) -> list[dict[str, Any]]:
    """The curated message changes of `room-edits.yaml`, each with its form fields.

    The file is a list of entries with `name`, `description` and `edit`. A missing file
    gives no entry.
    """
    if not path.is_file():
        return []
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [
        {
            "name": entry["name"],
            "description": entry["description"],
            "fields": _form_fields(_EDIT.validate_python(entry["edit"])),
        }
        for entry in entries
    ]


def _form_fields(edit: Edit) -> list[tuple[str, str]]:
    """The /edit form fields of an edit, as `_edit_from` reads them."""
    fields: list[tuple[str, str]] = [("kind", edit.kind)]
    for name in _FIELDS[edit.kind]:
        value = getattr(edit, name)
        if isinstance(value, tuple):
            value = "\n".join(value)
        fields.append((name, "" if value is None else str(value)))
    return fields


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    """The server configuration; the host is always 127.0.0.1."""
    return uvicorn.Config(app, host="127.0.0.1", port=port)


def _edit_from(form: FormData) -> Edit:
    """The edit in the form; ValueError with the reason for a form the plan does not allow."""
    kind = form.get("kind")
    if not isinstance(kind, str):
        raise ValueError("The form has no edit kind.")
    if kind not in _FIELDS:
        raise ValueError(f"Unknown edit kind {kind!r}.")
    values: dict[str, Any] = {"kind": kind}
    for name in _FIELDS[kind]:
        value = form.get(name)
        if not isinstance(value, str):
            raise ValueError(f"The {kind} form has no field {name}.")
        values[name] = value
    for name in ("member", "private"):
        if name in values:
            if values[name] not in _BOOLEANS:
                raise ValueError(f"{name} must be true or false, got {values[name]!r}.")
            values[name] = _BOOLEANS[values[name]]
    if kind == "set_gate_date" and not _ISO_DATE.fullmatch(values["date"]):
        raise ValueError(f"date must be YYYY-MM-DD, got {values['date']!r}.")
    if kind == "set_owner" and values["role"] == "":
        values["role"] = None  # the entity gets no owner role
    if kind == "edit_message":
        # One file name per line; a blank line is no file.
        values["files"] = tuple(n.strip() for n in values["files"].splitlines() if n.strip())
    if kind == "post_message" and values["thread_ts"] == "":
        values["thread_ts"] = None  # a new top-level message
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        return _EDIT.validate_python(values)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in e['loc'][1:])}: {e['msg']}" for e in error.errors()
        )
    raise ValueError(f"The {kind} form is not valid: {problems}.")


def _page(
    state: RoomState,
    refusal: str | None,
    *,
    notice: str | None = None,
    curated: Sequence[dict[str, Any]] = (),
    seed: bool = False,
    status: int = 400,
) -> HTMLResponse:
    """The room page for the state; a refusal answers `status` and shows its reason.

    `notice` is the result of a save or a reseed. `seed` shows the Seed panel.
    """
    world = state.world
    names = {persona.user_id: persona.name for persona in world.personas}
    tasks = {(gate.id, task.id): task for gate in world.project.gates for task in gate.checklist}
    # A persona user id in a diff title becomes the persona name (longest id first).
    # The id must be a whole token: no letter, digit, underscore or hyphen on either side.
    alternatives = "|".join(re.escape(u) for u in sorted(names, key=len, reverse=True))
    ids = re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])" if names else "(?!)")
    readers = []
    for selection, diff, reworded, effect in zip(
        state.selections, state.diffs, state.reworded, state.last_effect, strict=True
    ):
        parts = {row.item.item_id: line_parts(row, names, tasks) for row in selection.lines}
        headings = {item_id: heading for item_id, (heading, _) in parts.items()}
        sections = [
            (
                title,
                [(row, *parts[row.item.item_id]) for row in selection.lines if row.section == key],
            )
            for key, title in SECTION_TITLES.items()
        ]
        readers.append(
            {
                "selection": selection,
                "window": window_line(selection),
                "context": context_lines(world, selection),
                "sections": [(title, rows) for title, rows in sections if rows],
                "diff": diff,
                "added": [(view, _title(view, headings, ids, names)) for view in diff.added],
                # A removed line is not in the selection, so its title takes persona names.
                "removed": [(view, _title(view, {}, ids, names)) for view in diff.removed],
                "changed": [
                    (
                        change,
                        _title(
                            change.before,
                            {} if "title" in change.fields else headings,
                            ids,
                            names,
                        ),
                        _title(change.after, headings, ids, names),
                        _changed_fields(change),
                    )
                    for change in diff.changed
                ],
                "reworded": [(item_id, parts.get(item_id, ("", ""))[0]) for item_id in reworded],
                "effect": effect,
            }
        )
    owned: dict[str, list[str]] = {}
    for entity_id, user_ids in state.owners.items():
        for user_id in user_ids:
            owned.setdefault(user_id, []).append(entity_id)
    channel_names = {channel.id: channel.name for channel in world.channels}
    html = _TEMPLATES.get_template("room.html").render(
        world=world,
        day=state.selections[0].day.isoformat() if state.selections else "",
        section_titles=SECTION_TITLES,
        names=names,
        channel_names=channel_names,
        readers=readers,
        owned=owned,
        owners=state.owners,
        members={
            channel.id: [p.name for p in world.personas if channel.id in p.channels]
            for channel in world.channels
        },
        humans=[p for p in world.personas if p.kind == "human"],
        tasks=[task for gate in world.project.gates for task in gate.checklist],
        roles=_ROLES,
        edits=[_describe(edit, names, channel_names) for edit in state.edits],
        no_change=bool(state.edits) and not any(state.last_effect),
        refusal=refusal,
        notice=notice,
        # state.messages is sorted by channel id, so each channel is one group.
        messages=[
            (channel, list(rows))
            for channel, rows in groupby(state.messages, key=lambda m: m.channel_name)
        ],
        roots=[m for m in state.messages if m.ts == m.thread_ts],
        message_changes=state.message_changes,
        scenarios=_SCENARIOS,
        curated=curated,
        seed=seed,
    )
    return HTMLResponse(html, status_code=200 if refusal is None else status)


def _title(
    view: LineView, headings: dict[str, str], ids: re.Pattern[str], names: dict[str, str]
) -> str:
    """The line heading if the selection has the line, else the title with persona names."""
    if view.item_id in headings:
        return headings[view.item_id]
    return ids.sub(lambda match: names[match.group(0)], view.title)


def _changed_fields(change: Change) -> list[str]:
    """One short note per changed field of a line, for example "rank 3 to 1"."""
    before, after = change.before, change.after
    notes = []
    if "section" in change.fields:
        notes.append(f"{SECTION_TITLES[before.section]} to {SECTION_TITLES[after.section]}")
    if "lane" in change.fields or "rank" in change.fields:
        old = "must see" if before.rank is None else f"rank {before.rank}"
        new = "must see" if after.rank is None else f"rank {after.rank}"
        notes.append(f"{old} to {new}")
    if "title" in change.fields:
        notes.append("title changed")
    if "reason" in change.fields:
        notes.append("reason changed")
    return notes


def _describe(edit: Edit, names: dict[str, str], channels: dict[str, str]) -> str:
    """One line of the edit list."""
    match edit:
        case SetRole():
            return f"{names.get(edit.user_id, edit.user_id)}: role {edit.role}"
        case SetMembership():
            verb = "joins" if edit.member else "leaves"
            channel = channels.get(edit.channel_id, edit.channel_id)
            return f"{names.get(edit.user_id, edit.user_id)} {verb} #{channel}"
        case SetPrivate():
            shown = "private" if edit.private else "public"
            return f"#{channels.get(edit.channel_id, edit.channel_id)}: {shown}"
        case SetOwner():
            return f"{edit.entity_id}: owner role {edit.role or 'none'}"
        case SetGateDate():
            return f"{edit.gate_id}: date {edit.date.isoformat()}"
        case SetTaskOwner():
            return f"{edit.task_id}: owner role {edit.role}"
        case EditMessage():
            channel = channels.get(edit.channel_id, edit.channel_id)
            return f"#{channel} message {edit.ts} edited: {_short(edit.text)}"
        case PostMessage():
            channel = channels.get(edit.channel_id, edit.channel_id)
            where = f"a reply in #{channel}" if edit.thread_ts else f"#{channel}"
            return f"{names.get(edit.user_id, edit.user_id)} posts to {where}: {_short(edit.text)}"
        case RunScenario():
            return f"Scenario {edit.name}"


def _short(text: str, limit: int = 60) -> str:
    """The text on one line, cut to `limit` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else f"{line[: limit - 3]}..."

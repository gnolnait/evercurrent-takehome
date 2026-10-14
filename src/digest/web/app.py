"""The demo room page: routes over a `RoomLike` (docs/plans/m3-ui.md, unit 9)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import FormData

from digest.daily.render import SECTION_TITLES, line_parts, window_line
from digest.models import Role
from digest.room import (
    Edit,
    RoomLike,
    RoomState,
    SetGateDate,
    SetMembership,
    SetOwner,
    SetPrivate,
    SetRole,
    SetTaskOwner,
)

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
}
_BOOLEANS = {"true": True, "false": False}
# Pydantic also reads a Unix time or a datetime as a date; the form allows YYYY-MM-DD only.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def create_app(room: RoomLike) -> FastAPI:
    """Routes: GET /, POST /edit, POST /reset."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    async def page() -> HTMLResponse:
        return _page(room.state(), None)

    @app.post("/edit")
    async def edit(request: Request) -> Response:
        # Each refusal is a ValueError: a bad form field here, an R3 check in the room.
        try:
            room.apply(_edit_from(await request.form()))
        except ValueError as error:
            return _page(room.state(), str(error))
        return RedirectResponse("/", status_code=303)

    @app.post("/reset")
    async def reset() -> RedirectResponse:
        room.reset()
        return RedirectResponse("/", status_code=303)

    return app


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
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        return _EDIT.validate_python(values)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in e['loc'][1:])}: {e['msg']}" for e in error.errors()
        )
    raise ValueError(f"The {kind} form is not valid: {problems}.")


def _page(state: RoomState, refusal: str | None) -> HTMLResponse:
    """The room page for the state; a refusal answers 400 and shows its reason."""
    world = state.world
    names = {persona.user_id: persona.name for persona in world.personas}
    tasks = {(gate.id, task.id): task for gate in world.project.gates for task in gate.checklist}
    readers = []
    for selection, diff, reworded, effect in zip(
        state.selections, state.diffs, state.reworded, state.last_effect, strict=True
    ):
        parts = {row.item.item_id: line_parts(row, names, tasks) for row in selection.lines}
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
                "sections": [(title, rows) for title, rows in sections if rows],
                "diff": diff,
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
    )
    return HTMLResponse(html, status_code=200 if refusal is None else 400)


def _describe(edit: Edit, names: dict[str, str], channels: dict[str, str]) -> str:
    """One line of the edit list."""
    match edit:
        case SetRole():
            return f"{names.get(edit.user_id, edit.user_id)} ({edit.user_id}): role {edit.role}"
        case SetMembership():
            verb = "joins" if edit.member else "leaves"
            channel = channels.get(edit.channel_id, edit.channel_id)
            return f"{names.get(edit.user_id, edit.user_id)} ({edit.user_id}) {verb} #{channel}"
        case SetPrivate():
            shown = "private" if edit.private else "public"
            return f"#{channels.get(edit.channel_id, edit.channel_id)}: {shown}"
        case SetOwner():
            return f"{edit.entity_id}: owner role {edit.role or 'none'}"
        case SetGateDate():
            return f"{edit.gate_id}: date {edit.date.isoformat()}"
        case SetTaskOwner():
            return f"{edit.task_id}: owner role {edit.role}"

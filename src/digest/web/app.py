"""The demo room page: routes over a `RoomLike` (docs/plans/m3-ui.md, unit 9)."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from digest.room import RoomLike


def create_app(room: RoomLike) -> FastAPI:
    """Routes: GET /, POST /edit, POST /reset."""
    raise NotImplementedError


def server_config(app: FastAPI, port: int) -> uvicorn.Config:
    """The server configuration; the host is always 127.0.0.1."""
    raise NotImplementedError

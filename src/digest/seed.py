"""Seed a world's corpus into a Slack workspace and keep a map to the posted messages.

See docs/plans/m5-seed.md. This file holds the interface only; the bodies come in the
code commit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from digest.models import Event
from digest.world.schema import World

ROLE_EMOJI: dict[str, str] = {
    "ME": ":gear:",
    "EE": ":zap:",
    "FW": ":floppy_disk:",
    "supply_chain": ":package:",
    "EM": ":clipboard:",
    "PM": ":calendar:",
}
BOT_EMOJI = ":robot_face:"
EVENT_TYPE = "downstream_world_event"
MAX_RETRIES = 5
REQUIRED_SCOPES = frozenset(
    {"chat:write", "chat:write.customize", "channels:read", "channels:join", "groups:read"}
)
FILE_SCOPE = "files:write"  # required only when the selection holds a file


class RateLimited(Exception):
    """A Web API call answered HTTP 429; `retry_after` is the Retry-After value in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after} s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class AuthInfo:
    team_id: str
    url: str  # the workspace URL, for example "https://scratch.slack.com/"
    scopes: frozenset[str]


@dataclass(frozen=True)
class SlackChannel:
    id: str
    name: str
    is_private: bool
    is_member: bool


class SlackSeedClient(Protocol):
    """The Slack calls the seeder makes. Every method can raise `RateLimited`."""

    def auth_test(self) -> AuthInfo: ...

    def list_channels(self) -> list[SlackChannel]:
        """Every public and private channel the bot can see, over all pages."""
        ...

    def join(self, channel_id: str) -> None: ...

    def post(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None,
        username: str,
        icon_emoji: str,
        metadata: dict[str, object],
    ) -> str:
        """Post a message; return its Slack `ts`."""
        ...

    def upload(self, channel_id: str, thread_ts: str, filename: str, content: bytes) -> str:
        """Upload a file into a thread; return the file's permalink."""
        ...


class MappedMessage(BaseModel):
    channel: str
    ts: str
    thread_ts: str


class MappedFile(BaseModel):
    permalink: str


class SeedMap(BaseModel):
    """The seed map file (plan, "Seed map"). `seed` is its only reader and writer."""

    version: int = 1
    team_id: str
    workspace_url: str
    world: str
    complete: bool = False
    channels: dict[str, str] = Field(default_factory=dict)  # world channel -> Slack id
    messages: dict[str, MappedMessage] = Field(default_factory=dict)  # "<ch>:<ts>"
    files: dict[str, MappedFile] = Field(default_factory=dict)  # "<ch>:<ts>/<name>"

    @classmethod
    def load(cls, path: Path) -> SeedMap | None:
        """The map at `path`, or None when the file does not exist."""
        raise NotImplementedError

    def save(self, path: Path) -> None:
        """Write the map atomically: a temporary file in the same folder, then replace."""
        raise NotImplementedError


@dataclass(frozen=True)
class Post:
    """One planned message (plan, "Interface")."""

    key: str  # "<world channel>:<world ts>"
    world_channel: str
    root_key: str  # the key of its thread root; equal to `key` for a root
    text: str  # after the mention rewrite (B4)
    username: str
    icon_emoji: str
    metadata: dict[str, object]
    files: tuple[str, ...]
    reactions: int


@dataclass(frozen=True)
class SeedReport:
    """Counts for the summary line (B12): writes acknowledged in this run."""

    posts: int
    files: int
    channels: int
    reactions_not_seeded: int
    complete: bool


def plan_seed(world: World, events: Sequence[Event], until_day: int | None) -> list[Post]:
    """The posts to make, in world order (B2 to B5, B7, B8).

    Pure. Raise ValueError for an edited or deleted event in the selection (B7), and for
    an `until_day` outside 1 to `world.days`.
    """
    raise NotImplementedError


def seed(
    world: World,
    posts: Sequence[Post],
    client: SlackSeedClient,
    map_path: Path,
    *,
    pace: float,
    sleep: Callable[[float], None],
    dry_run: bool,
) -> SeedReport:
    """Make the posts and uploads that the map at `map_path` does not hold yet.

    Owns every map read and write (B9, B10). See the plan for the checks before the
    first write (B1, B2, B14), retries (B11) and pace (B13).
    """
    raise NotImplementedError


def placeholder_file(name: str) -> bytes:
    """Placeholder bytes for a world file: a one-page PDF for a `.pdf` name, else UTF-8 text."""
    raise NotImplementedError


class SlackWebSeedClient:
    """`SlackSeedClient` over a `slack_sdk.WebClient` built with no retry handler (B11)."""

    def __init__(self, web_client: object) -> None:
        self._web = web_client

    def auth_test(self) -> AuthInfo:
        raise NotImplementedError

    def list_channels(self) -> list[SlackChannel]:
        raise NotImplementedError

    def join(self, channel_id: str) -> None:
        raise NotImplementedError

    def post(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None,
        username: str,
        icon_emoji: str,
        metadata: dict[str, object],
    ) -> str:
        raise NotImplementedError

    def upload(self, channel_id: str, thread_ts: str, filename: str, content: bytes) -> str:
        raise NotImplementedError

"""Seed a world's corpus into a Slack workspace and keep a map to the posted messages.

See docs/plans/m5-seed.md.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from functools import partial
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from digest.models import Event, ts_datetime
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
PLACEHOLDER_LINE = "Kestrel K2 test data"
RESUME_LIMIT = (
    "the map is from a run that did not finish; an operation that Slack accepted just "
    "before that run stopped is done again"
)
_MENTION = re.compile(r"<@([^>]+)>")


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

    def update(self, channel_id: str, ts: str, text: str, *, metadata: dict[str, object]) -> None:
        """Update a posted message's text in place (chat.update); see docs/plans/m5-reseed.md."""
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
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write the map atomically: a temporary file in the same folder, then replace."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(self.model_dump_json(indent=2) + "\n")
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


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


class SeedBlocked(ValueError):
    """A check before the first write failed (B15). The message names the problem."""


@dataclass(frozen=True)
class SeedReport:
    """Counts for the summary line (B12): writes acknowledged in this run.

    For a dry run, the counts are 0 and `complete` is True when nothing blocks a real run.
    """

    posts: int
    files: int
    channels: int
    reactions_not_seeded: int
    complete: bool
    map_path: Path  # the map file the run used
    updated: int = 0  # messages updated in place in this run (docs/plans/m5-reseed.md R8)
    removed: tuple[str, ...] = ()  # world keys in the reference and the map, not in posts


def plan_seed(world: World, events: Sequence[Event], until_day: int | None) -> list[Post]:
    """The posts to make, in world order (B2 to B5, B7, B8).

    Pure. Raise ValueError for an edited or deleted event in the selection (B7), for a
    poster that is not a persona, and for an `until_day` outside 1 to `world.days`.
    """
    selected = list(enumerate(events))
    if until_day is not None:
        if not 1 <= until_day <= world.days:
            raise ValueError(f"--until-day must be from 1 to {world.days}, got {until_day}")
        end = datetime.combine(world.date_of(until_day), time.max, ZoneInfo(world.timezone))
        selected = [(i, event) for i, event in selected if ts_datetime(event.ts) <= end]
    selected.sort(key=lambda pair: (pair[1].ts, pair[0]))
    personas = {persona.user_id: persona for persona in world.personas}
    names = {user_id: persona.name for user_id, persona in personas.items()}
    posts: list[Post] = []
    for _, event in selected:
        key = f"{event.channel_id}:{event.ts}"
        if event.edited or event.deleted:
            raise ValueError(f"event {key} is edited or deleted; the seeder cannot seed it yet")
        persona = personas.get(event.user_id)
        if persona is None:
            raise ValueError(f"event {key}: poster {event.user_id} is not a persona")
        posts.append(
            Post(
                key=key,
                world_channel=event.channel_id,
                root_key=f"{event.channel_id}:{event.thread_ts}",
                text=_MENTION.sub(lambda match: f"@{names.get(match[1], 'unknown')}", event.text),
                username=persona.name,
                icon_emoji=BOT_EMOJI if persona.role is None else ROLE_EMOJI[persona.role],
                metadata={
                    "event_type": EVENT_TYPE,
                    "event_payload": {
                        "world": world.name,
                        "channel": event.channel_id,
                        "ts": event.ts,
                        "thread_ts": event.thread_ts,
                    },
                },
                files=event.files,
                reactions=len(event.reactions),
            )
        )
    return posts


def seed(
    world: World,
    posts: Sequence[Post],
    client: SlackSeedClient,
    map_path: Path | None,
    *,
    pace: float,
    sleep: Callable[[float], None],
    dry_run: bool,
    reference: Sequence[Post] | None = None,
) -> SeedReport:
    """Make the posts and uploads that the map at `map_path` does not hold yet.

    With `reference`, the posts the map was seeded from, also update in place each mapped
    message whose text differs from its reference post, and report in `removed` the mapped
    reference keys that `posts` no longer holds. Nothing is deleted (docs/plans/m5-reseed.md).

    Owns every map read and write (B9, B10). A `map_path` of None means
    `.slack-seed/<team_id>-<world>.json` in the working directory. See the plan for the
    checks before the first write (B1, B2, B10, B14), retries (B11) and pace (B13). A
    check that fails raises `SeedBlocked`; a dry run prints it as a blocker line instead.
    A client error stops the run as it is. A run that stops before its first update, post
    or upload writes no new map file; after a write, the map keeps every acknowledged post
    and upload (B9, B15). An update changes no map entry, so a stopped run repeats it.
    """
    auth = _retry(client.auth_test, sleep)
    path = map_path or Path(".slack-seed") / f"{auth.team_id}-{world.name}.json"
    seed_map = _open_map(path, auth, world.name)
    scopes = REQUIRED_SCOPES | ({FILE_SCOPE} if any(post.files for post in posts) else set())
    scope_blockers = [f"missing scope: {scope}" for scope in sorted(scopes - auth.scopes)]
    if scope_blockers and not dry_run:
        raise SeedBlocked("; ".join(scope_blockers))
    listed = _retry(client.list_channels, sleep)
    slack_ids, to_join, blockers = _resolve_channels(world, posts, listed)
    blockers += _changed_channels(world, slack_ids, seed_map)
    blockers += _missing_roots(posts, seed_map)
    compared = {item.key: item for item in reference or ()}
    blockers += _moved_messages(posts, compared)
    blockers += scope_blockers
    reactions = sum(post.reactions for post in posts)
    selected = {post.key for post in posts}
    removed = tuple(key for key in compared if key in seed_map.messages and key not in selected)
    edits = {
        post.key
        for post in posts
        if post.key in seed_map.messages
        and post.key in compared
        and compared[post.key].text != post.text
    }
    if dry_run:
        changes = []
        if reference is not None:
            changes = [f"updates: {len(edits)}", *(f"removed: {key}" for key in removed)]
        for line in [*_pending_lines(world, posts, seed_map), *changes, *blockers]:
            print(line)
        return SeedReport(0, 0, 0, reactions, complete=not blockers, map_path=path, removed=removed)
    if blockers:
        raise SeedBlocked("; ".join(blockers))
    if path.exists() and not seed_map.complete:
        print(RESUME_LIMIT)
    written = False  # pace only after the first acknowledged write of the run (B13)
    posted = uploaded = updated = 0
    used: set[str] = set()
    for post in posts:
        channel = slack_ids[post.world_channel]
        pending = [name for name in post.files if f"{post.key}/{name}" not in seed_map.files]
        if post.key in seed_map.messages and not pending and post.key not in edits:
            continue
        if channel in to_join:
            _retry(partial(client.join, channel), sleep)
            to_join.discard(channel)
        if not written:  # save the map just before the first write of the run (B15, R7)
            seed_map.complete = False
            seed_map.save(path)
        if post.key in edits:  # no map entry changes, so no checkpoint (R7)
            mapped = seed_map.messages[post.key]
            change = partial(
                client.update, mapped.channel, mapped.ts, post.text, metadata=post.metadata
            )
            _retry(change, sleep, pace if written else 0)
            written = True
            updated += 1
            used.add(mapped.channel)
            print(f"updated {post.key}", flush=True)
            if not pending:
                continue
        seed_map.channels[post.world_channel] = channel
        if post.key not in seed_map.messages:
            root = None if post.root_key == post.key else seed_map.messages[post.root_key].ts
            send = partial(
                client.post,
                channel,
                post.text,
                thread_ts=root,
                username=post.username,
                icon_emoji=post.icon_emoji,
                metadata=post.metadata,
            )
            ts = _retry(send, sleep, pace if written else 0)
            written = True
            seed_map.messages[post.key] = MappedMessage(
                channel=channel, ts=ts, thread_ts=root or ts
            )
            seed_map.save(path)
            posted += 1
            used.add(channel)
            print(f"posted {post.key}", flush=True)
        thread_ts = seed_map.messages[post.key].thread_ts
        for name in pending:
            send_file = partial(client.upload, channel, thread_ts, name, placeholder_file(name))
            permalink = _retry(send_file, sleep, pace if written else 0)
            written = True
            seed_map.files[f"{post.key}/{name}"] = MappedFile(permalink=permalink)
            seed_map.save(path)
            uploaded += 1
            used.add(channel)
            print(f"uploaded {post.key}/{name}", flush=True)
    seed_map.complete = True
    seed_map.save(path)
    return SeedReport(
        posted,
        uploaded,
        len(used),
        reactions,
        complete=True,
        map_path=path,
        updated=updated,
        removed=removed,
    )


def _retry[T](operation: Callable[[], T], sleep: Callable[[float], None], pace: float = 0) -> T:
    """Call `operation`; after `RateLimited`, wait and call it again, up to MAX_RETRIES times.

    A `pace` above 0 is a wait before each attempt (B13). Any other error goes up at once.
    """
    retries = 0
    while True:
        if pace > 0:
            sleep(pace)
        try:
            return operation()
        except RateLimited as error:
            if retries == MAX_RETRIES:
                raise
            retries += 1
            sleep(error.retry_after)


def _open_map(path: Path, auth: AuthInfo, world: str) -> SeedMap:
    """The saved map, or a new one. Raise `SeedBlocked` for a map of another workspace or world."""
    saved = SeedMap.load(path)
    if saved is None:
        return SeedMap(team_id=auth.team_id, workspace_url=auth.url.rstrip("/"), world=world)
    if saved.team_id != auth.team_id or saved.world != world:
        raise SeedBlocked(
            f"map {path} is for team_id {saved.team_id} and world {saved.world}, "
            f"not team_id {auth.team_id} and world {world}"
        )
    return saved


def _resolve_channels(
    world: World, posts: Sequence[Post], listed: Sequence[SlackChannel]
) -> tuple[dict[str, str], set[str], list[str]]:
    """Match each selected world channel to a Slack channel by name and visibility (B1).

    Return the Slack id of each world channel, the Slack ids the bot must join, and one
    blocker line per channel that does not match.
    """
    by_name = {channel.name: channel for channel in listed}
    selected = {post.world_channel for post in posts}
    known = {channel.id for channel in world.channels}
    blockers = [f"missing channel: {channel}" for channel in sorted(selected - known)]
    slack_ids: dict[str, str] = {}
    to_join: set[str] = set()
    for channel in world.channels:
        if channel.id not in selected:
            continue
        found = by_name.get(channel.name)
        if found is not None and found.is_private != channel.private:
            blockers.append(f"visibility mismatch: {channel.name}")
        elif found is None or (found.is_private and not found.is_member):
            note = " (private: missing or not shared with the bot)" if channel.private else ""
            blockers.append(f"missing channel: {channel.name}{note}")
        else:
            slack_ids[channel.id] = found.id
            if not found.is_member:
                to_join.add(found.id)
    return slack_ids, to_join, blockers


def _changed_channels(world: World, slack_ids: Mapping[str, str], seed_map: SeedMap) -> list[str]:
    """One blocker per world channel whose Slack id differs from the one in the map (B10).

    The map holds the id in `channels` and in the `channel` of each saved message.
    """
    saved: dict[str, set[str]] = {}
    for world_channel, slack_id in seed_map.channels.items():
        saved.setdefault(world_channel, set()).add(slack_id)
    for key, message in seed_map.messages.items():
        saved.setdefault(key.split(":", 1)[0], set()).add(message.channel)
    return [
        f"channel id changed: {channel.name}"
        for channel in world.channels
        if channel.id in slack_ids and saved.get(channel.id, set()) - {slack_ids[channel.id]}
    ]


def _missing_roots(posts: Sequence[Post], seed_map: SeedMap) -> list[str]:
    """One blocker per reply whose root is neither in the map nor earlier in `posts` (B2)."""
    known = set(seed_map.messages)
    blockers: list[str] = []
    for post in posts:
        if post.root_key != post.key and post.root_key not in known:
            blockers.append(f"missing thread root {post.root_key} for reply {post.key}")
        known.add(post.key)
    return blockers


def _moved_messages(posts: Sequence[Post], compared: Mapping[str, Post]) -> list[str]:
    """One blocker per post whose channel or thread root differs from its reference post (R2a)."""
    return [
        f"moved message {post.key}: its channel or thread root differs from the reference"
        for post in posts
        if post.key in compared
        and (post.world_channel, post.root_key)
        != (compared[post.key].world_channel, compared[post.key].root_key)
    ]


def _pending_lines(world: World, posts: Sequence[Post], seed_map: SeedMap) -> list[str]:
    """One dry-run line per world channel with posts or uploads to make, in world order."""
    lines: list[str] = []
    for channel in world.channels:
        mine = [post for post in posts if post.world_channel == channel.id]
        count = sum(post.key not in seed_map.messages for post in mine)
        files = sum(
            f"{post.key}/{name}" not in seed_map.files for post in mine for name in post.files
        )
        if count or files:
            lines.append(f"{channel.name}: {count} posts, {files} files")
    return lines


def placeholder_file(name: str) -> bytes:
    """Placeholder bytes for a world file: a one-page PDF for a `.pdf` name, else UTF-8 text."""
    if not name.lower().endswith(".pdf"):
        return f"{PLACEHOLDER_LINE}\n".encode()
    content = (
        f"BT /F1 14 Tf 72 720 Td ({_pdf_text(name)}) Tj "
        f"0 -24 Td ({PLACEHOLDER_LINE}) Tj ET\n".encode("ascii")
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(pdf)


def _pdf_text(text: str) -> str:
    """`text` as the body of a PDF literal string: ASCII, with `\\`, `(` and `)` escaped."""
    ascii_text = text.encode("ascii", "replace").decode("ascii")
    return ascii_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class SlackWebSeedClient:
    """`SlackSeedClient` over a `slack_sdk.WebClient` built with no retry handler (B11)."""

    def __init__(self, web_client: object) -> None:
        self._web: Any = web_client

    def auth_test(self) -> AuthInfo:
        response = self._call("auth_test")
        scopes = _header(response.headers, "x-oauth-scopes") or ""
        return AuthInfo(
            team_id=response["team_id"],
            url=response["url"],
            scopes=frozenset(scope.strip() for scope in scopes.split(",") if scope.strip()),
        )

    def list_channels(self) -> list[SlackChannel]:
        channels: list[SlackChannel] = []
        cursor = ""
        while True:
            page = {"cursor": cursor} if cursor else {}
            response = self._call(
                "conversations_list",
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                **page,
            )
            channels += [
                SlackChannel(
                    id=channel["id"],
                    name=channel["name"],
                    is_private=bool(channel.get("is_private")),
                    is_member=bool(channel.get("is_member")),
                )
                for channel in response["channels"]
            ]
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return channels

    def join(self, channel_id: str) -> None:
        self._call("conversations_join", channel=channel_id)

    def post(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None,
        username: str,
        icon_emoji: str,
        metadata: Mapping[str, object],  # Mapping: a dict with narrower values type-checks
    ) -> str:
        response = self._call(
            "chat_postMessage",
            channel=channel_id,
            text=text,
            thread_ts=thread_ts,
            username=username,
            icon_emoji=icon_emoji,
            metadata=metadata,
        )
        return response["ts"]

    def update(
        self, channel_id: str, ts: str, text: str, *, metadata: Mapping[str, object]
    ) -> None:
        self._call("chat_update", channel=channel_id, ts=ts, text=text, metadata=metadata)

    def upload(self, channel_id: str, thread_ts: str, filename: str, content: bytes) -> str:
        response = self._call(
            "files_upload_v2",
            channel=channel_id,
            thread_ts=thread_ts,
            filename=filename,
            content=content,
        )
        return response["file"]["permalink"]

    def _call(self, method: str, **arguments: object) -> Any:
        """Call a `WebClient` method; turn an HTTP 429 with Retry-After into `RateLimited`."""
        # slack-sdk is in the optional `slack` extra; a top-level import would stop every
        # command when the extra is not installed.
        from slack_sdk.errors import SlackApiError

        try:
            return getattr(self._web, method)(**arguments)
        except SlackApiError as error:
            response = error.response
            retry_after = _header(getattr(response, "headers", None), "retry-after")
            if getattr(response, "status_code", None) != 429 or retry_after is None:
                raise
        raise RateLimited(float(retry_after))


def _header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    """The value of the HTTP header `name`, matched without regard to case."""
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return str(value)
    return None

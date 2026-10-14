"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 9)."""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator, Set
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import UTC, date, datetime, time, timedelta
from itertools import combinations
from pathlib import Path
from statistics import median
from time import sleep
from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

import typer
import yaml
from pydantic import ValidationError

from digest import __version__
from digest.absence import detect_absences
from digest.corpus.avoid import load_avoid_names
from digest.corpus.render import render_world
from digest.daily.render import build_digest, render_digest, select_digest
from digest.daily.select import Selection
from digest.daily.slack import slack_message
from digest.driver import (
    LineView,
    ReaderDiff,
    ScriptLine,
    StoreView,
    diff_selections,
    make_event,
)
from digest.eval.extraction import run_extraction_eval, write_report
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.extract.stage import ExtractReport, refuse_other_prompt_version, run_extract
from digest.extract.verify import DROP_FIELDS
from digest.llm.cassette import CassetteMiss, Cassettes
from digest.llm.client import LLM, from_settings
from digest.models import Event
from digest.priors import DEFAULT_PRIORS_PATH, Priors, load_priors
from digest.replay import read_events, replay_world
from digest.seed import (
    SeedBlocked,
    SeedMap,
    SlackSeedClient,
    SlackWebSeedClient,
    message_permalink,
    plan_seed,
)
from digest.seed import seed as seed_corpus
from digest.settings import Settings
from digest.store import Store
from digest.world.golden import derive_golden, write_golden
from digest.world.load import load_world
from digest.world.manifest import load_manifest
from digest.world.schema import World

if TYPE_CHECKING:
    from slack_sdk import WebClient
    from slack_sdk.webhook import WebhookClient

_DEMO_WORLD = "kestrel"
_DEMO_READERS = ("U0ME01", "U0SC01", "U0EM01")
_DEMO_ROLES = ("ME", "supply_chain", "EM")  # the roles of _DEMO_READERS, in the same order
_RULE = "-" * 40
_WEBHOOK_PREFIX = "https://hooks.slack.com/"
_BOT_TOKEN_PREFIX = "xoxb-"
_SLACK_ERROR_CODE = re.compile(r"[a-z_]+")  # a body such as invalid_blocks
_SLACK_USER_ID = re.compile(r"[UW][A-Z0-9]+")
_AUTH_ERRORS = frozenset({"invalid_auth", "not_authed"})
_AT_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?")

app = typer.Typer(help="Role- and phase-aware daily Slack digest.", no_args_is_help=True)


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def demo(
    day: int = typer.Option(6, "--day", help="World day of the digests (1 is the first day)."),
) -> None:
    """Produce digests for three roles from the bundled synthetic corpus (offline).

    The command replays the bundled world into a temporary store one day at a time and runs
    the extract stage at the end of each day from the recorded cassettes. It prints the three
    digests, then the pairwise item overlap of the three. A cassette directory that does not
    exist or holds no cassette file stops the command before any replay. A failed thread in
    any extract run stops the command before it builds a digest. The command never constructs the settings
    object, so it reads no API key and no `.env` file. It writes nothing outside the
    temporary directory.
    """
    loaded, selections = _demo_selections(day)
    digests = [render_digest(loaded, selection) for selection in selections]
    shown = [{line.item.item_id for line in selection.lines} for selection in selections]
    typer.echo(f"\n\n{_RULE}\n\n".join(digests))
    typer.echo(f"\n{_overlap_line(*shown)}")


@app.command()
def post(
    day: int = typer.Option(6, "--day", help="World day of the digests (1 is the first day)."),
    dm: str | None = typer.Option(
        None,
        "--dm",
        metavar="USER_ID",
        help=(
            "Send each digest as a direct message from the bot in SLACK_BOT_TOKEN to this "
            "Slack user id, not to the webhook."
        ),
    ),
    seed_map: Path | None = typer.Option(
        None,
        "--seed-map",
        help="Seed map from `digest seed`: link sources to the seeded Slack messages.",
    ),
) -> None:
    """Post the three demo digests to the Slack incoming webhook in SLACK_WEBHOOK_URL.

    The command reads SLACK_WEBHOOK_URL from the environment, then from `.env`. The URL
    must start with https://hooks.slack.com/ and hold no whitespace, else the command
    exits 1 before it builds a digest. It builds the three digests as `digest demo` does
    and posts nothing until all three are built. It posts one message per reader, in
    reader order. Before the first send, it prints one line with the number of digests
    and the destination. It stops at the first message that Slack refuses or that fails
    to send. A send failure shows the reader and the exception type only. A refusal shows
    the reader, the status and the response body. It shows the body only when the body is
    a Slack error code (lower-case letters and underscores) that is not part of the URL,
    else it says the body is withheld. A refusal after a partial post also says how many
    digests were already sent and that a rerun sends all of them again. A missing URL
    names the `.env` file in the working directory. The command never constructs the
    settings object, so it reads no API key. No output contains the webhook URL.

    With `--dm USER_ID`, the command does not use the webhook. It sends each digest as a
    direct message from the app's bot to the Slack user `USER_ID` (see `_post_dm`).

    With `--seed-map PATH`, the command loads the map that `digest seed` wrote before it
    checks the destination. A missing or unreadable map, or a map for another world than
    the demo world, exits 1. The source links of mapped messages then point at the seeded
    Slack messages, with their file links and channels. Before the first send, the command
    prints how many source links are not in the map, when any are not. Without a map, the
    command makes no `auth.test` call (docs/plans/m5-links.md).
    """
    links = None if seed_map is None else _load_seed_map(seed_map)
    if dm is not None:
        _post_dm(day, dm, links)
        return
    url = _webhook_url()
    if url is None:
        _refuse(_not_set("SLACK_WEBHOOK_URL"))
    if not url.startswith(_WEBHOOK_PREFIX) or any(char.isspace() for char in url):
        _refuse(f"SLACK_WEBHOOK_URL must start with {_WEBHOOK_PREFIX} and hold no whitespace")
    loaded, selections = _demo_selections(day)
    messages = [slack_message(loaded, selection, links) for selection in selections]
    client = _webhook_client(url)
    _echo_unmapped(selections, links)
    typer.echo(f"sending {len(messages)} digests to the webhook")
    for sent, (selection, message) in enumerate(zip(selections, messages, strict=True)):
        reader = selection.profile.name
        rerun = _rerun_note(sent, len(messages))
        # A refusal raised in an except block would chain the caught exception, so each
        # handler stores the reason and the refusal comes after the try statement.
        failure: str | None = None
        try:
            response = client.send(text=message["text"], blocks=message["blocks"])
        # The message of an exception can hold the URL, so the refusal shows the type only.
        except Exception as error:
            failure = f"cannot post the digest for {reader}: {type(error).__name__}{rerun}"
        if failure is not None:
            _refuse(failure)
        if response.status_code != 200:
            # Show only a Slack error code that is not part of the URL. Another body, such
            # as a proxy page, can echo the URL.
            body = response.body
            shown = (
                body
                if _SLACK_ERROR_CODE.fullmatch(body) and body not in url
                else "body withheld (not a Slack error code)"
            )
            _refuse(
                f"Slack refused the digest for {reader}: "
                f"status {response.status_code}, {shown}{rerun}"
            )
        typer.echo(f"posted the digest for {reader}")


def _post_dm(day: int, user_id: str, links: SeedMap | None) -> None:
    """Send the three demo digests as direct messages from the bot to the Slack user `user_id`.

    The function reads SLACK_BOT_TOKEN from the environment, then from `.env`. The user id
    must match `[UW][A-Z0-9]+` and the token must start with `xoxb-`, else the command exits
    1 before it builds a digest. It builds the three digests as `digest demo` does and sends
    nothing until all three are built. It opens the DM once with `conversations.open`. It
    posts one message per reader with `chat.postMessage`, in reader order. It stops at the
    first failure. Before it opens the DM, it prints one line with the number of digests
    and the destination. A Slack API error shows the reader and the text of
    `_slack_failure`. An `invalid_auth` or `not_authed` error also says whether the token
    came from the environment or from `.env` (see `_env_source`). A refusal after a
    partial post also says how many digests were already sent and that a rerun sends all
    of them again. A missing token names the `.env` file in the working directory. No
    output contains the token.

    With a seed map in `links`, the function calls `auth.test` before it opens the DM. It
    exits 1 when that call fails (with the text of `_dm_failure`) or when the team_id of
    the token is not the team_id of the map. Before the destination line, it can print the
    count line of `_echo_unmapped`.
    """
    if not _SLACK_USER_ID.fullmatch(user_id):
        _refuse("--dm must be a Slack user id: U or W, then upper-case letters and digits")
    token = _env_value("SLACK_BOT_TOKEN")
    if token is None:
        _refuse(_not_set("SLACK_BOT_TOKEN"))
    source = _env_source("SLACK_BOT_TOKEN")
    if not token.startswith(_BOT_TOKEN_PREFIX):
        _refuse(f"SLACK_BOT_TOKEN must start with {_BOT_TOKEN_PREFIX}")
    loaded, selections = _demo_selections(day)
    messages = [slack_message(loaded, selection, links) for selection in selections]
    client = _slack_web_client(token)
    # The message of an exception can hold the token, so a refusal shows _slack_failure only.
    # Each refusal comes after its try statement, so no exception chains to it.
    failure: str | None = None
    if links is not None:
        try:
            team_id = client.auth_test()["team_id"]
        except Exception as error:
            failure = f"cannot read the workspace of the bot token: {_dm_failure(error, source)}"
        if failure is not None:
            _refuse(failure)
        if team_id != links.team_id:
            _refuse("the seed map is for another Slack workspace (team) than the bot token")
    _echo_unmapped(selections, links)
    typer.echo(f"sending {len(messages)} digests by DM to {user_id}")
    try:
        channel = client.conversations_open(users=user_id)["channel"]["id"]
    except Exception as error:
        failure = f"cannot open the DM with {user_id}: {_dm_failure(error, source)}"
    if failure is not None:
        _refuse(failure)
    for sent, (selection, message) in enumerate(zip(selections, messages, strict=True)):
        reader = selection.profile.name
        rerun = _rerun_note(sent, len(messages))
        try:
            client.chat_postMessage(channel=channel, text=message["text"], blocks=message["blocks"])
        except Exception as error:
            failure = f"cannot post the digest for {reader}: {_dm_failure(error, source)}{rerun}"
        if failure is not None:
            _refuse(failure)
        typer.echo(f"posted the digest for {reader}")


def _load_seed_map(path: Path) -> SeedMap:
    """The seed map at `path`. A missing or unreadable map, or one for another world, exits 1.

    A refusal prints no value read from the file, because a wrong path such as `.env` can
    hold a token. A validation error shows the top-level field and the message of each
    problem only, each line once.
    """
    # Each refusal comes after its try statement, so no exception chains to it.
    failure: str | None = None
    try:
        links = SeedMap.load(path)
    except OSError as error:
        failure = f"{path}: {error.strerror or error}"
    except UnicodeDecodeError as error:
        failure = f"{path}: not a seed map: {error}"
    except ValidationError as error:
        # A deeper `loc` part can be a dictionary key from the file, so show the top field only.
        problems = "; ".join(
            dict.fromkeys(
                f"{problem['loc'][0] if problem['loc'] else 'map'}: {problem['msg']}"
                for problem in error.errors(include_input=False, include_url=False)
            )
        )
        failure = f"{path}: not a seed map: {problems}"
    if failure is not None:
        _refuse(failure)
    if links is None:
        _refuse(f"{path}: the seed map does not exist")
    if links.world != _DEMO_WORLD:
        _refuse(f"{path}: the seed map is for another world, not {_DEMO_WORLD}")
    return links


def _echo_unmapped(selections: list[Selection], links: SeedMap | None) -> None:
    """Print how many source links on the lines of `selections` are not in the map (plan L4).

    The count covers every evidence entry of every line, before any render cut.
    """
    if links is None:
        return
    count = sum(
        message_permalink(links, f"{entry.channel_id}:{entry.message_ts}") is None
        for selection in selections
        for line in selection.lines
        for entry in line.item.evidence
    )
    if count:
        typer.echo(f"{count} source links are not in the seed map")


def _dm_failure(error: Exception, source: str) -> str:
    """The text of `_slack_failure`. An auth error also names the source of the token."""
    failure = _slack_failure(error)
    if failure in _AUTH_ERRORS:
        return f"{failure} (token from {source})"
    return failure


def _slack_failure(error: Exception) -> str:
    """Slack's error code of a Slack API error, else the exception type. It never raises.

    The function reads the code with the `get` method of the response. It shows the code
    only when it is lower-case letters and underscores. A response without a callable
    `get`, a lookup that raises, or a lookup that gives nothing gives the exception type only.
    """
    # slack-sdk is in the optional `slack` extra. _slack_web_client refuses before this
    # point when the extra is not installed.
    from slack_sdk.errors import SlackApiError

    if not isinstance(error, SlackApiError):
        return type(error).__name__
    # The response of a real error is a SlackResponse. It has `get`, but it is not a
    # Mapping. The lookup error can hold the token, so the function shows the type only.
    try:
        lookup = getattr(error.response, "get", None)
        code = lookup("error") if callable(lookup) else None
    except Exception:
        code = None
    if code is None:
        return type(error).__name__
    if isinstance(code, str) and _SLACK_ERROR_CODE.fullmatch(code):
        return code
    return "SlackApiError, error code withheld (not a Slack error code)"


def _rerun_note(sent: int, total: int) -> str:
    """The note for a refusal after `sent` of `total` digests. It is empty when `sent` is 0."""
    if sent == 0:
        return ""
    if sent == 1:
        return f"; 1 digest was already sent, and a rerun sends all {total} again"
    return f"; {sent} digests were already sent, and a rerun sends all {total} again"


def _not_set(key: str) -> str:
    """The refusal for a missing `key`. It names the `.env` file in the working directory."""
    return f"{key} is not set in the environment or in {Path.cwd() / '.env'}"


# --- digest seed (docs/plans/m5-seed.md) -------------------------------------------------


@app.command()
def seed(
    world: str = typer.Option(..., "--world", help="World name under data/world/."),
    map_path: Path | None = typer.Option(
        None,
        "--map",
        help="Seed map file. Default: .slack-seed/<team_id>-<world>.json.",
    ),
    until_day: int | None = typer.Option(
        None, "--until-day", help="Seed only the events up to the end of this world day."
    ),
    pace: float = typer.Option(1.2, "--pace", min=0, help="Seconds between two writes."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Make no write; print what a run would do."
    ),
    events_path: Path | None = typer.Option(
        None, "--events", help="Corpus to seed. Default: the world's events.jsonl."
    ),
    reference_path: Path | None = typer.Option(
        None,
        "--reference",
        help=(
            "The corpus that Slack holds now (the last one seeded or pushed); update the "
            "messages whose text differs in place."
        ),
    ),
) -> None:
    """Post the world's corpus into the Slack workspace of SLACK_BOT_TOKEN, under persona
    names, and record a map from each world message to its Slack message.

    The command reads SLACK_BOT_TOKEN as `digest post --dm` does. It plans the posts
    before it makes a Slack call. A real run prints one line per acknowledged write, then
    the summary line. A dry run prints the count and blocker lines only, and exits 1 when
    a blocker stops a real run. A Slack error shows its error code only, any other error
    its type only, so no output holds the token.

    With --reference, the command updates in place each seeded message whose text
    differs from the reference, and lists the seeded messages the corpus no longer holds.
    It deletes nothing (docs/plans/m5-reseed.md). The reference must be the corpus Slack
    holds now, the last one seeded or pushed: a message changed back to text that matches
    the reference is not pushed. A dry run then also prints an `updates:` count and one
    `removed:` line per removed message; a real run prints one `not deleted:` line per
    removed message after the summary line.
    """
    world_dir = Path("data") / "world" / world
    # Each refusal comes after its try statement, so no exception chains to it.
    failure: str | None = None
    reference = None
    try:
        loaded = load_world(world_dir / "world.yaml")
        source = events_path or world_dir / "events.jsonl"
        events = list(read_events(source, loaded.workspace_url))
        posts = plan_seed(loaded, events, until_day)
        if reference_path is not None:
            seeded = list(read_events(reference_path, loaded.workspace_url))
            reference = plan_seed(loaded, seeded, until_day)
    except OSError as error:
        failure = f"{error.filename}: {error.strerror or error}"
    except (yaml.YAMLError, ValueError) as error:  # ValueError: validation, format, B7, day
        failure = str(error)
    if failure is not None:
        _refuse(failure)
    token = _env_value("SLACK_BOT_TOKEN")
    if token is None:
        _refuse(_not_set("SLACK_BOT_TOKEN"))
    if not token.startswith(_BOT_TOKEN_PREFIX):
        _refuse(f"SLACK_BOT_TOKEN must start with {_BOT_TOKEN_PREFIX}")
    client = _seed_client(token)
    try:
        report = seed_corpus(
            loaded,
            posts,
            client,
            map_path,
            pace=pace,
            sleep=sleep,
            dry_run=dry_run,
            reference=reference,
        )
    except SeedBlocked as error:  # a check before the first write; the message names it
        failure = str(error)
    # The message of any other error can hold the token, so the refusal shows
    # _slack_failure only.
    except Exception as error:
        failure = f"{_slack_failure(error)}; the map keeps what is done; run the command again"
    if failure is not None:
        _refuse(failure)
    if dry_run:
        if not report.complete:
            raise typer.Exit(code=1)
        return
    if reference is None:
        counts = f"{report.posts} posts and {report.files} files"
    else:
        counts = f"{report.posts} posts, {report.updated} updates and {report.files} files"
    typer.echo(
        f"seeded {counts} in {report.channels} channels; "
        f"{report.reactions_not_seeded} reactions not seeded; map {report.map_path}"
    )
    for key in report.removed:
        typer.echo(f"not deleted: {key}")


def _seed_client(token: str) -> SlackSeedClient:
    """The seeder's Slack client: a `SlackWebSeedClient` over a `WebClient` with no retry
    handler, so the seeder owns retries (plan B11). Tests patch this factory."""
    # slack-sdk is in the optional `slack` extra. A top-level import would stop every
    # command when the extra is not installed.
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        from slack_sdk import WebClient
    except ImportError:
        installed = False
    else:
        installed = True
    if not installed:
        _refuse("digest seed needs the slack extra; run uv sync --extra slack")
    return SlackWebSeedClient(WebClient(token=token, retry_handlers=[]))


@app.command()
def run(
    user: str = typer.Option(..., help="User id from the corpus or workspace."),
    as_of: str = typer.Option(..., "--as-of", help="Digest date, YYYY-MM-DD."),
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Store path; default DIGEST_DB, then data/<world>.sqlite."
    ),
) -> None:
    """Produce one user's digest for a date.

    The store must hold the world and be replayed and extracted to exactly the end of that day.
    """
    settings = Settings()
    name = world or settings.world
    try:
        day = date.fromisoformat(as_of)
    except ValueError:
        _refuse(f"--as-of must be YYYY-MM-DD, got {as_of!r}")
    path = db or settings.db or Path("data") / f"{name}.sqlite"
    if not path.is_file():  # Store.open would create an empty store file
        _refuse(f"no store at {path}; run digest replay first")
    try:
        loaded = load_world(Path("data") / "world" / name / "world.yaml")
        priors = load_priors()
    except (OSError, ValueError, yaml.YAMLError) as error:  # ValueError: ValidationError too
        _refuse(str(error))
    with _open_store(path) as store:
        # Replay stores the world directory name, so compare it with `name`, not `world.name`.
        stored_world = store.get_meta("world")
        if stored_world != name:
            _refuse(f"the store holds world {stored_world!r}, not {name!r}")
        try:
            digest = build_digest(store, loaded, priors, user, day)
        except KeyError:
            _refuse(f"unknown user {user}: no persona with that id in world {name}")
        except ValueError as error:  # a refused store, or a bot
            _refuse(str(error))
    typer.echo(digest)


@app.command()
def serve(
    port: int = typer.Option(8000, help="Port on 127.0.0.1."),
    record: bool = typer.Option(
        False,
        "--record",
        help="Allow model calls for a message change with no cassette, and record them.",
    ),
    slack: bool = typer.Option(
        False, "--slack", help="Show the Seed panel; a reseed reads SLACK_BOT_TOKEN."
    ),
) -> None:
    """Serve the demo room on 127.0.0.1 for day 6 of the bundled Kestrel world.

    The command builds the day-6 store as `digest demo` does, in a temporary directory
    that lasts until the server stops. It closes its own connection to the store before
    the server starts; the room opens the store for each rebuild. Without `--record` the
    room is offline and reads no API key. With `--record`, the room constructs the
    settings object only when a message change has no cassette. With `--slack`, the
    command reads SLACK_BOT_TOKEN as `digest seed` does, and the page can reseed the
    workspace from the saved corpus copy.
    """
    # The web extra is optional. A top-level import would break `digest demo` on a plain
    # `uv sync`, as `post` avoids for slack-sdk. digest.room is imported here beside the
    # web app that uses it.
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        import uvicorn

        from digest.room import Room, build_base
        from digest.web.app import create_app, server_config
    except ImportError:
        installed = False
    else:
        installed = True
    if not installed:
        _refuse("digest serve needs the web extra; run uv sync --extra web")
    reseed: _RoomReseed | None = None
    if slack:
        token = _env_value("SLACK_BOT_TOKEN")
        if token is None:
            _refuse(_not_set("SLACK_BOT_TOKEN"))
        if not token.startswith(_BOT_TOKEN_PREFIX):
            _refuse(f"SLACK_BOT_TOKEN must start with {_BOT_TOKEN_PREFIX}")
        reseed = _RoomReseed(_seed_client(token), Path("data") / "world" / _DEMO_WORLD)
    failure: str | None = None
    # build_base reads world day 6; the store must end on that day.
    with _demo_store(6) as (loaded, priors, store):
        try:
            base = build_base(store, loaded, priors)
            store.close()
            room = Room(base, record=record)
        except (ValueError, OSError) as error:  # a refused store
            failure = str(error)
        if failure is not None:
            _refuse(failure)
        typer.echo(f"serving the demo room at http://127.0.0.1:{port}/")
        uvicorn.Server(server_config(create_app(room, reseed=reseed), port)).run()


class _RoomReseed:
    """The room's reseed (plan m3-room-edits S2): the seeder over a saved corpus copy.

    The reference is the corpus that Slack holds now: the last pushed corpus at
    `.room/pushed-<world>.jsonl` when that file exists, else the committed corpus. After a
    real run that ends without an error, the copy's corpus becomes the pushed corpus. A dry
    run or an error never writes that file. It returns the seeder's lines and a summary
    line as text.
    """

    def __init__(self, client: SlackSeedClient, world_dir: Path) -> None:
        self._client = client
        self._world_dir = world_dir

    def __call__(self, copy_dir: Path, *, dry_run: bool) -> str:
        """Run the seeder; ValueError with a plain reason for a refusal."""
        if "reference" not in inspect.signature(seed_corpus).parameters:
            raise ValueError(
                "the seeder cannot update messages yet: digest.seed.seed takes no reference"
            )
        world = load_world(self._world_dir / "world.yaml")
        copy_path = copy_dir / "events.jsonl"
        copy = list(read_events(copy_path, world.workspace_url))
        pushed = Path(".room") / f"pushed-{world.name}.jsonl"
        source = pushed if pushed.is_file() else self._world_dir / "events.jsonl"
        reference = list(read_events(source, world.workspace_url))
        # The seeder adds `reference` in a later change; the dict keeps this call valid until then.
        options: dict[str, Any] = {
            "reference": plan_seed(world, reference, None),
            "pace": 1.2,
            "sleep": sleep,
            "dry_run": dry_run,
        }
        lines = io.StringIO()
        try:
            with redirect_stdout(lines):
                report = seed_corpus(
                    world, plan_seed(world, copy, None), self._client, None, **options
                )
        except ValueError:  # SeedBlocked and plan refusals; the message holds no token
            raise
        # The message of any other error can hold the token, so the refusal shows
        # _slack_failure only.
        except Exception as error:
            raise ValueError(_slack_failure(error)) from None
        if not dry_run:
            _replace_file(pushed, copy_path.read_bytes())
        # The seeder adds `updated` to its report in a later change.
        updated = getattr(report, "updated", None)
        counts = "" if updated is None else f"updated {updated}; "
        summary = (
            f"dry run: {counts}complete {report.complete}"
            if dry_run
            else f"{counts}seeded {report.posts} posts and {report.files} files in "
            f"{report.channels} channels; map {report.map_path}"
        )
        return f"{lines.getvalue()}{summary}"


def _replace_file(path: Path, content: bytes) -> None:
    """Write `content` to `path` atomically: a temporary file in the same directory, then
    a replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        handle.write(content)
    try:
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


@app.command("mcp")
def mcp_cmd(
    day: int = typer.Option(6, "--day", help="World day of the demo store; used without --db."),
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default the store's world."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Store to serve; default the demo store of --day."
    ),
) -> None:
    """Serve the query tools to an MCP client on stdio (docs/plans/m4-mcp.md M5).

    Without --db, the command builds the store of the bundled world for --day as
    `digest serve` does, in a temporary directory that lasts until the server stops. With
    --db, it serves that store, and --world defaults to the store's world. Each tool call
    opens the store, so a replay between two calls shows in the next call. Nothing but the
    protocol goes to stdout. The command reads no API key.
    """
    # The mcp extra is optional. A top-level import would break `digest demo` on a plain
    # `uv sync`, as `serve` avoids for the web extra.
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        from digest.mcp_server import build_server, check_store
    except ImportError:
        installed = False
    else:
        installed = True
    if not installed:
        _refuse("digest mcp needs the mcp extra; run uv sync --extra mcp")
    failure: str | None = None
    with ExitStack() as stack:
        if db is None:
            if world is not None:
                _refuse("--world needs --db; without --db the command serves the bundled world")
            loaded, priors, store = stack.enter_context(_demo_store(day))
            store.close()
            name = _DEMO_WORLD
            path = Path(store.path)
        else:
            path = db
            if world is None:
                if not path.is_file():
                    _refuse(f"no store at {path}; run digest replay first")
                with _open_store_readonly(path) as store:
                    world = _stored(store, "world")
            name = world
            loaded = _named_world(name)
            try:
                priors = load_priors(DEFAULT_PRIORS_PATH)
            except OSError as error:
                failure = f"{DEFAULT_PRIORS_PATH}: {error.strerror or error}"
            except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
                failure = f"{DEFAULT_PRIORS_PATH}: {_with_notes(error)}"
            if failure is not None:
                _refuse(failure)
        try:
            check_store(path, loaded, name, priors)
        except ValueError as error:  # a refused store
            failure = str(error)
        if failure is not None:
            _refuse(failure)
        build_server(path, loaded, priors).run("stdio")


@app.command("eval")
def eval_cmd(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Report path; default reports/eval-<date>.md."
    ),
) -> None:
    """Run the evaluation suite against the golden corpus and write the report.

    The eval runs in a fresh store at data/eval-<world>.sqlite. The command deletes that file
    and its -wal and -shm files first.
    """
    settings = Settings()
    name = world or settings.world
    world_dir = Path("data") / "world" / name
    store_path = Path("data") / f"eval-{name}.sqlite"
    try:
        loaded = load_world(world_dir / "world.yaml")
        priors = load_priors()
        llm = from_settings(settings)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{store_path}{suffix}").unlink(missing_ok=True)
        report = run_extraction_eval(world_dir, llm, priors, store_path)
    # CassetteMiss: replay found no recording. ValueError: a stale golden.jsonl, a manifest
    # with no thread for an event, a golden thread never run, a missing API key or a
    # malformed file. OSError: a missing world, manifest or golden file.
    except (CassetteMiss, ValueError, OSError, yaml.YAMLError) as error:
        _refuse(_with_notes(error))
    path = out or Path("reports") / f"eval-{loaded.date_of(loaded.days)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, path)
    typer.echo(str(path))
    for label, value in [
        ("thread recall", report.thread_recall),
        ("claim recall", report.claim_recall),
        ("claim precision", report.claim_precision),
        ("citation validity", report.citation_validity),
        ("absence false-alarm rate", report.absence_false_alarm_rate),
        ("absence recall", report.absence_recall),
        ("extract failures", report.extract_failed),
    ]:
        typer.echo(f"{label}: {'n/a' if value is None else round(value, 3)}")


@app.command()
def extract(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Store path; default DIGEST_DB, then data/<world>.sqlite."
    ),
) -> None:
    """Triage and extract pending thread deltas, one working day at a time, up to the store's day.

    A run starts at the day after the meta key `extract_until`. After a day with no failed
    thread, the run sets that key to the day. A failed thread stays pending for the next day.
    """
    settings = Settings()
    path = db or settings.db or Path("data") / f"{world or settings.world}.sqlite"
    # Store.open creates a missing file; checking first keeps a typo from creating one.
    if not path.is_file():
        _refuse(f"no store at {path}; run `digest replay` first")
    try:
        store = Store.open(path)
    except ValueError as error:  # another schema version; the message names the store
        _refuse(str(error))
    except (OSError, sqlite3.DatabaseError) as error:
        _refuse(f"cannot open store {path}: {error}")
    with store:
        name, until, timezone = (_stored(store, key) for key in ("world", "until", "timezone"))
        if world is not None and world != name:
            _refuse(f"--world {world} differs from the world {name} of store {path}")
        source = Path("data") / "world" / name / "world.yaml"
        try:
            loaded = load_world(source)
        except OSError as error:
            _refuse(f"{source}: {error.strerror or error}")
        except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
            _refuse(f"{source}: {error}")
        try:
            priors = load_priors(DEFAULT_PRIORS_PATH)
        except OSError as error:
            _refuse(f"{DEFAULT_PRIORS_PATH}: {error.strerror or error}")
        except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
            _refuse(f"{DEFAULT_PRIORS_PATH}: {_with_notes(error)}")
        zone = ZoneInfo(timezone)
        end = datetime.fromisoformat(until)
        try:
            refuse_other_prompt_version(store)
            llm = from_settings(settings)
            total = _extract_days(store, loaded, llm, priors, zone, end)
        # CassetteMiss: replay found no recording. ValueError: a missing API key, a store of
        # another prompt version or a malformed `extract_until`. OSError: no model ids file.
        except (CassetteMiss, ValueError, OSError) as error:
            _refuse(str(error))
    typer.echo(total.model_dump_json())


@app.command()
def absences(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Store path; default DIGEST_DB, then data/<world>.sqlite."
    ),
) -> None:
    """Print the absence items as of the store's day, one JSON object per line."""
    settings = Settings()
    name = world or settings.world
    path = db or settings.db or Path("data") / f"{name}.sqlite"
    if not path.is_file():  # Store.open would create an empty store file
        _refuse(f"no store at {path}; run digest replay first")
    try:
        loaded = load_world(Path("data") / "world" / name / "world.yaml")
        priors = load_priors()
    except (OSError, ValueError, yaml.YAMLError) as error:  # ValueError: ValidationError too
        _refuse(str(error))
    with _open_store(path) as store:
        stored_world, until = store.get_meta("world"), store.get_meta("until")
        if stored_world != name:
            _refuse(f"the store holds world {stored_world!r}, not {name!r}")
        if until is None:
            _refuse(f"store {path} has no meta key 'until'; run digest replay first")
        as_of = _instant(until)
        if as_of is None:
            _refuse(
                f"store {path} meta key 'until' is {until!r}, "
                "not an ISO timestamp with a UTC offset"
            )
        if store.thread_deltas("extract", EXTRACT_PROMPT_VERSION, "extract", as_of):
            _refuse(
                f"store {path} has thread deltas pending as of {until}; run digest extract first"
            )
        found = detect_absences(store, loaded.project, priors, as_of, ZoneInfo(loaded.timezone))
    for item in found:
        typer.echo(item.model_dump_json())


@app.command()
def replay(
    until: str = typer.Option(
        ..., "--until", help="Last day to ingest, YYYY-MM-DD, in the world's timezone."
    ),
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Store path; default DIGEST_DB, then data/<world>.sqlite."
    ),
) -> None:
    """Ingest the world's corpus into the store up to and including a day."""
    settings = Settings()
    name = world or settings.world
    try:
        day = date.fromisoformat(until)
    except ValueError:
        _refuse(f"--until must be YYYY-MM-DD, got {until!r}")
    world_dir = Path("data") / "world" / name
    # replay_world checks this too; checking first keeps a typo from creating a store file.
    if not (world_dir / "world.yaml").is_file():
        _refuse(f"no such world: {world_dir / 'world.yaml'}")
    path = db or settings.db or Path("data") / f"{name}.sqlite"
    with _open_store(path) as store:
        try:
            report = replay_world(store, world_dir, day)
        except ValueError as error:  # ReplayRefused and ReplayFormatError
            _refuse(str(error))
        except sqlite3.DatabaseError as error:
            _refuse(f"store error: {error}")
    typer.echo(report.model_dump_json())


@app.command()
def say(
    as_: str | None = typer.Option(
        None, "--as", help="Persona id of the sender; not a bot. Not with --script."
    ),
    channel: str | None = typer.Option(
        None, "--channel", help="Channel name; the persona must be a member. Not with --script."
    ),
    at: str | None = typer.Option(
        None,
        "--at",
        help="World time, YYYY-MM-DDTHH:MM[:SS] in the world's time zone, on the store's day "
        "and later than every event in the store. Not with --script.",
    ),
    text: str | None = typer.Option(None, "--text", help="Message text. Not with --script."),
    thread: str | None = typer.Option(
        None,
        "--thread",
        help="Ts of a top-level message in the channel to reply to. Not with --script.",
    ),
    script: Path | None = typer.Option(
        None,
        "--script",
        help="Script file: run each JSON line as its own say into a fresh scratch copy.",
    ),
    db: Path = typer.Option(
        ...,
        "--db",
        help="Scratch store path; made as a copy of the base store on first use. "
        "With --script, it must not exist yet.",
    ),
    base_db: Path | None = typer.Option(
        None,
        "--base-db",
        help="Base store path, never written; default DIGEST_DB, then data/<world>.sqlite.",
    ),
) -> None:
    """Add a message as a persona at a world time, into a scratch copy of the store.

    The command opens the base store read-only and never writes it. It refuses a message
    that breaks a rule with exit 1 and a one-line reason, and then writes nothing. It
    refuses to make a new scratch store when `<db>.script.jsonl` already exists, so a log
    never holds two sessions. It writes the event, sets the
    meta key `extract_until` to the day before the store's day when that key is not
    already earlier, appends the arguments as one JSON line to `<db>.script.jsonl` and
    prints the event's ts. Run `digest extract --db <scratch>` next.

    The command refuses a `--db` equal to the base store path, or one that resolves to the
    same file. Without `--script`, the command uses an existing `--db` as it is. A failed
    event write refuses on one line that starts with the `--db` path. A failed copy or
    event write removes a scratch store that this say made. A failed log append keeps it.
    The command keeps a scratch store that already existed.

    With `--script`, the command refuses a `--db` that already exists, or a
    `<db>.script.jsonl` that already exists, before the copy. It makes `--db` as a fresh
    copy of the base store, then runs every line of the file in order, each as its own say
    with the same rules. It appends to no script file. On any failure after the copy, it
    removes the scratch store. It prints the ts values only after it stores every line.
    """
    settings = Settings()
    base = base_db or settings.db or Path("data") / f"{settings.world}.sqlite"
    if db.resolve() == base.resolve() or (
        db.exists() and base.exists() and os.path.samefile(db, base)
    ):
        _refuse(f"--db {db} is the same file as the base store {base}; give a scratch path")
    single = {"--as": as_, "--channel": channel, "--at": at, "--text": text, "--thread": thread}
    if script is not None:
        given = [option for option, value in single.items() if value is not None]
        if given:
            _refuse(f"--script cannot be used with {', '.join(given)}")
        _say_script(script, db, base)
        return
    if as_ is None or channel is None or at is None or text is None:
        _refuse("say needs --as, --channel, --at and --text, or --script")
    try:
        line = ScriptLine(as_=as_, channel=channel, at=at, text=text, thread=thread)
    except ValidationError as error:
        _refuse(_first_error(error))
    fresh = not db.exists()
    log_path = Path(f"{db}.script.jsonl")
    if fresh and not base.is_file():
        _refuse(f"no base store at {base}; run digest replay first")
    with _open_store_readonly(base) if fresh else _open_store(db) as checked:
        world = _store_world(checked)
        try:
            event, meta = _checked_say(checked, world, line)
        except ValueError as error:
            _refuse(" ".join(str(error).split()))
        if fresh:
            if log_path.exists():  # after the rule checks, so a broken rule names its reason
                _refuse(f"script log {log_path} already exists; remove it or give another --db")
            scratch = _copy_store(checked, db)  # before the try: a refused copy removes nothing
            try:
                with scratch:  # inside the try: close before _remove_store, for Windows
                    scratch.upsert_events([event], meta=meta)
            except (OSError, sqlite3.Error) as error:
                _remove_store(db)
                _refuse(f"{db}: {' '.join(str(error).split())}")
            except BaseException:
                _remove_store(db)
                raise
        else:
            try:
                checked.upsert_events([event], meta=meta)
            except (OSError, sqlite3.Error) as error:  # keep a store this say did not make
                _refuse(f"{db}: {' '.join(str(error).split())}")
    with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
        log_file.write(line.model_dump_json() + "\n")
    typer.echo(event.ts)


@app.command()
def diff(
    as_of: str = typer.Option(..., "--as-of", help="Digest date, YYYY-MM-DD."),
    db: Path = typer.Option(..., "--db", help="Store B, the store after the change."),
    base_db: Path | None = typer.Option(
        None,
        "--base-db",
        help="Store A, the store before the change; default DIGEST_DB, then data/<world>.sqlite.",
    ),
    user: str | None = typer.Option(
        None, "--user", help="Compare only this user id; default every non-bot persona."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON array of the reader diffs."),
) -> None:
    """Show which readers' digests differ between two stores on a date, and why.

    For each reader with a change, the command lists the added, removed and changed lines,
    each with its title and reason. Then it names the readers with no change. With no change
    for any reader, it prints "no change" and exits 0. The readers are sorted by user_id, in
    the text output and in `--json`. A refused store or an unknown user exits 1 with the
    reason, as `digest run` does. The command opens both stores read-only and never writes
    them.
    """
    settings = Settings()
    try:
        day = date.fromisoformat(as_of)
    except ValueError:
        _refuse(f"--as-of must be YYYY-MM-DD, got {as_of!r}")
    base = base_db or settings.db or Path("data") / f"{settings.world}.sqlite"
    for path in (base, db):
        if not path.is_file():  # a clearer reason than the error of a read-only open
            _refuse(f"no store at {path}; run digest replay first")
    with _open_store_readonly(base) as before_store, _open_store_readonly(db) as after_store:
        name = _stored(after_store, "world")
        base_name = _stored(before_store, "world")
        if base_name != name:
            _refuse(f"store {base} holds world {base_name!r}, but store {db} holds {name!r}")
        world = _store_world(after_store)
        try:
            priors = load_priors(DEFAULT_PRIORS_PATH)
        except ValidationError as error:
            _refuse(f"{DEFAULT_PRIORS_PATH}: {_first_error(error)}")
        except (OSError, ValueError, yaml.YAMLError) as error:
            _refuse(f"{DEFAULT_PRIORS_PATH}: {' '.join(str(error).split())}")
        if user is None:
            readers = sorted(p.user_id for p in world.personas if p.kind != "bot")
        else:
            readers = [user]
        diffs: list[ReaderDiff] = []
        for reader in readers:
            try:
                before = select_digest(before_store, world, priors, reader, day)
                after = select_digest(after_store, world, priors, reader, day)
            except KeyError:
                _refuse(f"unknown user {reader}: no persona with that id in world {name}")
            except ValueError as error:  # a refused store, or a bot
                _refuse(str(error))
            diffs.append(diff_selections(before, after))
    if as_json:
        typer.echo(json.dumps([d.model_dump(mode="json") for d in diffs], indent=2))
        return
    changed = [d for d in diffs if d.added or d.removed or d.changed]
    if not changed:
        typer.echo("no change")
        return
    for reader_diff in changed:
        typer.echo(reader_diff.user_id)
        for view in reader_diff.added:
            typer.echo(f"  added {_line_view(view)}")
        for view in reader_diff.removed:
            typer.echo(f"  removed {_line_view(view)}")
        for change in reader_diff.changed:
            typer.echo(f"  changed {_line_view(change.after)}")
            for field in change.fields:
                was, now = getattr(change.before, field), getattr(change.after, field)
                typer.echo(f"      {field}: {was} -> {now}")
    unchanged = [d.user_id for d in diffs if d not in changed]
    if unchanged:
        typer.echo(f"no change: {', '.join(unchanged)}")


world_app = typer.Typer(help="Inspect a scripted world.", no_args_is_help=True)
app.add_typer(world_app, name="world")


@world_app.command("check")
def world_check(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
) -> None:
    """Load and validate a world file; print counts per day and per channel."""
    source: Path | str = "settings"  # names the error when Settings() fails before a path exists
    try:
        source = Path("data/world") / (world or Settings().world) / "world.yaml"
        loaded = load_world(source)
    except OSError as exc:
        typer.echo(f"{source}: {exc.strerror or exc}", err=True)
        raise typer.Exit(code=1) from exc
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        typer.echo(f"{source}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"{source}: {len(loaded.events)} events, {len(loaded.personas)} personas, "
        f"{len(loaded.entities)} entities"
    )
    per_day = Counter(event.day for event in loaded.events)
    typer.echo("events per day:")
    for day in range(1, loaded.days + 1):
        typer.echo(f"  {loaded.date_of(day)}  day {day}: {per_day[day]}")
    per_channel = Counter(event.channel for event in loaded.events)
    typer.echo("events per channel:")
    for channel in loaded.channels:
        typer.echo(f"  {channel.name} ({channel.id}): {per_channel[channel.id]}")


@world_app.command("render")
def world_render(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
) -> None:
    """Render the world's events into events.jsonl through the render model."""
    settings = Settings()
    world_dir = Path("data/world") / (world or settings.world)
    manifest = world_dir / "manifest.json"
    try:
        report = render_world(
            load_world(world_dir / "world.yaml"),
            from_settings(settings),
            world_dir / "anchor.jsonl",
            load_manifest(manifest),
            load_avoid_names(),
            world_dir / "events.jsonl",
            manifest,
        )
    # ValueError covers RenderError, a missing API key and malformed world, manifest
    # and anchor files. The notes name the file that the loader read.
    except (OSError, ValueError, yaml.YAMLError, CassetteMiss) as error:
        _refuse("\n".join([str(error), *getattr(error, "__notes__", [])]))
    typer.echo(
        f"rendered {report.rendered} threads, reused {report.reused} anchor threads, "
        f"{report.messages} messages"
    )
    if report.chars_per_message:
        typer.echo(
            f"chars per message: median {median(report.chars_per_message)}, "
            f"max {max(report.chars_per_message)}"
        )


@world_app.command("golden")
def world_golden(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
) -> None:
    """Derive golden.jsonl from the world script and the render manifest."""
    world_dir = Path("data") / "world" / (world or Settings().world)
    try:
        loaded = load_world(world_dir / "world.yaml")
        manifest = load_manifest(world_dir / "manifest.json")
        priors = load_priors()
    except (OSError, ValueError, yaml.YAMLError) as error:  # ValueError: ValidationError too
        _refuse(str(error))
    try:
        golden = derive_golden(loaded, manifest, priors)
    except KeyError as error:
        _refuse(f"{world_dir / 'manifest.json'} has no thread for event {error.args[0]}")
    path = world_dir / "golden.jsonl"
    write_golden(golden, path)
    typer.echo(
        f"{path}: {len(golden.items)} items, {len(golden.absences)} absences, "
        f"{len(golden.distractors)} distractors"
    )


def _demo_selections(day: int) -> tuple[World, list[Selection]]:
    """Build the selections of the demo readers for a world day of the bundled world.

    The function builds the store with `_demo_store`. It returns the world and one
    selection for each reader of `_DEMO_READERS`, in the same order. A refused store stops
    the command. The function never constructs the settings object. It assumes that each
    reader got the digest of the previous world day; on world day 1, each digest is a
    first digest.
    """
    selections: list[Selection] = []
    # The refusal comes after the try statement, so no exception chains to it.
    failure: str | None = None
    with _demo_store(day) as (loaded, priors, store):
        today = loaded.date_of(day)
        previous = loaded.date_of(day - 1) if day > 1 else None
        try:
            for user_id in _DEMO_READERS:
                selections.append(
                    select_digest(store, loaded, priors, user_id, today, previous_digest=previous)
                )
        except (ValueError, OSError) as error:  # a refused store
            failure = str(error)
    if failure is not None:
        _refuse(failure)
    return loaded, selections


@contextmanager
def _demo_store(day: int) -> Iterator[tuple[World, Priors, Store]]:
    """Yield the bundled world, the priors and a store replayed and extracted through `day`.

    The function replays the bundled world into a temporary store one day at a time and
    runs the extract stage at the end of each day from the recorded cassettes. The store
    and its temporary directory last until the `with` block ends. A cassette directory that
    does not exist or holds no cassette file stops the command before any replay. A failed
    thread in any extract run stops the command before the function yields. The function
    never constructs the settings object.
    """
    world_dir = Path("data") / "world" / _DEMO_WORLD
    source = world_dir / "world.yaml"
    # Each refusal comes after its try statement, so no exception chains to it.
    failure: str | None = None
    try:
        loaded = load_world(source)
    except OSError as error:
        failure = f"{source}: {error.strerror or error}"
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
        failure = f"{source}: {error}"
    if failure is not None:
        _refuse(failure)
    if not 1 <= day <= loaded.days:
        _refuse(f"--day must be from 1 to {loaded.days}, got {day}")
    try:
        priors = load_priors(DEFAULT_PRIORS_PATH)
    except OSError as error:
        failure = f"{DEFAULT_PRIORS_PATH}: {error.strerror or error}"
    except (UnicodeDecodeError, yaml.YAMLError, ValidationError) as error:
        failure = f"{DEFAULT_PRIORS_PATH}: {_with_notes(error)}"
    if failure is not None:
        _refuse(failure)
    # Settings would load API keys from the environment and `.env`. The demo reads only
    # DIGEST_CASSETTES, and the client has no backend, so the mode is always replay.
    cassettes = Path(
        os.environ.get("DIGEST_CASSETTES") or Settings.model_fields["cassettes"].default
    )
    if not cassettes.is_dir() or not any(cassettes.glob("*.json")):
        _refuse(f"cassette directory {cassettes} does not exist or holds no cassette file")
    llm = LLM(None, Cassettes(cassettes), "replay")
    today = loaded.date_of(day)
    zone = ZoneInfo(loaded.timezone)
    # Windows can hold a handle on the store file after it closes. A cleanup error must not
    # replace the output or the exit code.
    with (
        tempfile.TemporaryDirectory(prefix="digest-demo-", ignore_cleanup_errors=True) as tmp,
        Store.open(Path(tmp) / f"{_DEMO_WORLD}.sqlite") as store,
    ):
        try:
            total = _extract_days(
                store, loaded, llm, priors, zone, _end_of(today, zone), replay=world_dir
            )
        # CassetteMiss: no recording for a request. ValueError: a refused replay or store.
        except (CassetteMiss, ValueError, OSError) as error:
            failure = str(error)
        if failure is not None:
            _refuse(failure)
        if total.failed:
            noun = "thread" if total.failed == 1 else "threads"
            _refuse(
                f"extract reported {total.failed} failed {noun} up to day {day}; "
                "the demo builds no digest from a partial extraction"
            )
        # The yield is outside the try statement, so an error in the caller's block is
        # not reported as an extract failure.
        yield loaded, priors, store


def _webhook_url() -> str | None:
    """SLACK_WEBHOOK_URL from the environment, else from `.env` (see `_env_value`)."""
    return _env_value("SLACK_WEBHOOK_URL")


def _env_value(key: str) -> str | None:
    """The variable `key` from the environment, else from a `KEY=VALUE` line in `.env`.

    A `.env` that starts with a UTF-16 byte-order mark is read as UTF-16 (the PowerShell
    5.1 default). Any other `.env` is read as UTF-8, with or without a byte-order mark. A
    file that does not decode exits 1 and names `.env`. The parser skips lines without
    `=` and accepts an `export ` prefix. It removes one pair of matching quotes. An
    unquoted value ends at ` #`, which starts an inline comment. The last line for the
    key wins. An empty value is no value. `_env_source` names the source of the value.
    """
    value = os.environ.get(key)
    if value:
        return value
    # Each refusal comes after its try statement, so no exception chains to it. A
    # UnicodeDecodeError holds the bytes of `.env`, which can hold a secret.
    failure: str | None = None
    try:
        data = Path(".env").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        failure = f".env: {error.strerror or error}"
    if failure is not None:
        _refuse(failure)
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as error:
        failure = f".env: cannot decode as {encoding}: {error.reason}"
    if failure is not None:
        _refuse(failure)
    for raw in text.splitlines():
        name, sep, rest = raw.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == key:
            rest = rest.strip()
            if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
                rest = rest[1:-1]
            else:
                rest = rest.partition(" #")[0].rstrip()
            value = rest
    return value or None


def _env_source(key: str) -> str:
    """Where `_env_value` reads `key`: "environment" when the variable is not empty, else ".env".

    The function reads the environment only. It does not read `.env`.
    """
    return "environment" if os.environ.get(key) else ".env"


def _webhook_client(url: str) -> WebhookClient:
    """A Slack webhook client that writes no log line, so no log line holds the URL."""
    # slack-sdk is in the optional `slack` extra. A top-level import would stop every
    # command when the extra is not installed.
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        from slack_sdk.webhook import WebhookClient
    except ImportError:
        installed = False
    else:
        installed = True
    if not installed:
        _refuse("digest post needs the slack extra; run uv sync --extra slack")
    # The client logs the URL at DEBUG level, on a send error and on a retry. A disabled
    # logger drops all three. The SDK default retry handlers retry a connection error.
    quiet = logging.getLogger("digest.slack_webhook")
    quiet.disabled = True
    return WebhookClient(url, logger=quiet)


def _slack_web_client(token: str) -> WebClient:
    """A Slack Web API client that writes no log line, so no log line holds the token.

    The client retries a connection error once (the SDK default). It retries a
    rate-limited call once, after the time in Slack's `Retry-After` header.
    """
    # slack-sdk is in the optional `slack` extra. A top-level import would stop every
    # command when the extra is not installed.
    # The refusal comes after the try statement, so no exception chains to it.
    try:
        from slack_sdk import WebClient
        from slack_sdk.http_retry import RateLimitErrorRetryHandler, default_retry_handlers
    except ImportError:
        installed = False
    else:
        installed = True
    if not installed:
        _refuse("digest post needs the slack extra; run uv sync --extra slack")
    # The client logs requests at DEBUG level and failures at ERROR level. A disabled
    # logger drops all of them.
    quiet = logging.getLogger("digest.slack_web")
    quiet.disabled = True
    handlers = [*default_retry_handlers(), RateLimitErrorRetryHandler()]
    return WebClient(token=token, logger=quiet, retry_handlers=handlers)


def _extract_days(
    store: Store,
    world: World,
    llm: LLM,
    priors: Priors,
    zone: ZoneInfo,
    end: datetime,
    *,
    replay: Path | None = None,
) -> ExtractReport:
    """Run the extract stage at the end of each world day up to `end`; return the sum.

    The run starts at the day after the meta key `extract_until`. After a day with no failed
    thread, the run sets that key to the day. With `replay`, the run first replays that world
    directory into the store up to the end of each day, as the extraction eval does.
    """
    done = store.get_meta("extract_until")
    glossary = world.glossary()
    purposes = {channel.id: channel.purpose for channel in world.channels}
    total = ExtractReport(
        threads=0,
        kept=0,
        dropped=0,
        failed=0,
        items_written=0,
        drops=dict.fromkeys(DROP_FIELDS, 0),
    )
    for day in range(1, world.days + 1):
        today = world.date_of(day)
        if done is not None and today <= date.fromisoformat(done):
            continue
        as_of = _end_of(today, zone)
        if as_of > end:
            break
        if replay is not None:
            replay_world(store, replay, today)
        report = run_extract(store, llm, glossary, world.project, priors, as_of, zone, purposes)
        total = total.plus(report)
        if report.failed == 0:
            store.set_meta("extract_until", today.isoformat())
    return total


def _say_script(script: Path, db: Path, base: Path) -> None:
    """Run every line of a script file as its own say into a new scratch store at `db`.

    Refuse a `db` that exists, or a `<db>.script.jsonl` that exists. Split the file into
    lines on "\\n" only, so a text that holds U+0085, U+2028 or U+2029 replays. On any
    failure after the copy, remove the new scratch store, so a failure writes nothing. Print
    the ts values only after every line is stored.
    """
    if db.exists():
        _refuse(f"--db {db} already exists; --script runs into a new scratch store only")
    log_path = Path(f"{db}.script.jsonl")
    if log_path.exists():
        _refuse(f"script log {log_path} already exists; remove it or give another --db")
    try:
        raw_lines = [raw.removesuffix("\r") for raw in script.read_bytes().decode().split("\n")]
    except (OSError, UnicodeDecodeError) as error:
        _refuse(f"{script}: {error}")
    lines: list[tuple[int, ScriptLine]] = []
    for number, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            lines.append((number, ScriptLine.model_validate_json(raw)))
        except ValidationError as error:
            _refuse(f"{script}:{number}: {_first_error(error)}")
    if not base.is_file():
        _refuse(f"no base store at {base}; run digest replay first")
    with _open_store_readonly(base) as source:
        world = _store_world(source)
        scratch = _copy_store(source, db)
    stored: list[str] = []
    where = str(script)  # the script line that failed, for the refusal
    try:
        with scratch:
            for number, line in lines:
                where = f"{script}:{number}"
                event, meta = _checked_say(scratch, world, line)
                scratch.upsert_events([event], meta=meta)
                stored.append(event.ts)
    except (ValueError, OSError, sqlite3.Error) as error:
        _remove_store(db)
        _refuse(f"{where}: {' '.join(str(error).split())}")
    except BaseException:
        _remove_store(db)
        raise
    for ts in stored:
        typer.echo(ts)


def _checked_say(store: Store, world: World, line: ScriptLine) -> tuple[Event, dict[str, str]]:
    """The event of one say line and the meta to write with it.

    Raise ValueError with a one-line reason for a line that breaks rule S1, S2 or S3, or
    for a store whose meta is malformed. The function writes nothing.
    """
    if _AT_PATTERN.fullmatch(line.at) is None:
        raise ValueError(f"--at must be YYYY-MM-DDTHH:MM[:SS], got {line.at!r}")
    moment = datetime.fromisoformat(line.at)
    until = store.get_meta("until")
    end = None if until is None else _instant(until)
    if end is None:  # a ValueError, not _refuse: a script removes its scratch store first
        raise ValueError(
            f"store {store.path} meta key 'until' is {until!r}, "
            "not an ISO timestamp with a UTC offset"
        )
    day = end.astimezone(ZoneInfo(world.timezone)).date()
    view = StoreView(day=day, latest_event_ts=store.latest_event_ts())
    event = make_event(world, view, line.as_, line.channel, moment, line.text, line.thread)
    if line.thread is not None:  # rule S3
        found = store.events_in_thread(f"{event.channel_id}:{line.thread}", end)
        if not found or found[0].ts != line.thread:
            raise ValueError(
                f"--thread {line.thread} is not the ts of a top-level message "
                f"in channel {line.channel}"
            )
    # The next `digest extract` must revisit the store's day; keep an earlier key as it is.
    revisit = day - timedelta(days=1)
    done = store.get_meta("extract_until")
    try:
        earlier = done is not None and date.fromisoformat(done) < revisit
    except ValueError:
        raise ValueError(
            f"store {store.path} meta key 'extract_until' is {done!r}, not YYYY-MM-DD"
        ) from None
    return event, {} if earlier else {"extract_until": revisit.isoformat()}


def _store_world(store: Store) -> World:
    """The world that the store holds, loaded from data/world; refuse a missing or bad file."""
    return _named_world(_stored(store, "world"))


def _named_world(name: str) -> World:
    """The world `name`, loaded from data/world; refuse a missing or bad file."""
    source = Path("data") / "world" / name / "world.yaml"
    try:
        return load_world(source)
    except OSError as error:
        _refuse(f"{source}: {error.strerror or error}")
    except ValidationError as error:
        _refuse(f"{source}: {_first_error(error)}")
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        _refuse(f"{source}: {' '.join(str(error).split())}")


def _copy_store(store: Store, path: Path) -> Store:
    """Copy the store to a new file at `path` and open the copy; refuse on a failed copy."""
    try:
        return store.copy_to(path)
    except (OSError, ValueError, sqlite3.Error) as error:
        _refuse(f"cannot copy store {store.path} to {path}: {error}")


def _line_view(view: LineView) -> str:
    """One diff line: section, lane and rank, then the title and the reason."""
    place = "must-see" if view.lane == "must_see" else f"ranked {view.rank}"
    return f"[{view.section}, {place}] {view.title}: {view.reason}"


def _overlap_line(*shown: Set[str]) -> str:
    """The Jaccard index of the item ids of each pair of demo digests, to two decimals.

    `shown` holds one set for each reader of `_DEMO_READERS`, in the same order. Two empty
    sets have the index 0.00: two readers who see no item share no item.
    """
    pairs = []
    for (role_a, a), (role_b, b) in combinations(zip(_DEMO_ROLES, shown, strict=True), 2):
        union = a | b
        index = len(a & b) / len(union) if union else 0.0
        pairs.append(f"{role_a}/{role_b} {index:.2f}")
    return f"Item overlap: {', '.join(pairs)}"


def _end_of(day: date, zone: ZoneInfo) -> datetime:
    """The last instant of a day in a timezone, in UTC."""
    return datetime.combine(day, time.max, zone).astimezone(UTC)


def _stored(store: Store, key: str) -> str:
    """A meta value `digest replay` writes; refuse a store without it."""
    value = store.get_meta(key)
    if value is None:
        _refuse(f"store {store.path} has no meta key {key!r}; run `digest replay` first")
    return value


def _with_notes(error: BaseException) -> str:
    """The error message, then each note added to the error, one per line."""
    return "\n".join([str(error), *getattr(error, "__notes__", ())])


def _open_store(path: Path) -> Store:
    """Open the store, or refuse for a file that is not SQLite or of another schema version."""
    try:
        return Store.open(path)
    except ValueError as error:  # another schema version; the message names the store
        _refuse(str(error))
    except (OSError, sqlite3.DatabaseError) as error:
        _refuse(f"cannot open store {path}: {error}")


def _open_store_readonly(path: Path) -> Store:
    """Open the store read-only, or refuse for a file that is not a store of this schema."""
    try:
        return Store.open_readonly(path)
    except ValueError as error:  # empty, or another schema version; the message names the store
        _refuse(str(error))
    except (OSError, sqlite3.DatabaseError) as error:
        _refuse(f"cannot open store {path}: {error}")


def _remove_store(path: Path) -> None:
    """Remove a store file with its `-wal` and `-shm` files."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _first_error(error: ValidationError) -> str:
    """The first message of a validation error, on one line, after its location."""
    first = error.errors()[0]
    message = " ".join(first["msg"].split())
    location = ".".join(map(str, first["loc"]))
    return f"{location}: {message}" if location else message


def _instant(text: str) -> datetime | None:
    """The aware datetime of an ISO timestamp with an offset; None for anything else."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _refuse(reason: str) -> NoReturn:
    typer.echo(reason, err=True)
    raise typer.Exit(code=1)

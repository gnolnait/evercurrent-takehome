"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 9)."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Set
from datetime import UTC, date, datetime, time
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, NoReturn
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
from digest.eval.extraction import run_extraction_eval, write_report
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.extract.stage import ExtractReport, refuse_other_prompt_version, run_extract
from digest.extract.verify import DROP_FIELDS
from digest.llm.cassette import CassetteMiss, Cassettes
from digest.llm.client import LLM, from_settings
from digest.priors import DEFAULT_PRIORS_PATH, Priors, load_priors
from digest.replay import replay_world
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
    """
    if dm is not None:
        _post_dm(day, dm)
        return
    url = _webhook_url()
    if url is None:
        _refuse(_not_set("SLACK_WEBHOOK_URL"))
    if not url.startswith(_WEBHOOK_PREFIX) or any(char.isspace() for char in url):
        _refuse(f"SLACK_WEBHOOK_URL must start with {_WEBHOOK_PREFIX} and hold no whitespace")
    loaded, selections = _demo_selections(day)
    messages = [slack_message(loaded, selection) for selection in selections]
    client = _webhook_client(url)
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


def _post_dm(day: int, user_id: str) -> None:
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
    messages = [slack_message(loaded, selection) for selection in selections]
    client = _slack_web_client(token)
    typer.echo(f"sending {len(messages)} digests by DM to {user_id}")
    # The message of an exception can hold the token, so a refusal shows _slack_failure only.
    # Each refusal comes after its try statement, so no exception chains to it.
    failure: str | None = None
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

    The function replays the bundled world into a temporary store one day at a time and
    runs the extract stage at the end of each day from the recorded cassettes. It returns
    the world and one selection for each reader of `_DEMO_READERS`, in the same order. A
    cassette directory that does not exist or holds no cassette file stops the command
    before any replay. A failed thread in any extract run stops the command before it
    selects a digest. The function never constructs the settings object.
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
    selections: list[Selection] = []
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
            if total.failed:
                noun = "thread" if total.failed == 1 else "threads"
                _refuse(
                    f"extract reported {total.failed} failed {noun} up to day {day}; "
                    "the demo builds no digest from a partial extraction"
                )
            for user_id in _DEMO_READERS:
                selections.append(select_digest(store, loaded, priors, user_id, today))
        # CassetteMiss: no recording for a request. ValueError: a refused replay or store.
        except (CassetteMiss, ValueError, OSError) as error:
            failure = str(error)
    if failure is not None:
        _refuse(failure)
    return loaded, selections


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


def _not_yet(name: str) -> int:
    typer.echo(f"`digest {name}` is not implemented yet; see SPEC.md section 9.", err=True)
    return 2

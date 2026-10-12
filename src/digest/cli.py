"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 9)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import typer
import yaml
from pydantic import ValidationError

from digest import __version__
from digest.extract.stage import ExtractReport, run_extract
from digest.extract.verify import DROP_FIELDS
from digest.llm.cassette import CassetteMiss
from digest.llm.client import from_settings
from digest.priors import load_priors
from digest.replay import replay_world
from digest.settings import Settings
from digest.store import Store
from digest.world.load import load_world

app = typer.Typer(help="Role- and phase-aware daily Slack digest.", no_args_is_help=True)


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def demo() -> None:
    """Produce digests for three roles from the bundled synthetic corpus (offline)."""
    raise typer.Exit(code=_not_yet("demo"))


@app.command()
def run(
    user: str = typer.Option(..., help="User id from the corpus or workspace."),
    as_of: str = typer.Option(..., "--as-of", help="Digest date, YYYY-MM-DD."),
) -> None:
    """Produce one user's digest as of a date."""
    raise typer.Exit(code=_not_yet("run"))


@app.command("eval")
def eval_cmd(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Report path; default reports/eval-<date>.md."
    ),
) -> None:
    """Run the evaluation suite against the golden corpus and write the report."""
    raise typer.Exit(code=_not_yet("eval"))


@app.command()
def extract(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Store path; default DIGEST_DB, then data/<world>.sqlite."
    ),
) -> None:
    """Triage and extract pending thread deltas, one working day at a time, up to the store's day."""
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
        zone = ZoneInfo(timezone)
        end = datetime.fromisoformat(until)
        glossary = loaded.glossary()
        purposes = {channel.id: channel.purpose for channel in loaded.channels}
        total = ExtractReport(
            threads=0,
            kept=0,
            dropped=0,
            failed=0,
            items_written=0,
            drops=dict.fromkeys(DROP_FIELDS, 0),
        )
        try:
            llm = from_settings(settings)
            priors = load_priors()
            for day in range(1, loaded.days + 1):
                as_of = datetime.combine(loaded.date_of(day), time.max, zone).astimezone(UTC)
                if as_of > end:
                    break
                report = run_extract(
                    store, llm, glossary, loaded.project, priors, as_of, zone, purposes
                )
                total = total.plus(report)
        # CassetteMiss: replay found no recording. ValueError: a missing API key, a store of
        # another prompt version or a malformed priors file. OSError: no priors file.
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
    raise typer.Exit(code=_not_yet("absences"))


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
    try:
        store = Store.open(path)
    except ValueError as error:  # another schema version; the message names the store
        _refuse(str(error))
    except (OSError, sqlite3.DatabaseError) as error:
        _refuse(f"cannot open store {path}: {error}")
    with store:
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
    raise typer.Exit(code=_not_yet("world render"))


@world_app.command("golden")
def world_golden(
    world: str | None = typer.Option(
        None, "--world", help="World under data/world; default DIGEST_WORLD."
    ),
) -> None:
    """Derive golden.jsonl from the world script and the render manifest."""
    raise typer.Exit(code=_not_yet("world golden"))


def _stored(store: Store, key: str) -> str:
    """A meta value `digest replay` writes; refuse a store without it."""
    value = store.get_meta(key)
    if value is None:
        _refuse(f"store {store.path} has no meta key {key!r}; run `digest replay` first")
    return value


def _refuse(reason: str) -> NoReturn:
    typer.echo(reason, err=True)
    raise typer.Exit(code=1)


def _not_yet(name: str) -> int:
    typer.echo(f"`digest {name}` is not implemented yet; see SPEC.md section 9.", err=True)
    return 2

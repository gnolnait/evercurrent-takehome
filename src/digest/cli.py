"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 9)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import NoReturn

import typer
import yaml
from pydantic import ValidationError

from digest import __version__
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
    raise typer.Exit(code=_not_yet("extract"))


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


def _refuse(reason: str) -> NoReturn:
    typer.echo(reason, err=True)
    raise typer.Exit(code=1)


def _not_yet(name: str) -> int:
    typer.echo(f"`digest {name}` is not implemented yet; see SPEC.md section 9.", err=True)
    return 2

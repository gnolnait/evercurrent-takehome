"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 9)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import typer
import yaml

from digest import __version__
from digest.replay import ReplayFormatError, ingest
from digest.settings import Settings
from digest.store import Store
from digest.world.load import load_world_header

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
def eval_cmd() -> None:
    """Run the evaluation suite against the golden corpus."""
    raise typer.Exit(code=_not_yet("eval"))


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
    world_dir = Path("data") / "world" / name
    world_file, corpus = world_dir / "world.yaml", world_dir / "events.jsonl"
    if not world_file.is_file():
        _refuse(f"no such world: {world_file}")
    if not corpus.is_file():
        _refuse(f"no corpus for world {name!r}: {corpus}")
    try:
        header = load_world_header(world_file)
        zone = ZoneInfo(header.timezone)
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as error:
        # ZoneInfoNotFoundError is a KeyError.
        _refuse(f"cannot read {world_file}: {type(error).__name__}: {error}")
    try:
        day = date.fromisoformat(until)
    except ValueError:
        _refuse(f"--until must be YYYY-MM-DD, got {until!r}")
    end = datetime.combine(day, time.max, zone).astimezone(UTC)
    with Store.open(db or settings.db or Path("data") / f"{name}.sqlite") as store:
        stored_world = store.get_meta("world")
        if stored_world not in (None, name):
            _refuse(f"the store holds world {stored_world!r}, not {name!r}")
        stored_until = store.get_meta("until")
        if stored_until is not None and datetime.fromisoformat(stored_until) > end:
            _refuse(
                f"--until {day} ends at {end.isoformat()}, earlier than the store's until"
                f" {stored_until}; replay only moves forward, so delete the store to start over"
            )
        try:
            report = ingest(store, corpus, end, header.workspace_url)
        except ReplayFormatError as error:
            _refuse(str(error))
        store.set_meta("world", name)
        store.set_meta("timezone", header.timezone)
        store.set_meta("workspace_url", header.workspace_url)
        store.set_meta("until", end.isoformat())
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
    raise typer.Exit(code=_not_yet("world check"))


def _refuse(reason: str) -> NoReturn:
    typer.echo(reason, err=True)
    raise typer.Exit(code=1)


def _not_yet(name: str) -> int:
    typer.echo(f"`digest {name}` is not implemented yet; see SPEC.md section 9.", err=True)
    return 2

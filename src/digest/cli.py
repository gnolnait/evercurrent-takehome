"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 9)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, time
from pathlib import Path
from statistics import median
from typing import NoReturn
from zoneinfo import ZoneInfo

import typer
import yaml
from pydantic import ValidationError

from digest import __version__
from digest.absence import detect_absences
from digest.corpus.avoid import load_avoid_names
from digest.corpus.render import render_world
from digest.eval.extraction import run_extraction_eval, write_report
from digest.extract.prompts import EXTRACT_PROMPT_VERSION
from digest.extract.stage import ExtractReport, refuse_other_prompt_version, run_extract
from digest.extract.verify import DROP_FIELDS
from digest.llm.cassette import CassetteMiss
from digest.llm.client import from_settings
from digest.priors import DEFAULT_PRIORS_PATH, load_priors
from digest.replay import replay_world
from digest.settings import Settings
from digest.store import Store
from digest.world.golden import derive_golden, write_golden
from digest.world.load import load_world
from digest.world.manifest import load_manifest

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
    """Run the evaluation suite against the golden corpus and write the report.

    The eval runs in a fresh store at data/eval-<world>.sqlite. The command deletes that file
    first.
    """
    settings = Settings()
    name = world or settings.world
    world_dir = Path("data") / "world" / name
    store_path = Path("data") / f"eval-{name}.sqlite"
    try:
        loaded = load_world(world_dir / "world.yaml")
        priors = load_priors()
        llm = from_settings(settings)
        store_path.unlink(missing_ok=True)
        report = run_extraction_eval(world_dir, llm, priors, store_path)
    # CassetteMiss: replay found no recording. ValueError: a stale golden.jsonl, a missing
    # API key or a malformed file. OSError: a missing world, manifest or golden file.
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
        done = store.get_meta("extract_until")
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
            refuse_other_prompt_version(store)
            llm = from_settings(settings)
            for day in range(1, loaded.days + 1):
                today = loaded.date_of(day)
                if done is not None and today <= date.fromisoformat(done):
                    continue
                as_of = datetime.combine(today, time.max, zone).astimezone(UTC)
                if as_of > end:
                    break
                report = run_extract(
                    store, llm, glossary, loaded.project, priors, as_of, zone, purposes
                )
                total = total.plus(report)
                if report.failed == 0:
                    store.set_meta("extract_until", today.isoformat())
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

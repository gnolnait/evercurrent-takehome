"""Command line entry point. Subcommands fill in as milestones land (see SPEC.md section 8)."""

from __future__ import annotations

import typer

from digest import __version__

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


def _not_yet(name: str) -> int:
    typer.echo(f"`digest {name}` is not implemented yet; see SPEC.md section 8.", err=True)
    return 2

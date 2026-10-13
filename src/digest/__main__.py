"""Run the CLI as `python -m digest`.

Windows can block the `digest.exe` launcher that uv installs for the console script.
This module gives the same Typer app a second entry point that does not need the
launcher.
"""

from digest.cli import app

app(prog_name="digest")

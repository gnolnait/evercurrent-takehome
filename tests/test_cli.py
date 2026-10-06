from typer.testing import CliRunner

from digest import __version__
from digest.cli import app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_args_shows_help_and_fails() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "demo" in result.output

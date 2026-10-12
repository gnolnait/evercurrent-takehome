import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from digest.cli import app

REPO_ROOT = Path(__file__).parents[1]
runner = CliRunner()


def test_module_version_matches_console_app() -> None:
    module_result = subprocess.run(
        [sys.executable, "-m", "digest", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    console_result = runner.invoke(app, ["--version"])

    assert module_result.returncode == 0, module_result.stderr
    assert console_result.exit_code == 0
    assert module_result.stdout.strip() == console_result.stdout.strip()


def test_module_help_lists_demo_command() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "digest", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "demo" in result.stdout

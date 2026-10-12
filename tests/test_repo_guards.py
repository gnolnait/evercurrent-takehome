"""Tests for the two git-hook guards in tools/: the secret scan and the merged-PR push check.

The scripts are standalone (no package), so they load by file path. Fake tokens are built
by concatenation so this file never matches the scan itself.
"""

import importlib.util
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


secrets = _load("check_secrets")
pr_open = _load("check_pr_open")

FAKES = {
    "Slack bot token": "xox" + "b-" + "1234567890-1234567890123-" + "a" * 24,
    "Slack app token": "xapp" + "-1-" + "A0123456789-1234567890123-" + "b" * 64,
    "OpenRouter key": "sk-or" + "-v1-" + "c" * 64,
    "Anthropic key": "sk-ant" + "-api03-" + "d" * 40,
}


@pytest.mark.parametrize("kind", sorted(FAKES))
def test_each_token_kind_is_found_and_never_printed(
    kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "fixture.json"
    f.write_text(json.dumps({"note": "x", "token": FAKES[kind]}), encoding="utf-8")
    assert secrets.main([str(f)]) == 1
    out = capsys.readouterr().out
    assert kind in out
    assert FAKES[kind] not in out


def test_clean_files_pass(tmp_path: Path) -> None:
    f = tmp_path / "ok.py"
    f.write_text(
        'TOKEN = os.environ["SLACK_BOT_TOKEN"]  # xoxb- prefix alone is fine\n', encoding="utf-8"
    )
    assert secrets.main([str(f)]) == 0


def test_binary_and_missing_files_do_not_crash(tmp_path: Path) -> None:
    b = tmp_path / "blob.bin"
    b.write_bytes(bytes(range(256)))
    assert secrets.main([str(b), str(tmp_path / "gone.txt")]) == 0


def _runner(
    prs: list[dict[str, object]] | None, *, fail: bool = False
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if fail:
            raise FileNotFoundError("gh")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(prs), "")

    return run


def test_push_to_a_branch_whose_pr_merged_is_refused() -> None:
    assert pr_open.merged_pr("feat", _runner([{"number": 17, "state": "MERGED"}])) == 17


def test_push_is_allowed_with_an_open_pr_or_none() -> None:
    assert (
        pr_open.merged_pr(
            "feat", _runner([{"number": 18, "state": "OPEN"}, {"number": 17, "state": "MERGED"}])
        )
        is None
    )
    assert pr_open.merged_pr("feat", _runner([])) is None


def test_missing_gh_lets_the_push_through_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert pr_open.merged_pr("feat", _runner(None, fail=True)) is None
    assert "could not check" in capsys.readouterr().out


def test_main_refuses_with_the_pr_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr_open, "current_branch", lambda: "feat")
    monkeypatch.setattr(pr_open, "merged_pr", lambda branch, run=None: 17)
    assert pr_open.main() == 1
    assert "#17" in capsys.readouterr().out


def test_main_skips_main_and_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_open, "merged_pr", lambda branch, run=None: 17)  # never asked
    for name in ("main", "HEAD"):
        monkeypatch.setattr(pr_open, "current_branch", lambda name=name: name)
        assert pr_open.main() == 0

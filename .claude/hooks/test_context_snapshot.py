"""context_snapshot: PreCompact writes the state file, SessionStart prints it.

Every test runs the script with PATH set to git's directory only, so `gh` is
not found and no test touches the network. A fake `gh` goes first on PATH
where a test needs the PR section.

Run: cd .claude/hooks && python -m pytest -q test_context_snapshot.py
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from context_snapshot import MAX_LINES, MAX_PRINT_CHARS
from hooktest_support import IS_WINDOWS, PYTHON, launch

HOOK_DIR = Path(__file__).parent
HOOK_PY = HOOK_DIR / "context_snapshot.py"
FENCE = HOOK_DIR / "context-snapshot"

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A primary checkout with one commit and `main` one commit ahead of `base`."""
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "commit", "-q", "--allow-empty", "-m", "one")
    _git(r, "branch", "base")
    _git(r, "commit", "-q", "--allow-empty", "-m", "two")
    _git(r, "branch", "--set-upstream-to=base", "main")
    return r


def _env(extra_path=None):
    env = os.environ.copy()
    git_dir = os.path.dirname(shutil.which("git") or "git")
    env["PATH"] = os.pathsep.join(([str(extra_path)] if extra_path else []) + [git_dir])
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["CLAUDE_HOOK_PYTHON"] = PYTHON  # the fence cannot find python on this PATH
    return env


def _run(mode, cwd, payload=None, env=None, argv=None):
    return subprocess.run(
        (argv or [PYTHON, str(HOOK_PY)]) + [mode],
        input=json.dumps(payload if payload is not None else {"cwd": str(cwd)}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env or _env(),
    )


def _state(repo):
    return repo / "notes" / "handoffs" / "auto-state.md"


def _notes(repo, sub, count, prefix="f"):
    d = repo / "notes" / sub
    d.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = d / ("%s%03d.md" % (prefix, i))
        p.write_text("x" * i)
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    return d


def _fake_gh(tmp_path):
    """A `gh` on PATH that answers `pr list` and `pr checks` with fixed JSON."""
    d = tmp_path / "fakebin"
    d.mkdir()
    script = d / "fake_gh.py"
    script.write_text(
        "import json, sys\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['pr', 'list']:\n"
        "    print(json.dumps([{'number': 7, 'title': 'Add widget',"
        " 'headRefName': 'widget', 'isDraft': True}]))\n"
        "elif a[:2] == ['pr', 'checks']:\n"
        "    print(json.dumps([{'bucket': 'pass'}, {'bucket': 'pass'},"
        " {'bucket': 'fail'}]))\n"
        "    sys.exit(1)\n"
    )
    if IS_WINDOWS:
        (d / "gh.cmd").write_text('@"%s" "%s" %%*\n' % (PYTHON, script))
    else:
        gh = d / "gh"
        gh.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (PYTHON, script))
        gh.chmod(0o755)
    return d


def test_snapshot_writes_state_file(repo):
    """Setup: notes and a handoff. Action: snapshot. Assert: each section."""
    _notes(repo, "dispatch", 12, prefix="d")
    _notes(repo, "reviews", 2, prefix="r")
    _notes(repo, "handoffs", 1, prefix="h")
    p = _run("snapshot", repo, payload={"cwd": str(repo), "trigger": "auto"})
    assert p.returncode == 0
    text = _state(repo).read_text(encoding="utf-8")
    assert "(trigger: auto)" in text
    assert "[main] " in text
    assert "- main -> base: ahead 1" in text
    assert "- notes/dispatch/d011.md " in text
    assert "d001.md" not in text  # only the 10 newest
    assert "- notes/reviews/r001.md " in text
    assert "- notes/handoffs/h000.md" in text


def test_snapshot_from_linked_worktree_writes_to_primary(repo, tmp_path):
    """Setup: a linked worktree. Action: snapshot there. Assert: primary notes/."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert _run("snapshot", wt).returncode == 0
    assert _state(repo).is_file()
    assert not (wt / "notes").exists()
    assert "[side] " in _state(repo).read_text(encoding="utf-8")


def test_snapshot_stays_under_line_cap(repo):
    """Setup: 200 tracked branches and 40 notes per dir. Assert: <= MAX_LINES."""
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    # Bytes, not text: text mode on Windows writes CRLF, and update-ref rejects it.
    refs = "".join("create refs/heads/b%03d %s\n" % (i, head) for i in range(200))
    subprocess.run(["git", "-C", str(repo), "update-ref", "--stdin"], input=refs.encode(), check=True)
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        for i in range(200):
            fh.write('[branch "b%03d"]\n\tremote = .\n\tmerge = refs/heads/base\n' % i)
    _notes(repo, "dispatch", 40)
    _notes(repo, "reviews", 40)
    assert _run("snapshot", repo).returncode == 0
    lines = _state(repo).read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_LINES
    assert "## Newest hand-written handoff" in lines


def test_snapshot_survives_missing_gh(repo):
    """Setup: PATH without gh. Action: snapshot. Assert: exit 0, PRs skipped."""
    p = _run("snapshot", repo)
    assert p.returncode == 0
    assert "- skipped: gh not found" in _state(repo).read_text(encoding="utf-8")


def test_snapshot_lists_open_prs_with_checks(repo, tmp_path):
    """Setup: a fake gh. Action: snapshot. Assert: the PR line and check counts."""
    p = _run("snapshot", repo, env=_env(extra_path=_fake_gh(tmp_path)))
    assert p.returncode == 0
    text = _state(repo).read_text(encoding="utf-8")
    assert "- #7 Add widget [widget] draft checks: fail 1, pass 2" in text


def test_print_shows_both_files(repo):
    """Setup: state file and a handoff. Action: print. Assert: both in stdout."""
    d = repo / "notes" / "handoffs"
    d.mkdir(parents=True)
    _state(repo).write_text("STATE-BODY\n")
    (d / "next.md").write_text("HANDOFF-BODY\n")
    p = _run("print", repo, payload={"cwd": str(repo), "source": "compact"})
    assert p.returncode == 0
    assert p.stdout.startswith("Context was compacted.")
    assert "auto-state.md ===\nSTATE-BODY" in p.stdout
    assert "next.md ===\nHANDOFF-BODY" in p.stdout


def test_print_cuts_long_handoff_under_hook_output_cap(repo):
    """Claude Code shows only a preview of SessionStart stdout over 10,000 chars."""
    d = repo / "notes" / "handoffs"
    d.mkdir(parents=True)
    _state(repo).write_text("S" * 20_000)
    (d / "next.md").write_text("H" * 50_000)
    p = _run("print", repo)
    assert p.returncode == 0
    assert len(p.stdout) <= MAX_PRINT_CHARS
    assert "read the full file at " in p.stdout
    assert "next.md ===\nHHHH" in p.stdout


@pytest.mark.parametrize("argv", [None, launch(FENCE)], ids=["python", "fence"])
def test_print_exits_zero_with_no_files(repo, argv):
    """No notes/ at all: exit 0 and print nothing, through python and the fence."""
    p = _run("print", repo, argv=argv)
    assert p.returncode == 0
    assert p.stdout == ""

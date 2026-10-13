"""context_snapshot: PreCompact writes the state file, SessionStart prints it.

Every test runs with a PATH that holds git but no `gh`, so no test touches
the network. A fake `gh` goes first on PATH where a test needs the PR section.

Run: cd .claude/hooks && python -m pytest -q test_context_snapshot.py
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from context_snapshot import MAX_LINES, MAX_PRINT_CHARS
from hooktest_support import IS_WINDOWS, PYTHON, launch, sh_exe

HOOK_DIR = Path(__file__).parent
HOOK_PY = HOOK_DIR / "context_snapshot.py"
FENCE = HOOK_DIR / "context-snapshot"
# The SessionStart timeout that a project's settings.json gives the hook.
HOOK_TIMEOUT_S = 15

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A primary checkout with two commits and `main` one commit ahead of `base`."""
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "commit", "-q", "--allow-empty", "-m", "one")
    _git(r, "branch", "base")
    _git(r, "commit", "-q", "--allow-empty", "-m", "two")
    _git(r, "branch", "--set-upstream-to=base", "main")
    return r


@pytest.fixture(scope="session")
def tool_dir(tmp_path_factory):
    """A PATH directory with git and the fence's tools, but no gh."""
    if IS_WINDOWS:
        # gh installs to its own directory, and the bash launcher from
        # hooktest_support supplies the MSYS tools that the fence uses.
        # A fetch from a local origin needs the MSYS sh: without its
        # directory, git exits with a segmentation fault.
        return os.pathsep.join(os.path.dirname(p) for p in (shutil.which("git"), sh_exe()))
    # On POSIX, git and gh can share /usr/bin. The fence needs bash and env
    # (for its `env bash` shebang) and nothing else: it finds its own
    # directory without forking `dirname`, so dirname stays off this PATH.
    # The startup fetch tests run sh, sleep and env.
    d = tmp_path_factory.mktemp("tools")
    for name in ("git", "bash", "sh", "sleep", "env"):
        os.symlink(shutil.which(name), d / name)
    return str(d)


@pytest.fixture(autouse=True)
def no_gh_env(tool_dir, monkeypatch):
    monkeypatch.setenv("PATH", tool_dir)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOOK_PYTHON", PYTHON)  # python is not on this PATH


def _run(mode, cwd, payload=None, env=None, argv=None):
    return subprocess.run(
        (argv or [PYTHON, str(HOOK_PY)]) + [mode],
        # Raw UTF-8, as Claude Code sends it: ASCII escapes would hide a
        # payload read in the wrong encoding.
        input=json.dumps(
            payload if payload is not None else {"cwd": str(cwd)}, ensure_ascii=False
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env or os.environ.copy(),
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


def _fake_gh(tmp_path, list_delay_s=0):
    """A `gh` on PATH that answers `pr list` and `pr checks` with fixed JSON.

    `pr list` first sleeps `list_delay_s` seconds, as on a slow network."""
    d = tmp_path / "fakebin"
    d.mkdir()
    script = d / "fake_gh.py"
    script.write_text(
        (
            "import json, sys, time\n"
            "a = sys.argv[1:]\n"
            "if a[:2] == ['pr', 'list']:\n"
            "    time.sleep(%d)\n"
            "    print(json.dumps([{'number': 7, 'title': 'Add widget',"
            " 'headRefName': 'widget', 'isDraft': True}]))\n"
            "elif a[:2] == ['pr', 'checks']:\n"
            "    print(json.dumps([{'bucket': 'pass'}, {'bucket': 'pass'},"
            " {'bucket': 'fail'}]))\n"
            "    sys.exit(1)\n"
        )
        % list_delay_s
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
    path = os.pathsep.join([str(_fake_gh(tmp_path)), os.environ["PATH"]])
    p = _run("snapshot", repo, env=dict(os.environ, PATH=path))
    assert p.returncode == 0
    text = _state(repo).read_text(encoding="utf-8")
    assert "- #7 Add widget [widget] draft checks: fail 1, pass 2" in text


@pytest.mark.parametrize("argv", [None, launch(FENCE)], ids=["python", "fence"])
def test_print_shows_both_files(repo, argv):
    """Setup: a handoff, then a newer state file, as PreCompact leaves them.
    Action: print, through python and the fence. Assert: each file once, the
    age of the state file, and non-cp1252 text intact."""
    d = repo / "notes" / "handoffs"
    d.mkdir(parents=True)
    now = time.time()
    (d / "next.md").write_text("HANDOFF-BODY → ✓\n", encoding="utf-8")
    os.utime(d / "next.md", (now - 1200, now - 1200))
    _state(repo).write_text("STATE-BODY\n")
    os.utime(_state(repo), (now - 600, now - 600))
    p = _run("print", repo, payload={"cwd": str(repo), "source": "compact"}, argv=argv)
    assert p.returncode == 0
    assert p.stdout.startswith("Context was compacted.")
    assert "The auto-state file was written 10 minutes ago." in p.stdout
    assert p.stdout.count("auto-state.md ===\nSTATE-BODY") == 1
    assert "next.md ===\nHANDOFF-BODY → ✓" in p.stdout


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


def _spawn_dir(repo):
    return repo / "notes" / "handoffs" / "spawn"


def _spawn_payload(repo, prompt="Fix the README badge → now.", title="Fix stale README badge"):
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__ccd_session__spawn_task",
        "tool_input": {"title": title, "tldr": "The badge is stale.", "prompt": prompt},
    }


@pytest.mark.parametrize("argv", [None, launch(FENCE)], ids=["python", "fence"])
def test_spawn_hook_writes_handoff_and_names_it_in_prompt(repo, argv):
    """Setup: a spawn_task payload. Action: spawn-hook. Assert: one handoff
    file with the task and the work state; the prompt ends with its path."""
    p = _run("spawn-hook", repo, payload=_spawn_payload(repo), argv=argv)
    assert p.returncode == 0
    (doc,) = _spawn_dir(repo).glob("*-fix-stale-readme-badge.md")
    text = doc.read_text(encoding="utf-8")
    assert "## Prompt\nFix the README badge → now." in text
    assert "- main -> base: ahead 1" in text
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    updated = out["updatedInput"]
    assert updated["title"] == "Fix stale README badge"
    assert updated["tldr"] == "The badge is stale."
    assert updated["prompt"].startswith("Fix the README badge → now.\n\nHandoff: read ")
    assert updated["prompt"].rstrip().endswith("first.")
    assert doc.as_posix() in updated["prompt"].replace("\\", "/")


def test_spawn_hook_skips_prompt_that_names_a_handoff(repo):
    """A prompt that already names a spawn handoff gets no second file."""
    prompt = "Do it.\n\nHandoff: read C:/x/notes/handoffs/spawn/a.md first."
    p = _run("spawn-hook", repo, payload=_spawn_payload(repo, prompt=prompt))
    assert p.returncode == 0
    assert p.stdout == ""
    assert not _spawn_dir(repo).exists()


def test_spawn_cli_writes_handoff_and_prints_line(repo):
    """The orchestrator's command: prompt on stdin, the handoff line on stdout."""
    p = subprocess.run(
        [PYTHON, str(HOOK_PY), "spawn", "--title", "Add a test", "--prompt-file", "-"],
        input="Write the missing test → now.\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
    )
    assert p.returncode == 0, p.stderr
    (doc,) = _spawn_dir(repo).glob("*-add-a-test.md")
    assert "## Prompt\nWrite the missing test → now." in doc.read_text(encoding="utf-8")
    assert p.stdout.startswith("Handoff: read ")
    assert doc.as_posix() in p.stdout.replace("\\", "/")


def test_spawn_handoff_is_not_the_newest_handwritten_handoff(repo):
    """A spawn doc belongs to the child session; compaction must not show it."""
    _notes(repo, "handoffs", 1, prefix="h")
    _run("spawn-hook", repo, payload=_spawn_payload(repo))
    assert _run("snapshot", repo).returncode == 0
    assert "- notes/handoffs/h000.md" in _state(repo).read_text(encoding="utf-8")


@pytest.fixture
def origin(repo, tmp_path):
    """A local bare clone of `repo` as its `origin`: no network. No fetch yet,
    so `origin/main` exists only after the hook fetches."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    return bare


@pytest.mark.parametrize("argv", [None, launch(FENCE)], ids=["python", "fence"])
def test_startup_reports_behind_count_and_pr_titles(repo, origin, tmp_path, argv):
    """Setup: the primary on origin/main, a linked worktree one commit behind,
    a fake gh. Action: startup in the worktree. Assert: the fetch ran, the
    count is for the worktree HEAD, PR titles show without check calls."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt), "base")
    path = os.pathsep.join([str(_fake_gh(tmp_path)), os.environ["PATH"]])
    p = _run("startup", wt, env=dict(os.environ, PATH=path), argv=argv)
    assert p.returncode == 0
    assert "git fetch failed" not in p.stdout
    assert "- This checkout is 1 commit behind origin/main." in p.stdout
    assert "- #7 Add widget [widget] draft" in p.stdout
    assert "checks:" not in p.stdout


def test_startup_degrades_to_a_note_when_fetch_fails(repo, origin, tmp_path):
    """Setup: one good fetch, then an origin URL that does not exist. Action:
    startup. Assert: exit 0, a fetch note, and the count from the last fetch."""
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    _git(repo, "reset", "-q", "--hard", "base")
    p = _run("startup", repo)
    assert p.returncode == 0
    assert "- git fetch failed or timed out." in p.stdout
    assert "- This checkout is 1 commit behind origin/main." in p.stdout


def test_startup_stops_a_slow_fetch_at_its_timeout(repo, origin):
    """Setup: one good fetch, then an origin whose transport sleeps 30 s in a
    child of git, as git-remote-https does on a slow network. Action: startup.
    Assert: the run ends inside the 15 s hook timeout, with the note and the count."""
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "config", "protocol.ext.allow", "always")
    _git(repo, "remote", "set-url", "origin", "ext::sh -c sleep% 30")
    _git(repo, "reset", "-q", "--hard", "base")
    t = time.monotonic()
    p = _run("startup", repo)
    assert time.monotonic() - t < HOOK_TIMEOUT_S
    assert "- git fetch failed or timed out." in p.stdout
    assert "- This checkout is 1 commit behind origin/main." in p.stdout


def test_startup_lists_prs_while_a_slow_fetch_runs(repo, origin, tmp_path):
    """Setup: a fetch that sleeps 30 s and a `gh pr list` that takes 6 s.
    Action: startup. Assert: the PR titles show. One after the other, the
    fetch timeout leaves `gh` too little of the 10 s budget."""
    _git(repo, "config", "protocol.ext.allow", "always")
    _git(repo, "remote", "set-url", "origin", "ext::sh -c sleep% 30")
    path = os.pathsep.join([str(_fake_gh(tmp_path, list_delay_s=6)), os.environ["PATH"]])
    p = _run("startup", repo, env=dict(os.environ, PATH=path))
    assert "- git fetch failed or timed out." in p.stdout
    assert "- #7 Add widget [widget] draft" in p.stdout


def test_startup_fetch_cannot_prompt_for_credentials(repo, origin, tmp_path):
    """Setup: an origin whose transport writes its environment to a file.
    Action: startup. Assert: git and Git Credential Manager may not prompt,
    because a prompt at session start waits for a person."""
    log = (tmp_path / "env.txt").as_posix()
    _git(repo, "config", "protocol.ext.allow", "always")
    _git(repo, "remote", "set-url", "origin", "ext::sh -c env% >% " + log)
    assert _run("startup", repo).returncode == 0
    env = Path(log).read_text().splitlines()
    assert "GIT_TERMINAL_PROMPT=0" in env
    assert "GCM_INTERACTIVE=never" in env


def test_startup_fetch_updates_origin_main_without_a_fetch_mapping(repo, origin):
    """Setup: a fetch config that maps only `base`, as a single-branch clone
    has. Action: startup. Assert: the fetch still writes origin/main."""
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/base:refs/remotes/origin/base")
    _git(repo, "reset", "-q", "--hard", "base")
    p = _run("startup", repo)
    assert "- This checkout is 1 commit behind origin/main." in p.stdout


def test_startup_prints_nothing_outside_a_git_tree(tmp_path):
    """A session started outside git: exit 0 and no output."""
    plain = tmp_path / "plain"
    plain.mkdir()
    p = _run("startup", plain)
    assert p.returncode == 0
    assert p.stdout == ""

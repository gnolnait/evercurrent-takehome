"""The guard hook's settings.json command must fail CLOSED when guard_runner
is missing, and must never run a runner from anywhere but the project dir.

The `guard_runner` bash wrapper coerces a broken python child to exit 2, but it
cannot defend against its own absence: when the primary checkout is mid-update
(a peer session switching branches), `/bin/sh` exits 127 on the missing file
and Claude Code treats every exit other than 2 as non-blocking — the whole
guard layer silently drops out. The one artifact read in order to fire the hook
at all is the command string in `.claude/settings.json`, so the fence lives
there: run the runner from `CLAUDE_PROJECT_DIR`, coerce any exit outside
{0, 2} to a block, and block when it is missing.

The runner path is never taken from the current directory. An earlier revision
fell back to the cwd repo's copy when the primary's was absent, which allowed
an untrusted directory to supply the guard (see the two `cannot_supply` tests).

These tests execute the command string exactly as Claude Code does (`/bin/sh
-c`; Git Bash's `sh` on Windows), so they validate the artifact the consumer reads, not a copy of it.

Run: pytest .claude/hooks/test_guard_launch_fence.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from hooktest_support import IS_WINDOWS, sh_exe

SETTINGS = Path(__file__).parent.parent / "settings.json"


def guard_commands() -> list[str]:
    """The guard commands from the exact matcher groups the guards rely on."""
    hooks = json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"]
    bash = [
        h["command"]
        for entry in hooks
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
        if "guard_runner" in h["command"]
    ]
    mcp = [
        h["command"]
        for entry in hooks
        if "merge_pull_request" in entry.get("matcher", "")
        for h in entry["hooks"]
        if "guard_runner" in h["command"]
    ]
    assert bash and mcp
    return bash + mcp


# `/bin/sh` is the shell Claude Code uses. It is bash on macOS and dash on the
# Linux CI runner, and the two differ where this fence lives, so the portable
# subset is what has to hold. `SHELLS` adds an explicit dash leg on machines
# that have it, giving macOS the dialect CI will use.
# On Windows the hook shell is Git Bash's sh.exe (resolved by hooktest_support).
SH = sh_exe()
SHELLS = [SH] + (["/bin/dash"] if Path("/bin/dash").exists() else [])


def test_set_opts_actually_enables_the_option() -> None:
    """The helper's own mechanism must work in every shell it drives.

    `SHELLOPTS` is bash-only: exporting it enables nothing under dash, so an
    env-var version of `set_opts` made every hostile-shell assertion below
    pass without the option on — vacuously green exactly where `/bin/sh` is
    dash (the CI runner).
    """
    for shell in SHELLS:
        probe = subprocess.run(
            [shell, "-c", "set -e; false; echo REACHED"],
            capture_output=True,
            text=True,
        )
        assert "REACHED" not in probe.stdout, f"{shell} ignored set -e"


def run_fence(
    command: str,
    project_dir: Path | None,
    cwd: Path,
    payload: str = "{}",
    path: str | None = None,
    set_opts: str | None = None,
    extra_env: dict[str, str] | None = None,
    shell: str = SH,
) -> subprocess.CompletedProcess[str]:
    """Run the settings.json command the way Claude Code does: `/bin/sh -c`.

    `set_opts` turns on shell options (`"e"`, `"u"`) by prefixing `set -<opts>`
    to the command, NOT by exporting `SHELLOPTS` — that variable is a bash
    mechanism which dash ignores outright, so an env-var version of this
    helper silently tested nothing wherever `/bin/sh` is dash (the CI runner).
    """
    if set_opts:
        command = f"set -{set_opts}; {command}"
    if path is None:
        # Windows: the MSYS runtime needs the inherited PATH (and SYSTEMROOT)
        # to start at all; a bare "/usr/bin:/bin" is a POSIX-only narrowing.
        path = os.environ["PATH"] if IS_WINDOWS else "/usr/bin:/bin"
    env = {"PATH": path}
    if IS_WINDOWS:
        for key in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [shell, "-c", command],
        input=payload,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def stub(path: Path, body: str) -> None:
    """Write an executable guard_runner stub at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    """The project repo — the only place the fence may take the runner from."""
    p = tmp_path / "primary"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    return p


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """An attacker-writable directory holding a permissive runner stub."""
    d = tmp_path / "elsewhere"
    stub(d / ".claude" / "hooks" / "guard_runner", "exit 0")
    return d


def test_both_guard_hooks_share_one_fenced_command() -> None:
    """Both matchers launch through the same fail-closed fence."""
    commands = guard_commands()
    assert len(commands) == 2
    assert len(set(commands)) == 1
    assert "exit 2" in commands[0]


def test_the_runner_runs_and_its_verdict_passes_through(primary: Path) -> None:
    """The happy path: the project dir's runner owns the verdict."""
    (command,) = set(guard_commands())
    stub(primary / ".claude" / "hooks" / "guard_runner", "exit 0")
    assert run_fence(command, project_dir=primary, cwd=primary).returncode == 0
    stub(primary / ".claude" / "hooks" / "guard_runner", "exit 2")
    assert run_fence(command, project_dir=primary, cwd=primary).returncode == 2


def test_missing_runner_fails_closed(primary: Path, elsewhere: Path) -> None:
    """The incident: the runner is absent mid-checkout — block, don't skip."""
    (command,) = set(guard_commands())
    result = run_fence(command, project_dir=primary, cwd=elsewhere)
    assert result.returncode == 2
    assert "failing closed" in result.stderr


def test_the_current_directory_cannot_supply_the_runner(
    primary: Path, elsewhere: Path
) -> None:
    """A runner sitting in the cwd must never stand in for the project's."""
    (command,) = set(guard_commands())
    result = run_fence(command, project_dir=primary, cwd=elsewhere)
    assert result.returncode == 2, "a cwd runner was executed"


def test_a_crafted_gitfile_cannot_supply_the_runner(
    tmp_path: Path, primary: Path, elsewhere: Path
) -> None:
    """Regression: a `.git` gitfile pointing at the project's git dir used to
    pass a `--git-common-dir` equality check while `--show-toplevel` resolved
    to the attacker's directory, so the fence ran the attacker's runner."""
    (command,) = set(guard_commands())
    (elsewhere / ".git").write_text(f"gitdir: {primary}/.git\n")
    result = run_fence(command, project_dir=primary, cwd=elsewhere)
    assert result.returncode == 2, "a crafted gitfile supplied the runner"
    assert "failing closed" in result.stderr


def test_inherited_git_env_cannot_supply_the_runner(
    primary: Path, elsewhere: Path
) -> None:
    """The env-var sibling of the gitfile vector: GIT_DIR/GIT_WORK_TREE must
    not influence which runner the fence launches."""
    (command,) = set(guard_commands())
    result = run_fence(
        command,
        project_dir=primary,
        cwd=elsewhere,
        extra_env={"GIT_DIR": str(primary / ".git"), "GIT_WORK_TREE": str(elsewhere)},
    )
    assert result.returncode == 2


def test_unset_project_dir_fails_closed(primary: Path) -> None:
    """CLAUDE_PROJECT_DIR unset: nothing to launch — block."""
    (command,) = set(guard_commands())
    stub(primary / ".claude" / "hooks" / "guard_runner", "exit 0")
    assert run_fence(command, project_dir=None, cwd=primary).returncode == 2
    # An inherited nounset must not abort the fence on the unset var — that
    # exit would be 127, which Claude Code reads as non-blocking (fail-open).
    for shell in SHELLS:
        result = run_fence(
            command, project_dir=None, cwd=primary, set_opts="u", shell=shell
        )
        assert result.returncode == 2, shell
        assert "failing closed" in result.stderr


@pytest.mark.skipif(
    IS_WINDOWS,
    reason="Windows has no exec bit: MSYS treats any file starting with '#!' as "
    "executable, so chmod 0o644 cannot make the stub non-executable",
)
def test_non_executable_runner_fails_closed(primary: Path) -> None:
    """A present but non-executable runner is 'missing', not launchable."""
    (command,) = set(guard_commands())
    target = primary / ".claude" / "hooks" / "guard_runner"
    stub(target, "exit 0")
    target.chmod(0o644)
    assert run_fence(command, project_dir=primary, cwd=primary).returncode == 2


def test_a_crashing_runner_is_coerced_to_a_block(primary: Path) -> None:
    """A runner exiting outside {0, 2} — including sh's 126/127 when the file
    vanishes between the -x probe and the launch — must read as a block."""
    (command,) = set(guard_commands())
    for crash_rc in ("1", "126", "127"):
        stub(primary / ".claude" / "hooks" / "guard_runner", f"exit {crash_rc}")
        result = run_fence(command, project_dir=primary, cwd=primary)
        assert result.returncode == 2
        assert "failing closed" in result.stderr
        # An inherited errexit must not bail the fence between the crashing
        # child and the rc coercion — that would re-open fail-open.
        for shell in SHELLS:
            result = run_fence(
                command, project_dir=primary, cwd=primary, set_opts="e", shell=shell
            )
            assert result.returncode == 2, shell


def test_closed_stderr_cannot_break_the_block(primary: Path) -> None:
    """A BLOCKED echo failing on a closed stderr under errexit must not turn
    the block into a non-blocking exit 1.

    Both echoes need this. The missing-runner branch and the rc-coercion
    branch each write to stderr, so each needs its own `|| :`; covering only
    the first leaves a deletion in the second invisible to this suite. The
    pinned command string in `test_settings_guards.py` is not a substitute —
    an intentional edit updates that pin in lockstep, so only a behavioural
    assertion catches the semantic regression.
    """
    (command,) = set(guard_commands())
    runner = primary / ".claude" / "hooks" / "guard_runner"
    for shell in SHELLS:
        # Missing-runner branch: nothing stubbed yet.
        result = run_fence(
            f"exec 2>&-; {command}",
            project_dir=primary,
            cwd=primary,
            set_opts="e",
            shell=shell,
        )
        assert result.returncode == 2, f"missing branch, {shell}"
        # rc-coercion branch: a runner whose exit is outside {0, 2}.
        stub(runner, "exit 1")
        result = run_fence(
            f"exec 2>&-; {command}",
            project_dir=primary,
            cwd=primary,
            set_opts="e",
            shell=shell,
        )
        assert result.returncode == 2, f"coercion branch, {shell}"
        runner.unlink()


def test_stdin_payload_reaches_the_runner(primary: Path) -> None:
    """The fence hands the hook payload through to the runner unchanged."""
    (command,) = set(guard_commands())
    stub(
        primary / ".claude" / "hooks" / "guard_runner",
        "grep -q sentinel-payload && exit 2 || exit 0",
    )
    result = run_fence(
        command, project_dir=primary, cwd=primary, payload='{"x": "sentinel-payload"}'
    )
    assert result.returncode == 2

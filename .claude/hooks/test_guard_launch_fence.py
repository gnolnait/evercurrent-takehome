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

The prove-test gate's command is the same fence around a different runner, so
every behaviour test runs against both. Each fence has two branches: with
`CLAUDE_HOOK_PYTHON` set to an interpreter path it runs the runner's python
file with that interpreter directly, which skips the bash wrapper's launch
cost; otherwise it runs the bash wrapper. The shared behaviour tests run on
both branches; a few cases belong to one branch only (the exec bit to the
wrapper; the interpreter value to the direct branch). The never-blocking
track-edit launcher has the same two branches and its own test.

These tests execute the command string exactly as Claude Code does (`/bin/sh
-c`; Git Bash's `sh` on Windows), so they validate the artifact the consumer reads, not a copy of it.

Run: pytest .claude/hooks/test_guard_launch_fence.py
"""

import dataclasses
import functools
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from hooktest_support import IS_WINDOWS, PYTHON, sh_exe

SETTINGS = Path(__file__).parent.parent / "settings.json"
PROJECT = SETTINGS.parent.parent


@functools.cache
def _hooks() -> dict:
    return json.loads(SETTINGS.read_text())["hooks"]


def _commands(event: str, group_ok: Callable[[str], bool], needle: str) -> list[str]:
    """The commands containing `needle` in the `event` groups whose matcher
    passes `group_ok`."""
    return [
        h["command"]
        for entry in _hooks()[event]
        if group_ok(entry.get("matcher", ""))
        for h in entry["hooks"]
        if needle in h["command"]
    ]


def guard_commands() -> list[str]:
    """The guard commands from the exact matcher groups the guards rely on."""
    bash = _commands("PreToolUse", lambda m: m == "Bash", "guard_runner")
    mcp = _commands("PreToolUse", lambda m: "merge_pull_request" in m, "guard_runner")
    assert bash and mcp
    return bash + mcp


def gate_command() -> str:
    """The prove-test gate's command from the Bash matcher group."""
    (command,) = set(
        _commands("PreToolUse", lambda m: m == "Bash", "prove-test-gate-actions")
    )
    return command


def track_command() -> str:
    """The track-edit launcher from the PostToolUse edit-tools group."""
    (command,) = set(
        _commands("PostToolUse", lambda m: "Edit" in m, "prove-test-track-edit")
    )
    return command


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


@dataclass(frozen=True)
class Fence:
    """One blocking fence, on one of its two launch branches."""

    hook: str  # the bash wrapper the fence runs without CLAUDE_HOOK_PYTHON
    script: str  # the python file it runs directly with CLAUDE_HOOK_PYTHON
    branch: str  # "wrapper" or "direct"

    def command(self) -> str:
        return _COMMAND[self.hook]()

    def place(self, project: Path, body: str) -> Path:
        """Put a runner that does `body` where this branch launches it."""
        name = self.hook if self.branch == "wrapper" else self.script
        path = project / ".claude" / "hooks" / name
        stub(path, body)
        return path

    def env(self, **extra: str) -> dict[str, str]:
        # The direct branch runs `$CLAUDE_HOOK_PYTHON <script>`. The test's own
        # shell stands in for python, so one stub body serves both branches.
        env = {"CLAUDE_HOOK_PYTHON": SH} if self.branch == "direct" else {}
        env.update(extra)
        return env

    def run(
        self,
        project_dir: Path | None,
        cwd: Path,
        *,
        prefix: str = "",
        env: dict[str, str] | None = None,
        **kw,
    ) -> subprocess.CompletedProcess[str]:
        """Run this fence's command on this branch; `env` adds to or overrides
        the branch's own variables."""
        return run_fence(
            prefix + self.command(),
            project_dir,
            cwd,
            extra_env=self.env(**(env or {})),
            **kw,
        )


_COMMAND: dict[str, Callable[[], str]] = {
    "guard_runner": lambda: guard_commands()[0],
    "prove-test-gate-actions": gate_command,
}
_RUNNERS = (
    ("guard_runner", "guard_runner.py"),
    ("prove-test-gate-actions", "prove_test_gate_actions.py"),
)
FENCES = [Fence(h, s, b) for h, s in _RUNNERS for b in ("wrapper", "direct")]
WRAPPER = [f for f in FENCES if f.branch == "wrapper"]
DIRECT = [f for f in FENCES if f.branch == "direct"]


def _id(fence: Fence) -> str:
    return f"{fence.hook}-{fence.branch}"


@pytest.fixture(params=FENCES, ids=_id)
def fence(request: pytest.FixtureRequest) -> Fence:
    return request.param


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    """The project repo — the only place the fence may take the runner from."""
    p = tmp_path / "primary"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    return p


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """An attacker-writable directory holding permissive runner stubs, under
    both the wrapper and the script name of every fence."""
    d = tmp_path / "elsewhere"
    for hook, script in _RUNNERS:
        for name in (hook, script):
            stub(d / ".claude" / "hooks" / name, "exit 0")
    return d


def test_both_guard_hooks_share_one_fenced_command() -> None:
    """Both matchers launch through the same fail-closed fence."""
    commands = guard_commands()
    assert len(commands) == 2
    assert len(set(commands)) == 1
    assert "exit 2" in commands[0]


def test_the_runner_runs_and_its_verdict_passes_through(
    primary: Path, fence: Fence
) -> None:
    """The happy path: the project dir's runner owns the verdict. Only this
    branch's runner exists, so an exit 0 proves the branch ran it."""
    for verdict in (0, 2):
        fence.place(primary, f"exit {verdict}")
        assert fence.run(primary, primary).returncode == verdict


def test_missing_runner_fails_closed(
    primary: Path, elsewhere: Path, fence: Fence
) -> None:
    """The incident: the runner is absent mid-checkout — block, don't skip."""
    result = fence.run(primary, elsewhere)
    assert result.returncode == 2
    assert "missing or not executable" in result.stderr


def test_the_current_directory_cannot_supply_the_runner(
    primary: Path, elsewhere: Path, fence: Fence
) -> None:
    """A runner sitting in the cwd must never stand in for the project's."""
    assert fence.run(primary, elsewhere).returncode == 2, "a cwd runner was executed"


def test_a_crafted_gitfile_cannot_supply_the_runner(
    primary: Path, elsewhere: Path, fence: Fence
) -> None:
    """Regression: a `.git` gitfile pointing at the project's git dir used to
    pass a `--git-common-dir` equality check while `--show-toplevel` resolved
    to the attacker's directory, so the fence ran the attacker's runner."""
    (elsewhere / ".git").write_text(f"gitdir: {primary}/.git\n")
    result = fence.run(primary, elsewhere)
    assert result.returncode == 2, "a crafted gitfile supplied the runner"
    assert "failing closed" in result.stderr


def test_inherited_git_env_cannot_supply_the_runner(
    primary: Path, elsewhere: Path, fence: Fence
) -> None:
    """The env-var sibling of the gitfile vector: GIT_DIR/GIT_WORK_TREE must
    not influence which runner the fence launches."""
    git_env = {"GIT_DIR": str(primary / ".git"), "GIT_WORK_TREE": str(elsewhere)}
    assert fence.run(primary, elsewhere, env=git_env).returncode == 2


def test_unset_project_dir_fails_closed(primary: Path, fence: Fence) -> None:
    """CLAUDE_PROJECT_DIR unset: nothing to launch — block."""
    fence.place(primary, "exit 0")
    assert fence.run(None, primary).returncode == 2
    # An inherited nounset must not abort the fence on the unset var — that
    # exit would be 127, which Claude Code reads as non-blocking (fail-open).
    for shell in SHELLS:
        result = fence.run(None, primary, set_opts="u", shell=shell)
        assert result.returncode == 2, shell
        assert "failing closed" in result.stderr


@pytest.mark.skipif(
    IS_WINDOWS,
    reason="Windows has no exec bit: MSYS treats any file starting with '#!' as "
    "executable, so chmod 0o644 cannot make the stub non-executable",
)
@pytest.mark.parametrize("fence", WRAPPER, ids=_id)
def test_non_executable_runner_fails_closed(primary: Path, fence: Fence) -> None:
    """A present but non-executable wrapper is 'missing', not launchable. The
    direct branch hands its script to the interpreter, so it has no exec bit
    to lose."""
    fence.place(primary, "exit 0").chmod(0o644)
    assert fence.run(primary, primary).returncode == 2


@pytest.mark.parametrize("crash_rc", ["1", "126", "127"])
def test_a_crashing_runner_is_coerced_to_a_block(
    primary: Path, fence: Fence, crash_rc: str
) -> None:
    """A runner exiting outside {0, 2} — including sh's 126/127 when the file
    vanishes between the probe and the launch — must read as a block."""
    fence.place(primary, f"exit {crash_rc}")
    result = fence.run(primary, primary)
    assert result.returncode == 2
    # The coercion's own message: a fence that never launched the runner
    # also exits 2, but says "missing".
    assert f"exited {crash_rc}" in result.stderr
    # An inherited errexit must not bail the fence between the crashing
    # child and the rc coercion — that would re-open fail-open.
    for shell in SHELLS:
        result = fence.run(primary, primary, set_opts="e", shell=shell)
        assert result.returncode == 2, shell


def test_closed_stderr_cannot_break_the_block(primary: Path, fence: Fence) -> None:
    """A BLOCKED echo failing on a closed stderr under errexit must not turn
    the block into a non-blocking exit 1.

    Both echoes need this. The missing-runner branch and the rc-coercion
    branch each write to stderr, so each needs its own `|| :`; covering only
    the first leaves a deletion in the second invisible to this suite. The
    pinned command string in `test_settings_guards.py` is not a substitute —
    an intentional edit updates that pin in lockstep, so only a behavioural
    assertion catches the semantic regression.
    """
    closed = {"prefix": "exec 2>&-; ", "set_opts": "e"}
    for shell in SHELLS:
        # Missing-runner branch: nothing stubbed yet.
        result = fence.run(primary, primary, shell=shell, **closed)
        assert result.returncode == 2, f"missing branch, {shell}"
        # rc-coercion branch: a runner whose exit is outside {0, 2}.
        runner = fence.place(primary, "exit 1")
        result = fence.run(primary, primary, shell=shell, **closed)
        assert result.returncode == 2, f"coercion branch, {shell}"
        runner.unlink()


def test_stdin_payload_reaches_the_runner(primary: Path, fence: Fence) -> None:
    """The fence hands the hook payload through to the runner unchanged. The
    control payload must pass, so a launch failure (also 2) cannot pass for
    the sentinel."""
    fence.place(primary, "grep -q sentinel-payload && exit 2 || exit 0")
    for payload, want in (('{"x": "sentinel-payload"}', 2), ('{"x": "other"}', 0)):
        result = fence.run(primary, primary, payload=payload)
        assert result.returncode == want, payload


@pytest.mark.parametrize("fence", DIRECT, ids=_id)
def test_a_missing_interpreter_fails_closed(primary: Path, fence: Fence) -> None:
    """CLAUDE_HOOK_PYTHON names nothing runnable: the shell exits 127, which
    Claude Code would read as non-blocking unless the fence coerces it."""
    fence.place(primary, "exit 0")
    missing = {"CLAUDE_HOOK_PYTHON": str(primary / "no-such-python")}
    result = fence.run(primary, primary, env=missing)
    assert result.returncode == 2
    assert "exited 127" in result.stderr


@pytest.mark.parametrize("builtin", ["exec", "eval"])
@pytest.mark.parametrize("fence", DIRECT, ids=_id)
def test_a_shell_builtin_cannot_stand_in_for_the_interpreter(
    primary: Path, fence: Fence, builtin: str
) -> None:
    """`exec "$s"` would replace the fence's own shell, so a crashing runner's
    exit would skip the coercion. Only a path (a name with a slash) may name
    the interpreter; any other value falls back to the wrapper, absent here."""
    fence.place(primary, "exit 1")
    result = fence.run(primary, primary, env={"CLAUDE_HOOK_PYTHON": builtin})
    assert result.returncode == 2
    assert "missing or not executable" in result.stderr


@pytest.mark.parametrize("fence", DIRECT, ids=_id)
def test_without_its_script_the_direct_branch_runs_the_wrapper(
    primary: Path, fence: Fence
) -> None:
    """CLAUDE_HOOK_PYTHON set but no python file beside the wrapper (a
    project synced from an older kit): the fence runs the wrapper. Its exit 0
    proves that, since a fence that found no runner would block with 2."""
    dataclasses.replace(fence, branch="wrapper").place(primary, "exit 0")
    assert fence.run(primary, primary).returncode == 0


@pytest.mark.parametrize("fence", DIRECT, ids=_id)
def test_with_both_runners_present_the_direct_branch_wins(
    primary: Path, fence: Fence
) -> None:
    """A project holds both files. If the fence preferred the wrapper there,
    every other case would still pass and the direct branch would never run."""
    dataclasses.replace(fence, branch="wrapper").place(primary, "exit 2")
    fence.place(primary, "exit 0")
    assert fence.run(primary, primary).returncode == 0


@pytest.mark.parametrize("branch", ["wrapper", "direct"])
def test_the_track_edit_launcher_writes_a_marker(tmp_path: Path, branch: str) -> None:
    """Track-edit never blocks, so a launcher that silently stops running it
    writes no marker, and the prove-test gate then passes unproven tests. Run
    the project's real launcher and runner on a test-file edit and require
    the marker the gate reads."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    test_file = repo / "tests" / "test_x.py"
    test_file.write_text("def test_x():\n    assert True\n")
    state = tmp_path / "state"
    payload = json.dumps(
        {
            "session_id": "s1",
            "cwd": str(repo),
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(test_file)},
        }
    )
    env = {"PROVE_TEST_STATE_DIR": str(state)}
    if branch == "direct":
        env["CLAUDE_HOOK_PYTHON"] = PYTHON
    result = run_fence(
        track_command(), PROJECT, repo, payload=payload, extra_env=env
    )
    assert result.returncode == 0
    assert list(state.glob("s1-*.json")), result.stderr

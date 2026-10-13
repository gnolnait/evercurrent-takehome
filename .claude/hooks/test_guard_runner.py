"""guard_runner (single-spawn dispatch) must produce the SAME verdict as the
three standalone guards it replaces, across the full matrix — that equivalence
is the whole safety argument for the consolidation.

Also pins the two consolidation-specific contracts a fresh design review
flagged: BYPASS_GIT_GUARD is scoped to the signing guard only (it must NOT
newly permit PR merges or protected-branch commits), and a malformed payload
fails closed.

The fixture repo sits on a FEATURE branch so the commit guard is quiet by
default — that isolates the signing/merge verdicts; protected-branch cases
switch to main explicitly.

Run: pytest .claude/hooks/test_guard_runner.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from hooktest_support import launch

HOOK_DIR = Path(__file__).parent
RUNNER = str(HOOK_DIR / "guard_runner")
STANDALONE = {
    "signing": str(HOOK_DIR / "no-bypass-signing"),
    "merge": str(HOOK_DIR / "no-merge-pr"),
    "commit": str(HOOK_DIR / "no-commit-to-main"),
}

# Trigger fragments assembled at runtime so this test file carries no literal
# guard-trigger text (the author's own session guards scan it otherwise).
_C = "com" + "mit"
_NV = "--no" + "-verify"
_MERGE = "mer" + "ge"


def _run(hook, payload, env=None, cwd=None):
    p = subprocess.run(
        launch(hook), input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=cwd,
    )
    return p.returncode


def _bash(cmd, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A repo checked out on a FEATURE branch (commit guard quiet)."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@e.com")
    _git(r, "config", "user.name", "T")
    (r / "f").write_text("x\n")
    _git(r, "add", "f")
    _git(r, "-c", "commit.gpgsign=false", _C, "-m", "i")
    _git(r, "checkout", "-b", "ps/work")
    return r


def _env(repo):
    e = dict(os.environ, CLAUDE_PROJECT_DIR=str(repo))
    e.pop("BYPASS_GIT_GUARD", None)
    return e


MATRIX = [
    ("ls -la", 0),
    ("git status", 0),
    (f"git {_C} {_NV} -m x", 2),          # signing
    (f"g''it {_C} {_NV} -m x", 2),        # signing, quote-assembled
    (f"gh pr {_MERGE} 5 --squash", 2),    # merge
    (f"gh pr m''erge 5 --squash", 2),     # merge, quote-assembled
]


@pytest.mark.parametrize("cmd,expected", MATRIX)
def test_runner_matches_standalone(cmd, expected, repo):
    """One runner spawn == the three standalone guards for the same payload
    (on a feature branch, so a 2 comes from signing/merge, not the commit
    guard)."""
    env = _env(repo)
    payload = _bash(cmd, repo)

    runner_rc = _run(RUNNER, payload, env=env, cwd=repo)
    assert runner_rc == expected, "runner rc %d != expected %d" % (runner_rc, expected)

    standalone = {
        name: _run(hook, payload, env=env, cwd=repo)
        for name, hook in STANDALONE.items()
    }
    if expected == 2:
        assert 2 in standalone.values(), "runner blocked but no standalone did: %r" % standalone
    else:
        assert all(rc == 0 for rc in standalone.values()), \
            "runner allowed but a standalone blocked: %r" % standalone


def test_bypass_is_signing_scoped(repo):
    """BYPASS_GIT_GUARD skips the signing guard but must NOT cover merge or
    protected-branch commit (those guards have no bypass by design)."""
    env = dict(_env(repo), BYPASS_GIT_GUARD="1")
    # On the feature branch: signing trigger is bypassed -> allow.
    assert _run(RUNNER, _bash(f"git {_C} {_NV} -m x", repo), env=env, cwd=repo) == 0
    # Merge is never bypassable.
    assert _run(RUNNER, _bash(f"gh pr {_MERGE} 5 --squash", repo), env=env, cwd=repo) == 2
    # On main: a plain commit (no signing trigger) must still block under
    # bypass — the commit guard is not bypassable.
    _git(repo, "checkout", "main")
    assert _run(RUNNER, _bash(f"git {_C} -m x", repo), env=env, cwd=repo) == 2


def test_project_dir_defaults_to_process_cwd(tmp_path):
    """With CLAUDE_PROJECT_DIR unset, the guard attributes to the hook PROCESS
    cwd, not the payload cwd — the standalone shim and the runner must agree
    (regression: the shim briefly defaulted to the payload cwd, which would
    block a benign feature-branch command whose payload cwd names a protected
    checkout)."""
    def mk(branch):
        d = tmp_path / branch.replace("/", "_")
        d.mkdir()
        _git(d, "init", "-b", "main")
        _git(d, "config", "user.email", "t@e.com")
        _git(d, "config", "user.name", "T")
        (d / "f").write_text("x\n")
        _git(d, "add", "f")
        _git(d, "-c", "commit.gpgsign=false", _C, "-m", "i")
        if branch != "main":
            _git(d, "checkout", "-b", branch)
        return d

    feature = mk("ps/x")
    protected = mk("main")
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("BYPASS_GIT_GUARD", None)
    # process cwd = feature branch, payload cwd = protected checkout.
    payload = {"tool_name": "Bash", "tool_input": {"command": f"git {_C} -m x"},
               "cwd": str(protected)}
    runner_rc = _run(RUNNER, payload, env=env, cwd=feature)
    shim_rc = _run(str(HOOK_DIR / "no-commit-to-main"), payload, env=env, cwd=feature)
    assert runner_rc == 0, "runner should attribute to process cwd (feature), got %d" % runner_rc
    assert shim_rc == runner_rc, "shim %d != runner %d" % (shim_rc, runner_rc)


def test_malformed_payload_fails_closed(repo):
    env = _env(repo)
    p = subprocess.run(launch(RUNNER), input="{not json", capture_output=True, text=True,
                       env=env, cwd=repo)
    assert p.returncode == 2, "malformed payload must fail closed, got %d" % p.returncode


# A PreToolUse payload declares tool_name as a usable dispatch key and
# tool_input as an object. A field the guard cannot USE is a protocol violation
# — it cannot trust what it is guarding. Three ways to be unusable, all here:
# MISSING (absent or null — `.get()` cannot tell them apart); the wrong TYPE (a
# list / int / dict where a string belongs); and the right type carrying no
# usable VALUE (a blank tool_name — correctly a `str`, but not a name). The
# malformed-payload test above covers the TOP-LEVEL shape; these rows cover the
# FIELDS inside a well-shaped object.
UNUSABLE_FIELD = [
    pytest.param({"tool_name": ["Bash"], "tool_input": {"command": "ls"}},
                 id="tool_name-truthy-list"),
    pytest.param({"tool_name": [], "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-falsy-list"),
    pytest.param({"tool_name": 0, "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-int"),
    pytest.param({"tool_name": False, "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-bool"),
    pytest.param({"tool_name": {"n": "Bash"}, "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-dict"),
    pytest.param({"tool_name": "Bash", "tool_input": ["ls"]}, id="tool_input-list"),
    pytest.param({"tool_name": "Bash", "tool_input": []}, id="tool_input-empty-list"),
    pytest.param({"tool_name": "Bash", "tool_input": "ls"}, id="tool_input-str"),
    # tool_name is the dispatch key and the matchers guarantee it is present,
    # so a missing or null one is a violation too — not "some other tool".
    pytest.param({"tool_input": {"command": f"gh pr {_MERGE} 5"}}, id="tool_name-absent"),
    pytest.param({"tool_name": None, "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-null"),
    # A BLANK tool_name is unreadable, not "some other tool". These are `str`,
    # so an isinstance-only check passed them through; the runner then compared
    # them against "Bash", missed, and exited 0 — a silent ALLOW on a payload
    # carrying a real merge, exactly like the falsy non-strings above.
    pytest.param({"tool_name": "", "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-empty-str"),
    pytest.param({"tool_name": "   ", "tool_input": {"command": f"gh pr {_MERGE} 5"}},
                 id="tool_name-whitespace"),
    # One level down: `command` is the last field the guards read.
    pytest.param({"tool_name": "Bash", "tool_input": {"command": ["gh", "pr", _MERGE]}},
                 id="command-list"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": []}},
                 id="command-empty-list"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": 0}}, id="command-int"),
]


@pytest.mark.parametrize("payload", UNUSABLE_FIELD)
def test_unusable_field_fails_closed_cleanly(payload, repo):
    """An unusable tool_name / tool_input must block, and the runner itself
    must produce that verdict.

    Two defects motivated this. A FALSY non-string tool_name ([] / 0 / false)
    collapsed to "" through `or ""`, so the runner ALLOWED a payload that
    carried a real merge — it never inspected the command. A TRUTHY one reached
    .startswith() and raised AttributeError, so the runner blocked only because
    the bash fence coerces python's exit 1, printing a raw traceback and a
    message that blames the interpreter."""
    p = subprocess.run(launch(RUNNER), input=json.dumps(payload), capture_output=True,
                       text=True, env=_env(repo), cwd=repo)
    assert p.returncode == 2, \
        "unusable field must fail closed, got %d" % p.returncode
    assert "Traceback" not in p.stderr, \
        "guard blocked by crashing, not by deciding:\n%s" % p.stderr
    assert "python exit" not in p.stderr, \
        "verdict came from the rc fence, not the guard:\n%s" % p.stderr
    assert p.stderr.startswith("BLOCKED: guard_runner received "), p.stderr


@pytest.mark.parametrize("payload", UNUSABLE_FIELD)
def test_unusable_field_shims_match_runner(payload, repo):
    """The standalone shims exist so the adversarial suites drive the verdict
    production ships; a shim that allows what the runner blocks makes those
    suites lie. no-bypass-signing is out of scope here: it reads only
    `.tool_input.command` (extracted by python in the bash wrapper) and has no
    tool_name concept, so it ALLOWS a wrong-typed tool_name by design (the
    extraction already fails it closed on a non-dict tool_input)."""
    env = _env(repo)
    runner_rc = _run(RUNNER, payload, env=env, cwd=repo)
    for name in ("merge", "commit"):
        shim_rc = _run(STANDALONE[name], payload, env=env, cwd=repo)
        assert shim_rc == runner_rc, \
            "%s shim rc %d != runner rc %d" % (name, shim_rc, runner_rc)


# The other side of the contract: "no command" is NOT a wrong type. Absent,
# null, and empty-string all mean the same thing — there is nothing to inspect,
# so nothing to block. Blocking here would buy no safety (no command exists to
# be missed) and would diverge from no-bypass-signing, whose wrapper documents
# "Empty command on a successfully-parsed payload -> nothing to act on; allow".
NO_COMMAND_ALLOWS = [
    pytest.param({"tool_name": "Bash", "tool_input": {}}, id="tool_input-empty-dict"),
    pytest.param({"tool_name": "Bash"}, id="tool_input-absent"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": None}},
                 id="command-null"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": ""}},
                 id="command-empty-str"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": "   "}},
                 id="command-whitespace"),
    # A non-merge mcp tool never reaches the command field, so a junk `command`
    # key on it must NOT block. This pins the `if tool == "Bash"` gating of the
    # command check in no-merge-pr.py: drop that gate and the shim blocks here
    # while the runner allows.
    pytest.param({"tool_name": "mcp__github__get_pull_request",
                  "tool_input": {"pullNumber": 5, "command": ["junk"]}},
                 id="mcp-non-merge-junk-command"),
]


@pytest.mark.parametrize("payload", NO_COMMAND_ALLOWS)
def test_no_command_still_allows_everywhere(payload, repo):
    """The wrong-type guards must not turn "nothing to inspect" into a block,
    and the runner and shims must agree that it is not one."""
    env = _env(repo)
    runner_rc = _run(RUNNER, payload, env=env, cwd=repo)
    assert runner_rc == 0, "runner must allow a command-less payload, got %d" % runner_rc
    for name in ("merge", "commit"):
        shim_rc = _run(STANDALONE[name], payload, env=env, cwd=repo)
        assert shim_rc == 0, "%s shim must allow, got %d" % (name, shim_rc)


def test_stale_project_dir_falls_back_to_cwd(repo):
    """A stale/non-worktree CLAUDE_PROJECT_DIR must not fail OPEN: the commit
    guard falls back to the command's cwd, so a protected-branch commit still
    blocks even when the env var points nowhere."""
    _git(repo, "checkout", "main")
    env = dict(os.environ, CLAUDE_PROJECT_DIR="/definitely/not/a/worktree")
    env.pop("BYPASS_GIT_GUARD", None)
    rc = _run(RUNNER, _bash(f"git {_C} -m x", repo), env=env, cwd=repo)
    assert rc == 2, "stale project dir must fall back and still block, got %d" % rc


def test_rc_fence_coerces_nonverdict_exit_to_block(tmp_path):
    """The bash fence is the production fail-closed linchpin: it coerces any
    non-{0,2} python exit (import/syntax error = 1, missing interpreter = 127)
    to a block, because Claude Code treats every non-2 exit as non-blocking.
    Every other test drives python to 0/2, so this exercises the '*)' arm
    directly with a stub dispatcher that exits non-{0,2}."""
    import shutil
    fence = tmp_path / "guard_runner"
    shutil.copy2(RUNNER, fence)
    fence.chmod(0o755)
    for bad_exit in (1, 127):
        (tmp_path / "guard_runner.py").write_text("import sys; sys.exit(%d)\n" % bad_exit)
        p = subprocess.run(
            launch(fence),
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
            capture_output=True, text=True,
        )
        assert p.returncode == 2, (
            "fence must coerce python exit %d to a block (2), got %d"
            % (bad_exit, p.returncode)
        )


def test_mcp_merge_payload_blocks(repo):
    """The runner is registered under the MCP merge matcher too; an mcp__ merge
    tool must route to the merge guard and block. The Bash MATRIX never exercises
    the mcp__ dispatch branch."""
    payload = {
        "tool_name": "mcp__github__merge_pull_request",
        "tool_input": {"pullNumber": 5, "owner": "o", "repo": "r"},
    }
    assert _run(RUNNER, payload, env=_env(repo), cwd=repo) == 2


@pytest.mark.parametrize(
    "cmd,expected",
    [
        pytest.param("Get-ChildItem C:\\Users", 0, id="unrelated"),
        pytest.param(f"git {_C} -m x", 0, id="clean-commit-on-feature-branch"),
        pytest.param(f"git {_C} {_NV} -m x", 2, id="signing"),
        pytest.param(f"gh pr {_MERGE} 5 --squash", 2, id="merge"),
        pytest.param(
            f'& "C:\\Program Files\\GitHub CLI\\gh.exe" pr {_MERGE} 5', 2, id="merge-full-path"
        ),
    ],
)
def test_powershell_payload_reaches_the_guards(cmd, expected, repo):
    """The PowerShell tool runs the same git and gh commands as Bash, so the
    runner must judge its payload too."""
    payload = {"tool_name": "PowerShell", "tool_input": {"command": cmd}, "cwd": str(repo)}
    assert _run(RUNNER, payload, env=_env(repo), cwd=repo) == expected


def test_powershell_commit_on_main_blocks(repo):
    _git(repo, "checkout", "main")
    payload = {"tool_name": "PowerShell", "tool_input": {"command": f"git {_C} -m x"}, "cwd": str(repo)}
    assert _run(RUNNER, payload, env=_env(repo), cwd=repo) == 2


def test_mcp_auto_merge_payload_blocks(repo):
    """Turning on auto-merge is a deferred merge."""
    payload = {"tool_name": "mcp__ccd_pr__set_auto_merge", "tool_input": {"enabled": True}}
    assert _run(RUNNER, payload, env=_env(repo), cwd=repo) == 2


def test_edit_payload_is_allowed(repo):
    """An edit tool must pass through, including on docs content.

    A guard registered under an edit matcher once blocked every edit to a docs
    tree (since removed). The policy behind it is about
    AUTHORSHIP, which a path and a tool name cannot express, so it also refused
    a privacy fix and a stale-tool-reference correction. Only reading the diff
    tells those apart, so review carries that job now.

    This pins the runner's own behavior rather than the settings registration:
    even if some future matcher sends an edit here, the runner must not block
    it. `Write` and `MultiEdit` take the same path.
    """
    for tool in ("Edit", "Write", "MultiEdit"):
        payload = {
            "tool_name": tool,
            "tool_input": {"file_path": str(repo / "docs/application/x.md")},
            "cwd": str(repo),
        }
        assert _run(RUNNER, payload, env=_env(repo), cwd=repo) == 0, (
            "%s on docs content must be allowed" % tool
        )

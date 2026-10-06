"""prove_test_gate_actions: markers + payload in, verdict out.

Run: cd .claude/hooks && python -m pytest -q test_prove_test_gate_actions.py
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hooktest_support import PYTHON, launch
from prove_test_gate_actions import is_allowed
from prove_test_track_edit import safe_session, sha256_file

HOOK_DIR = Path(__file__).parent
GATE_PY = HOOK_DIR / "prove_test_gate_actions.py"
FENCE = HOOK_DIR / "prove-test-gate-actions"
SESSION = "sess-1"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PROVE_TEST_STATE_DIR", str(d))
    monkeypatch.setenv("PROVE_TEST_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.delenv("BYPASS_PROVE_TEST", raising=False)
    return d


def _pending_test(repo, state, name="test_math.py", session=SESSION, marker_repo=None):
    """Write a test file plus the marker track-edit would have written."""
    t = repo / "tests" / name
    t.write_text("def test_adds():\n    assert 1 + 1 == 2\n")
    sha = sha256_file(str(t))
    marker = state / ("%s-%s.json" % (safe_session(session), sha))
    marker.write_text(
        json.dumps(
            {
                "path": str(t),
                "sha256": sha,
                "repo": str(marker_repo or repo),
                "session_id": session,
                "edited_at": "2026-01-01T00:00:00Z",
            }
        )
    )
    return t, marker


def _gate(command, cwd, session=SESSION, argv=None, tool="Bash"):
    payload = {
        "tool_name": tool,
        "session_id": session,
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }
    return subprocess.run(
        argv or [PYTHON, str(GATE_PY)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=os.environ.copy(),
    )


def _denied(p):
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return False
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out["permissionDecision"] == "deny"


def test_no_markers_allows(repo, state):
    assert not _denied(_gate("git push", repo))


def test_pending_marker_denies_non_trivial_action(repo, state):
    t, _m = _pending_test(repo, state)
    p = _gate("git commit -m x", repo)
    assert _denied(p)
    reason = json.loads(p.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert str(t) in reason
    assert sha256_file(str(t)) in reason
    assert "prove-test.py" in reason


@pytest.mark.parametrize("suffix", [".json", ".skip.json"])
def test_proof_or_skip_clears_marker_and_allows(repo, state, suffix):
    t, marker = _pending_test(repo, state)
    proof = repo / ".test-proof"
    proof.mkdir()
    (proof / (sha256_file(str(t)) + suffix)).write_text("{}")
    assert not _denied(_gate("git push", repo))
    assert not marker.exists()


def test_proof_for_stale_sha_does_not_clear(repo, state):
    """Editing the test after proving it re-pends: the proof is keyed on the
    CURRENT content sha."""
    t, marker = _pending_test(repo, state)
    proof = repo / ".test-proof"
    proof.mkdir()
    (proof / (sha256_file(str(t)) + ".json")).write_text("{}")
    t.write_text("def test_adds():\n    assert 2 + 2 == 4\n")
    assert _denied(_gate("git push", repo))
    assert marker.exists()


def test_deleted_test_file_clears_marker(repo, state):
    t, marker = _pending_test(repo, state)
    t.unlink()
    assert not _denied(_gate("git push", repo))
    assert not marker.exists()


def test_unreadable_marker_is_dropped(repo, state):
    bad = state / ("%s-deadbeef.json" % SESSION)
    bad.write_text("{not json")
    assert not _denied(_gate("git push", repo))
    assert not bad.exists()


def test_other_session_markers_do_not_gate(repo, state):
    _pending_test(repo, state, session="someone-else")
    assert not _denied(_gate("git push", repo))


def test_other_repo_markers_do_not_gate(repo, state, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    _pending_test(repo, state, marker_repo=other)
    assert not _denied(_gate("git push", repo))


def test_non_bash_tool_is_not_gated(repo, state):
    _pending_test(repo, state)
    assert not _denied(_gate("git push", repo, tool="Edit"))


def test_bypass_allows_and_logs(repo, state, monkeypatch, tmp_path):
    t, _m = _pending_test(repo, state)
    monkeypatch.setenv("BYPASS_PROVE_TEST", "1")
    assert not _denied(_gate("git push", repo))
    (line,) = (tmp_path / "bypass.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert entry["blocked_command"] == "git push"
    assert entry["session_id"] == SESSION
    assert entry["pending_paths"] == [str(t)]


def test_bypass_does_not_log_allowlisted_commands(repo, state, monkeypatch, tmp_path):
    _pending_test(repo, state)
    monkeypatch.setenv("BYPASS_PROVE_TEST", "1")
    assert not _denied(_gate("git status", repo))
    assert not (tmp_path / "bypass.jsonl").exists()


@pytest.mark.parametrize(
    "command",
    [
        "prove-test --test tests/test_math.py::test_adds",
        "/abs/path/prove-test --test x",
        "python dev-kit/bin/prove-test.py --test x",
        "python3 dev-kit/bin/prove-test.py --skip-reason too-expensive",
        "uv run python dev-kit/bin/prove-test.py --test x",
        "cd /repo && python dev-kit/bin/prove-test.py --test x",
        "  git status",
        "git diff --stat",
        "git log -3",
        "git show HEAD",
        "git ls-files",
        "git rev-parse HEAD",
        "git blame x.py",
        "ls",
        "ls -la",
        "cat x.py",
        "head -5 x",
        "tail x",
        "find . -name x",
        "rg foo",
        "grep -n foo x",
        "stat x",
        "wc -l x",
        "pytest -q",
        "uv run pytest tests/test_math.py",
        "uv run --with pytest pytest -q",
        "python -m pytest -q",
        "chmod +x f",
        "touch f",
        "mkdir -p d",
        "mv a b",
        "cp a b",
        "rm f",
        "echo hi",
        "printf x",
    ],
)
def test_allowlisted_commands_pass(command):
    assert is_allowed(command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git push",
        "gh pr create",
        "python src/digest/main.py",
        "cat",
        "find",
        "cd /repo; python dev-kit/bin/prove-test.py",
        'cd "$(evil)" && prove-test --test x',
        "cd /repo | x && prove-test",
        "cd /repo\nprove-test --test x",
        "BYPASS=1 python dev-kit/bin/prove-test.py",
        "python dev-kit/bin/other.py prove-test.py",
        "uv sync",
        "lsof",
        "gitx status",
    ],
)
def test_non_allowlisted_commands_are_gated(command):
    assert not is_allowed(command)


def test_allowlisted_command_passes_with_pending_marker(repo, state):
    _pending_test(repo, state)
    assert not _denied(_gate("python dev-kit/bin/prove-test.py --test x", repo))
    assert not _denied(_gate("uv run pytest -q", repo))


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"tool_name": ""}'])
def test_malformed_payload_fails_closed(repo, state, payload):
    p = subprocess.run(
        [PYTHON, str(GATE_PY)], input=payload, capture_output=True, text=True, cwd=repo
    )
    assert p.returncode == 2
    assert "BLOCKED" in p.stderr


def test_fence_passes_deny_verdict_through(repo, state):
    _pending_test(repo, state)
    assert _denied(_gate("git push", repo, argv=launch(FENCE)))
    assert not _denied(_gate("git status", repo, argv=launch(FENCE)))


def test_fence_coerces_python_crash_to_block(tmp_path):
    fence = tmp_path / "prove-test-gate-actions"
    shutil.copy2(FENCE, fence)
    for bad_exit in (1, 127):
        (tmp_path / "prove_test_gate_actions.py").write_text(
            "import sys; sys.exit(%d)\n" % bad_exit
        )
        p = subprocess.run(
            launch(fence),
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
            capture_output=True,
            text=True,
        )
        assert p.returncode == 2, (bad_exit, p.returncode)
        assert "failing" in p.stderr


def test_fence_blocks_when_interpreter_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOK_PYTHON", str(tmp_path / "no-such-python"))
    p = subprocess.run(
        launch(FENCE),
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert p.returncode == 2

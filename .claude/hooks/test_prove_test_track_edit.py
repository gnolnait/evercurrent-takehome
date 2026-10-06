"""prove_test_track_edit: payload in, pending marker out.

Run: cd .claude/hooks && python -m pytest -q test_prove_test_track_edit.py
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hooktest_support import PYTHON, launch
from prove_test_track_edit import is_test_file, safe_session, sha256_file

HOOK_DIR = Path(__file__).parent
HOOK_PY = HOOK_DIR / "prove_test_track_edit.py"
FENCE = HOOK_DIR / "prove-test-track-edit"

TEST_BODY = "def test_adds():\n    assert 1 + 1 == 2\n"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "src").mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("PROVE_TEST_STATE_DIR", str(d))
    monkeypatch.delenv("BYPASS_PROVE_TEST", raising=False)
    return d


def _run(payload, cwd, argv=None):
    p = subprocess.run(
        argv or [PYTHON, str(HOOK_PY)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=os.environ.copy(),
    )
    return p


def _edit(path, session="sess-1", cwd=None):
    payload = {
        "tool_name": "Edit",
        "session_id": session,
        "tool_input": {"file_path": str(path)},
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def _markers(state):
    return sorted(state.glob("*.json")) if state.exists() else []


def test_edit_of_test_file_writes_marker(repo, state):
    t = repo / "tests" / "test_math.py"
    t.write_text(TEST_BODY)
    p = _run(_edit(t), cwd=repo)
    assert p.returncode == 0
    (marker,) = _markers(state)
    sha = sha256_file(str(t))
    assert marker.name == "sess-1-%s.json" % sha
    rec = json.loads(marker.read_text())
    assert rec["sha256"] == sha
    assert rec["session_id"] == "sess-1"
    assert os.path.samefile(rec["path"], t)
    assert os.path.samefile(rec["repo"], repo)


def test_relative_path_resolves_against_payload_cwd(repo, state):
    t = repo / "tests" / "test_rel.py"
    t.write_text(TEST_BODY)
    p = _run(_edit("tests/test_rel.py", cwd=repo), cwd=HOOK_DIR)
    assert p.returncode == 0
    assert len(_markers(state)) == 1


def test_session_id_is_sanitized_in_filename(repo, state):
    t = repo / "tests" / "test_math.py"
    t.write_text(TEST_BODY)
    _run(_edit(t, session="a/b c"), cwd=repo)
    (marker,) = _markers(state)
    assert marker.name.startswith(safe_session("a/b c") + "-")
    assert marker.name.startswith("a_b_c-")


@pytest.mark.parametrize(
    "rel",
    ["src/thing.py", "tests/conftest.py", "tests/fixtures/test_data.py", "README.md"],
)
def test_non_test_files_write_no_marker(repo, state, rel):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(TEST_BODY)
    assert _run(_edit(f), cwd=repo).returncode == 0
    assert _markers(state) == []


def test_test_named_file_without_test_function_is_not_tracked(repo, state):
    """`cmd_test.py` can be a SOURCE module; no `def test_` -> not a test."""
    f = repo / "src" / "cmd_test.py"
    f.write_text("def run():\n    return 1\n")
    _run(_edit(f), cwd=repo)
    assert _markers(state) == []


def test_async_test_function_counts(repo, state):
    t = repo / "tests" / "test_async.py"
    t.write_text("async def test_x():\n    assert True\n")
    _run(_edit(t), cwd=repo)
    assert len(_markers(state)) == 1


@pytest.mark.parametrize("suffix", [".json", ".skip.json"])
def test_existing_proof_or_skip_suppresses_marker(repo, state, suffix):
    t = repo / "tests" / "test_math.py"
    t.write_text(TEST_BODY)
    proof = repo / ".test-proof"
    proof.mkdir()
    (proof / (sha256_file(str(t)) + suffix)).write_text("{}")
    _run(_edit(t), cwd=repo)
    assert _markers(state) == []


def test_file_outside_git_repo_is_not_tracked(tmp_path, state):
    loose = tmp_path / "loose" / "tests"
    loose.mkdir(parents=True)
    t = loose / "test_x.py"
    t.write_text(TEST_BODY)
    _run(_edit(t), cwd=tmp_path)
    assert _markers(state) == []


def test_bypass_writes_no_marker(repo, state, monkeypatch):
    t = repo / "tests" / "test_math.py"
    t.write_text(TEST_BODY)
    monkeypatch.setenv("BYPASS_PROVE_TEST", "1")
    _run(_edit(t), cwd=repo)
    assert _markers(state) == []


@pytest.mark.parametrize(
    "payload",
    ["{not json", "[]", json.dumps({"tool_name": "Edit"}), json.dumps({"session_id": "s"})],
)
def test_malformed_payload_never_blocks_the_edit(repo, state, payload):
    p = _run(payload, cwd=repo)
    assert p.returncode == 0
    assert _markers(state) == []


def test_fence_exits_zero_and_tracks(repo, state):
    t = repo / "tests" / "test_math.py"
    t.write_text(TEST_BODY)
    p = _run(_edit(t), cwd=repo, argv=launch(FENCE))
    assert p.returncode == 0, p.stderr
    assert len(_markers(state)) == 1


def test_fence_exits_zero_when_python_child_crashes(tmp_path):
    fence = tmp_path / "prove-test-track-edit"
    shutil.copy2(FENCE, fence)
    (tmp_path / "prove_test_track_edit.py").write_text("raise SystemExit(1)\n")
    p = _run(json.dumps({"tool_name": "Edit"}), cwd=tmp_path, argv=launch(fence))
    assert p.returncode == 0


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/r/tests/test_a.py", True),
        ("/r/src/a_test.py", True),
        ("/r/tests/unit/helpers.py", True),
        ("C:\\r\\tests\\test_a.py", True),
        ("/r/tests/conftest.py", False),
        ("/r/tests/fixtures/test_a.py", False),
        ("/r/tests/__fixtures__/test_a.py", False),
        ("/r/src/a.py", False),
        ("/r/tests/test_a.txt", False),
    ],
)
def test_is_test_file_shapes(path, expected):
    assert is_test_file(path) is expected

#!/usr/bin/env python3
"""prove_test_track_edit - PostToolUse hook on Edit|Write|MultiEdit.

When the agent edits a test file, write a pending marker at
~/.claude/state/prove-test-pending/<session>-<file-sha>.json. The marker is
consumed by `prove_test_gate_actions` (PreToolUse on Bash): the next
non-trivial action is blocked until a matching `.test-proof/<sha>.json` (or
`.skip.json`) exists at the repo root.

Two-stage by design: edits are iterative; friction at each Edit is wrong.
Friction at the *next non-trivial action* (commit, push, gh pr) is right.

Never blocks the edit itself: every path exits 0, including a malformed
payload (hook_payload's fail-closed exit is caught and discarded here; its
launcher, the settings.json command or the bash fence `prove-test-track-edit`,
also forces exit 0).

Bypass: BYPASS_PROVE_TEST=1 exported before the session launches (an inline
`BYPASS_X=1 cmd` does NOT reach hooks). The bypass is enforced and logged in
prove_test_gate_actions; this hook silently no-ops on bypass so markers do not
accumulate.

State dir override: PROVE_TEST_STATE_DIR (tests use it).
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_payload as payload  # noqa: E402  (post-path-insert)

_TEST_FN = re.compile(r"^[ \t]*(async[ \t]+)?def test_", re.MULTILINE)


def state_dir():
    return os.environ.get("PROVE_TEST_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "state", "prove-test-pending"
    )


def safe_session(session_id):
    """Filename-safe session id. Must match the gate's rule exactly."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def proof_exists(repo, sha):
    proof_dir = os.path.join(repo, ".test-proof")
    return os.path.isfile(os.path.join(proof_dir, sha + ".json")) or os.path.isfile(
        os.path.join(proof_dir, sha + ".skip.json")
    )


def git_toplevel(directory):
    """Canonical repo root for `directory`, or "" outside a git tree."""
    try:
        out = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    top = out.stdout.strip()
    if out.returncode != 0 or not top or not os.path.isdir(top):
        return ""
    return os.path.realpath(top)


def resolve_path(file_path, cwd):
    """Absolute, symlink-resolved path. The leaf (and any missing parents) may
    not exist yet, so resolve the deepest existing ancestor and re-append."""
    p = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    p = os.path.normpath(p)
    parent, leaf = os.path.split(p)
    while parent and not os.path.exists(parent):
        nxt, base = os.path.split(parent)
        if nxt == parent:
            break
        leaf = os.path.join(base, leaf)
        parent = nxt
    if parent and os.path.exists(parent):
        return os.path.join(os.path.realpath(parent), leaf)
    return p


def is_test_file(abs_path):
    """pytest-shaped path that is not harness plumbing. Content is checked
    separately (has_test_function)."""
    text = abs_path.replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    if name == "conftest.py":
        return False
    if "/fixtures/" in text or "/__fixtures__/" in text:
        return False
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return "/tests/" in text or text.startswith("tests/")


def has_test_function(abs_path):
    """The `*_test.py` glob also matches SOURCE files that merely end in
    _test.py. A flagged file with no pytest test function is not a real test,
    so it is not gated. (test_*.py and tests/ files need a function too.)"""
    try:
        with io.open(abs_path, encoding="utf-8", errors="replace") as fh:
            return bool(_TEST_FN.search(fh.read()))
    except OSError:
        return False


def track(data, tool_input):
    """Return the marker path written, or None when nothing is tracked."""
    if os.environ.get("BYPASS_PROVE_TEST") == "1":
        return None
    session_id = data.get("session_id")
    file_path = tool_input.get("file_path")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(file_path, str) or not file_path:
        return None

    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        cwd = os.getcwd()
    abs_path = resolve_path(file_path, cwd)

    if not is_test_file(abs_path):
        return None
    if not os.path.isfile(abs_path):
        return None
    if not has_test_function(abs_path):
        return None

    repo = git_toplevel(os.path.dirname(abs_path))
    if not repo:
        return None
    sha = sha256_file(abs_path)
    # A proof or skip for this exact content already covers the current state.
    if proof_exists(repo, sha):
        return None

    sdir = state_dir()
    os.makedirs(sdir, exist_ok=True)
    marker = os.path.join(sdir, "%s-%s.json" % (safe_session(session_id), sha))
    record = {
        "path": abs_path,
        "sha256": sha,
        "repo": repo,
        "session_id": session_id,
        "edited_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump(record, fh, separators=(",", ":"))
    return marker


def main():
    try:
        data, _tool, tool_input = payload.read_payload("prove-test-track-edit")
        track(data, tool_input)
    except SystemExit:
        pass  # hook_payload fails closed with exit 2; this hook never blocks
    except Exception:  # noqa: BLE001 - a tracking failure must not block an edit
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

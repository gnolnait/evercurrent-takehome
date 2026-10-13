#!/usr/bin/env python3
"""prove_test_gate_actions - PreToolUse hook (Bash) that blocks non-trivial
actions until pending test-edit markers are resolved.

Companion to prove_test_track_edit (PostToolUse). Track-edit writes a marker
when a test file is edited without a matching proof. This hook reads those
markers and denies the *next* non-trivial Bash action (commit, push, gh pr,
running unrelated code) until the agent runs prove-test (or records a
structured skip).

Two-stage by design: edits are iterative, so blocking each Edit is wrong.
Blocking at the next non-trivial action is right. Registered on the Bash and
PowerShell tools only; edits and reads are never gated.

A marker resolves (and is deleted) when the test file's CURRENT sha256 has a
`.test-proof/<sha>.json` or `.test-proof/<sha>.skip.json` at the marker's repo
root, when the file no longer exists, or when the marker is unreadable.
Markers from a different repo than the Bash call's are ignored (sub-agents
share the parent session id; one repo's pending test must not gate another's).

Allowlist (benign Bash shapes pass even with pending markers):
  prove-test itself (`prove-test`, `prove-test.py`, `python[3] .../prove-test.py`),
    optionally after a bare `cd <dir> &&` prefix - the proof command must never
    deadlock behind the gate
  git diff/status/log/show/ls-files/rev-parse/blame  - read-only git
  ls, cat, head, tail, find, rg, grep, jq, stat, wc  - reading
  pytest, `uv run ...pytest...`, `python[3] -m pytest`  - running tests is
    iteration; gating it deadlocks the agent
  chmod, touch, mkdir, mv, cp, rm, echo, printf  - file ops are not the gate point

Verdict: a deny is printed as PreToolUse JSON (permissionDecision "deny") with
exit 0. Malformed payloads fail closed through hook_payload (exit 2); the bash
fence `prove-test-gate-actions` coerces any other non-{0,2} exit to a block.

Bypass: `export BYPASS_PROVE_TEST=1` BEFORE launching the session. Inline
`BYPASS_X=1 cmd` does NOT reach hooks. Each bypassed (otherwise-denied) command
is logged to ~/.claude/state/prove-test-bypass.jsonl (override:
PROVE_TEST_BYPASS_LOG) for retro surfacing.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_payload as payload  # noqa: E402  (post-path-insert)
from prove_test_track_edit import (  # noqa: E402
    git_toplevel,
    proof_exists,
    safe_session,
    sha256_file,
    state_dir,
)
from shell_segments import SHELL_TOOLS  # noqa: E402

_SUBJECT = "the prove-test gate"

# Commands allowed bare or with arguments (`ls`, `ls -la`).
_BARE_OR_ARGS = (
    "git diff", "git status", "git log", "git show", "git ls-files",
    "git rev-parse", "git blame",
    "ls", "head", "tail", "rg", "grep", "jq", "stat", "wc", "pytest",
)
# Commands allowed only with arguments (`cat x`, not bare `cat`).
_ARGS_ONLY = (
    "cat", "find", "chmod", "touch", "mkdir", "mv", "cp", "rm", "echo", "printf",
)
_PY_PYTEST = re.compile(r"^(?:[^\s]*/)?python(?:3(?:\.\d+)?)?(?:\.exe)?\s+-m\s+pytest(?:\s|$)")
_PROVE_TEST_NAMES = ("prove-test", "prove-test.py")
_PYTHON_NAMES = re.compile(r"^python(?:3(?:\.\d+)?)?(?:\.exe)?$")
_PREFIX_METACHARS = re.compile(r"[;|`$&\n]")


def bypass_log_path():
    return os.environ.get("PROVE_TEST_BYPASS_LOG") or os.path.join(
        os.path.expanduser("~"), ".claude", "state", "prove-test-bypass.jsonl"
    )


def _basename(token):
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _strip_cd_prefix(cmd):
    """Skip an optional leading bare `cd <dir> &&` so `cd <repo> && prove-test`
    self-allows. Only a prefix with NO shell metacharacters is stripped:
    `cd x; <injected> && prove-test` or `cd "$(...)" && prove-test` would
    otherwise smuggle a gated command past the self-allow. A metachar in the
    prefix fails closed (stays gated)."""
    if not cmd.startswith("cd ") or "&&" not in cmd:
        return cmd
    prefix, rest = cmd.split("&&", 1)
    if _PREFIX_METACHARS.search(prefix):
        return cmd
    return rest.lstrip()


def is_prove_test_command(cmd):
    tokens = _strip_cd_prefix(cmd).split()
    if not tokens:
        return False
    if _basename(tokens[0]) in _PROVE_TEST_NAMES:
        return True
    # `python dev-kit/bin/prove-test.py ...` (the kit's documented form) and
    # `uv run python dev-kit/bin/prove-test.py ...`.
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    return (
        len(tokens) >= 2
        and _PYTHON_NAMES.match(_basename(tokens[0])) is not None
        and _basename(tokens[1]) in _PROVE_TEST_NAMES
    )


def is_allowed(command):
    """True when `command` is a benign shape that passes despite pending
    markers. Anchored on the command head (leading whitespace stripped)."""
    trimmed = command.lstrip()
    if is_prove_test_command(trimmed):
        return True
    for word in _BARE_OR_ARGS:
        if trimmed == word or trimmed.startswith(word + " "):
            return True
    for word in _ARGS_ONLY:
        if trimmed.startswith(word + " "):
            return True
    if trimmed.startswith("uv run ") and "pytest" in trimmed[len("uv run "):]:
        return True
    return _PY_PYTEST.match(trimmed) is not None


def _same_repo(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def unresolved_markers(session_id, cwd):
    """Resolve this session's markers; return [(marker, test_path)] still
    pending. Stale markers are deleted as a side effect.

    `cwd`'s repo is looked up only when the session has a marker: the lookup
    spawns git, and most Bash calls have none."""
    sdir = state_dir()
    if not os.path.isdir(sdir):
        return []
    pattern = os.path.join(glob.escape(sdir), glob.escape(safe_session(session_id)) + "-*.json")
    markers = sorted(glob.glob(pattern))
    if not markers:
        return []
    current_repo = git_toplevel(cwd)
    pending = []
    for marker in markers:
        try:
            with open(marker, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            _remove(marker)
            continue
        path = record.get("path") if isinstance(record, dict) else None
        repo = record.get("repo") if isinstance(record, dict) else None
        if not isinstance(path, str) or not path or not isinstance(repo, str) or not repo:
            _remove(marker)
            continue
        # Cross-repo scoping. Outside any git tree, consider every marker.
        if current_repo and not _same_repo(repo, current_repo):
            continue
        if not os.path.isfile(path):
            _remove(marker)  # test file deleted/renamed since the edit
            continue
        try:
            sha = sha256_file(path)
        except OSError:
            _remove(marker)
            continue
        if proof_exists(repo, sha):
            _remove(marker)
            continue
        pending.append((marker, path))
    return pending


def log_bypass(session_id, command, paths):
    log = bypass_log_path()
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": session_id,
            "blocked_command": command,
            "pending_paths": paths,
        }
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def deny_reason(paths):
    try:
        first_sha = sha256_file(paths[0])
    except OSError:
        first_sha = "<sha>"
    listing = "\n".join("  - %s" % p for p in paths)
    return """prove-test gate: {n} edited test file(s) lack a load-bearing proof.

Pending:
{listing}

Run prove-test on each to confirm the test catches the bug it asserts:
  prove-test --test <test-id> \\
    --break blank:<src-file>:<line>-<line> \\
    --run "uv run python -m pytest <test-file>::<test-name>" \\
    --expect-error '<specific regex from broken output>' \\
    --format "uv run ruff format <test-file>"
  (blank BLANKS those source lines - aim at the SINGLE load-bearing line, not a
   whole function/file, so the break is a clean failure not an ImportError.)
  (--format is optional but saves a re-proof: the proof is keyed on the test
   file's sha, so a formatter run afterwards voids it. Formatting first makes
   the pre-commit pass a no-op.)

Or record a structured skip:
  prove-test --test <test-id> \\
    --skip-reason <too-expensive|external-service|nondeterministic|not-new-assertion|snapshot-not-load-bearing> \\
    --skip-note "<why>"

Known traps:
  * prove-test must be the command head - or preceded ONLY by a bare `cd <dir> &&`,
    which this hook strips so the proof command never deadlocks behind the gate. Still
    BLOCKED: a newline-separated `cd <dir>` then prove-test (no &&); a
    `VAR=... python ...prove-test.py` assignment prefix; any cd-prefix carrying shell
    metacharacters (fails closed).
  * Do NOT pipe inside --run (`| tail`). A pipeline's exit code is the last command's
    (tail -> 0), so the broken run looks like it PASSED and the RED check fails.
  * --run executes from --workspace (the repo root), NOT the test's directory.
  * --expect-error must match exactly ONE region. Anchor on a unique line and drop
    redundant assertions that echo the same string.

Artifacts land at <repo>/.test-proof/{sha}.json (or .skip.json). The
hook re-checks the current file SHA - edit the test more and the marker stays
pending until proof/skip matches the new SHA.

Bypass (logged for retro): `export BYPASS_PROVE_TEST=1` BEFORE launching
this session. Inline `BYPASS_PROVE_TEST=1 cmd` does NOT reach PreToolUse
hooks.""".format(n=len(paths), listing=listing, sha=first_sha)


def evaluate(data, tool, tool_input):
    """Return None to allow, or the deny reason string."""
    if tool not in SHELL_TOOLS:
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    command = payload.read_command(_SUBJECT, tool_input)
    if not command:
        return None

    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        cwd = os.getcwd()
    pending = unresolved_markers(session_id, cwd)
    if not pending:
        return None
    if is_allowed(command):
        return None
    paths = [p for _m, p in pending]
    if os.environ.get("BYPASS_PROVE_TEST") == "1":
        log_bypass(session_id, command, paths)
        return None
    return deny_reason(paths)


def main():
    data, tool, tool_input = payload.read_payload(_SUBJECT)
    reason = evaluate(data, tool, tool_input)
    if reason is not None:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        sys.stdout.write(json.dumps(out) + "\n")
    sys.exit(0)


if __name__ == "__main__":
    main()

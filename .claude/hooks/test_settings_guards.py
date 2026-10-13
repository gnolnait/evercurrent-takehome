"""The git guard runner must be registered UNCONDITIONED in project settings.

An `if: "Bash(git *)"` gate on the guard hook is a quoting bypass: `g''it
commit` does not match the glob, so the hook never runs and the quote-aware
parsers never see the command. The runner is cheap enough to run on every Bash
call; conditioning belongs to nobody. It also covers the MCP merge surface.

Run: pytest .claude/hooks/test_settings_guards.py
"""

import json
import re
from pathlib import Path

SETTINGS = Path(__file__).parents[1] / "settings.json"
# The full launch fence, pinned exactly. A bare `$CLAUDE_PROJECT_DIR/...`
# command fails OPEN when the file is transiently absent (a peer session
# checking out the primary): /bin/sh exits 127, and Claude Code treats any
# exit other than 2 as non-blocking. The fence runs the guard as a child and
# coerces any exit other than 0/2 to 2 (the file can also vanish between the
# `-x` probe and the launch), and exits 2 when the runner is missing.
#
# The runner is taken from `CLAUDE_PROJECT_DIR` and NOWHERE else. An earlier
# revision fell back to the cwd repo's own copy so a worktree session kept its
# guards while the primary was mid-checkout; that convenience was the source
# of two separate trust holes (a foreign repo's runner standing in, then a
# crafted `.git` gitfile pointing `--git-common-dir` at the primary while
# `--show-toplevel` resolved to an attacker-writable directory). Availability
# during a checkout is not worth a path by which an untrusted directory
# supplies the guard: blocking briefly is the safe direction, and the message
# says to retry.
#
# With `CLAUDE_HOOK_PYTHON` set and `guard_runner.py` present, the fence runs
# the python file with that interpreter itself, which skips the bash wrapper's
# launch cost (about 80 ms per call on Windows). Both paths still come from
# `CLAUDE_PROJECT_DIR`, and both exits go through the same coercion. Without
# the variable, or without the python file, the fence runs the wrapper. The
# variable counts only when it is a path (it holds a `/` or `\`): a shell runs
# such a name as a file, never as a builtin, and `exec "$s"` would replace the
# fence's own shell and skip the coercion. The backslash is single-quoted
# because Windows hands the command to MSYS sh on a command line that
# collapses an unquoted `\\`.
#
# Two inherited-shell hazards remain fenced: the child runs in an `if` so
# errexit cannot bail between it and `rc=$?`, and every `CLAUDE_PROJECT_DIR`
# and `CLAUDE_HOOK_PYTHON` expansion carries `-` so nounset cannot abort on an
# unset var. The BLOCKED echoes carry `|| :` so a closed stderr cannot turn a
# block into a non-blocking exit 1. Behavior is covered by
# `test_guard_launch_fence.py`; this file pins the registration.
RUNNER = (
    'w="${CLAUDE_PROJECT_DIR-}/.claude/hooks/guard_runner"; '
    's="${CLAUDE_PROJECT_DIR-}/.claude/hooks/guard_runner.py"; '
    'p="${CLAUDE_HOOK_PYTHON-}"; '
    "case \"$p\" in */* | *'\\'*) ;; *) p= ;; esac; "
    'if [ -n "$p" ] && [ -f "$s" ]; then if "$p" "$s"; then rc=0; else rc=$?; fi; '
    'elif [ -x "$w" ]; then if "$w"; then rc=0; else rc=$?; fi; '
    'else echo "BLOCKED: guard_runner missing or not executable at '
    '${CLAUDE_PROJECT_DIR-}/.claude/hooks/guard_runner (checkout mid-update?); '
    'failing closed. Retry shortly." >&2 || :; exit 2; fi; '
    'if [ "$rc" != 0 ] && [ "$rc" != 2 ]; then '
    'echo "BLOCKED: guard_runner exited $rc (vanished, broken mid-run, or a bad '
    'CLAUDE_HOOK_PYTHON?); failing closed. Retry shortly." >&2 || :; rc=2; fi; '
    'exit "$rc"'
)


def test_every_guard_registration_uses_the_fence():
    """No registration may launch the runner by bare path.

    The per-matcher tests below pin the groups that exist today; a hook group
    added later (an edit-tool matcher, a new MCP surface) would not be covered
    by them, and a bare `$CLAUDE_PROJECT_DIR/...` command fails OPEN when the
    file is briefly absent — the exact hole the fence closes. So assert the
    invariant over EVERY registration, not one per known group.
    """
    cfg = json.loads(SETTINGS.read_text())
    commands = [
        h.get("command", "")
        for g in cfg["hooks"]["PreToolUse"]
        for h in g.get("hooks", [])
        if "guard_runner" in h.get("command", "")
    ]
    assert commands, "guard_runner is not registered at all"
    for command in commands:
        assert command == RUNNER, (
            "a guard_runner registration bypasses the fail-closed fence "
            "(bare-path launch fails open when the file is absent): %r" % (command,)
        )


def test_runner_registered_in_bash_group_and_unconditioned():
    """Exact command, inside a group whose matcher is exactly "Bash" — a
    substring/any-group lookup would be satisfied by an MCP-only registration
    or a renamed backup script and silently vacate the pin."""
    cfg = json.loads(SETTINGS.read_text())
    bash_groups = [
        g for g in cfg["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"
    ]
    assert bash_groups, "no PreToolUse group with matcher exactly 'Bash'"
    bash_hooks = [h for g in bash_groups for h in g.get("hooks", [])]
    entries = [h for h in bash_hooks if h.get("command") == RUNNER]
    assert entries, "guard_runner not registered in the Bash group"
    for entry in entries:
        assert "if" not in entry, (
            "guard_runner carries a conditioning 'if' gate (quoting bypass): %r"
            % (entry,)
        )


def test_runner_also_covers_mcp_merge_tools():
    """The runner must additionally be registered under the MCP
    merge_pull_request matcher — the Bash group alone doesn't see MCP calls,
    and the merge guard is the one with an MCP surface."""
    cfg = json.loads(SETTINGS.read_text())
    mcp_groups = [
        g
        for g in cfg["hooks"]["PreToolUse"]
        if "merge_pull_request" in g.get("matcher", "")
    ]
    assert mcp_groups, "no PreToolUse group matching mcp merge_pull_request"
    commands = [
        h.get("command", "") for g in mcp_groups for h in g.get("hooks", [])
    ]
    assert RUNNER in commands, "guard_runner not registered under the MCP merge matcher"


def test_no_edit_tool_registration():
    """The runner must NOT be registered for the file-editing tools.

    An edit-tool matcher once existed to block agent edits to a docs tree.
    It was removed because the policy it enforced is about
    AUTHORSHIP and a PreToolUse hook cannot see authorship — it sees a path and
    a tool name, so it refused a privacy fix, a stale-tool-reference correction
    and a wording-neutral refactor along with any real authoring.

    This asserts the removal rather than merely not testing for it, so the same
    registration cannot return without someone deleting this test and reading
    why it exists. Telling those cases apart needs a person reading the diff,
    which is review, not a matcher.

    Matchers are APPLIED, not substring-scanned. A literal check for "Edit"
    misses a pattern that reaches the edit tools without naming them — `.*` and
    `E.*` both match `Edit` and would reinstate the guard silently. Both regex
    semantics are tried because the runtime's choice is not documented, and a
    matcher that reaches an edit tool under either one is a registration.
    """
    cfg = json.loads(SETTINGS.read_text())
    edit_tools = ("Edit", "Write", "MultiEdit", "NotebookEdit")
    offenders = []
    for group in cfg["hooks"]["PreToolUse"]:
        matcher = group.get("matcher", "")
        try:
            pattern = re.compile(matcher)
        except re.error as exc:
            # Fail closed. A pattern Python cannot compile may still match an
            # edit tool in the runtime's own regex flavor, so skipping it would
            # leave the invariant unverified while the test reported green.
            raise AssertionError(
                "PreToolUse matcher %r does not compile here, so this test "
                "cannot tell whether it reaches the edit tools: %s" % (matcher, exc)
            ) from exc
        for tool in edit_tools:
            if pattern.search(tool) or pattern.fullmatch(tool):
                offenders.append((matcher, tool))
                break
    assert not offenders, (
        "a PreToolUse group reaches the file-editing tools: %r. A path+tool-name "
        "hook cannot distinguish authoring from repair, so it blocks repairs "
        "too; that judgement belongs to review." % (offenders,)
    )

#!/usr/bin/env python3
"""Standalone CLI for the no-commit-to-main guard.

Thin shim over no_commit_to_main_impl.evaluate_commit — the SAME code the
production guard_runner dispatches to, so the adversarial test suite that
invokes this hook exercises the path that actually ships. Reads the PreToolUse
JSON on stdin; exit 2 == block, exit 0 == allow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_payload as payload  # noqa: E402  (post-path-insert)
from no_commit_to_main_impl import evaluate_commit  # noqa: E402
from shell_segments import SHELL_TOOLS, tool_command  # noqa: E402

_SUBJECT = "the no-commit-to-main guard"


def main():
    # Parse + shape-check through the shared reader, so this shim cannot drift
    # from the verdict guard_runner produces for the same payload — the whole
    # reason the shim exists is for the suite to drive the shipped behavior.
    data, tool, tin = payload.read_payload(_SUBJECT)

    if tool not in SHELL_TOOLS:
        sys.exit(0)

    cmd = tool_command(tool, payload.read_command(_SUBJECT, tin))
    if not cmd.strip():
        sys.exit(0)

    hook_cwd = data.get("cwd") or ""
    if not hook_cwd or not os.path.isdir(hook_cwd):
        hook_cwd = os.getcwd()
    # Default project_dir to the hook PROCESS cwd (os.getcwd()), NOT the
    # payload cwd — this matches the production runner's _project_dir() and the
    # old bash hook's `${CLAUDE_PROJECT_DIR:-$PWD}`. evaluate_commit still falls
    # back to hook_cwd if this dir turns out not to be a worktree.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    blocked, message = evaluate_commit(cmd, hook_cwd, project_dir)
    if blocked:
        sys.stderr.write(message)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()

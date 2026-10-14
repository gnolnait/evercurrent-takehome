#!/usr/bin/env python3
"""Standalone CLI for the no-unreviewed-pr guard.

Thin shim over no_unreviewed_pr_impl.evaluate, the same code guard_runner
dispatches to, so the test suite that invokes this hook drives the shipped
behavior. Reads the PreToolUse JSON on stdin; exit 2 == block, exit 0 == allow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_payload as payload  # noqa: E402  (post-path-insert)
from no_unreviewed_pr_impl import evaluate  # noqa: E402
from shell_segments import SHELL_TOOLS, tool_command  # noqa: E402


def main():
    data, tool, tool_input = payload.read_payload("the no-unreviewed-pr guard")
    if tool not in SHELL_TOOLS:
        sys.exit(0)
    cmd = tool_command(tool, payload.read_command("the no-unreviewed-pr guard", tool_input))
    if not cmd.strip():
        sys.exit(0)
    cwd = data.get("cwd") or ""
    blocked, message = evaluate(cmd, cwd if cwd and os.path.isdir(cwd) else os.getcwd())
    if blocked:
        sys.stderr.write(message)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Standalone CLI for the no-merge-pr guard.

Thin shim over no_merge_pr_impl.evaluate — the SAME code the production
guard_runner dispatches to, so the adversarial test suite that invokes this hook
exercises the path that actually ships. Reads the PreToolUse JSON on stdin;
exit 2 == block, exit 0 == allow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_payload as payload  # noqa: E402  (post-path-insert)
from no_merge_pr_impl import evaluate  # noqa: E402


def main():
    # Parse + shape-check through the shared reader, so this shim cannot drift
    # from the verdict guard_runner produces for the same payload — the whole
    # reason the shim exists is for the suite to drive the shipped behavior.
    _data, tool, tool_input = payload.read_payload("the no-merge-pr guard")

    # `command` only matters on the Bash path — evaluate() returns before
    # reading it for an mcp tool. Check it under the same condition, or a weird
    # `command` on an mcp payload would block here while the runner allows it.
    if tool == "Bash":
        payload.read_command("the no-merge-pr guard", tool_input)

    blocked, message = evaluate(tool, tool_input)
    if blocked:
        sys.stderr.write(message)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()

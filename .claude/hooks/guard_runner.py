#!/usr/bin/env python3
"""guard_runner - single-spawn PreToolUse dispatch for the git and PR guards.

Replaces three bash-wrapper + python-parser spawns with ONE python
process: read the payload once through hook_payload, then run each guard's pure
evaluate() entry. First blocking verdict wins. Fail-closed is owned by the bash
wrapper (guard_runner) which coerces any non-{0,2} exit to a block; this
module's own job is to never exit 0 when a check says block.

Standalone guards remain (their test suites invoke them directly); this is the
production dispatch path wired in settings.json.
"""
import sys

# Inline import note: sibling hook modules live beside this file, not on a
# package path; add our own dir so `import` resolves them under the plugin cache
# and the repo alike.
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_payload as payload  # noqa: E402  (post-path-insert)
import no_bypass_signing_impl as signing  # noqa: E402
import no_merge_pr_impl as merge  # noqa: E402
import no_commit_to_main_impl as commit  # noqa: E402
import no_unreviewed_pr_impl as review  # noqa: E402


def _hook_cwd(data):
    cwd = data.get("cwd") or ""
    return cwd if cwd and os.path.isdir(cwd) else os.getcwd()


def _project_dir():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def main():
    # Parse + shape-check once, in the shared reader every entry point calls, so
    # the runner and the shims cannot drift on what they accept.
    data, tool, tin = payload.read_payload("guard_runner")

    # MCP tools: only the merge guard has an MCP surface.
    if tool.startswith("mcp__"):
        blocked, message = merge.evaluate(tool, tin)
        if blocked:
            sys.stderr.write(message)
            sys.exit(2)
        sys.exit(0)

    if tool != "Bash":
        sys.exit(0)

    cmd = payload.read_command("guard_runner", tin)
    if not cmd.strip():
        sys.exit(0)

    # BYPASS_GIT_GUARD is honored ONLY for the signing guard, matching the
    # standalone contract: no-bypass-signing has an escape hatch, but
    # no-merge-pr, no-commit-to-main and no-unreviewed-pr have NO bypass by design. A blanket
    # runner-level short-circuit would silently widen the bypass to PR merges
    # and protected-branch commits — so the env is scoped here, not in the
    # bash fence.
    bypass_signing = os.environ.get("BYPASS_GIT_GUARD") == "1"

    # Thunks, not eager calls: first block wins, and a guard that never runs
    # never pays its cost — notably the commit guard's git subprocesses, which
    # must not fire when the signing guard already blocked.
    checks = []
    if not bypass_signing:
        checks.append(lambda: signing.evaluate(cmd))
    checks.append(lambda: merge.evaluate(tool, tin))
    checks.append(
        lambda: commit.evaluate_commit(cmd, _hook_cwd(data), _project_dir())
    )
    checks.append(lambda: review.evaluate(cmd, _hook_cwd(data)))

    for check in checks:
        blocked, message = check()
        if blocked:
            sys.stderr.write(message)
            sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()

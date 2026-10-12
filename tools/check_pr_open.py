"""Refuse a push to a branch whose pull request has already merged (pre-push hook).

A commit pushed after the merge never reaches main, and nothing says so: on 2026-10-12
two review fixes on PR #17 missed main this way. The hook asks GitHub, through `gh`, for
the branch's pull requests. If one merged and none is open, the push stops with a hint
to open a follow-up branch. Without `gh` or a network, it prints a warning and lets the
push through: the check needs GitHub, and a missing tool must not block all work.
"""

import json
import os
import subprocess
from collections.abc import Callable

Run = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)


def current_branch() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def merged_pr(branch: str, run: Run = _run) -> int | None:
    """The number of a merged PR for `branch` when no PR for it is open, else None."""
    try:
        out = run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,state,isCrossRepository",
                "--limit",
                "20",
            ]
        ).stdout
        prs = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"pr-not-merged: could not check the PR state ({error}); push allowed")
        return None
    # A PR from a fork with the same branch name says nothing about this repo's branch.
    prs = [pr for pr in prs if not pr.get("isCrossRepository", False)]
    if any(pr["state"] == "OPEN" for pr in prs):
        return None
    merged = [pr["number"] for pr in prs if pr["state"] == "MERGED"]
    return max(merged) if merged else None


def main() -> int:
    # prek names the branch being pushed; it can differ from the checked-out one.
    branch = current_branch()
    pushed = os.environ.get("PRE_COMMIT_REMOTE_BRANCH", "").removeprefix("refs/heads/")
    if pushed:
        branch = pushed
    if branch in ("main", "HEAD"):
        return 0
    number = merged_pr(branch)
    if number is None:
        return 0
    print(
        f"PR #{number} for branch {branch!r} has already merged; a push now never reaches"
        f" main. Open a follow-up branch from origin/main and cherry-pick the new commits."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

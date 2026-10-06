import functools
import os
import subprocess

from git_commit_events import emit_events


def _git_output(directory: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\n")


def _is_worktree(directory: str) -> bool:
    return _git_output(directory, "rev-parse", "--is-inside-work-tree") == "true"


# Memoized: the branch walk resolves the same toplevel for the project dir on
# every commit/switch event (a `checkout -b && commit` chain hits it 2+ times).
# Process-per-invocation makes cache staleness a non-issue.
@functools.lru_cache(maxsize=None)
def _canon_git_toplevel(directory: str) -> str:
    top = _git_output(directory, "rev-parse", "--show-toplevel")
    if not top or not os.path.isdir(top):
        return ""
    return os.path.realpath(top)


def _target_is_project(target: str, project_dir: str) -> bool:
    # Lenient match, for commits: an unresolvable dir counts as the project
    # (fail closed - better to block than to let a mis-attributed commit through).
    if not target:
        return True
    project_top = _canon_git_toplevel(project_dir)
    if not project_top:
        return True
    target_top = _canon_git_toplevel(target)
    if not target_top:
        return True
    return target_top == project_top


def _target_is_project_strict(target: str, project_dir: str) -> bool:
    # Strict match, for switches: only a switch provably in the project repo may
    # change its protection state (fail closed - an unresolvable switch dir must
    # not unprotect the branch walk).
    if not target:
        return False
    project_top = _canon_git_toplevel(project_dir)
    if not project_top:
        return False
    target_top = _canon_git_toplevel(target)
    if not target_top:
        return False
    return target_top == project_top


def _is_protected(branch: str) -> bool:
    return branch == "main"


def _block_message(branch: str) -> str:
    return (
        "BLOCKED: refusing to 'git commit' on protected base branch '{0}'.\n"
        "Work belongs on a feature branch that reaches '{0}' through a PR.\n"
        "Create a feature branch first, then commit:\n"
        "  git switch -c <short-description>\n"
        "(If your command already does 'git checkout -b <branch>' before the commit,\n"
        "make sure the branch name is not itself 'main'.)\n"
    ).format(branch)


def _ambiguous_message(branch: str) -> str:
    # A command whose git surface could not be safely parsed (an unresolvable
    # heredoc, or unbalanced quoting around a git+commit) while on a protected
    # branch. Don't claim the user ran `git commit` — say what actually
    # happened, so a benign-but-unparseable command isn't mislabeled.
    return (
        "BLOCKED: could not safely parse this command's git operations while on\n"
        "protected base branch '{0}', so it is refused (fail-safe). If it does not\n"
        "commit, simplify the quoting/heredoc; if it does, switch to a feature\n"
        "branch first: git switch -c <short-description>.\n"
    ).format(branch)


def evaluate_commit(
    cmd: str, hook_cwd: str, project_dir: str
) -> tuple[bool, str]:
    try:
        events = emit_events(cmd, hook_cwd)
    except Exception:
        events = "__AMBIGUOUS__"
    # No commit/switch events (e.g. "commit" only inside a non-git command) -> allow.
    # This is the hot-path exit: project-dir resolution and every git subprocess
    # below run only for commands that produced events.
    if not events:
        return False, ""

    # Resolve the effective project dir (a git subprocess, so only after the
    # events hot-path exit above): if CLAUDE_PROJECT_DIR is stale/missing/not a
    # worktree, fall back to the command's cwd — otherwise a bad env var would
    # leave `branch` empty and fail OPEN on a protected checkout.
    if not _is_worktree(project_dir):
        project_dir = hook_cwd

    branch = _git_output(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    on_protected = _is_protected(branch)
    # Not starting on a protected branch -> nothing to guard (same as before).
    if not on_protected:
        return False, ""

    # Walk the events in execution order, tracking which branch HEAD is on. Both
    # the branch switches and the commit targets come from the same quote-aware
    # parse, so a shell operator inside a quoted message or branch name can't
    # desynchronize the walk.
    for event in events.splitlines():
        fields = event.split("\t", 2)
        fields.extend([""] * (3 - len(fields)))
        kind, directory, arg = fields
        if kind == "__AMBIGUOUS__":
            # unattributable commit-shape while on a protected branch -> fail closed
            return True, _ambiguous_message(branch)
        if kind == "switch":
            # Only a switch provably in THIS repo changes its protection state; a
            # sibling repo's checkout must not unprotect the project's branch walk.
            if _target_is_project_strict(directory, project_dir):
                on_protected = _is_protected(arg)
            continue
        if kind == "commit":
            if on_protected and _target_is_project(directory, project_dir):
                return True, _block_message(branch)
    return False, ""

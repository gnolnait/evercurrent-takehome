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


@functools.lru_cache(maxsize=None)
def _canon_git_common_dir(directory: str) -> str:
    # The git dir every worktree of one repo shares. A main checkout reports
    # it relative to the queried dir (".git"), a linked worktree absolutely.
    if not directory:
        return ""
    common = _git_output(directory, "rev-parse", "--git-common-dir")
    if not common:
        return ""
    return os.path.realpath(os.path.join(directory, common))


def _checkout_of(target: str, project_top: str) -> str:
    """Toplevel of the checkout an event runs in, or "" for a foreign repo.

    A linked worktree of the project repo shares its git dir but has its own
    HEAD, so each checkout is keyed by its own toplevel. An empty or
    unresolvable target counts as the project checkout (fail closed: better to
    block than to let a mis-attributed commit through).
    """
    if not target:
        return project_top
    target_top = _canon_git_toplevel(target)
    if not target_top:
        return project_top
    if target_top == project_top:
        return target_top
    if _canon_git_common_dir(target_top) == _canon_git_common_dir(project_top):
        return target_top
    return ""


def _checkout_of_strict(target: str, project_top: str) -> str:
    # Strict match, for switches: only a switch provably in a checkout of the
    # project repo may change that checkout's protection state (fail closed: an
    # unresolvable switch dir must not unprotect the branch walk).
    if not target or not _canon_git_toplevel(target):
        return ""
    return _checkout_of(target, project_top)


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

    project_top = _canon_git_toplevel(project_dir)
    branch = _git_output(project_dir, "rev-parse", "--abbrev-ref", "HEAD")

    # HEAD per checkout, keyed by toplevel: the project's main checkout and
    # each linked worktree carry their own. A checkout's HEAD is read lazily,
    # the first time an event lands in it, and then follows the switches the
    # command makes there. Both the switches and the commit targets come from
    # the same quote-aware parse, so a shell operator inside a quoted message
    # or branch name can't desynchronize the walk.
    heads = {project_top: branch}

    def _head(checkout: str) -> str:
        if checkout not in heads:
            heads[checkout] = _git_output(checkout, "rev-parse", "--abbrev-ref", "HEAD")
        return heads[checkout]

    for event in events.splitlines():
        fields = event.split("\t", 2)
        fields.extend([""] * (3 - len(fields)))
        kind, directory, arg = fields
        if kind == "__AMBIGUOUS__":
            # unattributable commit-shape while on a protected branch -> fail closed
            if _is_protected(branch):
                return True, _ambiguous_message(branch)
            continue
        if kind == "switch":
            # Only a switch provably in a checkout of THIS repo changes that
            # checkout's HEAD; a sibling repo's checkout must not unprotect the
            # project's branch walk.
            checkout = _checkout_of_strict(directory, project_top)
            if checkout:
                heads[checkout] = arg
            continue
        if kind == "commit":
            checkout = _checkout_of(directory, project_top)
            if checkout and _is_protected(_head(checkout)):
                return True, _block_message(_head(checkout))
    return False, ""

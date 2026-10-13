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


def _repo_above(path: str) -> str:
    """Toplevel of the repo holding the nearest existing ancestor of path, or ""."""
    parent = os.path.dirname(path)
    while parent != path and not os.path.isdir(parent):
        path, parent = parent, os.path.dirname(parent)
    return _canon_git_toplevel(parent) if parent != path else ""


def _checkout_of(target: str, project_top: str, new_checkouts: bool = False) -> str:
    """Toplevel of the checkout an event runs in, or "" for a foreign repo.

    A linked worktree of the project repo shares its git dir but has its own
    HEAD, so each checkout is keyed by its own toplevel. An empty target (a path
    the parser could not resolve) counts as the project checkout (fail closed:
    better to block than to let a mis-attributed commit through). A target that
    is not in a repo yet is the project checkout when the command runs `git
    worktree add`, makes a link or moves a directory (new_checkouts), because
    each can put a project checkout there. Otherwise it is judged by the repo
    around it; with none, it is foreign, such as a scratch repo the command makes
    with `git init`.
    """
    if not target:
        return project_top
    target_top = _canon_git_toplevel(target)
    if not target_top and not new_checkouts:
        target_top = _repo_above(target)
        if not target_top:
            return ""
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
    if "$" in branch or "`" in branch:
        return True  # `git switch "$B"`: a branch only bash knows may be main
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
    # A git write verb in text the parser cannot read as commands (code fed to an
    # interpreter or a shell, a verb in a variable, quoting shlex rejects, a GIT_DIR
    # redirect) while on a protected branch. Don't claim the user ran `git commit` —
    # say what actually happened, so a benign-but-unparseable command isn't mislabeled.
    return (
        "BLOCKED: this command may run a git write (commit, push, merge, ...) in a\n"
        "form the guard cannot attribute to a repo (code fed to an interpreter or a\n"
        "shell, a git verb in a variable, unusual quoting, a GIT_DIR redirect), while\n"
        "on protected base branch '{0}', so it is refused (fail-safe). Run the git\n"
        "write as its own plain command (git -C <repo> ...), or switch to a feature\n"
        "branch first: git switch -c <short-description>.\n"
    ).format(branch)


def _parse_failed_message(branch: str) -> str:
    # The parser raised, so nothing is known about the command: do not claim it
    # names a git write.
    return (
        "BLOCKED: could not safely parse this command's git operations while on\n"
        "protected base branch '{0}', so it is refused (fail-safe). If it does not\n"
        "write with git, simplify the quoting/heredoc; if it does, switch to a\n"
        "feature branch first: git switch -c <short-description>.\n"
    ).format(branch)


def evaluate_commit(
    cmd: str, hook_cwd: str, project_dir: str
) -> tuple[bool, str]:
    parse_failed = False
    try:
        events = emit_events(cmd, hook_cwd)
    except Exception:
        events, parse_failed = "__AMBIGUOUS__", True
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
    new_checkouts = "new-checkout" in events.splitlines()

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
                if parse_failed:
                    return True, _parse_failed_message(branch)
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
            checkout = _checkout_of(directory, project_top, new_checkouts)
            if checkout and _is_protected(_head(checkout)):
                return True, _block_message(_head(checkout))
    return False, ""

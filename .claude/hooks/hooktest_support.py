"""Shared launch helpers for the hook test suites (not a test module).

The hooks are bash scripts with a `#!/usr/bin/env bash` shebang. POSIX execs
them directly; Windows cannot exec a shebang script, so there they are routed
through Git for Windows' `bin/bash.exe` launcher, which puts the MSYS tool dir
on PATH the way Claude Code's Git Bash does. `bash` on a bare Windows PATH
lookup can resolve to WSL (`System32`) or a Store alias, which would run the
hook in a different filesystem namespace, so those are never used.
"""
import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def _usable(path):
    if not path:
        return False
    low = path.lower()
    return "system32" not in low and "windowsapps" not in low


@functools.lru_cache(maxsize=None)
def _git_root():
    """Git for Windows install root, derived from `git --exec-path`."""
    try:
        out = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    # <root>/mingw64/libexec/git-core  (or clangarm64/...)
    p = Path(out)
    return p.parents[2] if len(p.parents) >= 3 else None


@functools.lru_cache(maxsize=None)
def _find(name):
    if not IS_WINDOWS:
        found = shutil.which(name)
        return found or "/bin/%s" % name
    # Prefer Git for Windows' `<root>/bin/<name>.exe` launcher: it puts the MSYS
    # tool dir (`usr/bin`: dirname, cat, grep) on PATH before starting the
    # shell, as Claude Code's own Git Bash does. The raw `usr/bin/<name>.exe`
    # inherits the caller's PATH, so started from PowerShell or a uv-run venv
    # it cannot find `dirname`/`cat` and every hook fence misfires.
    roots = [_git_root()] + [
        Path(base) for base in (r"C:\Program Files\Git", r"C:\Program Files (x86)\Git")
    ]
    for root in roots:
        if root is None:
            continue
        cand = root / "bin" / (name + ".exe")
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if _usable(found):
        return found
    return name


def bash_exe():
    return _find("bash")


def sh_exe():
    """The shell Claude Code runs hook commands under: /bin/sh on POSIX, Git
    Bash's sh.exe on Windows."""
    return "/bin/sh" if not IS_WINDOWS else _find("sh")


def launch(path):
    """argv that executes the bash hook script at `path`."""
    if IS_WINDOWS:
        return [bash_exe(), str(path)]
    return [str(path)]


PYTHON = sys.executable

# The variables that tell git WHICH repository to act on, as `git rev-parse
# --local-env-vars` lists them (git 2.55). Git exports GIT_DIR to its hooks when
# the command runs in a linked worktree, and a pre-push hook runs this suite.
# Inherited, GIT_DIR points every fixture's `git init`, `git config`, `git add`
# and `git commit` at the real repository instead of its temp dir: on 2026-10-11
# that reinitialised the project repo as bare and overwrote its committer
# identity. GIT_EDITOR and GIT_EXEC_PATH stay; they do not select a repository.
GIT_REPO_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)


def scrub_git_env(environ):
    """Remove every repository-selecting git variable from `environ` in place.
    Returns the names removed, so a caller can report what was inherited."""
    removed = []
    for name in GIT_REPO_ENV_VARS:
        if name in environ:
            del environ[name]
            removed.append(name)
    return removed

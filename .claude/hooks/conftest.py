"""Session setup for the hook test suites.

Scrub the repository-selecting git variables from the process environment
before any test runs. Every test here builds its own git repos in a temp dir
and spawns git with a copy of `os.environ`. When this suite runs as a
pre-push hook from a linked worktree, git has exported GIT_DIR into that
environment, and each fixture's `git init`, `git config`, `git add` and
`git commit` then acts on the real repository (2026-10-11: the project repo
was reinitialised as bare and its committer identity overwritten). The scrub
lives here, not in the pre-commit entry, so `just test-hooks`, prek and a bare
pytest run are protected alike.
"""

import os

import pytest

from hooktest_support import scrub_git_env

scrub_git_env(os.environ)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """`-n auto` here starts two workers per CPU.

    Most of these tests wait for bash, git and python processes to start, not
    for the CPU. On 8 cores (Windows), the suite took 487 s serial, 188 s with
    8 workers, 107 s with 16 and 116 s with 24. PYTEST_XDIST_AUTO_NUM_WORKERS
    still overrides this: None hands the choice back to xdist.
    """
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    return 2 * (os.cpu_count() or 1)

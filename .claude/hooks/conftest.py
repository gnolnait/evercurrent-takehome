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

from hooktest_support import scrub_git_env

scrub_git_env(os.environ)

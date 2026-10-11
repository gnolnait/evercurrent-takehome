"""The hook test suites must never inherit a repository-selecting git variable.

Run: pytest .claude/hooks/test_git_env_scrub.py
"""

import os

from hooktest_support import GIT_REPO_ENV_VARS, scrub_git_env


def test_scrub_removes_repo_vars_and_keeps_the_rest():
    env = {"GIT_DIR": "/x/.git", "GIT_WORK_TREE": "/x", "GIT_EDITOR": "true", "PATH": "p"}

    removed = scrub_git_env(env)

    assert sorted(removed) == ["GIT_DIR", "GIT_WORK_TREE"]
    assert env == {"GIT_EDITOR": "true", "PATH": "p"}


def test_session_environment_has_no_repo_vars():
    """conftest.py scrubs os.environ at import. A GIT_DIR exported by a git
    hook (a pre-push from a linked worktree) must not reach any fixture."""
    inherited = [name for name in GIT_REPO_ENV_VARS if name in os.environ]

    assert inherited == []

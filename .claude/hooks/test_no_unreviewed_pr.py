"""Tests for the no-unreviewed-pr PreToolUse guard.

Runs the hook wrapper (.claude/hooks/no-unreviewed-pr) the way Claude Code
does: a JSON payload on stdin, exit 2 == block, exit 0 == allow. Every case
builds its own repo in a temp dir, with its own `.review/`.

Run: pytest .claude/hooks/test_no_unreviewed_pr.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from hooktest_support import launch

HOOK = str(Path(__file__).parent / "no-unreviewed-pr")
CREATE = "gh pr create --draft --title t --body-file .pr-body.md"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@e.com")
    _git(r, "config", "user.name", "T")
    _commit(r, "a")
    _git(r, "checkout", "-q", "-b", "feat")
    _commit(r, "b")
    return r


def _head(repo):
    return _git(repo, "rev-parse", "HEAD")


def _run(cmd, cwd, env=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}
    e = dict(os.environ)
    e.pop("PR_REVIEW_OVERRIDE", None)
    e.update(env or {})
    p = subprocess.run(launch(HOOK), input=json.dumps(payload), capture_output=True,
                       text=True, cwd=str(cwd), env=e)
    assert p.returncode in (0, 2), "hook exited %d: %s" % (p.returncode, p.stderr)
    assert "Traceback" not in p.stderr, p.stderr
    return p.returncode, p.stderr


def _verdict(root, sha, head=None, complete=True):
    d = root / ".review" / sha
    d.mkdir(parents=True, exist_ok=True)
    (d / "verdict.json").write_text(json.dumps(
        {"head": head or sha, "verification_complete": complete, "findings": []}))


def _codex(root, sha, text):
    d = root / ".review" / sha
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex.md").write_text(text)


def _log(root):
    path = root / ".review" / "pr-create.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- Commands that create a PR: blocked with no review ------------------------
CREATES = [
    pytest.param(CREATE, id="gh-pr-create"),
    pytest.param("gh pr new", id="gh-pr-new-alias"),
    pytest.param("gh -R o/r pr create --fill", id="gh-global-flag"),
    pytest.param("GH_TOKEN=x gh pr create", id="env-prefix"),
    pytest.param("timeout 60 gh pr create", id="timeout-wrapper"),
    pytest.param("git push -u origin feat && gh pr create", id="after-push"),
    pytest.param("echo $(gh pr create)", id="command-substitution"),
    pytest.param('bash -c "gh pr create --fill"', id="bash-c"),
    pytest.param('bash -lc "gh pr create --fill"', id="bash-lc-flag-cluster"),
    pytest.param("gh api graphql --input request.json", id="graphql-input-file"),
    pytest.param("gh api graphql -F query=@mutation.graphql", id="graphql-query-file"),
    pytest.param('eval "gh pr create --fill"', id="eval"),
    pytest.param("gh api graphql -f query='mutation{createPullRequest(input:{})}'",
                 id="graphql"),
    pytest.param("gh api repos/o/r/pulls -f title=t -f head=feat -f base=main",
                 id="rest-gh-fields"),
    pytest.param("gh api -X POST repos/o/r/pulls", id="rest-gh-post"),
    pytest.param("curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
                 id="rest-curl"),
]


@pytest.mark.parametrize("cmd", CREATES)
def test_pr_creation_without_review_blocks(cmd, repo):
    rc, err = _run(cmd, repo)
    assert rc == 2, "PR creation with no review was allowed: %r" % cmd
    assert "BLOCKED" in err


# --- Commands that do not create a PR: allowed with no review -----------------
NOT_CREATES = [
    pytest.param("gh pr list", id="gh-pr-list"),
    pytest.param("gh pr view 5 --json body", id="gh-pr-view"),
    pytest.param("gh pr edit 5 --body-file .pr-body.md", id="gh-pr-edit"),
    pytest.param("gh api repos/o/r/pulls", id="rest-list"),
    pytest.param("gh api -X GET repos/o/r/pulls -f state=open", id="rest-explicit-get"),
    pytest.param("gh api --method=GET repos/o/r/pulls -f state=open", id="rest-method-eq-get"),
    pytest.param("gh api -XGET repos/o/r/pulls -f state=open", id="rest-glued-get"),
    pytest.param("curl -G https://api.github.com/repos/o/r/pulls -d state=open",
                 id="rest-curl-get"),
    pytest.param("gh api -X GET search/code -f q=createPullRequest", id="search-for-mutation"),
    pytest.param("gh pr create --help", id="gh-pr-create-help"),
    pytest.param("gh help pr create", id="gh-help-pr-create"),
    pytest.param('echo "gh pr create"', id="echo-argument"),
    pytest.param('grep -n "gh pr create" SKILL.md', id="grep-argument"),
    pytest.param("cat <<'EOF'\ngh pr create\nEOF", id="heredoc-body"),
    pytest.param("git log --oneline -3", id="git-log"),
]


@pytest.mark.parametrize("cmd", NOT_CREATES)
def test_non_creation_allows(cmd, repo):
    rc, err = _run(cmd, repo)
    assert rc == 0, "non-creating command was blocked: %r\n%s" % (cmd, err)
    assert _log(repo) == []


# --- Evidence -----------------------------------------------------------------
def test_verdict_for_head_allows_and_logs(repo):
    _verdict(repo, _head(repo))
    rc, err = _run(CREATE, repo)
    assert rc == 0, err
    (entry,) = _log(repo)
    assert entry["route"] == "verdict"
    assert entry["head"] == _head(repo)


def test_codex_output_naming_head_allows(repo):
    _codex(repo, _head(repo), "Reviewed-HEAD: %s\n\nNo findings.\n" % _head(repo))
    rc, err = _run(CREATE, repo)
    assert rc == 0, err
    assert [e["route"] for e in _log(repo)] == ["codex"]


def test_codex_output_without_sha_blocks(repo):
    # A failed run leaves a short last message that names no commit.
    _codex(repo, _head(repo), "stream error: 429 Too Many Requests\n")
    rc, err = _run(CREATE, repo)
    assert rc == 2
    assert "codex.md does not contain" in err


def test_verdict_with_other_head_blocks(repo):
    _verdict(repo, _head(repo), head="0" * 40)
    rc, err = _run(CREATE, repo)
    assert rc == 2
    assert "names head '%s'" % ("0" * 40) in err


def test_incomplete_verdict_blocks(repo):
    _verdict(repo, _head(repo), complete=False)
    rc, err = _run(CREATE, repo)
    assert rc == 2
    assert "verification_complete not true" in err


def test_unparseable_verdict_blocks(repo):
    d = repo / ".review" / _head(repo)
    d.mkdir(parents=True)
    (d / "verdict.json").write_text("{not json")
    rc, err = _run(CREATE, repo)
    assert rc == 2
    assert "is not a JSON object" in err


def test_review_of_an_older_commit_blocks(repo):
    """The retro failure: a commit after the review makes it stale."""
    _verdict(repo, _head(repo))
    _commit(repo, "c")
    rc, err = _run(CREATE, repo)
    assert rc == 2
    assert _head(repo) in err


def test_short_sha_directory_does_not_count(repo):
    _verdict(repo, _head(repo)[:7], head=_head(repo))
    rc, _err = _run(CREATE, repo)
    assert rc == 2


def test_linked_worktree_reads_the_main_checkout(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    sha = _commit(wt, "w")
    # Evidence in the linked worktree's own tree is not where /review-local writes.
    _verdict(wt, sha)
    rc, _err = _run(CREATE, wt)
    assert rc == 2, "evidence outside the main checkout was accepted"
    _verdict(repo, sha)
    rc, err = _run(CREATE, wt)
    assert rc == 0, err
    assert [e["head"] for e in _log(repo)] == [sha]


def test_head_flag_selects_that_branch(repo):
    feat = _head(repo)
    _git(repo, "checkout", "-q", "main")
    _verdict(repo, feat)
    rc, err = _run("gh pr create --head feat --fill", repo)
    assert rc == 0, err
    rc, _err = _run("gh pr create --fill", repo)
    assert rc == 2, "the review of feat was accepted for main's HEAD"


def test_unresolvable_head_flag_blocks(repo):
    _verdict(repo, _head(repo))
    rc, err = _run("gh pr create --head no-such-branch", repo)
    assert rc == 2
    assert "cannot resolve refs/heads/no-such-branch" in err


@pytest.mark.parametrize("cmd", [
    pytest.param("git commit --allow-empty -m x && " + CREATE, id="commit-first"),
    pytest.param("git switch main && " + CREATE, id="switch-first"),
    pytest.param("cd sub && " + CREATE, id="cd-first"),
    pytest.param("git update-ref HEAD HEAD~1 && " + CREATE, id="update-ref-first"),
])
def test_command_that_moves_head_blocks_despite_review(cmd, repo):
    _verdict(repo, _head(repo))
    rc, err = _run(cmd, repo)
    assert rc == 2
    assert "changes directory or HEAD" in err


API_CREATE = "gh api repos/o/r/pulls -f title=t -f head=other -f base=main"


def test_api_creation_ignores_the_review_of_head(repo):
    """An API call names a remote branch, not this checkout's HEAD."""
    _verdict(repo, _head(repo))
    rc, err = _run(API_CREATE, repo)
    assert rc == 2
    assert "use gh pr create" in err
    assert _log(repo) == []


def test_api_creation_with_override_allows_and_logs(repo):
    rc, err = _run('PR_REVIEW_OVERRIDE="bot PR" ' + API_CREATE, repo)
    assert rc == 0, err
    assert [e["route"] for e in _log(repo)] == ["override"]


def test_nul_in_head_flag_blocks_without_crash(repo):
    rc, err = _run('gh pr create --head "\x00"', repo)
    assert rc == 2
    assert "cannot resolve" in err


# --- Override -------------------------------------------------------------------
def test_override_with_reason_allows_and_logs(repo):
    rc, err = _run('PR_REVIEW_OVERRIDE="typo in README" ' + CREATE, repo)
    assert rc == 0, err
    (entry,) = _log(repo)
    assert entry["route"] == "override"
    assert entry["reason"] == "typo in README"


@pytest.mark.parametrize("cmd", [
    pytest.param("PR_REVIEW_OVERRIDE= " + CREATE, id="empty"),
    pytest.param('PR_REVIEW_OVERRIDE="  " ' + CREATE, id="whitespace"),
])
def test_override_without_reason_blocks(cmd, repo):
    rc, err = _run(cmd, repo)
    assert rc == 2
    assert "blank reason" in err


@pytest.mark.parametrize("cmd", [
    pytest.param("export PR_REVIEW_OVERRIDE=x; " + CREATE, id="export-segment"),
    pytest.param("PR_REVIEW_OVERRIDE=x true && " + CREATE, id="other-command"),
])
def test_override_on_another_segment_does_not_count(cmd, repo):
    rc, _err = _run(cmd, repo)
    assert rc == 2


def test_override_in_hook_environment_does_not_count(repo):
    rc, _err = _run(CREATE, repo, env={"PR_REVIEW_OVERRIDE": "x"})
    assert rc == 2


def test_unwritable_log_blocks_the_override(repo):
    (repo / ".review").write_text("a file where the log directory belongs\n")
    rc, err = _run('PR_REVIEW_OVERRIDE="typo" ' + CREATE, repo)
    assert rc == 2
    assert "could not record this PR creation" in err


def test_block_message_paths_use_one_separator(repo):
    """The printed Codex command must run as-is in Git Bash on Windows."""
    rc, err = _run(CREATE, repo)
    assert rc == 2
    root = _git(repo, "rev-parse", "--show-toplevel")
    assert '-o "%s/.review/%s/codex.md"' % (root, _head(repo)) in err, err


def test_outside_a_repository_blocks(tmp_path):
    rc, err = _run(CREATE, tmp_path)
    assert rc == 2
    assert "cannot find the main checkout" in err

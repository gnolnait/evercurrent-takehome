"""Tests for the no-merge-pr PreToolUse guard.

Runs the actual hook wrapper (.claude/hooks/no-merge-pr) the way Claude Code
does: a JSON payload on stdin, exit 2 == block, exit 0 == allow. Covers the
merge vectors, the prefixed/wrapped/substitution/heredoc evasions, the
false-positive cases the guard must let through, character escaping, and the
known obfuscation gaps (xfail).

Run: pytest .claude/hooks/test_no_merge_pr.py
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hooktest_support import PYTHON, launch

HOOK = str(Path(__file__).parent / "no-merge-pr")
HOOK_PY = str(Path(__file__).parent / "no-merge-pr.py")


def _verdict(payload):
    p = subprocess.run(
        launch(HOOK), input=json.dumps(payload), capture_output=True, text=True
    )
    if p.returncode == 2:
        return "BLOCK"
    if p.returncode == 0:
        return "ALLOW"
    raise AssertionError("hook exited %d; stderr=%s" % (p.returncode, p.stderr))


def run_bash(cmd):
    return _verdict({"tool_name": "Bash", "tool_input": {"command": cmd}})


def run_tool(name):
    return _verdict({"tool_name": name, "tool_input": {"foo": "bar"}})


# --- Bash commands that MUST be blocked --------------------------------------
BLOCK = [
    # core vectors
    pytest.param("gh pr merge 123 --squash", id="gh-pr-merge"),
    pytest.param("gh pr merge --auto --delete-branch", id="gh-pr-merge-auto"),
    pytest.param("gh   pr    merge", id="gh-pr-merge-extra-spaces"),
    pytest.param("gh --repo o/r pr merge 5", id="gh-global-flag-before-sub"),
    pytest.param("gh api -X PUT repos/o/r/pulls/123/merge", id="gh-api-rest-put"),
    pytest.param(
        "gh api --method PUT /repos/o/r/pulls/42/merge -f merge_method=squash",
        id="gh-api-method",
    ),
    pytest.param(
        'curl -X PUT https://api.github.com/repos/o/r/pulls/9/merge -d "{}"',
        id="curl-rest",
    ),
    pytest.param(
        'curl -X PUT "https://api.github.com/repos/o/r/pulls/9/merge"',
        id="curl-rest-quoted-url",
    ),
    pytest.param(
        "wget --method=PUT https://api.github.com/repos/o/r/pulls/9/merge",
        id="wget-rest",
    ),
    pytest.param("https PUT api.github.com/repos/o/r/pulls/9/merge", id="httpie-rest"),
    pytest.param(
        "gh api graphql -f query='mutation { mergePullRequest(input:{}) }'",
        id="graphql-gh-api",
    ),
    pytest.param(
        "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{})}'",
        id="graphql-enable-automerge",
    ),
    pytest.param(
        "gh api graphql -f query='mutation{enqueuePullRequest(input:{})}'",
        id="graphql-enqueue-merge-queue",
    ),
    pytest.param(
        'curl https://api.github.com/graphql -d \'{"query":"mutation{mergePullRequest}"}\'',
        id="graphql-curl",
    ),
    pytest.param('gh api -X PUT "repos/o/r/pulls/${PR}/merge"', id="rest-var-pr-number"),
    # operators / splitting
    pytest.param("cd /tmp && gh pr merge 5", id="and-chain"),
    pytest.param("false || gh pr merge", id="or-chain"),
    pytest.param("echo hi; gh pr merge 5", id="semicolon-chain"),
    pytest.param("true | gh pr merge", id="pipe-into-merge"),
    pytest.param("gh pr merge &", id="background-ampersand"),
    pytest.param("cd /tmp\ngh pr merge", id="newline-separated"),
    pytest.param("diff <(gh pr merge 5) /dev/null", id="process-substitution"),
    pytest.param("echo $(gh pr merge 5)", id="command-subst-dollar-paren"),
    pytest.param("echo `gh pr merge 5`", id="command-subst-backtick"),
    pytest.param("{ gh pr merge 5; }", id="brace-group"),
    pytest.param("for x in 1; do gh pr merge $x; done", id="for-do-loop"),
    pytest.param("(echo hi) && (gh pr merge)", id="second-subshell"),
    pytest.param(
        "curl -X PUT https://api.github.com/repos/o/r/pulls/9/merge | cat",
        id="rest-url-then-pipe",
    ),
    # prefixed / wrapped
    pytest.param("GH_TOKEN=ghp_xxx gh pr merge 5", id="env-prefix"),
    pytest.param("A=1 GH_TOKEN=x gh pr merge 5", id="two-env-prefixes"),
    pytest.param("/usr/local/bin/gh pr merge 5", id="abs-path"),
    pytest.param("command gh pr merge 5", id="command-wrapper"),
    pytest.param("sudo gh pr merge", id="sudo-wrapper"),
    pytest.param("doas gh pr merge 5", id="doas-wrapper"),
    pytest.param("setsid gh pr merge 5", id="setsid-wrapper"),
    pytest.param("ionice gh pr merge 5", id="ionice-wrapper"),
    pytest.param("ionice -c2 gh pr merge", id="ionice-flag-wrapper"),
    pytest.param("env GH_TOKEN=x gh pr merge", id="env-wrapper-plus-assign"),
    pytest.param("echo 5 | xargs gh pr merge", id="xargs"),
    pytest.param("xargs -n1 gh pr merge", id="xargs-flag"),
    # arg-taking wrappers (leading duration / niceness / flags before the command)
    pytest.param("timeout 5 gh pr merge 5", id="timeout-duration"),
    pytest.param("timeout -k 10 5 gh pr merge", id="timeout-flags-duration"),
    pytest.param("nice -n5 gh pr merge", id="nice-adjacent"),
    pytest.param("nice -n 5 gh pr merge", id="nice-separate"),
    pytest.param("stdbuf -oL gh pr merge", id="stdbuf-flag"),
    pytest.param(
        "timeout 5 gh api -X PUT repos/o/r/pulls/9/merge", id="timeout-wrapped-rest"
    ),
    pytest.param("eval \"gh pr merge 5\"", id="eval-quoted"),
    pytest.param("eval gh pr merge 5", id="eval-unquoted"),
    pytest.param('bash -c "gh pr merge 5"', id="bash-c"),
    pytest.param("sh -c 'gh api -X PUT repos/o/r/pulls/1/merge'", id="sh-c-rest"),
    pytest.param('bash -c "cd x && gh pr merge"', id="bash-c-with-and"),
    # character escaping
    pytest.param("\\gh pr merge", id="escaped-binary-alias-bypass"),
    pytest.param("gh pr merge \\\n  --squash", id="line-continuation"),
    pytest.param('gh pr merge --body "say \\"hi\\""', id="escaped-quote-in-body"),
    # arithmetic bitshift `<<` is an operator, not a heredoc opener: the phantom
    # delimiter (`2`) never recurs, so the "body" ran to EOF and swallowed the
    # real merge on the next line (fail-open). Unterminated bodies are kept.
    pytest.param("(( 1 << 2 ))\ngh pr merge 123", id="arith-bitshift-paren"),
    pytest.param("echo $((1 << 2))\ngh pr merge 123", id="arith-bitshift-expansion"),
    pytest.param("x=$(( 1 << 2 ))\ngh pr merge 123", id="arith-bitshift-assign"),
    # An expansion glued to the FRONT of the verb still runs it: bash expands
    # before it resolves the command word, so an empty or unset variable leaves
    # plain `gh`. The command word therefore has to match on the literal tail.
    pytest.param("${x}gh pr merge 123", id="expansion-prefixed-command-word"),
    pytest.param("${x:-}gh pr merge 123", id="expansion-default-prefixed-command-word"),
    pytest.param("${a}${b}gh pr merge 123", id="two-expansions-prefixed-command-word"),
    pytest.param("${HOME}/gh pr merge 123", id="expansion-path-prefixed-command-word"),
    # the swallowed body must be RE-SCANNED, not treated as one segment: the
    # merge is not the body's first word here, and in the second case only the
    # SECOND of two pending heredocs is unterminated.
    pytest.param(
        "(( 1 << 2 ))\necho ok\ngh pr merge 123", id="arith-bitshift-merge-not-first-word"
    ),
    pytest.param("echo $((1 << 2))\nfoo && gh pr merge 5", id="arith-bitshift-merge-after-and"),
    # RECURRING-operand variant: the bitshift operand recurs as a later standalone
    # line, so the phantom body closes EARLY (not at EOF) and the run-to-EOF
    # rewind never fires — the real merge on the middle line is dropped as body
    # data unless `<<` inside `(( ))`/`$(( ))` is read as an operator.
    pytest.param("(( 1 << 2 ))\ngh pr merge 5\n2", id="arith-bitshift-recur-paren"),
    pytest.param(": $((0 << Q))\ngh pr merge 5\nQ", id="arith-bitshift-recur-expansion"),
    # A `)` closing a `$((..))`/`$(..)` substitution is NOT a word boundary, so a
    # glued `#` is a literal word char, not a comment (bash runs the merge). A
    # subshell `)` IS a boundary (see ALLOW). An escaped `\)` never closed a frame.
    pytest.param("echo $((1))#;gh pr merge 5", id="comment-after-arith-close-not-comment"),
    pytest.param("echo $(echo x)#;gh pr merge 5", id="comment-after-cmdsub-close-not-comment"),
    pytest.param("echo \\)#;gh pr merge 5", id="comment-after-escaped-paren-not-comment"),
    pytest.param("cat <<A <<B\nbody A\nA\ngh pr merge 123", id="unterminated-second-heredoc"),
    # Many phantom openers must not blow the interpreter stack: a RecursionError
    # would exit 1, which is NOT the hook's block contract (2) and so reads as
    # allow. The body rescan is iterative, not recursive.
    pytest.param(
        "\n".join(["(( 1 << 2 ))"] * 1200) + "\ngh pr merge 1",
        id="many-phantom-openers-no-recursion",
    ),
    # A `<<EOF` inside a comment opens no heredoc (bash ignores the line), so a
    # `gh pr merge` on the next line is a real command, not body data.
    pytest.param("# <<EOF\ngh pr merge 1\nEOF", id="comment-heredoc-lookalike"),
    pytest.param("gh pr merge 1 # <<EOF\nbody\nEOF", id="real-merge-then-comment-heredoc"),
    # `)` is a command terminator, so `#` right after it starts a comment (bash
    # ignores the rest of the line) — the `<<EOF` there opens no heredoc.
    pytest.param("(true)# <<EOF\ngh pr merge 1\nEOF", id="comment-after-close-paren"),
    pytest.param("'>/gh' api -X PUT repos/o/r/pulls/9/merge", id="quoted-lookalike-rest"),
    # Windows names: Git Bash runs `gh.exe` and a full path to it as `gh`.
    pytest.param("gh.exe pr merge 5", id="gh-exe-suffix"),
    pytest.param("GH.EXE pr merge 5", id="gh-exe-upper-case"),
    pytest.param('"/c/Program Files/GitHub CLI/gh.exe" pr merge 5', id="gh-exe-full-path"),
    # Windows shells nested in Bash run their argument text as a command.
    pytest.param('powershell.exe -NoProfile -Command "gh pr merge 5"', id="powershell-command"),
    pytest.param(
        'powershell -ExecutionPolicy Bypass -Command "gh pr merge 5"',
        id="powershell-option-value-before-command",
    ),
    pytest.param('pwsh -c "gh pr merge 5"', id="pwsh-c"),
    pytest.param('powershell "gh pr merge 5"', id="powershell-positional"),
    pytest.param('cmd //c "gh pr merge 5"', id="cmd-double-slash-c"),
    pytest.param("cmd.exe /c gh pr merge 5", id="cmd-c-unquoted"),
    pytest.param("powershell -Command gh pr merge 5", id="powershell-command-unquoted"),
    pytest.param("pwsh -c gh pr merge 5", id="pwsh-c-unquoted"),
    # PowerShell runs bare operands as one command, also after an option value.
    pytest.param("powershell gh pr merge 5", id="powershell-bare-operands"),
    pytest.param(
        "powershell -ExecutionPolicy Bypass gh pr merge 5", id="powershell-option-value-then-operands"
    ),
    # A backslash in the inner command is a path separator, not an escape.
    pytest.param('powershell -Command "C:\\x\\gh.exe pr merge 1"', id="powershell-backslash-path"),
    pytest.param('cmd /c "C:\\Git\\gh.exe pr merge 1"', id="cmd-backslash-path"),
    # An encoded command cannot be read, so it is refused.
    pytest.param("powershell -EncodedCommand ZwBoAA==", id="powershell-encoded"),
    pytest.param("pwsh -enc ZwBoAA==", id="pwsh-encoded-short"),
    pytest.param("powershell /enc ZwBoAA==", id="powershell-encoded-slash"),
    pytest.param("pwsh --EncodedCommand ZwBoAA==", id="pwsh-encoded-double-dash"),
]

# --- Bash commands that MUST be allowed (no false positive) -------------------
ALLOW = [
    pytest.param("git merge main", id="git-merge-local"),
    pytest.param("git merge main 2>&1", id="git-merge-redirect"),
    pytest.param("git merge main | tee log", id="git-merge-piped"),
    pytest.param("gh pr view 123", id="gh-pr-view"),
    pytest.param("gh pr list --json number,mergeable", id="gh-pr-list-mergeable"),
    pytest.param('gh pr create --title "fix merge bug" --body x', id="gh-pr-create"),
    pytest.param("gh pr checks 123", id="gh-pr-checks"),
    pytest.param("gh api repos/o/r/pulls/123", id="gh-api-get-pr"),
    pytest.param('gh pr list --search "merge conflict"', id="gh-pr-list-search"),
    pytest.param("curl https://api.github.com/repos/o/r/pulls/9", id="curl-get-pr"),
    pytest.param("timeout 5 git merge main", id="wrapper-around-git-merge"),
    pytest.param('nice -n5 echo "gh pr merge"', id="wrapper-around-quoted-prose"),
    pytest.param(
        "gh api graphql -f query='mutation{dequeuePullRequest(input:{})}'",
        id="graphql-dequeue-harmless",
    ),
    pytest.param("git push origin HEAD", id="git-push"),
    pytest.param("GIT_SSH=x git push origin main", id="git-push-env-prefix"),
    pytest.param("grep -r mergePullRequest src/", id="grep-mutation-name"),
    pytest.param("rg mergePullRequest packages/", id="ripgrep-mutation"),
    pytest.param("ls -la && pytest packages/backend", id="no-merge-keyword"),
    # false positives the quote/heredoc awareness fixes
    pytest.param(
        'jj describe -m "title" -m "blah gh pr merge and pulls/9/merge and gh api mergePullRequest"',
        id="multiline-commit-message",
    ),
    pytest.param('git commit -m "explain gh pr merge guard"', id="commit-message"),
    pytest.param(
        "cat > f.txt <<'EOF'\nuse gh pr merge\nor gh api ... mergePullRequest\nor PUT /pulls/9/merge\nEOF",
        id="heredoc-body-with-triggers",
    ),
    pytest.param(
        "cat > b.txt <<'EOF'\ndocs gh api mergePullRequest pulls/9/merge\nEOF\ngh pr create --title x --body-file b.txt",
        id="heredoc-then-real-gh-pr-create",
    ),
    # A `#` glued to a subshell-closing `)` IS a comment in bash, so the rest of
    # the line is dropped and the merge never runs — must not false-block.
    pytest.param("(echo hi)#;gh pr merge 5", id="comment-after-subshell-close-is-comment"),
    pytest.param("diff <(git show a) <(git show b)", id="proc-subst-non-merge"),
    pytest.param('echo "$(date) merging the pr"', id="merge-word-in-subst-output"),
    pytest.param("echo hi # gh pr merge", id="trailing-comment"),
    # escaping that must NOT block (a shell would not run gh's merge either)
    pytest.param("echo gh\\ pr\\ merge", id="escaped-spaces-under-echo"),
    pytest.param("\\gh\\ pr\\ merge", id="escaped-spaces-one-token"),
    pytest.param("gh pr merge\\;", id="escaped-semicolon-makes-merge-token"),
    # Windows names and shells that do not merge
    pytest.param("gh.exe pr view 5", id="gh-exe-view"),
    pytest.param(
        'powershell -NoProfile -Command "Get-WinEvent -LogName Application -MaxEvents 1"',
        id="powershell-unrelated-command",
    ),
    pytest.param("powershell -ExecutionPolicy Bypass -File build.ps1", id="powershell-file"),
    pytest.param("powershell -ep Bypass -File build.ps1", id="powershell-ep-is-execution-policy"),
    pytest.param('cmd //c "dir"', id="cmd-unrelated"),
]

# --- PowerShell tool payloads --------------------------------------------------
# The PowerShell tool reaches the same guards. A backslash is not an escape there,
# so a Windows path must still resolve to its program name.
POWERSHELL_BLOCK = [
    pytest.param("gh pr merge 5", id="ps-gh-pr-merge"),
    pytest.param('& "C:\\Program Files\\GitHub CLI\\gh.exe" pr merge 5', id="ps-call-full-path"),
    pytest.param("gh pr list; gh pr merge 5", id="ps-second-statement"),
]

POWERSHELL_ALLOW = [
    pytest.param("gh pr view 5", id="ps-gh-pr-view"),
    pytest.param("Get-ChildItem C:\\Users\\x", id="ps-unrelated"),
]

MCP_BLOCK = [
    pytest.param("mcp__github__merge_pull_request", id="mcp-official"),
    pytest.param("mcp__my-gh-server__merge_pull_request", id="mcp-custom-server"),
    pytest.param("mcp__ccd_pr__set_auto_merge", id="mcp-desktop-auto-merge"),
]

MCP_ALLOW = [
    pytest.param("mcp__github__get_pull_request", id="mcp-get-pr"),
    pytest.param("mcp__github__create_pull_request", id="mcp-create-pr"),
    pytest.param("mcp__github__update_pull_request_branch", id="mcp-update-branch"),
    pytest.param("mcp__observability__search_logs", id="mcp-unrelated"),
    pytest.param("mcp__gh__pr_merge", id="mcp-pr_merge-not-broadened"),
    pytest.param("mcp__gh__pull_request_merge", id="mcp-reversed-not-broadened"),
]

# Deliberate obfuscation: the merge command is hidden from static inspection,
# either by building it dynamically and handing it to eval / a piped shell, or
# by encoding it with bash ANSI-C `$'\xNN'` / `$'\uNNNN'` / octal escapes that
# the shell expands at runtime but shlex does not. A real shell WOULD run the
# merge, so the ideal verdict is BLOCK -- but defeating this would mean flagging
# every `| sh` / `eval $(...)` and decoding every `$'...'`, a broad false-
# positive cost (and the hex form can hide the "merge" keyword entirely, slipping
# even the stage-1 gate). The guard's job is to stop accidental/normal merges,
# not adversarial obfuscation, so these are accepted gaps, documented here as
# xfail rather than silently untested.
OBFUSCATIONS = [
    pytest.param('eval "$(echo gh pr merge)"', id="eval-of-command-substitution"),
    pytest.param("printf 'gh pr merge' | sh", id="printf-piped-to-sh"),
    pytest.param("$'\\u0067\\u0068' pr merge 5", id="unicode-escaped-binary"),
    pytest.param("gh pr $'\\x6d\\x65\\x72\\x67\\x65' 5", id="hex-escaped-keyword"),
]


@pytest.mark.parametrize("cmd", BLOCK)
def test_blocks_merge(cmd):
    assert run_bash(cmd) == "BLOCK"


@pytest.mark.parametrize("cmd", ALLOW)
def test_allows_non_merge(cmd):
    assert run_bash(cmd) == "ALLOW"




def run_powershell(cmd):
    return _verdict({"tool_name": "PowerShell", "tool_input": {"command": cmd}})


@pytest.mark.parametrize("cmd", POWERSHELL_BLOCK)
def test_blocks_merge_from_powershell_tool(cmd):
    assert run_powershell(cmd) == "BLOCK"


@pytest.mark.parametrize("cmd", POWERSHELL_ALLOW)
def test_allows_non_merge_from_powershell_tool(cmd):
    assert run_powershell(cmd) == "ALLOW"


@pytest.mark.parametrize("name", MCP_BLOCK)
def test_blocks_mcp_merge_tool(name):
    assert run_tool(name) == "BLOCK"


@pytest.mark.parametrize("name", MCP_ALLOW)
def test_allows_other_mcp_tools(name):
    assert run_tool(name) == "ALLOW"


@pytest.mark.xfail(
    reason="deliberate interpreter obfuscation (dynamic command handed to "
    "eval/sh); out of the guard's threat model",
    strict=False,
)
@pytest.mark.parametrize("cmd", OBFUSCATIONS)
def test_known_obfuscation_gaps(cmd):
    # Ideal behavior is BLOCK; currently ALLOW. xfail documents the accepted gap.
    assert run_bash(cmd) == "BLOCK"


# --- guard-infrastructure failure must fail CLOSED ----------------------------
def test_broken_parser_fails_closed(tmp_path):
    """A parser that cannot deliver a verdict (import error, missing file,
    missing interpreter -> any exit other than 0/2) must BLOCK the
    merge-shaped command, not fall through as non-blocking."""
    hook_dir = Path(__file__).parent
    wrapper = tmp_path / "no-merge-pr"
    shutil.copy2(hook_dir / "no-merge-pr", wrapper)
    (tmp_path / "no-merge-pr.py").write_text("import sys; sys.exit(1)\n")

    # Hostile TMPDIR: the failure handler must not depend on temp-file creation
    # (a bash heredoc does — and a failed heredoc exits 1 = non-blocking, which
    # would silently undo the printf conversion this test pins).
    env = {**os.environ, "TMPDIR": str(tmp_path / "does-not-exist")}
    p = subprocess.run(
        launch(wrapper),
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 5 --squash"}}
        ),
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2, (
        "broken parser must fail closed (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "BLOCKED" in p.stderr


# --- an unreadable payload must fail CLOSED -----------------------------------
@pytest.mark.parametrize(
    "stdin, message",
    [
        # not valid JSON at all -> the parse guard
        pytest.param(
            "{not valid json", "could not parse the hook payload", id="malformed"
        ),
        # nothing on stdin at all -> also the parse guard, and the likeliest
        # way to reach it in practice: a closed or empty pipe.
        pytest.param("", "could not parse the hook payload", id="empty-stdin"),
        # valid JSON but a top-level array -> the isinstance(dict) guard
        pytest.param("[]", "non-object hook payload", id="non-object"),
    ],
)
def test_bad_payload_fails_closed(stdin, message):
    """A payload the guard cannot read is a protocol violation — the guard
    cannot see what it is guarding, so it must BLOCK, not silently allow, and
    must say so cleanly rather than dump a traceback. Runs the pure-python
    guard directly, NOT the wrapper, so each assertion pins the guard's own
    verdict instead of the wrapper's fail-closed rc fence."""
    p = subprocess.run(
        [PYTHON, HOOK_PY], input=stdin, capture_output=True, text=True
    )
    assert p.returncode == 2, (
        "bad payload must fail closed (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "Traceback" not in p.stderr
    assert "failing closed" in p.stderr
    assert message in p.stderr


def test_malformed_payload_blocks_through_the_wrapper():
    """End to end, the GUARD must block a malformed payload — not the fence.

    The guard used to exit 0 here, and 0 is a verdict the fence passes straight
    through, so this was an outright fail-OPEN. The exit-2 assertion pins that
    regression, but note it cannot be reddened by blanking the guard's exit:
    the fence coerces every crash to 2 as well. The remaining assertions are
    what distinguish the two layers — the guard's own message, no traceback,
    and none of the fence's interpreter-blaming fallback.
    """
    p = subprocess.run(
        launch(HOOK), input="{not valid json", capture_output=True, text=True
    )
    assert p.returncode == 2, (
        "malformed payload must block end to end (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "Traceback" not in p.stderr
    assert "could not parse the hook payload" in p.stderr
    assert "could not run (python exit" not in p.stderr


# A PreToolUse payload declares tool_name as a usable dispatch key and
# tool_input as an object. A field the guard cannot USE is a protocol violation
# — it cannot trust what it is guarding, so it must fail closed. Three ways to
# be unusable, all here: MISSING (absent or null — `.get()` cannot tell them
# apart); the wrong TYPE (a list / int / dict where a string belongs); and the
# right type carrying no usable VALUE (a blank tool_name — correctly a `str`,
# but not a name). evaluate() declares (str, dict), so this entry point owns the
# check.
UNUSABLE_FIELD = [
    pytest.param({"tool_name": ["Bash"], "tool_input": {"command": "ls"}},
                 id="tool_name-truthy-list"),
    pytest.param({"tool_name": [], "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-falsy-list"),
    pytest.param({"tool_name": 0, "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-int"),
    pytest.param({"tool_name": False, "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-bool"),
    pytest.param({"tool_name": {"n": "Bash"}, "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-dict"),
    pytest.param({"tool_name": "Bash", "tool_input": ["ls"]}, id="tool_input-list"),
    pytest.param({"tool_name": "Bash", "tool_input": []}, id="tool_input-empty-list"),
    pytest.param({"tool_name": "Bash", "tool_input": "ls"}, id="tool_input-str"),
    # tool_name is the dispatch key and the matchers guarantee it is present,
    # so a missing or null one is a violation too — not "some other tool".
    pytest.param({"tool_input": {"command": "gh pr merge 5"}}, id="tool_name-absent"),
    pytest.param({"tool_name": None, "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-null"),
    # A BLANK tool_name is unreadable, not "some other tool". These are `str`,
    # so an isinstance-only check passed them through and the guard exited 0 on
    # a payload carrying a real merge — the same silent ALLOW the falsy
    # non-strings above produced.
    pytest.param({"tool_name": "", "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-empty-str"),
    pytest.param({"tool_name": "   ", "tool_input": {"command": "gh pr merge 5"}},
                 id="tool_name-whitespace"),
    # One level down: `command` is the last field this guard reads, and only on
    # the Bash path — evaluate() returns before reading it for an mcp tool.
    pytest.param({"tool_name": "Bash", "tool_input": {"command": ["gh", "pr", "merge"]}},
                 id="command-list"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": []}},
                 id="command-empty-list"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": 0}}, id="command-int"),
]


@pytest.mark.parametrize("payload", UNUSABLE_FIELD)
def test_unusable_field_fails_closed_cleanly(payload):
    """An unusable tool_name / tool_input must block, and this guard must
    produce that verdict itself.

    A FALSY non-string tool_name ([] / 0 / false) collapsed to "" through
    `or ""`, so the guard ALLOWED a payload that carried a real merge — it
    never inspected the command. A TRUTHY one reached .startswith() inside
    evaluate() and raised AttributeError, so the guard blocked only because the
    bash fence coerces python's exit 1, printing a raw traceback and a message
    that blames the interpreter."""
    p = subprocess.run(
        launch(HOOK), input=json.dumps(payload), capture_output=True, text=True
    )
    assert p.returncode == 2, (
        "unusable field must fail closed (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "Traceback" not in p.stderr, (
        "guard blocked by crashing, not by deciding:\n%s" % p.stderr
    )
    assert "python exit" not in p.stderr, (
        "verdict came from the rc fence, not the guard:\n%s" % p.stderr
    )
    assert p.stderr.startswith("BLOCKED: the no-merge-pr guard received "), p.stderr


# --- quoting must not route around the parser ---------------------------------
def test_blocks_quote_assembled_merge_token():
    """`gh pr m''erge` executes a merge (the shell joins the quoted fragments);
    a raw-substring "merge"/"enqueue" prefilter fast-allowed exactly this, so
    the wrapper must hand EVERY payload to the quote-aware parser."""
    assert run_bash("gh pr m''erge 5 --squash") == "BLOCK"

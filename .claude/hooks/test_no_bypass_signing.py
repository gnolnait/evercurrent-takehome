"""Tests for the no-bypass-signing PreToolUse guard.

Runs the actual hook (.claude/hooks/no-bypass-signing) the way Claude Code
does: a JSON payload on stdin, exit 2 == block, exit 0 == allow.

Run: pytest .claude/hooks/test_no_bypass_signing.py
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from hooktest_support import PYTHON, bash_exe, launch

HOOK = str(Path(__file__).parent / "no-bypass-signing")


def _verdict(cmd, env_extra=None):
    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        launch(HOOK),
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        env=env,
        capture_output=True,
        text=True,
    )
    if p.returncode == 2:
        return "BLOCK"
    if p.returncode == 0:
        return "ALLOW"
    raise AssertionError("hook exited %d; stderr=%s" % (p.returncode, p.stderr))


# ---- blocks: the documented triggers, in the shapes that occur in practice -----------

def test_blocks_plain_no_verify_commit():
    assert _verdict("git commit --no-verify -m x") == "BLOCK"


def test_blocks_no_verify_in_compound_command():
    assert _verdict("cd /tmp && git commit --no-verify -m x") == "BLOCK"


def test_blocks_no_verify_with_env_prefix():
    assert _verdict("FOO=1 BAR=2 git commit --no-verify -m x") == "BLOCK"


def test_blocks_no_verify_under_env_wrapper():
    assert _verdict("env GIT_AUTHOR_NAME=x git commit --no-verify -m x") == "BLOCK"


def test_blocks_no_gpg_sign():
    assert _verdict("git commit --no-gpg-sign -m x") == "BLOCK"


def test_blocks_gpgsign_false_config():
    assert _verdict("git -c commit.gpgsign=false commit -m x") == "BLOCK"


def test_blocks_commit_dash_n_alias():
    """`git commit -n` is --no-verify; the old regex version missed it."""
    assert _verdict("git commit -n -m x") == "BLOCK"


def test_blocks_git_with_global_flags_before_commit():
    assert _verdict("git -C /tmp/repo commit --no-verify -m x") == "BLOCK"


def test_unbalanced_quotes_around_trigger_fail_closed():
    assert _verdict("echo don't && git commit --no-verify -m x") == "BLOCK"


def test_unparseable_dash_n_alias_fails_closed():
    """`-n` is a --no-verify alias, so an unparseable command carrying it must
    fail CLOSED. The fail-closed hint set omitted `-n`, so this allowed."""
    assert _verdict('git commit -n "unclosed') == "BLOCK"


def test_blocks_gpgsign_false_spellings_and_case():
    assert _verdict("git -c commit.gpgSign=false commit -m x") == "BLOCK"
    assert _verdict("git -c commit.gpgsign=0 commit -m x") == "BLOCK"
    assert _verdict("git -c commit.gpgsign=no commit -m x") == "BLOCK"
    assert _verdict("git -c commit.gpgsign=off commit -m x") == "BLOCK"


def test_allows_gpgsign_true():
    assert _verdict("git -c commit.gpgsign=true commit -m x") == "ALLOW"


def test_blocks_gpgsign_empty_value():
    """`commit.gpgsign=` (empty, e.g. from empty interpolation) parses false."""
    assert _verdict("git -c commit.gpgsign= commit -m x") == "BLOCK"


def test_blocks_clustered_dash_n():
    assert _verdict("git commit -nm x") == "BLOCK"
    assert _verdict("git commit -an -m x") == "BLOCK"
    assert _verdict("git commit -anm x") == "BLOCK"


def test_allows_n_consumed_as_short_option_value():
    """-mn attaches 'n' as the VALUE of -m, not a flag."""
    assert _verdict("git commit -mn") == "ALLOW"
    assert _verdict("git commit -am x") == "ALLOW"


def test_blocks_inline_alias_commit_dash_n():
    assert _verdict("git -c alias.ci=commit ci -n -m x") == "BLOCK"
    assert _verdict('git -c "alias.ci=commit -n" ci -m x') == "BLOCK"


def test_allows_inline_alias_clean_commit():
    assert _verdict("git -c alias.ci=commit ci -m x") == "ALLOW"


def test_blocks_shell_alias_with_trigger():
    assert _verdict("git -c 'alias.ci=!git commit --no-verify -m x' ci") == "BLOCK"
    assert _verdict("git -c 'alias.ci=!git commit -n -m x' ci") == "BLOCK"


def test_allows_gpgsign_text_as_data():
    """The config-off string as a pattern/message is data, not a -c value."""
    assert _verdict("git grep commit.gpgsign=false") == "ALLOW"
    assert _verdict("git log --grep commit.gpgsign=false") == "ALLOW"
    assert _verdict("git commit -m commit.gpgsign=false") == "ALLOW"


def test_allows_flag_lookalike_as_option_value():
    """A value-taking option consumes its argument: these commit with a
    message that merely looks like the trigger flag."""
    assert _verdict("git commit -m -n") == "ALLOW"
    assert _verdict("git commit -am -n") == "ALLOW"
    assert _verdict("git commit --message --no-verify") == "ALLOW"


def test_blocks_command_wrapper():
    assert _verdict("command git commit --no-verify -m x") == "BLOCK"


def test_blocks_sudo_time_wrappers():
    assert _verdict("time git commit --no-verify -m x") == "BLOCK"


def test_blocks_sh_c_wrapped_bypass():
    assert _verdict("sh -c 'git commit --no-verify -m x'") == "BLOCK"
    assert _verdict('bash -c "git commit -n -m x"') == "BLOCK"


def test_blocks_eval_wrapped_bypass():
    assert _verdict("eval 'git commit --no-verify -m x'") == "BLOCK"


def test_blocks_git_exe_name():
    """Git Bash runs `git.exe`, any case, and a full path to it as `git`."""
    assert _verdict("git.exe commit --no-verify -m x") == "BLOCK"
    assert _verdict("GIT.EXE commit --no-verify -m x") == "BLOCK"
    assert _verdict('"/c/Program Files/Git/cmd/git.exe" commit -n -m x') == "BLOCK"


def test_allows_git_exe_clean_commit():
    assert _verdict("git.exe commit -m x") == "ALLOW"


def test_blocks_windows_shell_wrapped_bypass():
    """powershell, pwsh and cmd run their argument text as a command."""
    assert _verdict('powershell -NoProfile -Command "git commit --no-verify -m x"') == "BLOCK"
    assert _verdict("pwsh -c 'git commit --no-verify -m x'") == "BLOCK"
    assert _verdict('cmd //c "git commit -n -m x"') == "BLOCK"
    assert _verdict("cmd.exe /c git commit --no-verify -m x") == "BLOCK"


def test_blocks_encoded_powershell_command():
    """An encoded command cannot be read, so it fails closed."""
    assert _verdict("powershell -EncodedCommand ZwBpAHQA") == "BLOCK"


def test_allows_windows_shell_wrapped_clean_commit():
    assert _verdict('powershell -Command "git commit -m x"') == "ALLOW"
    assert _verdict('cmd //c "git status"') == "ALLOW"


def test_blocks_no_gpg_sign_abbreviation():
    """git accepts unambiguous long-option prefixes: --no-gpg-sig, --no-verif."""
    assert _verdict("git commit --no-gpg-sig -m x") == "BLOCK"
    assert _verdict("git commit --no-verif -m x") == "BLOCK"
    assert _verdict("git commit --no-g -m x") == "BLOCK"


def test_blocks_gpgsign_off_on_commit_creating_verbs():
    assert _verdict("git -c commit.gpgsign=false merge --no-ff other") == "BLOCK"
    assert _verdict("git -c commit.gpgsign=false revert HEAD") == "BLOCK"


def test_allows_gpgsign_off_on_readonly_verb():
    """gpgsign=false is inert for a non-commit verb -> must not false-block."""
    assert _verdict("git -c commit.gpgsign=false status --short") == "ALLOW"
    assert _verdict("git -c commit.gpgsign=false log --oneline") == "ALLOW"


def test_allows_pathspec_from_file_consumes_no_verify():
    """--pathspec-from-file <file>: a following --no-verify is its value."""
    assert _verdict("git commit --pathspec-from-file --no-verify -m x") == "ALLOW"


def test_blocks_bash_lc_cluster():
    """`-c` as the trailing char of a leading option cluster (bash -lc)."""
    assert _verdict("bash -lc 'git commit --no-verify -m x'") == "BLOCK"
    assert _verdict("bash -ic 'git commit -n -m x'") == "BLOCK"


def test_blocks_deeply_nested_wrapper_fails_closed():
    """Past the recursion cap, a trigger-shaped payload fails closed."""
    inner = "git commit --no-verify -m x"
    for _ in range(4):
        inner = f"bash -c {inner!r}"
    assert _verdict(inner) == "BLOCK"


def test_blocks_arg_wrapper_forms():
    assert _verdict("nice git commit --no-verify -m x") == "BLOCK"
    assert _verdict("nice -n5 git commit --no-verify -m x") == "BLOCK"
    assert _verdict("timeout 5 git commit --no-verify -m x") == "BLOCK"


def test_blocks_command_and_time_with_options():
    assert _verdict("command -p git commit --no-verify -m x") == "BLOCK"
    assert _verdict("time -p git commit --no-verify -m x") == "BLOCK"


def test_blocks_gpgsign_off_carried_into_shell_alias():
    cmd = "git -c commit.gpgsign=false -c 'alias.ci=!git commit -m x' ci"

    assert _verdict(cmd) == "BLOCK"


def test_blocks_gpgsign_carried_into_wrapped_shell_alias():
    """The alias expansion starts with a transparent wrapper."""
    cmd = "git -c commit.gpgsign=false -c 'alias.ci=!command git commit -m x' ci"

    assert _verdict(cmd) == "BLOCK"


def test_blocks_arg_wrapper_attached_option():
    """Attached-value wrapper options are single -* tokens, still skipped."""
    assert _verdict("stdbuf -oL git commit --no-verify -m x") == "BLOCK"


def test_allows_arg_wrapper_separate_value_option_accepted_open():
    """`timeout -s TERM 5 git ...`: -s takes a separate non-numeric value we
    don't model, so this adversarial-shaped form is accepted-open (a bare
    `timeout 5 git ...` accident IS still caught — see below)."""
    assert _verdict("timeout -s TERM 5 git commit --no-verify -m x") == "ALLOW"
    assert _verdict("timeout 5 git commit --no-verify -m x") == "BLOCK"


def test_allows_bash_running_script_with_dash_c_arg():
    """`bash script.sh -c '...'` runs script.sh; -c is the SCRIPT's arg, not
    bash's, so the quoted string is not a shell payload to recurse into."""
    cmd = "bash script.sh -c 'git commit --no-verify inside a string'"

    assert _verdict(cmd) == "ALLOW"


def test_blocks_bash_o_option_before_dash_c():
    """bash -o pipefail -c '...': -o takes a value, then -c is the payload."""
    cmd = "bash -o pipefail -c 'git commit --no-verify -m x'"

    assert _verdict(cmd) == "BLOCK"


def test_blocks_control_flow_prefixed_bypass():
    """Shell control-flow keywords precede the real command (normal agent Bash)."""
    assert _verdict("if git commit --no-verify -m x; then echo ok; fi") == "BLOCK"
    assert _verdict("! git commit --no-verify -m x") == "BLOCK"
    assert _verdict("{ git commit --no-verify -m x; }") == "BLOCK"
    assert _verdict("until git commit --no-verify -m x; do sleep 1; done") == "BLOCK"


def test_blocks_real_command_on_heredoc_opener_line():
    """The rest of the heredoc-opener line is still a real command."""
    cmd = "git commit --no-verify -m x <<EOF\nirrelevant body\nEOF"

    assert _verdict(cmd) == "BLOCK"


def test_allows_no_verify_as_grep_pattern():
    """`--no-verify` is DATA for grep/log, not a hook-skip option."""
    assert _verdict("git grep -e --no-verify") == "ALLOW"
    assert _verdict("git log --grep --no-verify") == "ALLOW"


def test_blocks_push_no_verify():
    """--no-verify skips the pre-push hook."""
    assert _verdict("git push --no-verify origin main") == "BLOCK"


def test_blocks_rebase_no_verify():
    """--no-verify skips the pre-rebase hook."""
    assert _verdict("git rebase --no-verify origin/main") == "BLOCK"


def test_blocks_am_no_verify_and_dash_n():
    """`git am -n` / `git am --no-verify` skip applypatch hooks."""
    assert _verdict("git am -n patch.mbox") == "BLOCK"
    assert _verdict("git am --no-verify patch.mbox") == "BLOCK"


def test_allows_push_dash_n_dry_run():
    """-n is dry-run for push, not --no-verify."""
    assert _verdict("git push -n origin main") == "ALLOW"


def test_blocks_backslash_quoted_heredoc_post_bypass():
    r"""`<<\EOF` is a (literal) heredoc; a real bypass after its close blocks."""
    cmd = "cat <<\\EOF\nbody git commit --no-verify\nEOF\ngit commit --no-verify -m x"
    assert _verdict(cmd) == "BLOCK"


def test_allows_indented_delimiter_lookalike_in_plain_heredoc():
    """Plain `<<EOF` does NOT close on an indented EOF-looking body line, so a
    trigger later in the same body stays data."""
    cmd = "cat <<EOF > f\n    EOF\ngit commit --no-verify mention in body\nEOF"
    assert _verdict(cmd) == "ALLOW"


def test_blocks_bypass_after_hyphenated_heredoc_delimiter():
    """A hyphenated/dotted delimiter must be captured whole, or the stripper
    over-consumes past the real close and misses a following bypass."""
    cmd = "cat <<END-OF-FILE > notes.md\nbody\nEND-OF-FILE\ngit commit --no-verify -m x"
    assert _verdict(cmd) == "BLOCK"

    cmd2 = "cat <<'EOF.DOC'\nbody git commit --no-verify\nEOF.DOC\ngit commit --no-verify -m y"
    assert _verdict(cmd2) == "BLOCK"

    # `<<EOF!` — a bang-terminated bare delimiter. `[\w.-]+` stopped at `!`,
    # captured "EOF", never matched the "EOF!" close, and swallowed the real
    # bypass below. The bare-word class now matches the same grammar as main's
    # no-commit-to-main guard.
    cmd3 = "cat <<EOF!\nbody git commit --no-verify\nEOF!\ngit commit --no-verify -m z"
    assert _verdict(cmd3) == "BLOCK"


def test_blocks_bypass_after_arithmetic_bitshift():
    """`<<` inside arithmetic (`(( a << b ))`, `$((a << b))`) is a bitshift
    operator, not a heredoc opener — but `_HEREDOC` matches `<< b` as a heredoc
    whose delimiter (`b`/`2`) never recurs as a close line. Dropping that phantom
    body ran to EOF and swallowed a real `git commit --no-verify` on the next
    line (fail-open, the bypass was allowed). An unterminated body is now kept
    (not dropped) so the trigger stays visible -> fail closed (BLOCK)."""
    for expr in ("(( 1 << 2 ))", "echo $((1 << 2))", "x=$(( 1 << 2 ))"):
        cmd = f"{expr}\ngit commit --no-verify -m x"
        assert _verdict(cmd) == "BLOCK", expr


def test_blocks_bypass_after_unterminated_real_heredoc():
    """A genuinely unterminated heredoc — a real `<<EOF` whose delimiter never
    closes — runs to EOF. Its body is KEPT (not dropped) so a `git commit
    --no-verify` swallowed inside can't slip through. Pins the keep-unterminated-
    body branch independently of the arithmetic cases (which never open a
    heredoc)."""
    assert _verdict("cat <<EOF\ngit commit --no-verify -m x") == "BLOCK"
    assert _verdict("cat <<-EOF\n\tgit commit --no-verify -m x") == "BLOCK"


def test_blocks_bypass_after_arithmetic_bitshift_recurring_operand():
    """Recurring-operand variant: when the misread bitshift operand recurs as a
    later standalone line, the phantom heredoc closes EARLY on that line (not at
    EOF), so the keep-unterminated-body guard does not fire and the real bypass
    was swallowed as body data (a genuine fail-open). The context scan now reads
    `<<` inside `$(( ))` / `(( ))` as an operator, so no phantom heredoc opens."""
    for cmd in (
        "(( 1 << 2 ))\ngit commit --no-verify -m x\n2",
        ": $((0 << Q))\ngit commit --no-verify -m x\nQ",
    ):
        assert _verdict(cmd) == "BLOCK", cmd


def test_allows_bitshift_expansion_with_non_bypass_commit():
    """Bash-faithful, not fail-closed-by-blocking: a real bitshift expansion
    (`N=$((1<<4))`) followed by a NON-bypass commit must not be misread as a
    heredoc and must not false-block."""
    assert _verdict("N=$((1<<4))\ngit commit -m ok") == "ALLOW"


def test_blocks_bypass_after_quote_or_comment_heredoc_lookalike():
    """A `<<` inside a quote or a comment is literal / ignored — bash opens no
    heredoc, so the following line is a REAL command. The marker regex matched
    the quoted/commented `<< X` as an opener and consumed the real bypass as its
    body (fail-open). Masking quote/comment offsets keeps the trigger visible,
    matching the two sibling guards which already block these."""
    for cmd in (
        "echo '$((1 << Q))'\ngit commit --no-verify -m x\nQ",
        'echo "<<EOF"\ngit commit --no-verify -m x\nEOF',
        "# <<EOF\ngit commit --no-verify -m x\nEOF",
        # `)` terminates a command, so `#` right after it is a comment opener.
        "(true)# <<EOF\ngit commit --no-verify -m x\nEOF",
    ):
        assert _verdict(cmd) == "BLOCK", cmd


def test_allows_arg_wrapper_not_wrapping_git():
    """`timeout 5 echo git commit ...` — the command is echo, not git; the
    'git' in echo's args must not be mis-read as the command word."""
    cmd = "timeout 5 echo git commit --no-verify is forbidden"

    assert _verdict(cmd) == "ALLOW"


def test_allows_untracked_files_does_not_consume_no_verify():
    """--untracked-files takes an OPTIONAL attached value; a following
    --no-verify is a real flag, not its argument."""
    assert _verdict("git commit --untracked-files --no-verify -m x") == "BLOCK"


def test_allows_dash_u_cluster_mode():
    """-uno == -u no (untracked mode); the 'n' is not -n."""
    assert _verdict("git commit -uno -m x") == "ALLOW"


def test_allows_ambiguous_no_verify_prefix():
    """--no-ver and shorter are ambiguous with --no-verbose (git rejects them),
    so we must not block them as if they were --no-verify."""
    assert _verdict("git commit --no-ver -m x") == "ALLOW"
    assert _verdict("git commit --no-verb -m x") == "ALLOW"


# ---- allows: trigger text as DATA must not block (old version false-blocked) ---------

def test_allows_commit_message_mentioning_no_verify():
    """The 2026-07-06 false-block class: a quoted message with a ';' before
    prose that begins 'git ... --no-verify' was split mid-string by the old
    tr-based parser and matched as a git-leading segment."""
    cmd = 'git commit -m "docs: explain the guard; git commit --no-verify is blocked"'

    assert _verdict(cmd) == "ALLOW"


def test_allows_plain_commit_message_mentioning_flag():
    assert _verdict('git commit -m "explain --no-verify usage"') == "ALLOW"


def test_allows_echo_mentioning_git_no_verify():
    assert _verdict('echo "git commit --no-verify is forbidden"') == "ALLOW"


def test_allows_gh_pr_title_mentioning_flag():
    assert _verdict('gh pr create --title "block --no-verify commits"') == "ALLOW"


def test_allows_dash_n_on_push():
    """-n means dry-run for push, not no-verify."""
    assert _verdict("git push -n origin main") == "ALLOW"


def test_allows_clean_commit():
    assert _verdict("git commit -m x") == "ALLOW"


def test_allows_unrelated_command():
    assert _verdict("ls -la") == "ALLOW"


def test_allows_grep_for_no_verify_in_files():
    assert _verdict("grep -rn -- --no-verify .claude/hooks/") == "ALLOW"


# ---- bypass --------------------------------------------------------------------------

def test_bypass_env_var_allows():
    assert (
        _verdict("git commit --no-verify -m x", env_extra={"BYPASS_GIT_GUARD": "1"})
        == "ALLOW"
    )


# ---- structure: the two-file split must be robust ------------------------------------

import importlib.util  # noqa: E402  (kept next to the tests that use it)
import shutil  # noqa: E402
import sys  # noqa: E402

HOOK_DIR = Path(__file__).parent
CLI_SHIM = HOOK_DIR / "no-bypass-signing.py"
PY_IMPL = HOOK_DIR / "no_bypass_signing_impl.py"


def _load_impl():
    """Import the underscore-named implementation module."""
    spec = importlib.util.spec_from_file_location("no_bypass_signing_impl", PY_IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Trigger-shaped shapes a bash glob pre-filter would have fast-allowed (rc=0)
# while the parser blocks them. With no pre-filter, every git command reaches the
# parser, so all of these must BLOCK end-to-end. Regression pin for the removed
# Stage-1 ⊆ Stage-2 routing invariant.
@pytest.mark.parametrize(
    "cmd",
    [
        "git -c Commit.GpgSign=false merge x",   # config keys are case-insensitive
        "git -c COMMIT.GPGSIGN=0 revert HEAD",
        "git 'am' -n patch.mbox",                # shlex unquotes the verb
        "git c'ommit' -n -m x",                  # split-quoted verb
        "git -c alias.a=am a -n patch.mbox",     # inline alias expands to `am`
        "git am\t-n patch.mbox",                 # tab between `am` and `-n`
    ],
)
def test_blocks_trigger_shaped_that_a_prefilter_would_miss(cmd):
    assert _verdict(cmd) == "BLOCK"


# Representative block + allow shapes spanning the parser's decision surface;
# the seam test asserts the wrapper agrees with the parser on each.
_SEAM_COMMANDS = [
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git -c commit.gpgsign=false commit -m x",
    "git am --no-verify patch.mbox",
    "git push --no-verify origin main",
    "git -c alias.ci=commit ci -n -m x",
    "sh -c 'git commit --no-verify -m x'",
    "git commit -m x",
    "git commit -mn",
    "git push -n origin main",
    "git grep -e --no-verify",
    "ls -la",
]


def test_seam_wrapper_verdict_matches_parser_direct():
    """The wrapper must never reach a verdict the parser wouldn't: for every
    command already exercised in this suite, the full pipeline (stdin JSON ->
    wrapper -> parser) agrees with the parser invoked directly. Mechanically
    forbids reintroducing a bash-side fast-allow that diverges from the parser.
    """
    def parser_direct(cmd):
        rc = subprocess.run(
            [PYTHON, str(CLI_SHIM), cmd], capture_output=True, text=True
        ).returncode
        return "BLOCK" if rc == 2 else "ALLOW" if rc == 0 else "ERROR(%d)" % rc

    for cmd in _SEAM_COMMANDS:
        assert _verdict(cmd) == parser_direct(cmd), cmd


def test_blocked_message_content_on_stderr():
    """A block must explain itself and offer the override hint — not exit 2
    with an empty, unexplained stderr."""
    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    p = subprocess.run(
        launch(HOOK),
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m x"}}
        ),
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2
    assert "BLOCKED" in p.stderr
    assert "BYPASS_GIT_GUARD" in p.stderr


def test_parser_fails_closed_when_scan_raises(monkeypatch, capsys):
    """The bare `except` in main() is the inner fail-closed fence: if scan()
    raises unexpectedly, the parser must still exit 2 and name the exception in
    its message. Without this test the handler is mutation-survivable (deleting
    it or making it exit 0 passes every other test)."""
    mod = _load_impl()

    def _boom(_cmd):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mod, "scan", _boom)
    monkeypatch.setattr(sys, "argv", ["no-bypass-signing.py", "git commit --no-verify -m x"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
    assert "RuntimeError" in capsys.readouterr().err  # repr(e) carried through


def test_wrapper_does_not_inline_python_via_heredoc():
    """CI-portable pin for THIS PR's regression: the parser must stay a sibling
    FILE, never re-inlined via a here-document fed to python. bash 3.2 can't
    parse a heredoc nested in `$()`, which blocked 100% of git. Unlike
    test_wrapper_parses_under_system_bash_3_2 (skips on CI's bash 5.x), this
    static check runs everywhere.
    """
    src = Path(HOOK).read_text()
    assert "no-bypass-signing.py" in src, "wrapper must invoke the sibling parser file"
    assert "<<'PY'" not in src and "<<PY" not in src, "parser must not be inlined via heredoc"
    # No heredoc opener inside a `$(...)` command substitution — the exact
    # bash-3.2 parse killer. Naive paren-depth scan; adequate for this wrapper.
    depth, i = 0, 0
    while i < len(src):
        if src[i : i + 2] == "$(":
            depth += 1
            i += 2
            continue
        if src[i] == ")" and depth:
            depth -= 1
        elif src[i : i + 2] == "<<" and depth:
            raise AssertionError("heredoc inside $() is unparseable on bash 3.2")
        i += 1


def test_wrapper_parses_under_system_bash_3_2():
    """This hook's whole reason to exist: no heredoc-in-$(), so the wrapper must
    PARSE under macOS system bash 3.2 (`/usr/bin/env bash`). `bash -n` is a
    parse-only check. The prior version failed here, blocking 100% of git.

    Skips (rather than fails) where no 3.2 binary exists, e.g. Linux CI on bash
    5.x — which is exactly why this can't be the ONLY guard, but it does pin
    local dev where the regression actually bites.
    """
    ver = subprocess.run(
        [bash_exe(), "-c", "echo $BASH_VERSION"], capture_output=True, text=True
    )
    if not ver.stdout.startswith("3.2"):
        import pytest

        pytest.skip("no bash 3.2 available (got %r)" % ver.stdout.strip())
    p = subprocess.run([bash_exe(), "-n", HOOK], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_wrapper_fails_closed_when_python_impl_is_broken(tmp_path):
    """If no-bypass-signing.py can't produce a clean verdict — syntax/import
    error, an exit code other than 0/2, a signal death — the WRAPPER must still
    BLOCK a trigger-shaped command. A guard that fails open when its own parser
    is broken is worse than no guard: the bypass runs silently.

    Regression for the exec-fail-open: `exec python3 ...` surfaced Python's own
    exit code (1 for a syntax error) to the harness, which treats non-2 as
    non-blocking -> the git bypass was ALLOWED.
    """
    wrapper = tmp_path / "no-bypass-signing"
    shutil.copy(HOOK, wrapper)
    wrapper.chmod(0o755)
    # A sibling .py that raises at import time -> Python exits 1 (not 0/2).
    (tmp_path / "no-bypass-signing.py").write_text("raise SystemExit(1)\n")

    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    # Hostile TMPDIR: the rc-dispatch failure handler must not depend on
    # temp-file creation (a heredoc does; failed heredoc -> exit 1 = allow).
    env["TMPDIR"] = str(tmp_path / "does-not-exist")
    p = subprocess.run(
        launch(wrapper),
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m x"}}
        ),
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2, (
        "a broken python impl must fail CLOSED (exit 2 / block); "
        "got exit %d, stderr=%r" % (p.returncode, p.stderr)
    )


def test_wrapper_fails_closed_when_parser_file_is_missing(tmp_path):
    """A wrapper whose sibling parser file is absent must BLOCK (its dedicated
    missing-parser handler), and must do so even when the failure message
    cannot go through a temp file (hostile TMPDIR pins printf-not-heredoc)."""
    wrapper = tmp_path / "no-bypass-signing"
    shutil.copy(HOOK, wrapper)
    wrapper.chmod(0o755)
    # deliberately NO sibling no-bypass-signing.py

    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    env["TMPDIR"] = str(tmp_path / "does-not-exist")
    p = subprocess.run(
        launch(wrapper),
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m x"}}
        ),
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2, (
        "missing parser must fail CLOSED (exit 2 / block); "
        "got exit %d, stderr=%r" % (p.returncode, p.stderr)
    )
    assert "missing/unreadable" in p.stderr


def test_py_impl_direct_cli_contract():
    """no-bypass-signing.py has a standalone CLI contract (argv[1] -> exit code)
    that the wrapper depends on: block=2, allow=0, and no arg=0 (defensive).
    """
    def rc(*args):
        return subprocess.run(
            [PYTHON, str(CLI_SHIM), *args], capture_output=True, text=True
        ).returncode

    assert rc("git commit --no-verify -m x") == 2   # trigger -> block
    assert rc("git commit -m x") == 0               # clean -> allow
    assert rc() == 0                                # no arg -> allow (unreachable via wrapper)


# --- guard-infrastructure failure must fail CLOSED ----------------------------

_TRIGGER_PAYLOAD = json.dumps(
    {"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify -m x"}}
)


def _infra_verdict(tmp_path, interpreter):
    """Run the wrapper with CLAUDE_HOOK_PYTHON pinned to `interpreter`, the
    fence's explicit interpreter override, so a missing or broken python is
    simulated without rebuilding PATH (which is not portable to Windows)."""
    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    env["CLAUDE_HOOK_PYTHON"] = interpreter
    # Hostile TMPDIR too: failure handlers must not depend on temp-file
    # creation (a bash heredoc does; a failed heredoc exits 1 = non-blocking).
    env["TMPDIR"] = str(tmp_path / "does-not-exist")
    return subprocess.run(
        launch(HOOK), input=_TRIGGER_PAYLOAD, env=env, capture_output=True, text=True
    )


def test_missing_python_fails_closed(tmp_path):
    """Without an interpreter the wrapper cannot see the command it is
    guarding; that must BLOCK loudly (exit 2) — not silently allow, and not die
    at exit 1 trying to emit the message through a heredoc with no writable
    TMPDIR."""
    p = _infra_verdict(tmp_path, str(tmp_path / "no-such-python"))
    assert p.returncode == 2, (
        "missing python must fail closed (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "python" in p.stderr


def test_broken_python_fails_closed(tmp_path):
    """An interpreter that is present but failing must not be swallowed into an
    empty command string (which reads as nothing-to-inspect and allows)."""
    broken = tmp_path / "broken-python"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    p = _infra_verdict(tmp_path, str(broken))
    assert p.returncode == 2, (
        "broken python must fail closed (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "python" in p.stderr


@pytest.mark.parametrize(
    "payload",
    ["[]", '"git commit --no-verify"', '{"tool_input": "git commit --no-verify"}'],
)
def test_non_object_payload_fails_closed(payload):
    """A payload (or tool_input) that is valid JSON but not an object cannot
    carry a command the wrapper can see: block, as jq's indexing error did,
    rather than read it as an empty command and allow."""
    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    p = subprocess.run(
        launch(HOOK), input=payload, env=env, capture_output=True, text=True
    )
    assert p.returncode == 2, (p.returncode, p.stderr)
    assert "could not extract the command" in p.stderr


def test_bypass_guidance_is_accurate():
    """The block message must teach the bypass forms that actually work (env
    exported at session launch / user runs it themselves) and must NOT carry
    the old inline-prefix claim: an inline `BYPASS_GIT_GUARD=1 git ...` in a
    Bash tool call never reaches the hook's environment."""
    env = os.environ.copy()
    env.pop("BYPASS_GIT_GUARD", None)
    p = subprocess.run(
        launch(HOOK), input=_TRIGGER_PAYLOAD, env=env, capture_output=True, text=True
    )
    assert p.returncode == 2
    assert "exported" in p.stderr
    assert "override for one call" not in p.stderr


# --- quoting must not route around the parser ---------------------------------
def test_blocks_quote_assembled_git_token():
    """`g''it commit --no-verify` executes git; any raw-substring gate (script
    prefilter or a settings-level `if: Bash(git *)`) misses the assembled
    token, so the script itself must block it whenever it runs."""
    assert _verdict("g''it commit --no-verify -m x") == "BLOCK"


def test_blocks_backslash_assembled_git_token():
    assert _verdict("g\\it commit --no-verify -m x") == "BLOCK"


# --- gate-removal false-block narrowing (review-panel finding) ----------------
# scan()'s unparseable/over-nested fail-closed paths must match `git` at a word
# boundary, not as a substring of 'legit'/'digit'.
def test_unparseable_non_git_substring_allows():
    assert _verdict("echo 'oops legit -n") == "ALLOW"
    assert _verdict("echo 'digit -n") == "ALLOW"


def test_unparseable_real_git_bypass_still_blocks():
    C = "com" + "mit"
    NV = "--no" + "-verify"
    # unbalanced quote + a real git bypass word -> still fail closed
    assert _verdict("git %s %s -m 'unclosed" % (C, NV)) == "BLOCK"

"""Tests for the no-commit-to-main PreToolUse guard.

Runs the actual hook wrapper (.claude/hooks/no-commit-to-main) the way Claude
Code does: a JSON payload on stdin, exit 2 == block, exit 0 == allow.

Run: pytest .claude/hooks/test_no_commit_to_main.py
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from git_commit_events import msys_to_windows
from hooktest_support import IS_WINDOWS, PYTHON, launch

HOOK = str(Path(__file__).parent / "no-commit-to-main")
HOOK_PY = str(Path(__file__).parent / "no-commit-to-main.py")


class _ShellPath(type(Path())):
    """A Path whose str() is shell-safe on Windows too.

    The tests splice repo paths into shell command strings. A Windows path's
    backslashes are shell escapes (a `cd C:<bs>Users<bs>x` parses as `cd C:Usersx`),
    so render with forward slashes, which git and Windows both accept. On POSIX
    this is a no-op.
    """

    def __str__(self):
        return super().__str__().replace("\\", "/")


@pytest.fixture
def repos(tmp_path):
    tmp_path = _ShellPath(tmp_path)
    project = tmp_path / "project"
    sibling = tmp_path / "sibling"
    missing = tmp_path / "missing"

    for repo in (project, sibling):
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "hook-test@example.com"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Hook Test"],
            cwd=repo,
            check=True,
        )
        (repo / "README.md").write_text("init\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    return {"project": project, "sibling": sibling, "missing": missing}


@pytest.fixture
def worktree(repos):
    """A linked worktree of the project repo, on branch `feat/wt`. It shares
    the project's git dir but has its own HEAD."""
    path = repos["project"].parent / "project-wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feat/wt", str(path)],
        cwd=repos["project"],
        check=True,
    )
    return path


def _verdict(cmd, project, cwd=None, payload_cwd=None, tool="Bash"):
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    payload = {"tool_name": tool, "tool_input": {"command": cmd}}
    if payload_cwd is not None:
        payload["cwd"] = str(payload_cwd)
    p = subprocess.run(
        launch(HOOK),
        input=json.dumps(payload),
        cwd=cwd or project,
        env=env,
        capture_output=True,
        text=True,
    )
    if p.returncode == 2:
        return "BLOCK"
    if p.returncode == 0:
        return "ALLOW"
    raise AssertionError("hook exited %d; stderr=%s" % (p.returncode, p.stderr))


def test_allows_foreign_git_c_commit_while_project_on_main(repos):
    assert (
        _verdict(f"git -C {repos['sibling']} commit -m update", repos["project"])
        == "ALLOW"
    )


def test_blocks_plain_project_commit_on_main(repos):
    assert _verdict("git commit -m update", repos["project"]) == "BLOCK"


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param("git.exe commit -m update", id="git-exe"),
        pytest.param("GIT.EXE commit -m update", id="git-exe-upper-case"),
        pytest.param("/usr/bin/git commit -m update", id="full-path"),
        pytest.param('"/c/Program Files/Git/cmd/git.exe" commit -m update', id="windows-full-path"),
    ],
)
def test_blocks_project_commit_on_main_by_any_git_name(cmd, repos):
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_foreign_commit_by_full_git_path(repos):
    cmd = f'"/c/Program Files/Git/cmd/git.exe" -C {repos["sibling"]} commit -m update'
    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_allows_git_exe_as_an_argument_on_main(repos):
    """Only the command word is read as a program name, not each argument."""
    cmd = "gh issue create --title git.exe --body commit"
    assert _verdict(cmd, repos["project"]) == "ALLOW"


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param('powershell -Command "git commit -m update"', id="powershell"),
        pytest.param("pwsh -c 'git commit -m update'", id="pwsh"),
        pytest.param('cmd //c "git commit -m update"', id="cmd"),
        pytest.param('powershell -Command "GIT commit -m update"', id="powershell-upper-case-program"),
    ],
)
def test_blocks_windows_shell_wrapped_commit_on_main(cmd, repos):
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_commit_on_main_from_powershell_tool(repos):
    assert _verdict("git commit -m update", repos["project"], tool="PowerShell") == "BLOCK"


def test_allows_foreign_commit_with_backslash_path_from_powershell_tool(repos):
    sibling = str(repos["sibling"]).replace("/", "\\")
    cmd = f"git -C {sibling} commit -m update"
    assert _verdict(cmd, repos["project"], tool="PowerShell") == "ALLOW"


def test_allows_plain_project_commit_on_feature_branch(repos):
    subprocess.run(
        ["git", "switch", "-c", "feature/hook-test"],
        cwd=repos["project"],
        check=True,
    )

    assert _verdict("git commit -m update", repos["project"]) == "ALLOW"


def test_project_commit_honors_in_command_branch_switch(repos):
    cmd = f"git -C {repos['project']} checkout -b tmp/hook-test && git -C {repos['project']} commit -m update"

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_allows_foreign_commit_after_project_read_only_git_segment(repos):
    cmd = f"git -C {repos['project']} status && git -C {repos['sibling']} commit -m update"

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_unresolvable_git_c_path_on_main(repos):
    assert (
        _verdict(f"git -C {repos['missing']} commit -m update", repos["project"])
        == "BLOCK"
    )


def test_allows_foreign_commit_after_cd_to_sibling(repos):
    assert (
        _verdict(f"cd {repos['sibling']} && git commit -m update", repos["project"])
        == "ALLOW"
    )


def test_allows_foreign_commit_with_semicolon_in_message(repos):
    """A ';' inside a quoted -m message must not split the parse and misattribute
    the commit to the project repo (false positive hit 2026-07-06)."""
    cmd = f'cd {repos["sibling"]} && git commit -m "first clause; second clause"'

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_allows_foreign_git_c_commit_with_shell_operators_in_message(repos):
    cmd = f'git -C {repos["sibling"]} commit -m "do X && do Y | done"'

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_project_commit_with_semicolon_in_message_on_main(repos):
    assert (
        _verdict('git commit -m "first clause; second clause"', repos["project"])
        == "BLOCK"
    )


def test_blocks_commit_after_comment_line(repos):
    """A '#' must not swallow the rest of the command (shlex's comment rule
    differs from the shell's and would hide the commit from the guard)."""
    assert _verdict(": # note\ngit commit -m update", repos["project"]) == "BLOCK"


def test_blocks_commit_with_hash_inside_env_assignment(repos):
    assert (
        _verdict("GIT_AUTHOR_NAME=a#b git commit -m update", repos["project"])
        == "BLOCK"
    )


def test_blocks_commit_after_subshell_cd(repos):
    """A cd inside (...) is scoped to the subshell; the commit after the
    closing paren runs in the project repo and must stay blocked."""
    assert (
        _verdict(f"(cd {repos['sibling']}) && git commit -m update", repos["project"])
        == "BLOCK"
    )


def test_blocks_commit_after_command_substitution_cd(repos):
    cmd = f"echo $(cd {repos['sibling']}); git commit -m update"

    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_commit_inside_subshell_after_cd(repos):
    """A commit inside the parens genuinely runs in the cd'd sibling."""
    cmd = f"(cd {repos['sibling']} && git commit -m update)"

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_quoted_project_git_c_path(repos):
    cmd = f'git -C "{repos["project"]}" commit -m "a; b"'

    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_switch_back_to_main_after_branch_name_containing_operator(repos):
    """A quoted branch name with a shell operator inside it must not
    desynchronize the walk (a quote-blind split used to mis-read it): the later
    switch back to `main` still re-protects the commit."""
    cmd = 'git checkout -b "feat;x" && git checkout main && git commit -m update'

    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_commit_on_main_prefixed_feature_branch(repos):
    """Only `main` itself is protected; a branch merely prefixed `main-` is an
    ordinary feature branch."""
    cmd = 'git checkout -b "main-x;y" && git commit -m update'

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_allows_switch_to_feature_branch_name_containing_operator(repos):
    cmd = 'git checkout -b "feat;x" && git commit -m update'

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_sibling_switch_does_not_unprotect_project_commit(repos):
    """A checkout in a sibling repo must not move the PROJECT's branch state
    off protected — the later plain commit still runs in the project."""
    cmd = f"git -C {repos['sibling']} checkout -b feat/sib && git commit -m update"

    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_plain_commit_when_payload_cwd_is_sibling(repos):
    """The Bash tool's cwd (payload .cwd) is where a plain commit runs; a
    sibling cwd must not be attributed to the project repo."""
    assert (
        _verdict("git commit -m update", repos["project"], payload_cwd=repos["sibling"])
        == "ALLOW"
    )


def test_blocks_shell_wrapper_commit(repos):
    """A commit-shaped payload inside sh -c / eval is code this parser doesn't
    descend into — ambiguous, fail closed."""
    assert _verdict('sh -c "git commit -m update"', repos["project"]) == "BLOCK"
    assert _verdict('eval "git commit -m update"', repos["project"]) == "BLOCK"


def test_allows_sibling_commit_message_mentioning_git_commit(repos):
    """Wrapper detection keys on the command word, not message content."""
    cmd = f'cd {repos["sibling"]} && git commit -m "how to use git commit"'

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_git_dir_redirected_commit(repos):
    """--git-dir/--work-tree redirect the repo away from the effective dir;
    attribution is ambiguous, fail closed."""
    cmd = (
        f"cd {repos['sibling']} && git --git-dir {repos['project']}/.git "
        f"--work-tree {repos['project']} commit -m update"
    )

    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_env_redirected_commit(repos):
    cmd = (
        f"cd {repos['sibling']} && GIT_DIR={repos['project']}/.git "
        f"GIT_WORK_TREE={repos['project']} git commit -m update"
    )

    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_unresolvable_variable_repo_path_fails_closed(repos):
    """A shell variable's in-command value is unknowable here (the hook env
    may hold a stale value) — fail closed even though the real target may be
    a sibling."""
    cmd = f'W={repos["sibling"]}; git -C "$W" commit -m update'

    assert _verdict(cmd, repos["project"]) == "BLOCK"


# --------------------------------------------------------------------------------------
# Heredoc bodies are stdin DATA, not shell (false block 2026-07-07: a sibling
# commit preceded by a prose heredoc — apostrophes/semicolons in the body — was
# denied via the ValueError->AMBIGUOUS path). Interpreter-fed heredocs are code
# and must stay fail-closed.
# --------------------------------------------------------------------------------------


def test_allows_sibling_heredoc_prose_then_commit(repos):
    """Replay of the 2026-07-07 false block (trimmed)."""
    cmd = (
        f"cd {repos['sibling']}\n"
        "cat >> notes.md <<'EOF'\n"
        "retired entries; pushed detail down (user's summary, don't optimize)\n"
        "the guard's quote-parser chokes on this lone apostrophe; git commit words too\n"
        "EOF\n"
        "git add -A && git commit -m update"
    )
    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_allows_project_data_heredoc_documenting_git_commit(repos):
    """A heredoc that merely DOCUMENTS `git commit` (no real commit) is data."""
    cmd = (
        "cat >> notes.md <<'EOF'\n"
        "to fix, run: git commit -m something\n"
        "EOF\n"
        "echo done"
    )
    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_project_commit_after_data_heredoc(repos):
    """A REAL commit on main after an unrelated data heredoc still blocks."""
    cmd = (
        "cat >> notes.md <<'EOF'\n"
        "just some prose; nothing here\n"
        "EOF\n"
        "git commit -m update"
    )
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_quoted_punctuation_delimiter_body_is_data(repos):
    """A QUOTED delimiter may contain punctuation (`<<'EOF!'`); its body must
    still be treated as data, not shell — else a `git checkout` in the body
    unprotects a later real commit (fail-open) and body prose false-blocks."""
    # fail-open guard: body switch must NOT unprotect the real commit
    fo = "cat <<'EOF!'\ngit checkout -b feat/body\nEOF!\ngit commit -m real"
    assert _verdict(fo, repos["project"]) == "BLOCK"
    # false-block guard: on the PROJECT repo on main, body prose mentioning
    # git commit with no real commit must not emit a phantom commit event
    fb = "cat <<'EOF!' >/dev/null\nthis documents git commit -m x\nEOF!\necho done"
    assert _verdict(fb, repos["project"]) == "ALLOW"


def test_unquoted_punctuation_delimiter_body_is_data(repos):
    """A BARE delimiter may also carry punctuation (`<<EOF!`, `<<EOF\\!`); bash
    reads to the matching `EOF!` line, so the body is data. Capturing only the
    word-prefix `EOF` never matched the close line and swallowed the later real
    commit as body (fail-open, found live by adversarial review)."""
    # fail-open guard: the real commit after the closed body must BLOCK on main
    fo = "cat <<EOF!\njust prose here\nEOF!\ngit commit -m real"
    assert _verdict(fo, repos["project"]) == "BLOCK"
    # backslash-escaped punctuation: bash quote-removal makes the marker `EOF!`
    fo2 = "cat <<EOF\\!\njust prose here\nEOF!\ngit commit -m real"
    assert _verdict(fo2, repos["project"]) == "BLOCK"
    # false-block guard: body prose mentioning git commit (no real commit) allows
    fb = "cat <<EOF! >/dev/null\nthis documents git commit -m x\nEOF!\necho done"
    assert _verdict(fb, repos["project"]) == "ALLOW"


def test_substitution_delimiter_fails_closed(repos):
    """A delimiter word carrying substitution syntax (`<<$(echo EOF)`,
    ``<<`echo EOF``` , `<<$((1))`) is read by bash as a larger word than this
    scanner captures; stripping a partial-prefix body would hide the later real
    commit (fail-open). These must fail CLOSED — `<<` stays a plain operator so
    the real commit on main is still blocked."""
    for delim in ("$(echo EOF)", "`echo EOF`", "$((1))"):
        cmd = f"cat <<{delim}\njust prose\n{delim}\ngit commit -m real"
        assert _verdict(cmd, repos["project"]) == "BLOCK", delim


def test_unterminated_real_heredoc_fails_closed(repos):
    """A genuinely unterminated heredoc — a real `<<EOF` whose delimiter never
    appears as a close line — runs to EOF. The body can't be located, so a real
    `git commit` swallowed inside it must not slip through: mark ambiguous and
    fail closed (BLOCK). Pins the run-to-EOF branch independently of the
    arithmetic cases (which no longer open a heredoc at all)."""
    assert _verdict("cat <<EOF\ngit commit -m real", repos["project"]) == "BLOCK"
    assert _verdict("cat <<-EOF\n\tgit commit -m real", repos["project"]) == "BLOCK"


def test_arithmetic_bitshift_is_not_a_heredoc(repos):
    """`<<` inside arithmetic (`(( a << b ))`, `$((a << b))`) is a bitshift
    operator, not a heredoc opener — but the marker scanner matches `<< b` as a
    heredoc whose delimiter (`b`/`2`) never recurs as a close line. Before the
    fix the phantom body ran to EOF and SWALLOWED the real `git commit` on the
    next line (fail-open, allowed on main). An opened body that never reaches
    its close delimiter is now unresolvable -> fail closed (BLOCK)."""
    for expr in ("(( 1 << 2 ))", "echo $((1 << 2))", "x=$(( 1 << 2 ))"):
        cmd = f"{expr}\ngit commit -m real"
        assert _verdict(cmd, repos["project"]) == "BLOCK", expr


def test_arithmetic_bitshift_recurring_operand_swallows_commit(repos):
    """Recurring-operand variant of the arithmetic bitshift fail-open. The
    misread delimiter (`Q`) here RECURS as a later standalone line, so the
    phantom heredoc body closes EARLY (not at EOF) — the `if not closed`
    fail-closed guard never fires and the real `git commit` on the middle line
    is swallowed as body data (no event -> ALLOW on main). Bash runs the commit
    (the `<<` is arithmetic, `Q` is a separate command that errors). Must BLOCK.
    """
    # $(( )) form: `Q` recurs as a trailing line -> early phantom close
    fo = ": $((0 << Q))\ngit commit -m real\nQ"
    assert _verdict(fo, repos["project"]) == "BLOCK"
    # (( )) command form with a recurring operand
    fo2 = "(( 0 << Q ))\ngit commit -m real\nQ"
    assert _verdict(fo2, repos["project"]) == "BLOCK"
    # a genuine heredoc inside a command-sub inside arithmetic keeps its body as
    # DATA (bash feeds it to `cat`); the switch in that body must NOT unprotect
    # the later real commit (fail-open guard for the nested-cmdsub case).
    nested = ": $(( $(cat <<E\ngit checkout -b feat/x\nE\n) << 2 ))\ngit commit -m real"
    assert _verdict(nested, repos["project"]) == "BLOCK"


def test_arithmetic_introducer_split_by_line_continuation(repos):
    """Bash removes `\\`+newline continuations before tokenizing, so `$(\\<nl>(`
    and `(\\<nl>(` open arithmetic. Matching the introducer on the raw text split
    the token, classified it as a command-sub/subshell, and misread the inner
    `<<` as a heredoc that swallowed the real commit (fail-open). Introducers are
    now matched over collapsed continuations."""
    for expr in (": $(\\\n(0 << X))", "(\\\n(0 << X))", ": $(( $(\\\n(0 << X)) + 0 ))"):
        cmd = f"{expr}\ngit commit -m real\nX"
        assert _verdict(cmd, repos["project"]) == "BLOCK", expr


def test_arithmetic_subscript_and_grouping_do_not_desync(repos):
    """`$(( ))` counts parens and ignores brackets, so an array subscript
    (`a[0]`) or grouping parens inside it don't desync the frame — the `<<`
    stays an operator and the real commit on main is still seen. (The bracket-
    delimited `$[ ]` form has no such immunity and is a documented residual.)"""
    for expr in ("a=(0)\n: $(( a[0] << Q ))", "a=(0)\n: $(( a[0] + (1 << Q) ))"):
        cmd = f"{expr}\ngit commit -m real\nQ"
        assert _verdict(cmd, repos["project"]) == "BLOCK", expr


def test_backtick_command_sub_does_not_expose_heredoc_switch(repos):
    """Backtick command substitution is not context-tracked, but a heredoc/switch
    smuggled through it must still fail closed (guards against the ctx desync a
    `` `((` `` or a backtick-nested heredoc inside `$(( ))` could otherwise
    cause). Both land a real commit on main in bash, so both must BLOCK."""
    # backtick-nested real heredoc inside arithmetic
    c1 = (
        ": $(( `printf 1; cat >/dev/null <<E\n"
        "git checkout -b feat/body\nE\n` << 2 ))\ngit commit -m real"
    )
    assert _verdict(c1, repos["project"]) == "BLOCK"
    # a stray `((` inside backticks must not leak an arith frame onto a later
    # real heredoc (leaving its switch body inline would unprotect the commit)
    c2 = (
        "`((`\ncat <<EOF >/dev/null\ngit checkout -b feat/body\nEOF\n"
        "git commit -m real"
    )
    assert _verdict(c2, repos["project"]) == "BLOCK"


def test_quoted_dollar_delimiter_body_is_data(repos):
    """A `$`/backtick INSIDE a quoted delimiter is a literal delimiter char, not
    substitution — the body is still stripped as data (bailing on it, as a raw
    `$`-scan would, wrongly exposes the body's git-commit prose: a false block)."""
    for d in ("'a$b'", "'a`b'"):
        marker = d[1:-1]  # the literal close line (quotes removed)
        # body prose mentioning git commit, no real commit -> allow
        fb = f"cat <<{d} >/dev/null\nthis documents git commit -m x\n{marker}\necho done"
        assert _verdict(fb, repos["project"]) == "ALLOW", d
        # a real commit after the closed body still blocks (delimiter recognized)
        real = f"cat <<{d}\nprose\n{marker}\ngit commit -m real"
        assert _verdict(real, repos["project"]) == "BLOCK", d


def test_plain_heredoc_tab_indented_delim_is_body(repos):
    """Plain `<<` does NOT strip leading tabs: a tab-indented delimiter line is
    body data (bash closes only on the flush delimiter). Treating it as an early
    close wrongly exposed the body's `git commit` line (false block). `<<-` DOES
    strip tabs and must still close on a tab-indented delimiter."""
    # plain `<<`: tab-indented EOF and the git-commit line are BOTH body
    plain = "cat <<EOF >/dev/null\n\tEOF\ngit commit -m body\nEOF\necho done"
    assert _verdict(plain, repos["project"]) == "ALLOW"
    # `<<-`: leading tabs stripped, so the tab-indented EOF closes the body and
    # the later real commit on main is seen
    dash = "cat <<-EOF\n\tsome body\n\tEOF\ngit commit -m real"
    assert _verdict(dash, repos["project"]) == "BLOCK"


def test_backslash_newline_in_delimiter_word(repos):
    """A line-continuation (`\\`+newline) INSIDE the delimiter word: bash joins
    it away, so `<<EO\\<nl>F` closes on `EOF`. Capturing only the pre-join prefix
    never matched the real close line and swallowed a later real commit (fail-
    open, found live). Also guards the `<<-\\<nl>` case where the delimiter regex
    could backtrack `-?` to empty and treat `-` as the delimiter."""
    # mid-word continuation
    c1 = "cat <<EO\\\nF\nbody\nEOF\ngit commit -m real"
    assert _verdict(c1, repos["project"]) == "BLOCK"
    # continuation right after `<<-` (tab-strip) — the -? backtrack case
    c2 = "cat <<-\\\nEOF\nbody\n\tEOF\ngit commit -m real"
    assert _verdict(c2, repos["project"]) == "BLOCK"
    # quoted prefix then continuation
    c3 = "cat <<'EO'\\\nF\nbody\nEOF\ngit commit -m real"
    assert _verdict(c3, repos["project"]) == "BLOCK"


def test_crlf_delimiter_body_is_data(repos):
    r"""CRLF line endings: bash treats the CR as an ordinary char in the
    delimiter word, so `<<EOF\r` closes on the line `EOF\r`. Stopping the
    capture at the CR (via `\s`) matched the wrong close line and swallowed the
    later real commit (fail-open, found live)."""
    cmd = "cat <<EOF\r\nbody\r\nEOF\r\ngit commit -m real"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_complex_delimiter_fails_closed(repos):
    r"""A delimiter this scanner can't resolve to bash's literal close marker
    unambiguously — an embedded backslash-escape in a double-quoted delimiter
    (bash quote-removal: `<<"EO\"F"` closes on `EO"F`), a mixed quote+bare word,
    or a bare word with a trailing `$` — is NOT stripped: `<<` stays an operator
    so the later real commit on main stays visible (fail closed, not fail open)."""
    for delim, close in (
        (r'"EO\"F"', 'EO"F'),          # escaped quote inside double quotes
        (r'"EO\$F"', "EO$F"),          # escaped dollar inside double quotes
        ("'EOF'x", "EOFx"),            # mixed quoted + bare
        ("EOF$X", "EOF$X"),            # bare word with literal $
    ):
        cmd = f"cat <<{delim}\nbody\n{close}\ngit commit -m real"
        assert _verdict(cmd, repos["project"]) == "BLOCK", delim


def test_unsupported_delimiter_body_switch_does_not_unprotect(repos):
    r"""For an unsupported delimiter the body can't be located, so a
    `git checkout -b` in the body must NOT be read as a real switch that
    unprotects a later real commit (fail-open: leaving the body inline let the
    body's checkout emit a spurious switch event). Unsupported heredocs are
    ambiguous → blocked, regardless of a switch-shaped body."""
    for delim, close in (("'EOF'x", "EOFx"), ("EOF$X", "EOF$X"), (r'"EO\"F"', 'EO"F')):
        cmd = f"cat <<{delim}\ngit checkout -b feat/body\n{close}\ngit commit -m real"
        assert _verdict(cmd, repos["project"]) == "BLOCK", delim


def test_unquoted_body_line_continuation_closes_early(repos):
    r"""An UNQUOTED heredoc (`<<EOF`) honors `\`+newline continuations in its
    body: `EO\`+newline+`F` joins to `EOF` and closes the heredoc EARLY (bash
    verified). Matching only exact physical lines closed late and swallowed the
    real later commit as body (fail-open). A QUOTED delimiter has a literal body
    — the same bytes stay body and do NOT close early."""
    # unquoted: the joined EO\<nl>F closes the body; the commit after runs -> BLOCK
    fo = "cat <<EOF\nEO\\\nF\ngit commit -m real\nEOF"
    assert _verdict(fo, repos["project"]) == "BLOCK"
    # quoted: body is literal, the commit line is data (no real commit) -> ALLOW
    ok = "cat <<'EOF' >/dev/null\nEO\\\nF\ngit commit -m x\nEOF\necho done"
    assert _verdict(ok, repos["project"]) == "ALLOW"


def test_herestring_not_treated_as_heredoc(repos):
    """`<<<` is a here-STRING, not a heredoc: it must not be mis-read as a
    heredoc opener that swallows the later real commit as its body."""
    cmd = "cat <<< x >/dev/null\ngit commit -m real"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_bash_heredoc_smuggled_commit(repos):
    """`bash <<EOF` executes its body — a commit inside is ambiguous, fail closed."""
    cmd = "bash <<'EOF'\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_dot_source_stdin_heredoc_commit(repos):
    cmd = ". /dev/stdin <<'EOF'\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_env_S_glued_heredoc_commit(repos):
    cmd = "env -Sbash <<'EOF'\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_sibling_commit_message_via_heredoc(repos):
    """A commit whose -F - message arrives via heredoc attributes normally."""
    cmd = (
        f"cd {repos['sibling']}\n"
        "git commit -F - <<'EOF'\n"
        "message with; operators && 'quotes'\n"
        "EOF"
    )
    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_project_commit_message_via_heredoc_on_main(repos):
    cmd = "git commit -F - <<'EOF'\nsome message\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_commit_after_comment_heredoc_lookalike(repos):
    """`# <<x` in a comment is not a heredoc — it must not swallow the commit."""
    cmd = "# <<true\ngit commit -m update\ntrue"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_commit_after_comment_following_close_paren(repos):
    """`)` terminates a command, so `#` right after it opens a comment (bash
    ignores the rest of the line) — the `<<x` there is not a heredoc and must
    not swallow the real commit on the next line."""
    cmd = "(true)# <<true\ngit commit -m update\ntrue"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_hash_after_substitution_close_is_not_a_comment(repos):
    """A `)` closing a `$((..))`/`$(..)` substitution is NOT a word boundary, so
    a glued `#` is a literal word char, not a comment. Mis-reading it as one hid
    a real `<<EOF` opener, so the body's `git checkout -b evil` was parsed as a
    live switch that unprotected the later commit (a protected-branch fail-open;
    bash keeps the checkout as heredoc DATA and lands the commit on main). Only a
    subshell-closing `)` opens a comment (see the test above); an escaped `\\)`
    never closed a frame, so it is not a boundary either."""
    for prefix in ("echo $((1))", "echo $(echo y)", "echo \\)"):
        cmd = f"{prefix}#<<EOF\ngit checkout -b evil\nEOF\ngit commit -m x"
        assert _verdict(cmd, repos["project"]) == "BLOCK", prefix
    # same-line: `$((1))#;git commit` — the `#` is literal, `;` splits, commit runs
    assert _verdict("echo $((1))#;git commit -m x", repos["project"]) == "BLOCK"


def test_blocks_piped_heredoc_into_bash(repos):
    """A body piped into a shell is code reaching that shell's stdin."""
    cmd = "cat <<'EOF' | bash\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_abs_path_bash_heredoc_commit(repos):
    cmd = "/bin/bash <<'EOF'\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_env_wrapped_bash_heredoc_commit(repos):
    cmd = "env bash <<'EOF'\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_multi_hop_piped_heredoc(repos):
    """Body content can flow through intermediate pipe stages (tee/cat)."""
    cmd = "cat <<'EOF' | tee /tmp/hookdoc-probe | bash\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_piped_heredoc_into_subshell_shell(repos):
    cmd = "cat <<'EOF' | ( bash )\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_blocks_piped_heredoc_into_brace_group_shell(repos):
    cmd = "cat <<'EOF' | { bash; }\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_env_S_noninterpreter_heredoc(repos):
    """`env -S<cmd>` is only code when <cmd> is a shell; `env -Scat` is data."""
    cmd = "env -Scat <<'EOF'\nnote says git commit -m x\nEOF"
    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_env_S_quoted_shell_string_heredoc(repos):
    """`env -S "bash -s"` runs a shell from stdin — the body is code."""
    cmd = "env -S \"bash -s\" <<'EOF'\ngit commit -m smuggled\nEOF"
    assert _verdict(cmd, repos["project"]) == "BLOCK"


def test_allows_piped_heredoc_into_python_dash_c(repos):
    """`python3 -c` executes argv code; piped stdin is data to it."""
    cmd = "cat <<'EOF' | grep git | python3 -c \"print(1)\"\nnote says git commit -m x\nEOF"
    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_allows_piped_heredoc_into_bash_dash_c(repos):
    """`bash -c` executes its argv string; piped stdin is data to it."""
    cmd = "cat <<'EOF' | bash -c 'true'\nnote says git commit -m x\nEOF"
    assert _verdict(cmd, repos["project"]) == "ALLOW"


# --- quoting must not route around the parser ---------------------------------
def test_blocks_quote_assembled_commit_token(repos):
    """`git c''ommit` commits (the shell joins the fragments); the old
    `*commit*` substring fast path allowed exactly this on a protected
    branch."""
    assert _verdict("git c''ommit -m x", repos["project"]) == "BLOCK"


# --- guard-infrastructure failure must fail CLOSED ----------------------------
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
        # valid JSON but a top-level array -> the isinstance(dict) guard.
        # Without it, .get() raises AttributeError and python exits 1; only the
        # wrapper's rc fence turns that into a block, so the python guard —
        # the code guard_runner mirrors — would fail OPEN on its own.
        pytest.param("[]", "non-object hook payload", id="non-object"),
    ],
)
def test_bad_payload_fails_closed(repos, stdin, message):
    """A payload the guard cannot read is a protocol violation — the guard
    cannot see what it is guarding, so it must BLOCK, not silently allow, and
    must say so cleanly rather than dump a traceback. Runs the pure-python
    guard directly, NOT the wrapper, so each assertion pins the guard's own
    verdict instead of the wrapper's fail-closed rc fence."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(repos["project"])
    p = subprocess.run(
        [PYTHON, HOOK_PY],
        input=stdin,
        cwd=repos["project"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2, (
        "bad payload must fail closed (exit 2), got %d; stderr=%s"
        % (p.returncode, p.stderr)
    )
    assert "Traceback" not in p.stderr
    assert "failing closed" in p.stderr
    assert message in p.stderr


# A PreToolUse payload declares tool_name as a usable dispatch key and
# tool_input as an object. The test above covers the TOP-LEVEL shape; these rows
# cover the FIELDS inside a well-shaped object. A field the guard cannot USE is a
# protocol violation — it cannot trust what it is guarding, so it must fail
# closed. Three ways to be unusable, all here: MISSING (absent or null —
# `.get()` cannot tell them apart); the wrong TYPE (a list / int / dict where a
# string belongs); and the right type carrying no usable VALUE (a blank
# tool_name — correctly a `str`, but not a name).
UNUSABLE_FIELD = [
    pytest.param({"tool_name": ["Bash"], "tool_input": {"command": "ls"}},
                 id="tool_name-truthy-list"),
    pytest.param({"tool_name": [], "tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-falsy-list"),
    pytest.param({"tool_name": 0, "tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-int"),
    pytest.param({"tool_name": False, "tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-bool"),
    pytest.param({"tool_name": {"n": "Bash"}, "tool_input": {"command": "ls"}},
                 id="tool_name-dict"),
    pytest.param({"tool_name": "Bash", "tool_input": ["ls"]}, id="tool_input-list"),
    pytest.param({"tool_name": "Bash", "tool_input": []}, id="tool_input-empty-list"),
    pytest.param({"tool_name": "Bash", "tool_input": "ls"}, id="tool_input-str"),
    # tool_name is the dispatch key and the matchers guarantee it is present,
    # so a missing or null one is a violation too — not "some other tool".
    pytest.param({"tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-absent"),
    pytest.param({"tool_name": None, "tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-null"),
    # A BLANK tool_name is unreadable, not "some other tool". These are `str`,
    # so an isinstance-only check passed them through and the guard exited 0 —
    # the same silent ALLOW the falsy non-strings above produced.
    pytest.param({"tool_name": "", "tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-empty-str"),
    pytest.param({"tool_name": "   ", "tool_input": {"command": "git com" + "mit -m x"}},
                 id="tool_name-whitespace"),
    # One level down: `command` is the last field this guard reads.
    pytest.param({"tool_name": "Bash", "tool_input": {"command": ["git", "com" + "mit"]}},
                 id="command-list"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": []}},
                 id="command-empty-list"),
    pytest.param({"tool_name": "Bash", "tool_input": {"command": 0}}, id="command-int"),
]


@pytest.mark.parametrize("payload", UNUSABLE_FIELD)
def test_unusable_field_fails_closed_cleanly(payload, repos):
    """An unusable tool_name / tool_input must block, and this guard must
    produce that verdict itself.

    `!= "Bash"` read any non-string tool_name as "not our tool" and exited 0 —
    a silent ALLOW, and a verdict divergence from the production guard_runner,
    which this shim exists to mirror. A non-dict tool_input reached .get() and
    raised AttributeError, so the guard blocked only because the bash fence
    coerces python's exit 1, printing a raw traceback and a message that blames
    the interpreter."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(repos["project"])
    p = subprocess.run(
        launch(HOOK),
        input=json.dumps(payload),
        cwd=repos["project"],
        env=env,
        capture_output=True,
        text=True,
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
    assert p.stderr.startswith(
        "BLOCKED: the no-commit-to-main guard received "
    ), p.stderr


# --- gate-removal false-block narrowing (review-panel finding) ----------------
# The guard now runs on every Bash command (no `if: Bash(git *)` gate). Its
# ambiguous-heredoc fail-closed path must NOT block a benign data-sink heredoc,
# while still blocking any command that could carry a git commit.
def test_ambiguous_heredoc_benign_datasink_allows(repos):
    V = "$" + "VAR"
    for cmd in ("cat <<%s" % V, "tee out <<%s" % V, "echo hi <<%s" % V):
        assert _verdict(cmd, repos["project"]) == "ALLOW", cmd


def test_ambiguous_heredoc_git_still_blocks(repos):
    V = "$" + "VAR"
    C = "com" + "mit"
    # git token (plain and quote-assembled) and interpreter-fed bodies must
    # still fail closed on a protected branch.
    for cmd in ("git %s -m x <<%s" % (C, V),
                "g''it %s -m x <<%s" % (C, V),
                "bash <<%s" % V):
        assert _verdict(cmd, repos["project"]) == "BLOCK", cmd


# --------------------------------------------------------------------------------------
# Linked worktrees share the project's git dir but carry their own HEAD. The branch
# that decides a commit is the one of the checkout the commit runs in, not the one
# of the main checkout. Found while investigating the 2026-10-09 false block, whose
# own cause was the Git Bash drive path covered by the msys tests below.
# --------------------------------------------------------------------------------------


def test_allows_cd_into_feature_worktree_commit(repos, worktree):
    """Setup: the main checkout is on `main`, the worktree on `feat/wt`.
    Action: cd into the worktree and commit. Assert: the worktree's branch
    decides, so the commit is allowed."""
    cmd = f"cd {worktree} && git add README.md && git commit -m update"

    assert _verdict(cmd, repos["project"]) == "ALLOW"


def test_blocks_cd_into_worktree_on_main(repos, worktree):
    """Setup: the main checkout moves to a feature branch and the worktree
    takes `main`. Action: cd into the worktree and commit. Assert: the commit
    runs on `main`, so it is blocked."""
    subprocess.run(
        ["git", "switch", "-c", "feature/hook-test"],
        cwd=repos["project"],
        check=True,
    )
    subprocess.run(["git", "switch", "main"], cwd=worktree, check=True)
    cmd = f"cd {worktree} && git commit -m update"

    assert _verdict(cmd, repos["project"]) == "BLOCK"


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param('W={wt}; cd "$W" && git commit -m update', id="shell-variable"),
        pytest.param("cd {missing} && git commit -m update", id="missing-dir"),
    ],
)
def test_untrusted_cd_prefix_fails_closed(repos, worktree, shape):
    """A cd target the hook cannot resolve (`$W` is unknowable here, `missing`
    is not a repo) is not trusted: the commit stays attributed to the main
    checkout on `main` even though the real target may be a feature worktree."""
    cmd = shape.format(wt=worktree, missing=repos["missing"])

    assert _verdict(cmd, repos["project"]) == "BLOCK"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/c/Users/x/repo", "C:/Users/x/repo"),
        ("/d", "D:"),
        ("/usr/bin", "/usr/bin"),
        ("C:/Users/x/repo", "C:/Users/x/repo"),
        ("relative/dir", "relative/dir"),
    ],
)
def test_msys_to_windows_maps_only_drive_paths(path, expected):
    assert msys_to_windows(path) == expected


@pytest.mark.skipif(not IS_WINDOWS, reason="Git Bash drive paths exist only on Windows")
def test_allows_cd_to_sibling_via_msys_drive_path(repos):
    """Git Bash writes `/c/Users/...`, and the hook runs git.exe directly,
    which cannot `-C` into that form. Map it, or the sibling is unresolvable
    and its commit fails closed (the false block of 2026-10-09)."""
    sibling = str(repos["sibling"])
    msys = "/" + sibling[0].lower() + sibling[2:]
    cmd = f"cd {msys} && git commit -m update"

    assert _verdict(cmd, repos["project"]) == "ALLOW"

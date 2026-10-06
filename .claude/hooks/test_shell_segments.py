"""Shared shell-segmentation cases for the command guards."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from no_bypass_signing_impl import evaluate as evaluate_signing  # noqa: E402
from no_merge_pr_impl import evaluate as evaluate_merge  # noqa: E402


def _merge_blocks(command):
    return evaluate_merge("Bash", {"command": command})[0]


def _signing_blocks(command):
    return evaluate_signing(command)[0]


GUARDS = [
    pytest.param(_merge_blocks, "gh", "pr merge 7", id="merge"),
    pytest.param(
        _signing_blocks,
        "git",
        "commit --no-verify -m x",
        id="signing",
    ),
]

BLOCK = [
    pytest.param("{command} > /dev/null", id="trailing-stdout"),
    pytest.param("{command} 2>&1", id="trailing-fd-dup"),
    pytest.param("cat <<<TAG\n{command}\nTAG", id="here-string-not-heredoc"),
    pytest.param("cat <<<EOF\n{command}\nEOF", id="here-string-delim-eof"),
    pytest.param(">/dev/null {command}", id="leading-stdout"),
    pytest.param("</dev/null {command}", id="leading-stdin"),
    pytest.param("2>/tmp/x {command}", id="leading-io-number"),
    pytest.param("0<<<WORD {command}", id="leading-io-number-here-string"),
    pytest.param(">>/tmp/x {command}", id="leading-append"),
    pytest.param(">out \\\n{command}", id="line-continuation-after-redirect"),
    pytest.param(">|/tmp/x {command}", id="leading-clobber"),
    # `&>|` is a bash 5.3 SYNTAX ERROR (nothing executes), so either verdict is
    # safe; these rows pin the distinct two-pass parse (`&`, then `>|`) so a
    # regression there cannot drift silently. Verdicts mirror the pre-refactor
    # per-guard rows: leading blocks, redirect-argument form allows.
    pytest.param("&>|/tmp/x {command}", id="leading-combined-clobber"),
    pytest.param("{command} &>/dev/null", id="trailing-both-streams"),
    pytest.param(">|/tmp/x {command} &>/dev/null", id="clobber-and-both-streams"),
    pytest.param("2>&1 {command}", id="leading-fd-dup"),
    pytest.param(">/dev/null 2>&1 {command}", id="leading-redirect-then-fd-dup"),
    pytest.param(">&1 {command}", id="leading-fd-dup-no-io-number"),
    pytest.param(">&out {command}", id="fd-dup-operator-with-file-target"),
    pytest.param("&>out {command}", id="both-streams-with-file-target"),
    pytest.param(">&- {command}", id="fd-close"),
    pytest.param("'>/{binary}' {args}", id="quoted-redirect-lookalike-is-command"),
    pytest.param(": >`{command}`", id="backtick-substitution-as-target"),
    pytest.param(": >$({command})", id="dollar-paren-substitution-as-target"),
    pytest.param(": >${{ {command}; }}", id="alternate-substitution-as-target"),
    pytest.param("GIT_DIR=${{HOME}}/x {command}", id="param-expansion-in-assignment-prefix"),
    pytest.param(
        "GIT_DIR=${{HOME:-${{PWD}}}}/x {command}",
        id="nested-param-expansion-in-assignment-prefix",
    ),
    pytest.param("{binary} -C ${{HOME}}/x {args}", id="param-expansion-in-flag-operand"),
    # A `${...}` may only be glued into its word when it is provably simple.
    # A quoted brace or a nested substitution inside one must keep splitting,
    # or the leftover text swallows the real command that follows.
    pytest.param("${{x:-'}}'}} ; {command}", id="quoted-brace-in-param-expansion"),
    pytest.param("${{x:-$({command})}}", id="substitution-nested-in-param-expansion"),
    pytest.param(
        ">${{x:-$({command})}}/f true",
        id="substitution-nested-in-param-expansion-target",
    ),
    pytest.param(
        ">${{x:-'}}'}}f ; {command}",
        id="quoted-brace-in-param-expansion-target",
    ),
    pytest.param(">a$(true)#; {command}", id="cmdsub-target-glued-hash-then-separator"),
    pytest.param(">a$(true)# {command}", id="cmdsub-target-glued-hash-same-command"),
    pytest.param(">a${{ true; }}#; {command}", id="brace-sub-target-glued-hash"),
    pytest.param(">a${{ true; }}# {command}", id="brace-sub-target-glued-hash-same-command"),
    pytest.param(">a`true`# {command}", id="backtick-target-glued-hash-same-command"),
    pytest.param(">a$((1+1))# {command}", id="arith-target-glued-hash-same-command"),
    pytest.param(">${{HOME}}/x {command}", id="param-expansion-target-then-command"),
    pytest.param("2>${{TMPDIR}}/e.log {command}", id="param-expansion-target-io-number"),
    pytest.param(": > >({command})", id="process-substitution-as-target"),
    pytest.param("<<<TAG {command}", id="here-string-command-position"),
    pytest.param("<<<foo;{command}", id="here-string-before-separator"),
    pytest.param("cat <<<'abc\n{command}", id="unterminated-single-quote-target"),
    pytest.param('cat <<<"abc\n{command}', id="unterminated-double-quote-target"),
]

ALLOW = [
    pytest.param(
        "cat <<EOF > notes.md\n{command}\nEOF",
        id="heredoc-body",
    ),
    pytest.param(
        "cat <<\\EOF\n{command}\nEOF",
        id="backslash-quoted-heredoc-body",
    ),
    pytest.param(
        "cat <<END-OF-FILE\n{command}\nEND-OF-FILE",
        id="hyphenated-heredoc-body",
    ),
    pytest.param(
        "cat <<'EOF.DOC'\n{command}\nEOF.DOC",
        id="quoted-dotted-heredoc-body",
    ),
    pytest.param(
        "cat <<EOF!\n{command}\nEOF!",
        id="punctuated-heredoc-body",
    ),
    pytest.param(
        "echo harmless &>/dev/null {command}",
        id="both-streams-redirect-argument",
    ),
    pytest.param(
        "echo harmless &>|/dev/null {command}",
        id="combined-clobber-redirect-argument",
    ),
    pytest.param("true >> {command}", id="redirect-target-named-command"),
    pytest.param("echo hi > {command}", id="redirect-target-with-trigger-arguments"),
    pytest.param("echo '{command}'", id="quoted-command-mention"),
    pytest.param(
        "cat <<-EOF\n\t{command}\n\tEOF",
        id="tab-stripped-heredoc-terminated-body",
    ),
]


@pytest.mark.parametrize("blocks,binary,args", GUARDS)
@pytest.mark.parametrize("template", BLOCK)
def test_redirect_forms_block_for_each_guard(blocks, binary, args, template):
    command = template.format(
        command=f"{binary} {args}",
        binary=binary,
        args=args,
    )

    assert blocks(command)


@pytest.mark.parametrize("blocks,binary,args", GUARDS)
@pytest.mark.parametrize("template", ALLOW)
def test_redirect_data_allows_for_each_guard(blocks, binary, args, template):
    command = template.format(
        command=f"{binary} {args}",
        binary=binary,
        args=args,
    )

    assert not blocks(command)

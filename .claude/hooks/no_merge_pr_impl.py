#!/usr/bin/env python3

"""no-merge-pr - block any attempt to merge a GitHub pull request.

Claude Code PreToolUse hook. Reads the hook JSON on stdin; exits 2 (with an
explanation on stderr) to block, or 0 to allow.

Why Python, and which interpreter:
  - Merge detection must be QUOTE- and HEREDOC-aware. A merge phrase that lives
    inside a quoted argument or a heredoc body (a commit message, an `echo`, a
    doc being written) is DATA, not a command, and must not trip the guard. We
    tokenize each command segment with shlex and dispatch on the COMMAND WORD:
    only a real merge-capable tool (gh / curl / ...) with merge-shaped args is
    blocked; jj / git / echo / cat carrying merge prose in their args are not.
    The exception is the text of a Windows shell (`powershell -Command "..."`),
    which is searched, not parsed, so merge prose there does block.
  - The bash fences resolve the interpreter (CLAUDE_HOOK_PYTHON, else python3,
    else python, skipping the Windows Store alias stub). This module is
    stdlib-only, so any Python 3.10+ works.

Vectors blocked:
  1. MCP tool   - any `mcp__<server>__*merge_pull_request*` or `*auto_merge*` tool
  2. gh CLI     - `gh pr merge ...`
  3. REST API   - PUT .../pulls/<n>/merge via gh / curl / wget / httpie
  4. GraphQL    - mergePullRequest / enablePullRequestAutoMerge / enqueuePullRequest
                  mutations, only when sent through an actual GitHub API call
                  (enqueue = add to merge queue = deferred merge)

Also recurses into `eval "..."` and `bash -c "..."`, so a merge wrapped in a
shell string is still caught. The text that `powershell`, `pwsh` or `cmd` runs
is not parsed (see shell_segments): it blocks when it names `merge` together
with gh or a GitHub API path, or a merge mutation, and an encoded PowerShell
command always blocks. The command word is read through `command_name`, so
`gh.exe` and a full path to it count as `gh`. The PowerShell tool's command is
read the same way as a Bash command.

Not blocked (intentional): `git merge` / `git push`; and a merge phrase that is
merely an argument to a non-merge command, outside a Windows shell. There is no
bypass.
"""
import re
import shlex

from shell_segments import (
    SHELL_TOOLS,
    WINDOWS_SHELLS,
    command_name,
    split_segments,
    tool_command,
    windows_shell_text,
)

HTTP_TOOLS = {"curl", "wget", "http", "https", "xh", "httpie"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# Bare words that may precede the real command word and should be skipped.
WRAPPERS = {"sudo", "command", "env", "time", "nohup", "exec", "builtin",
            "then", "do", "else", "!", "doas", "setsid"}
# Wrappers that may carry their own option flags and/or a numeric argument
# (duration, niceness, cpu mask, parallelism) before the real command, e.g.
# `timeout 5 gh ...`, `nice -n5 gh ...`, `stdbuf -oL gh ...`, `xargs -n1 gh ...`.
ARG_WRAPPERS = {"timeout", "nice", "ionice", "stdbuf", "xargs", "chrt",
                "taskset", "setpriv"}
NUMERIC_ARG = re.compile(r"^\d+(\.\d+)?[smhd]?$")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
MERGE_PATH = re.compile(r"pulls/[^/\s]+/merge")
GQL_MUT = re.compile(r"mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest")
MCP_MERGE = re.compile(r"merge_pull_request|auto_merge", re.I)
# In Windows shell text: a GitHub client or API path, and the merge mutations.
GITHUB_WORD = re.compile(r"\bgh\b|api\.github\.com|/graphql|pulls/")
GQL_MUT_LOWER = re.compile(GQL_MUT.pattern.lower())


class _Block(Exception):
    def __init__(self, reason, detail):
        self.reason = reason
        self.detail = detail


def block(reason, detail):
    raise _Block(reason, detail)


def _block_message(reason, detail):
    return (
        "BLOCKED: this merges (or enables auto-merge of) a GitHub pull request.\n"
        "  reason: {0}\n  detail: {1}\n\n".format(reason, detail)
        + "Claude must not merge PRs - that is a human decision. Stop and let the\n"
          "user merge it themselves, or ask them to. There is no bypass.\n"
    )


def _strip_expansion_prefix(word):
    """
    Drop leading expansions from a command word: `${x}gh` -> `gh`.

    Bash expands before it resolves the command word, so an expansion glued
    to the front of a verb runs that verb whenever the variable is empty or
    unset — `${x}gh pr merge` merges. The value is unknowable here, so the
    literal tail is what has to be matched; leaving the prefix on makes the
    word `${x}gh`, which matches nothing and lets the merge through.

    Only whole leading expansions are removed. A word that is nothing BUT an
    expansion (`$x`, `${x}`) still resolves to the empty string and is left
    to the caller's "no command word" path, exactly as before — that gap
    predates the shared segmenter and is not narrowed here.
    """
    while word.startswith("$") and len(word) > 1:
        if word[1] == "{":
            depth, k = 0, 1
            while k < len(word):
                if word[k] == "{":
                    depth += 1
                elif word[k] == "}":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
            else:
                return word  # unbalanced: leave it alone
            word = word[k:]
            continue
        k = 1
        while k < len(word) and (word[k].isalnum() or word[k] == "_"):
            k += 1
        if k == 1:
            return word  # `$` followed by something else — not an expansion
        word = word[k:]
    return word


def command_word_and_tokens(seg):
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        toks = seg.split()
    while toks:
        t = toks[0]
        base = t.rsplit("/", 1)[-1]
        if base in WRAPPERS or ENV_ASSIGN.match(t):
            toks.pop(0)
            continue
        if base in ARG_WRAPPERS:
            toks.pop(0)
            # Skip the wrapper's own option flags and numeric arg(s); the real
            # command word is the first token that is neither. The command can't
            # start with "-" or be a bare number, so it is never eaten here.
            while toks and (toks[0].startswith("-") or NUMERIC_ARG.match(toks[0])):
                toks.pop(0)
            continue
        break
    if not toks:
        return "", []
    return command_name(_strip_expansion_prefix(toks[0])), toks


def check_bash(cmd, depth=0):
    if depth > 3:
        return
    for seg in split_segments(cmd):
        cmdword, toks = command_word_and_tokens(seg)
        if not cmdword:
            continue
        rest = toks[1:]

        # Recurse into shell-string wrappers: eval "..." / bash -c "..."
        if cmdword == "eval":
            check_bash(" ".join(rest), depth + 1)
            continue
        if cmdword in SHELLS:
            if "-c" in rest:
                idx = rest.index("-c")
                if idx + 1 < len(rest):
                    check_bash(rest[idx + 1], depth + 1)
            continue
        if cmdword in WINDOWS_SHELLS:
            text = windows_shell_text(cmdword, rest)
            if text is None:
                block("encoded PowerShell command (cannot be read)", seg)
            text = text.lower()
            if GQL_MUT_LOWER.search(text) or ("merge" in text and GITHUB_WORD.search(text)):
                block("merge words in a Windows shell command (not parsed)", seg)
            continue

        # gh pr merge (pr and merge as consecutive tokens, robust to global flags)
        if cmdword == "gh":
            for a, b in zip(rest, rest[1:]):
                if a == "pr" and b == "merge":
                    block("gh pr merge", seg)

        # REST merge endpoint via an HTTP-capable tool. The /pulls/<n>/merge path
        # is merge-specific (the endpoint only accepts PUT).
        if cmdword == "gh" or cmdword in HTTP_TOOLS:
            for t in toks:
                if MERGE_PATH.search(t):
                    block("REST PR-merge endpoint (PUT .../pulls/N/merge)", seg)

        # GraphQL merge mutation, but only through an actual GitHub API call.
        is_api = (cmdword == "gh" and "api" in rest) or (
            cmdword in HTTP_TOOLS
            and any("api.github.com" in t or "/graphql" in t for t in toks)
        )
        if is_api and any(GQL_MUT.search(t) for t in toks):
            block("GraphQL PR-merge mutation", seg)


def evaluate(tool: str, tool_input: dict) -> tuple[bool, str]:
    try:
        if tool.startswith("mcp__"):
            if MCP_MERGE.search(tool):
                block("MCP pull-request merge tool", tool)
            return False, ""

        if tool not in SHELL_TOOLS:
            return False, ""
        cmd = tool_command(tool, tool_input.get("command", "") or "")
        if cmd.strip():
            check_bash(cmd)
    except _Block as blocked:
        return True, _block_message(blocked.reason, blocked.detail)
    return False, ""

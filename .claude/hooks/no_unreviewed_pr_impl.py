"""no-unreviewed-pr - block PR creation until a second-model review names HEAD.

Claude Code PreToolUse guard, dispatched by guard_runner. It blocks a command
that creates a pull request unless a review of the commit the PR would carry
exists under the main checkout:

  <root>/.review/<sha>/verdict.json  written by /review-local. Its "head" must
                                     equal <sha> and "verification_complete"
                                     must be true.
  <root>/.review/<sha>/codex.md      written by `codex exec -p review -o <path>`.
                                     It must contain the full 40-character <sha>.

<root> is the first worktree that `git worktree list --porcelain` prints, the
same root /review-local writes to. <sha> is HEAD of the directory the creation
runs in, or the `--head` branch of `gh pr create`. That directory is the hook
cwd, or <path> when the command starts with `cd <path> &&` and <path> is
absolute, ".", "..", or starts with "./" or "../". A later commit changes <sha>, so a review
goes stale by construction.

Override, for a trivial PR only: an assignment on the same command segment,
  PR_REVIEW_OVERRIDE="<reason>" gh pr create ...
The reason must not be blank. The guard ignores the variable in its own
environment and in every other segment (`export ...;` does not count).

Each allowed creation appends one JSON line to <root>/.review/pr-create.jsonl
with its route (verdict, codex or override). If that write fails, the guard
blocks: an unlogged override defeats the deletion trigger. The log records
allowed attempts, not merged PRs.

Deletion trigger: remove this guard when more than 5 of the last 10 log lines
have route "override". The count command is in the github skill.

Detected (scan broadly): `gh pr create` and its alias `gh pr new`, and three
API forms: the GraphQL createPullRequest mutation, a GraphQL
request read from a file or stdin, and a REST POST to `repos/<o>/<r>/pulls`
(an explicit POST, or gh field flags or curl data flags with no other method).
The shared segmenter finds these inside chains and substitutions. The guard
also reads `eval` strings and `bash -c` strings, including flag clusters such
as `-lc`. Quoted arguments of other commands, heredoc bodies and help
requests (--help, and -h for gh, curl and wget) do not match.

An API creation names a remote branch, not a local commit, so no review marker
can match it. Only the override allows it.

Fail closed: a command blocks when it changes directory anywhere except that
leading `cd <path> &&` (any other cd, pushd or popd), or when it moves HEAD (git checkout/switch/commit/reset/rebase/
merge/pull/cherry-pick/am/revert/update-ref, or a symbolic-ref write), because
the guard reads HEAD before the command runs. An unresolvable HEAD, `--head`
branch or main checkout also blocks.

Not detected: a user-defined gh alias; an httpie POST that has only key=value
items; a GraphQL body sent on stdin through a redirection
(`http POST <url>/graphql < request.json`), because the segmenter removes the
redirection before the guard reads the command. Argument-position escapes of
the shared command-word reader are known and not patched one by one: a flag
between `pr` and the subcommand (`gh pr -R o/r create`), wrapper options
before the command (`env -i`, `env -C dir`, `command -p`, `sudo -u x`,
`exec -a x`), a GraphQL body held in a variable or a substitution
(`-f query="$Q"`), and a help token that is a flag's value
(`gh pr create --title --help`). The dev-kit doc
docs/guard-parser/README.md tracks the shared-parser fix that closes them.
When one command holds two creations and the second log write fails, the
first stays logged as allowed. Two shell_segments gaps, shared with the merge and signing guards, hide
a creation until the segmenter fix lands: an unquoted `{owner}`/`{repo}`
placeholder in an API path, and a substitution inside double quotes
(`url="$(gh pr create)"`).
"""

import datetime
import json
import os
import re
import shlex
import subprocess

from no_merge_pr_impl import HTTP_TOOLS, SHELLS, command_word_and_tokens
from shell_segments import split_segments

OVERRIDE_VAR = "PR_REVIEW_OVERRIDE"
LOG_NAME = "pr-create.jsonl"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# The GraphQL endpoint as an argument: gh's "graphql" or "/graphql", or a URL
# whose path is /graphql or /api/graphql. A field value (head=fix/graphql) does
# not match.
GQL_ENDPOINT = re.compile(r"^(/?graphql|(https?://)?[^/\s=]+(/api)?/graphql)/?(\?.*)?$")
PULLS_PATH = re.compile(r"repos/[^/\s]+/[^/\s]+/pulls/?(\?.*)?$")
# Raw substrings of any command that can create a PR, for the unreadable case.
MAY_CREATE = ("gh", "pulls", "PullRequest", "graphql")
DIR_CHANGERS = {"cd", "pushd", "popd"}
# The one cd the guard follows: the command starts with `cd <path> &&`. Bash
# then runs the rest in <path>, or not at all. The path is one quoted or bare
# word with no expansion.
LEADING_CD = re.compile(
    r"""^\s*cd\s+("[^"$`\\]+"|'[^']+'|[^\s;&|<>()$`\\"'{}~*?\[]+)\s*&&""")
MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
# A background `&`: not part of &&, >&, &>, |& or 2>&1. It backgrounds the
# whole list before it, the leading cd included. A quoted `&` counts too.
BACKGROUND = re.compile(r"(?<![&>|])&(?![&>])")
HEAD_MOVERS = {"checkout", "switch", "commit", "reset", "rebase", "merge",
               "pull", "cherry-pick", "am", "revert", "update-ref"}
HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
# A shell's -c option, alone or in a flag cluster such as -lc.
SHELL_C_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
# A short flag with a file value glued to it: curl -d@body.json.
GLUED_FILE_ARG = re.compile(r"^-[A-Za-z]@")
# Help flags. httpie uses -h for --headers, so -h counts for these only.
SHORT_HELP_TOOLS = {"gh", "curl", "wget"}
GH_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")
CURL_DATA_FLAGS = ("-d", "--data", "--data-raw", "--data-binary",
                   "--data-urlencode", "--json", "--post-data", "--post-file")


class _Ambiguous(Exception):
    pass


def _prefix_and_tokens(seg):
    """Return (tokens before the command word, command word, its tokens)."""
    try:
        full = shlex.split(seg, posix=True)
    except ValueError:
        full = seg.split()
    cmdword, toks = command_word_and_tokens(seg)
    return full[: len(full) - len(toks)], cmdword, toks


def _flag_value(rest, short, long):
    """Value of `-H x`, `-Hx`, `--head x` or `--head=x`; None when absent."""
    for i, tok in enumerate(rest):
        if tok in (short, long):
            if i + 1 >= len(rest):
                raise _Ambiguous("%s has no value" % long)
            return rest[i + 1]
        if tok.startswith(long + "="):
            return tok[len(long) + 1:]
        if tok.startswith(short) and len(tok) > len(short):
            return tok[len(short):]
    return None


def _method(cmdword, rest):
    """The explicit HTTP method of an API call, in upper case, or None."""
    for i, tok in enumerate(rest):
        if tok in ("-G", "--get"):
            return "GET"
        for flag in ("--method", "--request"):
            if tok == flag and i + 1 < len(rest):
                return rest[i + 1].upper()
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1:].upper()
        if tok == "-X" and i + 1 < len(rest):
            return rest[i + 1].upper()
        if tok.startswith("-X") and len(tok) > 2:
            return tok[2:].upper()
    # httpie gives the method as its first argument: `http POST <url>`.
    if cmdword in HTTP_TOOLS and rest and rest[0].upper() in HTTP_METHODS:
        return rest[0].upper()
    return None


def _opaque_body(rest):
    """True when the request body comes from a file or stdin."""
    for tok in rest:
        if tok == "--input" or tok.startswith("--input="):
            return True
        # -F query=@x.graphql, -d @body.json, --data-binary @-, -d@body.json
        value = tok.split("=", 1)[1] if "=" in tok else tok
        if value.startswith("@") or GLUED_FILE_ARG.match(tok):
            return True
    return False


def _has_flag(toks, flags):
    for tok in toks:
        for flag in flags:
            if tok == flag or tok.startswith(flag + "="):
                return True
            # A glued short flag: -ftitle=x, -d{}.
            if len(flag) == 2 and tok.startswith(flag):
                return True
    return False


def _create_kind(cmdword, rest):
    """Return (form, through_api) for a PR creation in one segment.

    form is "" when the segment creates no PR.
    """
    if cmdword == "gh" and rest and rest[0] == "help":
        return "", False
    if "--help" in rest or (cmdword in SHORT_HELP_TOOLS and "-h" in rest):
        return "", False
    if cmdword == "gh":
        for a, b in zip(rest, rest[1:]):
            if a == "pr" and b in ("create", "new"):
                return "gh pr " + b, False
    is_api = (cmdword == "gh" and "api" in rest) or cmdword in HTTP_TOOLS
    if not is_api:
        return "", False
    graphql = any(GQL_ENDPOINT.match(t) for t in rest)
    if graphql:
        if any("createPullRequest" in t for t in rest):
            return "GraphQL createPullRequest", True
        if _opaque_body(rest):
            return "a GraphQL request read from a file or stdin", True
        return "", False
    if any(PULLS_PATH.search(t) for t in rest):
        method = _method(cmdword, rest)
        flags = GH_FIELD_FLAGS if cmdword == "gh" else CURL_DATA_FLAGS
        if method == "POST" or (method is None and _has_flag(rest, flags)):
            return "REST POST .../pulls", True
    return "", False


def _shell_strings(rest):
    """Every string that follows a -c flag, alone or in a cluster such as -lc.

    Each -c counts: one after the script operand (bash script.sh -lc x), and
    one that is an option value (bash --rcfile -lc -c x). Reading too much
    blocks a harmless command; reading only the first lets a creation through.
    """
    return [rest[i + 1] for i, tok in enumerate(rest[:-1]) if SHELL_C_FLAG.match(tok)]


def _writes_symbolic_ref(rest):
    """True for `git symbolic-ref <name> <ref>` or a delete; reading is safe."""
    if "symbolic-ref" not in rest:
        return False
    args = rest[rest.index("symbolic-ref") + 1:]
    if "-d" in args or "--delete" in args:
        return True
    return len([a for a in args if not a.startswith("-")]) >= 2


def _leading_cd(cmd, base):
    """Split off a leading `cd <path> &&`: return (directory, rest of command).

    Returns (base, cmd) when there is none. Only an absolute path, ".", "..",
    or a path that starts with "./" or "../" counts: bash looks any other name,
    .claude included, up in CDPATH.
    """
    m = LEADING_CD.match(cmd)
    if not m:
        return base, cmd
    path = m.group(1)
    if path[0] in "\"'":
        path = path[1:-1]
    if os.name == "nt":
        # Git Bash spells C:/x as /c/x.
        path = MSYS_DRIVE.sub(lambda d: d.group(1).upper() + ":/", path)
    if not (os.path.isabs(path) or path in (".", "..") or path.startswith(("./", "../"))):
        return base, cmd
    if BACKGROUND.search(cmd, m.end()):
        return base, cmd
    return os.path.normpath(os.path.join(base, path)), cmd[m.end():]


def _scan(cmd, found, movers, depth=0):
    """Collect each PR-creation segment and each dir change or HEAD move."""
    if depth > 3:
        raise _Ambiguous("shell strings nest too deep to read")
    for seg in split_segments(cmd):
        prefix, cmdword, toks = _prefix_and_tokens(seg)
        if not cmdword:
            continue
        rest = toks[1:]
        if cmdword == "eval":
            _scan(" ".join(rest), found, movers, depth + 1)
            continue
        if cmdword in SHELLS:
            for string in _shell_strings(rest):
                _scan(string, found, movers, depth + 1)
            continue
        if cmdword in DIR_CHANGERS:
            movers.append(seg)
            continue
        if cmdword == "git" and (any(t in HEAD_MOVERS for t in rest)
                                 or _writes_symbolic_ref(rest)):
            movers.append(seg)
            continue
        kind, through_api = _create_kind(cmdword, rest)
        if kind:
            found.append((kind, through_api, seg, prefix, cmdword, rest))


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", cwd, *args],
                                capture_output=True, text=True)
    except (OSError, ValueError):
        # ValueError: an argument holds a NUL byte, so git cannot run.
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _join(*parts):
    # git prints the main checkout with "/" on every platform. One separator
    # keeps the paths in the block message usable as Git Bash commands.
    return "/".join(parts)


def _main_checkout(cwd):
    out = _git(cwd, "worktree", "list", "--porcelain")
    first = out.splitlines()[0] if out else ""
    if not first.startswith("worktree "):
        return ""
    return first[len("worktree "):]


def _target_sha(cwd, cmdword, rest):
    head = None
    if cmdword == "gh" and "pr" in rest:
        head = _flag_value(rest, "-H", "--head")
    if head is None:
        ref = "HEAD"
    else:
        # A fork head is "owner:branch"; the local branch is the part after it.
        ref = "refs/heads/" + head.split(":", 1)[-1]
    sha = _git(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    if not FULL_SHA.match(sha):
        raise _Ambiguous("cannot resolve %s to a commit" % ref)
    return sha


def _override_reason(prefix):
    """The override reason on this segment: None when absent, "" when blank."""
    for tok in prefix:
        if tok.startswith(OVERRIDE_VAR + "="):
            return tok[len(OVERRIDE_VAR) + 1:].strip()
    return None


def _evidence(root, sha):
    """Return (route, problems). route is "" when no evidence passes."""
    base = _join(root, ".review", sha)
    problems = []
    verdict = _join(base, "verdict.json")
    if os.path.isfile(verdict):
        try:
            with open(verdict, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            problems.append("%s is not a JSON object" % verdict)
        elif data.get("head") != sha:
            problems.append("%s names head %r, not %s" % (verdict, data.get("head"), sha))
        elif data.get("verification_complete") is not True:
            problems.append("%s has verification_complete not true; finish the "
                            "panel or run the Codex review" % verdict)
        else:
            return "verdict", []
    else:
        problems.append("%s does not exist" % verdict)
    codex = _join(base, "codex.md")
    if os.path.isfile(codex):
        try:
            with open(codex, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        if sha in text:
            return "codex", []
        problems.append("%s does not contain %s" % (codex, sha))
    else:
        problems.append("%s does not exist" % codex)
    return "", problems


def _log(root, entry):
    path = _join(root, ".review", LOG_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _missing_message(kind, sha, root, problems):
    base = _join(root, ".review", sha)
    lines = ["BLOCKED: %s needs a second-model review of %s." % (kind, sha)]
    lines += ["  " + p for p in problems]
    lines += [
        "",
        "Run one of these on the final commit, then retry:",
        "  /review-local --base main",
        '  mkdir -p "%s" && codex exec -p review -o "%s" "<brief>. Start your reply'
        ' with the line Reviewed-HEAD: %s" </dev/null'
        % (base, _join(base, "codex.md"), sha),
        "",
        "For a trivial PR only, put an override with a reason on the same command:",
        '  %s="<why no review>" gh pr create ...' % OVERRIDE_VAR,
        "Each use is logged to %s." % _join(root, ".review", LOG_NAME),
    ]
    return "\n".join(lines) + "\n"


def _api_message(kind, detail):
    return (
        "BLOCKED: {0} creates a pull request through the API.\n"
        "  detail: {1}\n\n"
        "The request names a remote branch, not a local commit, so a review marker\n"
        "cannot match it. To use a review marker, use gh pr create from the branch\n"
        "checkout. For a trivial PR only, put an override with a reason on the same\n"
        "command:\n"
        '  {2}="<why no review>" gh api ...\n'
    ).format(kind, detail, OVERRIDE_VAR)


def _ambiguous_message(reason, detail):
    return (
        "BLOCKED: this command creates a pull request, and the review guard cannot\n"
        "tell which commit it carries (fail-safe).\n"
        "  reason: {0}\n  detail: {1}\n\n"
        "Run the PR creation as its own command, from the branch checkout, after\n"
        "the last commit.\n"
    ).format(reason, detail)


def evaluate(cmd: str, hook_cwd: str) -> tuple[bool, str]:
    found, movers = [], []
    cwd, body = _leading_cd(cmd, hook_cwd)
    try:
        _scan(body, found, movers)
    except _Ambiguous as exc:
        # Only a command that could create a PR may fail closed here.
        if any(word in cmd for word in MAY_CREATE):
            return True, _ambiguous_message(str(exc), cmd)
        return False, ""
    # Hot path: no PR creation, no git subprocess.
    if not found:
        return False, ""
    if movers:
        return True, _ambiguous_message("the command also changes directory or HEAD",
                                        movers[0])
    try:
        root = _main_checkout(cwd)
        if not root:
            raise _Ambiguous("cannot find the main checkout from %s" % cwd)
        entries = []
        for kind, through_api, seg, prefix, cmdword, rest in found:
            # For an API creation this is HEAD, which only the log records.
            sha = _target_sha(cwd, cmdword, rest)
            reason = _override_reason(prefix)
            if reason == "":
                return True, (
                    "BLOCKED: %s is set with a blank reason. State why this PR needs "
                    "no review:\n  %s=\"<reason>\" gh pr create ...\n"
                    % (OVERRIDE_VAR, OVERRIDE_VAR))
            if reason is not None:
                route = "override"
            elif through_api:
                return True, _api_message(kind, seg)
            else:
                route, problems = _evidence(root, sha)
                if not route:
                    return True, _missing_message(kind, sha, root, problems)
            entries.append({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
                    timespec="seconds"),
                "head": sha,
                "route": route,
                "reason": reason or "",
                "cwd": cwd,
            })
    except _Ambiguous as exc:
        return True, _ambiguous_message(str(exc), found[0][2])
    for entry in entries:
        try:
            _log(root, entry)
        except OSError as exc:
            return True, (
                "BLOCKED: could not record this PR creation in %s (%s). The log feeds\n"
                "the guard's deletion trigger, so an unlogged creation is refused.\n"
                % (_join(root, ".review", LOG_NAME), exc))
    return False, ""

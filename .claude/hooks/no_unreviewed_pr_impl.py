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
same root /review-local writes to. <sha> is HEAD of the hook cwd, or the
`--head` branch of `gh pr create`. A later commit changes <sha>, so a review
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

Detected (scan broadly): `gh pr create` and its alias `gh pr new`, the GraphQL
createPullRequest mutation through `gh api` or an HTTP tool, and a REST POST to
`repos/<o>/<r>/pulls` (an explicit POST, gh field flags, or curl data flags).
The shared segmenter finds these inside chains, substitutions, `eval` and
`bash -c`. Data (quoted arguments of other commands, heredoc bodies) does not
match.

Fail closed: a command that also changes directory or moves HEAD (cd, pushd,
popd, or a git checkout/switch/commit/reset/rebase/merge/pull/cherry-pick/am/
revert) blocks, because the guard reads HEAD before the command runs. An
unresolvable HEAD, `--head` branch or main checkout also blocks.

Not detected: a user-defined gh alias, and an httpie POST that has only
key=value items.
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
GQL_CREATE = re.compile(r"createPullRequest")
PULLS_PATH = re.compile(r"repos/[^/\s]+/[^/\s]+/pulls/?(\?.*)?$")
# Raw substrings of any command that can create a PR, for the unreadable case.
MAY_CREATE = ("gh", "pulls", "PullRequest")
DIR_CHANGERS = {"cd", "pushd", "popd"}
HEAD_MOVERS = {"checkout", "switch", "commit", "reset", "rebase", "merge",
               "pull", "cherry-pick", "am", "revert"}
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
        if tok.startswith(short) and len(tok) > len(short) and not tok.startswith("--"):
            return tok[len(short):]
    return None


def _is_post(toks):
    for i, tok in enumerate(toks):
        up = tok.upper()
        if up in ("POST", "-XPOST", "--METHOD=POST", "--REQUEST=POST"):
            return True
        if tok in ("-X", "--method", "--request") and i + 1 < len(toks):
            if toks[i + 1].upper() == "POST":
                return True
    return False


def _has_flag(toks, flags):
    for tok in toks:
        for flag in flags:
            if tok == flag or tok.startswith(flag + "="):
                return True
            # A glued short flag: -ftitle=x, -d{}.
            if len(flag) == 2 and tok.startswith(flag) and not tok.startswith("--"):
                return True
    return False


def _create_kind(cmdword, rest, toks):
    """Name the PR-creation form in one segment, or "" when there is none."""
    if cmdword == "gh":
        for a, b in zip(rest, rest[1:]):
            if a == "pr" and b in ("create", "new"):
                return "gh pr " + b
    is_api = (cmdword == "gh" and "api" in rest) or cmdword in HTTP_TOOLS
    if not is_api:
        return ""
    if any(GQL_CREATE.search(t) for t in toks):
        return "GraphQL createPullRequest"
    if any(PULLS_PATH.search(t) for t in toks):
        explicit_get = False
        for i, tok in enumerate(toks):
            if tok in ("-X", "--method", "--request") and i + 1 < len(toks):
                explicit_get = toks[i + 1].upper() != "POST"
        if _is_post(toks):
            return "REST POST .../pulls"
        if not explicit_get:
            flags = GH_FIELD_FLAGS if cmdword == "gh" else CURL_DATA_FLAGS
            if _has_flag(rest, flags):
                return "REST POST .../pulls"
    return ""


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
            if "-c" in rest:
                idx = rest.index("-c")
                if idx + 1 < len(rest):
                    _scan(rest[idx + 1], found, movers, depth + 1)
            continue
        if cmdword in DIR_CHANGERS:
            movers.append(seg)
            continue
        if cmdword == "git" and any(t in HEAD_MOVERS for t in rest):
            movers.append(seg)
            continue
        kind = _create_kind(cmdword, rest, toks)
        if kind:
            found.append((kind, seg, prefix, cmdword, rest))


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", cwd, *args],
                                capture_output=True, text=True)
    except OSError:
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
        '  mkdir -p "%s" && codex exec -p review -o "%s" "<brief that names %s>" </dev/null'
        % (base, _join(base, "codex.md"), sha),
        "",
        "For a trivial PR only, put an override with a reason on the same command:",
        '  %s="<why no review>" gh pr create ...' % OVERRIDE_VAR,
        "Each use is logged to %s." % _join(root, ".review", LOG_NAME),
    ]
    return "\n".join(lines) + "\n"


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
    try:
        _scan(cmd, found, movers)
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
        root = _main_checkout(hook_cwd)
        if not root:
            raise _Ambiguous("cannot find the main checkout from %s" % hook_cwd)
        entries = []
        for kind, seg, prefix, cmdword, rest in found:
            sha = _target_sha(hook_cwd, cmdword, rest)
            reason = _override_reason(prefix)
            if reason == "":
                return True, (
                    "BLOCKED: %s is set with a blank reason. State why this PR needs "
                    "no review:\n  %s=\"<reason>\" gh pr create ...\n"
                    % (OVERRIDE_VAR, OVERRIDE_VAR))
            if reason is not None:
                route = "override"
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
                "cwd": hook_cwd,
            })
    except _Ambiguous as exc:
        return True, _ambiguous_message(str(exc), found[0][1])
    try:
        for entry in entries:
            _log(root, entry)
    except OSError as exc:
        return True, (
            "BLOCKED: could not record this PR creation in %s (%s). The log feeds\n"
            "the guard's deletion trigger, so an unlogged creation is refused.\n"
            % (_join(root, ".review", LOG_NAME), exc))
    return False, ""

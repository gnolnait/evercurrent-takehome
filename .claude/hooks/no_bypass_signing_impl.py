#!/usr/bin/env python3

"""no-bypass-signing - block git invocations that skip pre-commit hooks or GPG signing.

Stage 2 of the PreToolUse guard. The bash wrapper (`no-bypass-signing`) does the
BYPASS_GIT_GUARD short-circuit and command-string extraction, then runs this as
a CHILD process (not `exec`) with the command string as argv[1]. Exit 2 (with an
explanation on stderr) blocks; exit 0 allows. Running as a child rather than
exec is deliberate: it lets the wrapper coerce any non-{0,2} exit (a syntax /
import error surfaces as exit 1) into a block instead of a fail-open allow.

Per CLAUDE.md Git Safety Protocol:
  "NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user
   explicitly requests it"

Triggers (exact argument tokens of a real git invocation):
  --no-verify / -n (commit)   skips pre-commit / commit-msg hooks
  --no-gpg-sign               skips GPG signing
  commit.gpgsign=false        same effect via `-c` config override

Parsing is quote-aware (shlex tokens, not a substring regex): the trigger has
to be an actual argument token of a git command. A commit message or PR title
that merely MENTIONS --no-verify is one quoted token and never matches — the
old `tr`-split + regex version false-blocked exactly that (a message with a
`;` before `git ... --no-verify ...` prose).

It also follows transparent wrappers (env / command / sudo / nice / timeout),
shell-string wrappers (`sh -c '...'`, `eval '...'`, incl. `bash -lc`, and
`powershell -Command` / `pwsh -c` / `cmd /c`; an encoded PowerShell command
fails closed), Windows program names (`git.exe`, a full path to git),
unambiguous flag abbreviations (--no-gpg-sig), short-option clusters (-nm),
and inline `-c alias.*=...` definitions. This is an ACCIDENT guard, not an
adversary guard (an adversary has BYPASS_GIT_GUARD=1): deliberately-obfuscated
forms are accepted-open — ANSI-C `$'--no-verify'` quoting, backtick/$()
command substitution running a standalone commit, aliases defined in a config
FILE (invisible to a command-text parser), `--config-env` indirection, an
arg-wrapper option that takes a separate non-numeric value (`timeout -s TERM
5 git ...`), and a re-entrant shell inside an inline alias (`!sh -c "git ..."`
— config propagates via env we don't model). Unparseable input containing a
trigger fails CLOSED.

Interpreter: the bash fences resolve one (CLAUDE_HOOK_PYTHON, else python3, else
python, skipping the Windows Store alias stub). This module is stdlib-only, so
any Python 3.10+ works. Mirrors no-merge-pr.py.
"""
import re
import shlex
import sys

from shell_segments import WINDOWS_SHELLS, command_name, split_segments, windows_shell_code

# Full long-option triggers. A token also matches by unambiguous PREFIX (git
# accepts `--no-gpg-sig`, `--no-verif`, ...): any `--no-v*` that is a prefix of
# --no-verify, or `--no-g*` prefix of --no-gpg-sign. The `--no-v`/`--no-g`
# floor keeps `--no-` alone (ambiguous) from matching.
TRIGGERS = ("--no-verify", "--no-gpg-sign")
# --no-verify / -n only SKIP A HOOK for these verbs (elsewhere `--no-verify`
# is data, e.g. a `git grep`/`git log --grep` pattern; `-n` means dry-run for
# push, line-count for shortlog, ...). --no-gpg-sign only skips signing for
# commit-creating verbs (+ tag). Gating on these avoids false-blocking audits.
VERIFY_VERBS = {"commit", "merge", "am", "push", "rebase"}
DASH_N_VERBS = {"commit", "am"}   # -n == --no-verify only here
GPGSIGN_FLAG_VERBS = {"commit", "merge", "revert", "cherry-pick", "rebase", "am", "tag"}
# Bare wrappers that run the following word as the real command. `command` /
# `time` may carry their own options (command -p, time -p); those are skipped.
WRAPPERS = {"command", "builtin", "exec", "sudo", "doas", "nohup", "time", "setsid"}
# Wrappers that carry option flags and/or args before the real command
# (`nice -n5 git ...`, `timeout 5 git ...`, `stdbuf -oL git ...`). Their option
# grammar varies, so check_segment advances to the first git/shell word rather
# than modeling each.
ARG_WRAPPERS = {"nice", "ionice", "stdbuf", "xargs", "chrt", "taskset", "timeout", "gtimeout"}
# Verbs that CREATE a commit, so `-c commit.gpgsign=false` actually skips
# signing for them (on `status`/`log`/... it is inert -> not a bypass).
COMMIT_VERBS = {"commit", "merge", "revert", "cherry-pick", "am", "rebase"}
# Leading shell control-flow keywords / group openers that precede the real
# command (`if git commit ...`, `! git commit`, `{ git commit; }`).
KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "}"}


def trigger_kind(tok):
    # Exact, or an UNAMBIGUOUS git long-option abbreviation. Returns "verify"
    # / "gpg" / None. The verify floor is `--no-veri` because `--no-ver` and
    # shorter are ambiguous with `--no-verbose` (git rejects them, so they
    # can't bypass anyway); the gpg-sign floor is `--no-g`.
    if tok == "--no-verify" or (
        len(tok) >= 9 and tok.startswith("--no-veri") and "--no-verify".startswith(tok)
    ):
        return "verify"
    if tok == "--no-gpg-sign" or (
        len(tok) >= 6 and tok.startswith("--no-g") and "--no-gpg-sign".startswith(tok)
    ):
        return "gpg"
    return None


def is_env_assignment(tok):
    name = tok.split("=", 1)[0]
    return "=" in tok and name and name.replace("_", "").isalnum() and not name[0].isdigit()


def tokenize(segment):
    """Tokenize ONE segment produced by the shared segmenter.

    The segmenter already removed operators, redirections and heredoc bodies, so
    this only has to respect quoting: `punctuation_chars` keeps a quoted `;` or
    `&&` inside a commit message from splitting the token (the 2026-07-06
    false-block). Comment handling stays off — the segmenter owns `#`.
    """
    lex = shlex.shlex(segment.replace("\n", ";"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


# Substrings that mark a command as trigger-shaped for the fail-CLOSED paths
# below (recursion cap, unparseable input). Both paths use this same set so they
# can't drift apart. `-n` is included because `git commit -n` / `git am -n` are
# --no-verify aliases: an unparseable `git commit -n "unclosed` must fail closed,
# not allow. This is a substring test on a command already known to be
# trigger-shaped and unparseable, so the extra breadth of `-n` only ever blocks
# in the safe direction.
TRIGGER_HINTS = ("--no-verify", "--no-veri", "--no-gpg", "--no-g", "gpgsign", "-n")


def _mentions_git(text):
    """Word-boundary `git`, so a substring like 'legit'/'digit' doesn't trip the
    fail-closed paths below. Safe on those paths — they run only on genuinely
    unparseable or over-nested text, where a quote-assembled `g''it` (which
    parses fine and never reaches them) is not a concern; a real bypass here
    carries a literal `git` word."""
    return re.search(r"\bgit\b", text) is not None


def scan(text, depth=0):
    """Return the first offending segment string in a command line, or None.

    Segmentation — quoting, heredocs, comments, substitutions, and the whole
    redirection grammar (fd-dup, clobber, both-streams, here-strings) — belongs
    to the shared segmenter, so this guard and no-merge-pr agree on WHERE a
    command word starts. A redirection is removed rather than treated as a
    boundary, because bash removes it before it resolves the command word.
    """
    if depth > 3:
        # Too many nested shell wrappers to follow. Fail CLOSED if the text is
        # trigger-shaped (a deeply-wrapped bypass) rather than silently allow.
        if _mentions_git(text) and any(h in text for h in TRIGGER_HINTS):
            return "<deeply-nested command containing a bypass trigger>"
        return None
    for segment in split_segments(text):
        try:
            toks = tokenize(segment)
        except ValueError:
            # Unbalanced quotes: this segment cannot be tokenized, so a trigger
            # inside it is unexaminable. The trigger-shape test reads the WHOLE
            # command (not the segment), keeping the pre-consolidation
            # fail-closed breadth.
            if _mentions_git(text) and any(h in text for h in TRIGGER_HINTS):
                return "<unparseable command containing a bypass trigger>"
            continue
        if toks and check_segment(toks, depth):
            return " ".join(toks)
    return None


def check_segment(toks, depth=0, inherited_gpgsign_off=False):
    """Return True if this simple command is a hook/signing-skipping git call.

    `inherited_gpgsign_off` carries a `-c commit.gpgsign=false` from an outer
    git command into the commit its shell alias runs.
    """
    head = 0
    # Strip leading env assignments, shell control-flow keywords, and
    # transparent wrappers (env / command / sudo / ...) so
    # `if FOO=1 command git commit --no-verify` still resolves to the git call.
    changed = True
    while changed:
        changed = False
        while head < len(toks) and toks[head] in KEYWORDS:
            head += 1
            changed = True
        while head < len(toks) and is_env_assignment(toks[head]):
            head += 1
            changed = True
        while head < len(toks) and toks[head] == "env":
            head += 1
            changed = True
            while head < len(toks):
                t = toks[head]
                if t == "-u" and head + 1 < len(toks):
                    head += 2
                    continue
                if t.startswith("-"):
                    head += 1
                    continue
                if is_env_assignment(t):
                    head += 1
                    continue
                break
        while head < len(toks) and toks[head].rsplit("/", 1)[-1] in WRAPPERS:
            head += 1
            changed = True
            # command / time may carry their own options (command -p, time -p).
            while head < len(toks) and toks[head].startswith("-"):
                head += 1
        if head < len(toks) and toks[head].rsplit("/", 1)[-1] in ARG_WRAPPERS:
            # Skip the wrapper's own options and numeric args (nice -n5,
            # timeout 5); the command word is the first token that is neither.
            # We deliberately do NOT model options that take a separate
            # non-numeric value (timeout -s TERM) — that wrapper form is
            # accepted-open (adversarial-shaped) rather than risk mis-reading a
            # command arg as the command (`timeout 5 echo git ...`).
            head += 1
            changed = True
            while head < len(toks) and (
                toks[head].startswith("-") or toks[head].replace(".", "", 1).isdigit()
            ):
                head += 1
    if head >= len(toks):
        return False

    word = command_name(toks[head])
    # Shell-string wrappers: recurse into the code they run. `-c` may be its
    # own token or the trailing char of a leading option cluster (bash -lc).
    # Only LEADING options count: once a non-option operand appears the shell is
    # running that as a script, so a later `-c` is the script's arg, not ours.
    if word == "eval":
        return scan(" ".join(toks[head + 1 :]), depth + 1) is not None
    if word in ("sh", "bash", "zsh", "dash", "ksh"):
        sh_args = toks[head + 1 :]
        m = 0
        while m < len(sh_args):
            t = sh_args[m]
            if not t.startswith("-") and not t.startswith("+"):
                break  # script/command operand -> stop; a later -c is its arg
            # bash/zsh -o/-O/+o <name> take a separate value (bash -o pipefail).
            if t in ("-o", "+o", "-O", "+O"):
                m += 2
                continue
            # `-c` on its own or as the trailing char of an option cluster
            # (bash -lc). The `o` guard avoids matching `-o` handled above.
            if t == "-c" or (len(t) > 1 and t[1] != "-" and t.endswith("c")):
                if m + 1 < len(sh_args):
                    return scan(sh_args[m + 1], depth + 1) is not None
                break
            m += 1
        return False
    if word in WINDOWS_SHELLS:
        codes = windows_shell_code(word, toks[head + 1 :])
        if codes is None:
            return True  # an encoded command cannot be read: fail closed
        return any(scan(code, depth + 1) is not None for code in codes)
    if word != "git":
        return False
    rest = toks[head + 1 :]

    # Config override: git config keys are case-insensitive and booleans
    # accept several false spellings, so normalize before comparing. Counted
    # ONLY as the value of `-c` — the same text as a grep pattern or commit
    # message is data.
    def is_gpgsign_off(tok):
        # Git bool-false spellings; the empty string ("commit.gpgsign=", e.g.
        # from an empty shell interpolation) also parses as false.
        key, _, val = tok.partition("=")
        return key.lower() == "commit.gpgsign" and val.lower() in ("false", "0", "no", "off", "")

    # Walk the git global flags to the verb, recording a gpgsign-off override
    # and inline alias definitions (`git -c alias.ci=... ci ...`); aliases from
    # config FILES are invisible here and out of scope. gpgsign-off only skips
    # signing for a commit-CREATING verb, so it's applied after the verb known
    # (avoids false-blocking `git -c commit.gpgsign=false status`).
    aliases, verb, j, gpgsign_off = {}, None, 0, False
    while j < len(rest):
        t = rest[j]
        if t == "-c" and j + 1 < len(rest):
            val = rest[j + 1]
            if is_gpgsign_off(val):
                gpgsign_off = True
            key, _, exp = val.partition("=")
            if "=" in val and key.lower().startswith("alias."):
                aliases[key[6:]] = exp
            j += 2
            continue
        if t in ("-C", "--git-dir", "--work-tree", "--namespace", "--exec-path") and j + 1 < len(rest):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        verb = t
        break
    args = rest[j + 1 :]
    if verb in aliases and depth < 2:
        exp = aliases[verb]
        if exp.startswith("!"):
            # Shell alias: the expansion is an arbitrary command line. A
            # gpgsign-off from the outer `-c` propagates into the alias's
            # commit regardless of wrappers in the expansion, so thread it
            # through (covers `!command git commit` too).
            return check_segment(
                exp[1:].split() + args, depth + 1,
                inherited_gpgsign_off=gpgsign_off or inherited_gpgsign_off,
            )
        exp_toks = exp.split()
        verb = exp_toks[0] if exp_toks else None
        args = exp_toks[1:] + args
    if verb is None:
        return False

    if (gpgsign_off or inherited_gpgsign_off) and verb in COMMIT_VERBS:
        return True

    # Scan the subcommand args. A value-taking option consumes its separate
    # argument, so a value that merely LOOKS like a trigger flag is data
    # (`git commit -m -n` commits with the message "-n";
    # `git commit --pathspec-from-file --no-verify` reads --no-verify as the
    # file). After `--` everything is pathspec. `--opt=value` (attached) never
    # consumes a following token. NB: options whose value is OPTIONAL and
    # attach-only (`--untracked-files[=<mode>]`, `-u`) do NOT consume a
    # following token, so they are NOT listed here.
    VALUE_LONGS = (
        "--message", "--file", "--template", "--author", "--date",
        "--pathspec-from-file", "--reuse-message", "--reedit-message",
        "--fixup", "--squash", "--cleanup",
    )
    SEP_VALUE_SHORTS = "mFCct"    # consume the NEXT token (separate value)
    CLUSTER_VALUE_SHORTS = "mFCctu"  # in a cluster, consume the rest as value (incl -u)
    i = 0
    while i < len(args):
        t = args[i]
        if t == "--":
            break
        if t in ("-m", "-F", "-C", "-c", "-t") or t in VALUE_LONGS:
            i += 2
            continue
        kind = trigger_kind(t)
        if kind == "verify" and verb in VERIFY_VERBS:
            return True
        if kind == "gpg" and verb in GPGSIGN_FLAG_VERBS:
            return True
        if verb in DASH_N_VERBS and t == "-n":
            return True
        if len(t) > 1 and t[0] == "-" and t[1] != "-":
            # Short-option cluster: scan left to right; a value-short consumes
            # the rest of the cluster as its value (-mn == -m n, -uno == -u no),
            # or the NEXT token if it ends the cluster (-am x). An `n` seen
            # before any value-short is a real -n.
            consumed_next = False
            for pos, ch in enumerate(t[1:], 1):
                if ch == "n" and verb in DASH_N_VERBS:
                    return True
                if ch in CLUSTER_VALUE_SHORTS:
                    # Rest of cluster is this option's value; consume next token
                    # only if it's a separate-value short at cluster end.
                    consumed_next = pos == len(t) - 1 and ch in SEP_VALUE_SHORTS
                    break
            i += 2 if consumed_next else 1
            continue
        i += 1
    return False


def evaluate(cmd: str) -> tuple[bool, str]:
    try:
        offending = scan(cmd)
    except Exception as e:
        # Fail CLOSED (inner layer): an unexpected exception while scanning must
        # block, never silently allow a possible bypass. The wrapper is the OUTER
        # fence — it coerces any non-{0,2} exit (import/interpreter/signal death,
        # which never reach this handler) to a block — so between the two, no
        # parser failure exits 0. Carry repr(e) into the message: a recurring
        # parser bug then blocks every trigger-shaped command, and this is the
        # only breadcrumb for diagnosing it from the hook's stderr.
        offending = "<parser error - fail closed: {0!r}>".format(e)
    if offending is None:
        return False, ""
    return True, (
        "BLOCKED: this command skips pre-commit hooks or GPG signing.\n"
        "  segment: {0}\n".format(offending)
        + "Per CLAUDE.md Git Safety Protocol, --no-verify / --no-gpg-sign / "
          "commit.gpgsign=false\n"
          "require explicit user approval. Investigate and fix the failing hook "
          "or signing\nissue instead of bypassing.\n"
          "To override (after user approval): relaunch the session with\n"
          "BYPASS_GIT_GUARD=1 exported, or the user runs the command themselves.\n"
          "(An inline `BYPASS_GIT_GUARD=1 git ...` prefix inside this Bash call\n"
          "does not reach the hook and will not work.)\n"
    )


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not cmd:
        sys.exit(0)
    blocked, message = evaluate(cmd)
    if blocked:
        sys.stderr.write(message)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()

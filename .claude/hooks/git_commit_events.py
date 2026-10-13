import collections
import contextlib
import io
import os
import re
import shlex
import sys

from shell_segments import WINDOWS_SHELLS, command_name

# Bash's literal close marker for any delimiter outside these forms — an embedded
# backslash-escape, an unquoted `$`/backtick that extends the word, a mid-word line
# continuation — can differ from a naive capture, so the body cannot be located:
# extract_heredocs returns ambiguous=True and the caller fails closed. The boundary
# lookahead makes a partial match (`<<EOF$X`, `<<'EOF'x`) ambiguous too.
HD = re.compile(
    r"""<<-?[ \t]*('[^']*'|"[^"\\]*"|\\?[A-Za-z0-9_][A-Za-z0-9_./!:@%+,=~-]*)"""
    r"""(?=[ \t\n;|&<>()]|$)"""
)
HDN = re.compile(r"__HEREDOC_(\d+)__$")


def _delim_marker(word):
    r"""Bash's literal close line for a delimiter WORD this scanner accepted:
    drop the surrounding quotes (single or double — HD only accepts forms whose
    quoted content is literal) or a leading `\` escape. Only HD-accepted forms
    reach here, so no other quote/escape processing is needed."""
    if word[:1] in ("'", '"'):
        return word[1:-1]
    if word[:1] == "\\":
        return word[1:]
    return word


def _pending(word, strip_tabs):
    """A pending heredoc entry (close marker, strip-leading-tabs?, quoted-body?).
    A quoted or backslash-escaped delimiter (`'EOF'`, `"EOF"`, `\\EOF`) has a
    literal body — no `\\`+newline continuation join when matching the close."""
    return (_delim_marker(word), strip_tabs, word[:1] in ("'", '"', "\\"))


def _peek(s, i, k):
    """Up to k *logical* chars from s[i], collapsing `\\`+newline continuations
    (bash removes them before tokenizing, so `$(\\<nl>(` is `$((`). Returns
    (text, end) where end is the raw index just past the last logical char, so a
    matched introducer can advance over the collapsed span in one step."""
    text, j = [], i
    while j < len(s) and len(text) < k:
        if s[j] == "\\" and j + 1 < len(s) and s[j + 1] == "\n":
            j += 2
            continue
        text.append(s[j])
        j += 1
    return "".join(text), j


def extract_heredocs(s):
    """Split heredoc BODIES out of the command text (quote-aware marker scan).

    Bodies are stdin DATA, not shell: segmenting them as commands attributed a
    body line like `git commit -m x` to the project repo, and a lone
    apostrophe in body prose made shlex raise -> AMBIGUOUS -> false block
    (2026-07-07, sibling-repo commit after a notes-file heredoc append).
    Markers are rewritten to a plain `__HEREDOC_<k>__` token so the fed
    command's segment can look up its own bodies; a heredoc feeding an
    interpreter is code and stays ambiguous (see process_segment). Comment
    aware: `# <<x` is not a heredoc and must not swallow the next command.
    Returns (text_without_bodies, [body, ...], ambiguous). `ambiguous` is True
    when a `<<` heredoc opens with a delimiter this scanner cannot resolve to
    bash's literal close marker, OR when an opened body never reaches its close
    delimiter (runs to EOF): its body can't be located, so leaving it inline is
    unsafe (a `git checkout` in the body would emit a spurious switch event that
    unprotects the walk — a fail-open) and guessing the close would swallow a
    real later command. The caller fails closed (blocks) on it.
    """
    out, bodies, pending = [], [], []
    i, n, q, mk = 0, len(s), None, 0
    # Inside `$(( ))` / `(( ))` bash reads `<<` as a bitshift, never a heredoc;
    # misreading it opens a phantom heredoc whose body swallows a real later `git
    # commit` (fail-open). A `$( )` nested inside arithmetic re-enables heredocs, so
    # only an `arith` frame reinterprets `<<`. Each frame is [kind, open_paren_count]
    # and pops when its parens close.
    ctx = []
    # Position and closed-frame kind of the most recent `)` that closed a ctx frame (see
    # the `#` handler).
    last_close_pos, last_close_kind = -1, None
    while i < n:
        c = s[i]
        if q:
            out.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in ("'", '"'):
            q = c
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if s[i + 1] == "\n":  # line continuation: bash joins the lines
                i += 2
                continue
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        # `$((1))#` is a literal `1#`, not a comment: a `)` ends a word only when it
        # closed a subshell, not when it closed a `$((..))`/`$(..)` substitution.
        # Reading it as a comment hid a `<<EOF` opener and left the body's `git
        # checkout` inline as a spurious switch event (fail-open). The position check
        # rejects a quoted or escaped `)` that never closed a frame.
        after_subshell = (
            i > 0
            and s[i - 1] == ")"
            and i - 1 == last_close_pos
            and last_close_kind == "subshell"
        )
        if c == "#" and (i == 0 or s[i - 1] in " \t\n;|&(" or after_subshell):
            j = s.find("\n", i)  # comment: no marker-matching inside
            out.append(s[i:] if j < 0 else s[i:j])
            i = n if j < 0 else j
            continue
        # Known residual (adversarial-only): arithmetic in bracket-delimited contexts —
        # legacy `$[ ... ]`, array subscripts `${a[i<<2]}`, substring offsets
        # `${s:o<<2}` — is not modeled, so a `<<` there is still misread as a heredoc.
        # Tracking `]` collides with array-subscript `]` and needs interleaved
        # `[`/`]`/`(`/`)` parsing that leaks a new fail-open per rule; the paren forms
        # have no such collision and are modeled.
        if c == "`":
            # Backticks do not nest: closing pops back through the sentinel, discarding
            # frames left open inside (a stray `((`), so they cannot leak an arith
            # context onto a later real heredoc.
            if any(f[0] == "backtick" for f in ctx):
                while ctx and ctx.pop()[0] != "backtick":
                    pass
            else:
                ctx.append(["backtick", 0])
            out.append(c)
            i += 1
            continue
        # Introducers are matched over collapsed `\`+newline continuations (see
        # `_peek`): raw-slice matching split the token and misread the inner `<<` as a
        # heredoc (fail-open).
        p3, e3 = _peek(s, i, 3)
        if p3 == "$((":
            ctx.append(["arith", 2])
            out.append("$((")
            i = e3
            continue
        p2, e2 = _peek(s, i, 2)
        if p2 == "((":
            ctx.append(["arith", 2])
            out.append("((")
            i = e2
            continue
        if p2 == "$(":
            ctx.append(["cmdsub", 1])
            out.append("$(")
            i = e2
            continue
        if c == "(":
            if ctx and ctx[-1][0] == "arith":
                ctx[-1][1] += 1
            else:
                ctx.append(["subshell", 1])
            out.append(c)
            i += 1
            continue
        if c == ")":
            last_close_pos, last_close_kind = i, None
            if ctx:
                ctx[-1][1] -= 1
                if ctx[-1][1] <= 0:
                    last_close_kind = ctx.pop()[0]
            out.append(c)
            i += 1
            continue
        if ctx and ctx[-1][0] == "arith" and s[i : i + 2] == "<<":
            out.append("<<")  # arithmetic left-shift operator, not a heredoc
            i += 2
            continue
        if s[i : i + 3] == "<<<":
            # here-STRING, not a heredoc: consume all three `<` so the inner
            # `<<` isn't re-read as a heredoc opener (which would swallow a
            # real later command as its "body" — a fail-open).
            out.append("<<<")
            i += 3
            continue
        if s[i : i + 2] == "<<":
            # Known residual (adversarial-only): a `\`+newline splitting the `<<`
            # operator itself (`<\<nl><EOF`) is not collapsed here, so bash reads a
            # heredoc and this scanner does not, leaving the body inline. Collapsing it
            # means rebuilding the delimiter region for HD.match.
            m = HD.match(s, i)
            if not m:
                # `<<\` + newline + DELIM: bash joins continuation lines
                # while reading the marker.
                k = i + 2
                if k < n and s[k] == "-":
                    k += 1
                j2 = k
                while s[j2 : j2 + 2] == "\\\n":
                    j2 += 2
                if j2 > k:
                    m = HD.match("<<" + s[j2 : j2 + 80])
                    if m:
                        pending.append(_pending(m.group(1), k > i + 2))
                        out.append(" __HEREDOC_%d__ " % mk)
                        mk += 1
                        i = j2 + (m.end() - 2)
                        continue
                # Unresolvable delimiter: the body cannot be located, so fail closed —
                # terminal, the caller ignores text and bodies once ambiguous.
                return "".join(out), bodies, True
            pending.append(_pending(m.group(1), m.group(0)[2:3] == "-"))
            out.append(" __HEREDOC_%d__ " % mk)
            mk += 1
            i = m.end()
            continue
        if c == "\n" and pending:
            out.append(c)
            i += 1
            for delim, strip_tabs, quoted in pending:
                # Bash's close-line rules: `<<-` strips leading tabs from the close
                # line, a plain `<<` needs an exact match; an unquoted delimiter honors
                # `\`+newline continuations in its body, so lines join before the
                # delimiter test and can close EARLY, while a quoted delimiter has a
                # literal body. Under-joining closed later than bash and swallowed a
                # real later command as body (fail-open).
                lines, closed = [], False
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:] if j < 0 else s[i:j]
                    i = n if j < 0 else j + 1
                    logical = line
                    while (
                        not quoted
                        and (len(logical) - len(logical.rstrip("\\"))) % 2 == 1
                        and i < n
                    ):
                        j = s.find("\n", i)
                        nxt = s[i:] if j < 0 else s[i:j]
                        i = n if j < 0 else j + 1
                        logical = logical[:-1] + nxt  # drop the escaping backslash, join
                        line = line + "\n" + nxt
                    close = logical.lstrip("\t") if strip_tabs else logical
                    if close == delim:
                        closed = True
                        break
                    lines.append(line)
                if not closed:
                    # The delimiter never appeared as a close line, so the body ran to
                    # EOF and swallowed everything after the `<<` — either a misread
                    # non-heredoc `<<` or a genuinely unterminated one. Both are unsafe
                    # to strip: a real later `git commit` would hide inside the phantom
                    # body (fail-open).
                    return "".join(out), bodies, True
                bodies.append("\n".join(lines))
            pending = []
            continue
        out.append(c)
        i += 1
    return "".join(out), bodies, False


def segment_bodies(toks):
    """The heredoc bodies attached to this segment's `__HEREDOC_<k>__` markers."""
    out = []
    for t in toks:
        m = HDN.match(t)
        if m:
            k = int(m.group(1))
            if k < len(heredoc_bodies):
                out.append(heredoc_bodies[k])
    return out


_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(?=/|$)")


def msys_to_windows(path):
    """Map a Git Bash drive path (`/c/Users/x`) to its Windows form (`C:/Users/x`).

    The command was written for Git Bash, but the hook runs git.exe directly,
    and git.exe cannot change into `/c/...`. Left as is, the directory is
    unresolvable and a commit there fails closed (the false block of
    2026-10-09). Any other path is returned unchanged.
    """
    return _MSYS_DRIVE.sub(lambda m: m.group(1).upper() + ":", path)


_VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")

# The command's own literal assignments that `_bind_vars` trusts (W=<path>; git -C $W).
shell_vars = {}
# True when the command sets GIT_DIR or GIT_WORK_TREE for the commands after it
# (set by `_emit`; a prefix on one command is read per segment).
git_dir_redirected = False


def _expand(word):
    """Substitute the bound names in word; any other `$` stays."""
    return _VAR_REF.sub(lambda m: shell_vars.get(m.group(1) or m.group(2), m.group(0)), word)


def norm(base, path):
    """Resolve a cd/-C path against the current effective dir.

    `$W` and `${W}` take the value of a literal assignment earlier in the same
    command (see `_bind_vars`). Returns None when the path still cannot be
    trusted: any other '$' is a value this parser cannot know (expanding against
    the hook's own environment resolved a stale value once) - callers treat None
    as unresolvable, which fails closed for commits and never unprotects for
    switches.
    """
    if path is not None:
        path = _expand(path)
    if path is None or "$" in path:
        return None
    if os.name == "nt":
        path = msys_to_windows(path)
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return os.path.normpath(path)
    if base is None:
        return None
    return os.path.normpath(os.path.join(base, path))


def tokenize(text):
    """Tokenize a shell command, respecting quotes.

    shlex with punctuation_chars keeps quoted strings whole (a ';' or '&&'
    inside a commit message no longer splits the command — the 2026-07-06
    false-block) and emits operator runs (&&, ;, |, parens, ...) as their own
    tokens, which mark segment boundaries. Newlines also separate commands, so
    map them to ';' first — inside a quoted token that substitution can't
    affect segmentation, and the token's content is never interpreted anyway.
    Comment handling is disabled: shlex's '#' rule differs from the shell's
    (e.g. `a#b` inside an assignment word is not a comment), and with newlines
    already mapped to ';' a swallowed "comment" would eat the rest of the
    command - hiding a real commit from the guard.
    """
    lex = shlex.shlex(text.replace("\n", ";"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)  # raises ValueError on unbalanced quotes


def is_env_assignment(tok):
    name = tok.split("=", 1)[0]
    return "=" in tok and name and name.replace("_", "").isalnum() and not name[0].isdigit()


# git's global options that take the next word as their value.
_GIT_ARG_OPTIONS = frozenset(
    ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env")
)


def git_global_options(words, j):
    """Read git's global options from words[j:]: return ([(flag, value)], index of
    the subcommand word). value is None for a flag that takes none."""
    opts = []
    while j < len(words):
        w = words[j]
        if w in _GIT_ARG_OPTIONS and j + 1 < len(words):
            opts.append((w, words[j + 1]))
            j += 2
        elif w.startswith("-"):
            opts.append((w, None))
            j += 1
        else:
            break
    return opts, j


# The git verbs that can put a commit on the checked-out branch or move it.
# `checkout` and `worktree` count only as `checkout -b`/`--orphan` and `worktree add`
# (_is_write).
_WRITE_VERBS = frozenset(
    ("commit", "push", "merge", "rebase", "reset", "switch", "cherry-pick", "stash",
     "pull", "am", "revert", "checkout", "worktree")
)
# The other git subcommands. After an option value written as code (`-C
# str(Path.cwd())`, `-C ${PWD}`), the subcommand is the first word in either set.
_OTHER_SUBCOMMANDS = frozenset(
    ("add", "apply", "archive", "bisect", "blame", "branch", "bundle", "cat-file",
     "clean", "clone", "config", "describe", "diff", "fetch", "for-each-ref",
     "format-patch", "fsck", "gc", "grep", "hash-object", "help", "init", "log",
     "ls-files", "ls-remote", "ls-tree", "merge-base", "mv", "notes", "patch-id",
     "range-diff", "reflog", "remote", "restore", "rev-list", "rev-parse", "rm",
     "shortlog", "show", "show-ref", "status", "submodule", "symbolic-ref", "tag",
     "update-ref", "version")
)
_SUBCOMMANDS = _WRITE_VERBS | _OTHER_SUBCOMMANDS
# Brackets, commas and operators separate words in shell and in the code an
# interpreter runs (`["git", "commit"]`, `f"git -C {d} commit"`).
_SEP = r"\s,;|&()\[\]{}<>+"
_PIECE = re.compile(
    r"\$'((?:\\.|[^'\\])*)'"  # $'...'
    r"|'([^']*)'"
    r'|"((?:\\.|[^"\\])*)"'
    r"|([^%s'\"]+)" % _SEP  # an unquoted run
)
_HAS_SEP = re.compile("[%s]" % _SEP)


def _word_lists(text):
    """Read text as words two ways, and return (joined, split, inner).

    joined reads it as shell: pieces with no separator between them form one word
    (`g''it` is git, `user.name="A log"` is one value). split reads it as code:
    each piece is a word (`f"git` is f and git), except that a quoted piece right
    after `x=` is its value (`alias.save="commit"`). inner holds each quoted
    piece that has a separator in it, to be read again as text of its own
    (`os.system('git commit')`).
    """
    joined, split, inner = [], [], []
    end = -1
    for m in _PIECE.finditer(text):
        piece = next(g for g in m.groups() if g is not None)
        unquoted = m.group(4) is not None
        if re.search(r"[;|&]", text[max(end, 0) : m.start()]):
            joined.append(";")  # a command boundary ends git's words
            split.append(";")
        adjacent, end = m.start() == end, m.end()
        joined.append(piece.replace("\\", "") if unquoted else piece)
        if adjacent and len(joined) > 1:
            joined[-2:] = ["".join(joined[-2:])]
        split.append(piece)
        if adjacent and not unquoted and len(split) > 1 and split[-2].endswith("="):
            split[-2:] = ["".join(split[-2:])]
        if not unquoted and _HAS_SEP.search(piece):
            inner.append(piece)
    return joined, split, inner


def _is_write(words, k):
    verb, after = words[k], words[k + 1 : k + 5]
    if verb.startswith(("$", "`", "*")):
        return True  # a verb only bash or Python knows (`"$verb"`, `*opts`) may be commit
    if verb == "checkout":
        return any(w in ("-b", "--orphan") for w in after)
    if verb == "worktree":
        return after[:1] == ["add"]
    return verb in _WRITE_VERBS


def names_git_write(text, depth=0):
    """True when a git write verb is the subcommand of a `git` word in text.

    For text the parser cannot read as commands: a shell -c string, the code an
    interpreter runs, a command with quoting shlex rejects. Prose that names git
    (`git refuses the rebase`, `git_commit_events.py`) does not count; the verb
    must be the first word after git's options. After an option value, which code
    can spread over several words, it is the first git subcommand or `$` word
    within 6 words. A name set to git (`g=git; "$g" commit`) counts as git. Case
    is ignored: Windows runs `GIT.EXE` as git, and reading `COMMIT` as commit only
    adds refusals.
    """
    joined, split, inner = _word_lists(text)
    for words in (joined, split):
        words = [w.lower() for w in words if w]
        names = set()  # names set to git, so "$g" runs git
        names |= {w.split("=", 1)[0] for w in words if re.fullmatch(r"\w+=git(\.exe)?", w)}
        gits = {"$" + n for n in names} | {"${%s}" % n for n in names}
        for i, word in enumerate(words):
            if command_name(word) != "git" and word not in gits:
                continue
            opts, v = git_global_options(words, i + 1)
            if any(value is not None for _, value in opts):
                last = min(v + 6, len(words) - 1)
                while v < last and words[v] not in _SUBCOMMANDS and words[v][:1] not in "$`*;":
                    v += 1
            if v < len(words) and _is_write(words, v):
                return True
    if depth < 4:
        return any(names_git_write(t, depth + 1) for t in inner)
    return False


# A cd whose target this parser cannot resolve: the effective dir becomes unknown.
UNKNOWN_DIR = object()


def process_segment(toks, effdir, piped=(), lead=0):
    """Handle one simple command; return the new effective dir (cd), UNKNOWN_DIR
    for a cd to a path that cannot be resolved, or None.

    `lead` is 1 when the segment starts with a redirection target (`2>&1 git
    ...` splits at `>&`, so this segment starts with `1`); the command word
    comes after it.
    """
    if not toks:
        return None
    if toks[0] == "cd" and len(toks) >= 2 and not toks[1].startswith("-"):
        target = norm(effdir, toks[1])
        return UNKNOWN_DIR if target is None else target

    # GIT_DIR / GIT_WORK_TREE redirect the repo somewhere this parser cannot attribute,
    # so a commit under them is ambiguous: set in this command (`GIT_DIR=.. git`,
    # `env GIT_DIR=.. git`), or for the commands after it (git_dir_redirected).
    head, redirected = min(lead, len(toks)), git_dir_redirected
    if any(_sets_git_dir(t) for t in toks):
        redirected = True
    while head < len(toks) and is_env_assignment(toks[head]):
        head += 1

    # eval / sh -c payloads are code inside a quoted token this parser does not descend
    # into, so a payload that names a git write is ambiguous. The same holds for code reaching a
    # shell's stdin: its own heredoc body (`bash <<EOF`) or a body piped in from the
    # previous segment (`cat <<EOF | bash`).
    h, env_s_cmd = head, None
    while h < len(toks):
        # A brace-group opener runs its body in this shell, and a keyword such
        # as `then` comes before the command word it runs.
        if toks[h] in ("{", "if", "then", "elif", "else", "while", "until", "do", "!"):
            h += 1
            continue
        if os.path.basename(toks[h]) in (
            "env", "command", "builtin", "exec", "sudo", "nohup", "nice",
        ):
            lookup = os.path.basename(toks[h]) == "command"
            h += 1
            while h < len(toks) and (
                toks[h].startswith("-") or is_env_assignment(toks[h])
            ):
                if lookup and re.match(r"-\w*[vV]", toks[h]):
                    return None  # `command -v` only looks a name up; it runs nothing
                if toks[h] == "-S" and h + 1 < len(toks):
                    env_s_cmd = toks[h + 1]  # `env -S "bash -s"`: quoted command string
                    h += 2
                    continue
                if toks[h].startswith("-S") and len(toks[h]) > 2:
                    env_s_cmd = toks[h][2:]  # glued `env -Sbash`: the real command
                h += 1
            continue
        break
    hb = command_name(toks[h]) if h < len(toks) else ""
    if env_s_cmd:
        parts = env_s_cmd.split()
        if parts:
            hb = command_name(parts[0])
    stdin_code = " ".join(segment_bodies(toks)) + " " + " ".join(piped)
    rest = (env_s_cmd.split()[1:] if env_s_cmd else []) + toks[h + 1 :]
    if hb in ("eval", "sh", "bash", "zsh", "dash", "ksh", ".", "source") or hb in WINDOWS_SHELLS:
        # With a short -c flag the shell executes its ARGV string; stdin is
        # plain data then (`cat <<EOF | bash -c 'true'` must not go ambiguous).
        payload = " ".join(rest)
        if not any(re.match(r"-\w*c$", t) for t in rest):
            payload += " " + stdin_code
        if names_git_write(payload):
            print("__AMBIGUOUS__")
        return None
    # A script interpreter reaches git only through stdin code, so only the body counts
    # and its argv is data. A -c/-e/-p code flag or a script-file operand means stdin is
    # data instead ('-' names stdin explicitly).
    if hb in ("python", "python3", "perl", "ruby", "node"):
        runs_stdin = True
        for t in rest:
            if HDN.match(t):
                continue
            if t == "-":
                break
            if re.match(r"-\w*[cepE]", t) or not t.startswith("-"):
                runs_stdin = False
                break
        if runs_stdin and names_git_write(stdin_code):
            print("__AMBIGUOUS__")
        return None

    for i, tok in enumerate(toks):
        # Any `git` token counts, but a Windows name or a path (`git.exe`,
        # `/usr/bin/git`) counts only as the command word. `"$g"` counts when
        # the command set g=git.
        word = _expand(tok)
        if word != "git" and (i != h or command_name(word) != "git"):
            continue
        gdir = effdir
        opts, j = git_global_options(toks, i + 1)
        for flag, value in opts:
            if flag == "-C":
                gdir = norm(gdir, value)
            elif flag in ("--git-dir", "--work-tree") or flag.startswith(
                ("--git-dir=", "--work-tree=")
            ):
                redirected = True  # repo redirected away from gdir - can't attribute
        verb = _expand(toks[j]) if j < len(toks) else None
        if verb is not None and ("$" in verb or "`" in verb):
            print("__AMBIGUOUS__")  # a verb only bash knows (`git $V`) may be commit
            break
        if verb == "worktree" and toks[j + 1 : j + 2] == ["add"]:
            # The new path may be a project checkout, so a commit there is not foreign.
            print("new-checkout")
        if verb == "commit":
            if redirected:
                print("__AMBIGUOUS__")
            else:
                print("commit\t" + (gdir or ""))
        elif verb in ("checkout", "switch"):
            # A `--` pathspec separator anywhere means file-restore mode, where HEAD
            # does not move. The event carries the repo dir the switch targets, so a
            # switch in a sibling checkout cannot unprotect this repo's branch state.
            target, filemode = "", False
            for t in toks[j + 1:]:
                if t == "--":
                    filemode = True
                elif t.startswith("-"):
                    pass
                elif not target:
                    target = t
            if target and not filemode:
                # A target that keeps a `$` is a branch only bash knows; the
                # evaluator treats it as protected.
                print("switch\t" + (gdir or "") + "\t" + _expand(target))
        break  # only the first git invocation of a segment counts
    return None


# Any assignment to a name: `W=..`, `W+=..`, `W[0]=..`.
_ASSIGNS = re.compile(r"([A-Za-z_]\w*)(\[[^\]]*\])?\+?=")


def _trusted_names(tokens, text):
    """Variable names whose single literal assignment `_bind_vars` may record.

    A name assigned twice anywhere (`then W=..`, `W+=..`, `W[0]=..`, a function
    body, `eval W=..`), or named as a bare word (`for W`, `read W`, `unset W`), may
    hold another value where it is used. A sourced file can set anything, and a
    nameref (`declare -n R=W`) can set W through another name. The tokens have lost their
    quotes and escapes, so a name that bash does not expand in text (`cd '$W'`,
    `cd \\$W`, the literal `$W`) is not trusted.
    """
    for i, tok in enumerate(tokens):
        if tok not in (".", "source", "eval", "declare", "typeset", "local"):
            continue
        prev = tokens[i - 1] if i else ";"
        if prev.strip("();<>|&") and prev not in ("then", "do", "else", "{", "!"):
            continue  # an argument (`-m 'source'`), not the command word
        return frozenset()
    counts = collections.Counter(m.group(1) for m in map(_ASSIGNS.match, tokens) if m)
    trusted = set(counts)
    trusted -= {n for n, c in counts.items() if c > 1}
    trusted -= set(tokens)
    literal = " ".join(_unexpanded(text))
    trusted -= {m.group(1) or m.group(2) for m in _VAR_REF.finditer(literal)}
    return frozenset(trusted)


def _unexpanded(text):
    """The parts of text where bash does not expand `$`: single-quoted strings outside
    double quotes (inside double quotes a `'` is a plain character), and `\\$W`."""
    spans, i, q, start = [], 0, None, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and q != "'":
            escaped = _VAR_REF.match(text, i + 1)
            if escaped:
                spans.append(escaped.group(0))
            i += 2
            continue
        if c == '"' and q != "'":
            q = None if q else '"'
        if c == "'" and q != '"':
            if q:
                spans.append(text[start:i])
            start, q = i + 1, None if q else "'"
        i += 1
    if q == "'":
        spans.append(text[start:])  # unclosed: treat the rest as quoted
    return spans


def _forks(op):
    """True for an operator that runs the command before it in a subshell: a pipe
    (which also runs the command after it in one) or `&`."""
    op = op.replace("||", "").replace("&&", "")
    return "|" in op or ("&" in op and not any(c in op for c in "<>"))


def _runs_after_failure(op):
    """True for an operator after which the next command runs even when the one
    before it failed (`;`, `||`, `&`). `&&`, a pipe (`a && b | c` runs the pipe
    only when a succeeded), grouping and redirections (which this parser also
    splits on) are not such operators."""
    op = op.strip("()")
    return ";" in op or "||" in op or op == "&"


def _is_assignment(segment):
    """True for a segment that is one `W=..` or `export W=..` and nothing else."""
    words = segment[1:] if segment[:1] == ["export"] else segment
    return len(words) == 1 and is_env_assignment(words[0])


def _bind_vars(segment, after, trusted):
    """Record `W=<literal>` (or `export W=<literal>`) from the run of assignments that
    opens the command (`S=..; T=..; git -C "$T" ..`), where each one surely runs:
    `_emit` calls this only while every segment so far was one assignment and every
    operator `;`, a newline or `&&`. The value must be literal once the names bound
    so far are substituted (`T="$S/plant"`)."""
    if not _is_assignment(segment):
        return  # `W=x cmd` sets W for cmd alone; `export S=x T=$S` expands T first
    if after is not None and _forks(after):
        return  # a pipe or `&` runs it in a subshell
    words = segment[1:] if segment[:1] == ["export"] else segment
    name, _, value = words[0].partition("=")
    if re.search(r"[*?[]", value):
        return  # bash expands an unquoted pattern where the name is used
    value = _expand(value)
    if name in trusted and value and "$" not in value and "`" not in value:
        shell_vars[name] = value


_GIT_DIR_VARS = ("GIT_DIR", "GIT_WORK_TREE")


def _sets_git_dir(tok):
    return is_env_assignment(tok) and tok.split("=", 1)[0] in _GIT_DIR_VARS


def _redirects_later_commands(tokens):
    """True when GIT_DIR or GIT_WORK_TREE is set for the commands after it:
    `export GIT_DIR=..` or a standalone `GIT_DIR=..;`, not a prefix of one command
    (`GIT_DIR=.. git status`), which process_segment reads per segment."""
    for i, tok in enumerate(tokens):
        if not _sets_git_dir(tok):
            continue
        j = i + 1
        while j < len(tokens) and is_env_assignment(tokens[j]):
            j += 1
        if j == len(tokens) or not tokens[j].strip("();<>|&"):
            return True
    return False


# A command that makes a link or moves a directory can put the project checkout at
# a new path.
_MAKES_CHECKOUT_PATH = re.compile(
    r"(?i)\bmklink\b|\bln\b[^;&|\n]*\s(-\w*s\w*|--symbolic)\b|symboliclink|junction"
    r"|\bmv\b"
)


def _emit(cmd, default):
    global heredoc_bodies, git_dir_redirected
    shell_vars.clear()
    git_dir_redirected = False
    if _MAKES_CHECKOUT_PATH.search(cmd.replace("\\\n", "")):
        print("new-checkout")
    cmd_text, heredoc_bodies, ambiguous = extract_heredocs(cmd)
    if ambiguous:
        # The body cannot be located, so read the WHOLE raw command: a real commit can
        # sit after the unresolvable heredoc (`cat <<$(echo EOF) … <close> … git
        # commit`), past where `cmd_text` stops.
        if names_git_write(cmd):
            print("__AMBIGUOUS__")
        return

    try:
        tokens = tokenize(cmd_text)
    except ValueError:
        # Only a git write needs guarding here; odd quoting elsewhere (an apostrophe in
        # echo text) is not a fail-closed case. The bodies are checked as well, so an
        # interpreter-fed body cannot slip through.
        if names_git_write(cmd_text) or any(names_git_write(b) for b in heredoc_bodies):
            print("__AMBIGUOUS__")
        return

    trusted = _trusted_names(tokens, cmd_text)
    git_dir_redirected = _redirects_later_commands(tokens)
    leading = True  # every segment so far is one assignment, every operator `;`/`&&`
    effdir = default
    # A cd to a path that does not exist yet succeeds only if the command makes it
    # first, and a cd after `&&` or `||` may be skipped. Past `;`, `||` or `&`, a
    # later command may then run in the old dir.
    cd_may_fail = False
    scope_stack = []  # (effdir, cd_may_fail) saved at each '(' - a cd inside a subshell is scoped
    segment = []
    prev_op = None
    piped = []  # heredoc bodies flowing into the next segment's stdin via '|'
    lead = 0  # 1 when the current segment starts with a redirection target
    for tok in tokens:
        if tok and not tok.strip("();<>|&"):  # pure-operator token -> boundary
            before, prev_op = prev_op, tok
            if leading:
                _bind_vars(segment, tok, trusted)
            leading = leading and (not segment or _is_assignment(segment)) and (
                tok == "&&" or not tok.strip(";")
            )
            new_dir = process_segment(segment, effdir, piped, lead)
            # A redirection starts the next command only when no command word
            # stands before it: only an earlier redirection target, env
            # assignments and an fd number (`2>&1 git`, `>a >b git`, `X=1 >f
            # git`). In `echo 2>&1 a b`, the words after it are echo's arguments.
            words_before = [t for t in segment[lead:] if not is_env_assignment(t)]
            lead = 1 if set(tok) & set("<>") and "(" not in tok else 0
            if words_before and not (len(words_before) == 1 and words_before[0].isdigit()):
                lead = 0
            if _forks(tok) or (before and "|" in before.replace("||", "")):
                new_dir = None  # a cd in a pipe or before `&` runs in a subshell
            if new_dir is UNKNOWN_DIR:
                effdir, cd_may_fail = None, False
            elif new_dir is not None:
                effdir, cd_may_fail = new_dir, not os.path.isdir(new_dir)
                if before and before.strip("()") in ("&&", "||"):
                    cd_may_fail = True  # skipped when the test before it decides so
            if cd_may_fail and _runs_after_failure(tok):
                effdir, cd_may_fail = None, False
            # Heredoc bodies flow into the next segment's stdin across pipe hops (`cat
            # <<EOF | tee x | bash`) — over-carrying only fails closed — and survive
            # pure grouping boundaries; any sequential operator resets.
            if "|" in tok.replace("||", ""):
                piped = segment_bodies(segment) + piped
            elif tok.strip("()") == "":
                pass  # grouping only: the group inherits the pipeline stdin
            else:
                piped = []
            segment = []
            # Subshell scoping: a `cd` inside `( )` must not change the directory of
            # commands after the closing paren, while a commit inside does run in it —
            # so process the pending segment before restoring on ')'.
            for ch in tok:
                if ch == "(":
                    scope_stack.append((effdir, cd_may_fail))
                elif ch == ")" and scope_stack:
                    effdir, cd_may_fail = scope_stack.pop()
            continue
        segment.append(tok)
    if leading:
        _bind_vars(segment, None, trusted)
    new_dir = process_segment(segment, effdir, piped, lead)


def emit_events(cmd: str, default_dir: str) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _emit(cmd, default_dir)
    return output.getvalue().rstrip("\n")


if __name__ == "__main__":
    print(emit_events(sys.argv[1], sys.argv[2]))

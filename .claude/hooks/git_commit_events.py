import contextlib
import io
import os
import re
import shlex
import sys

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


def norm(base, path):
    """Resolve a cd/-C path against the current effective dir.

    Returns None when the path cannot be trusted: a '$' means a shell variable
    whose in-command value this parser cannot know (expanding against the
    hook's own environment resolved a stale value once) - callers treat None
    as unresolvable, which fails closed for commits and never unprotects for
    switches.
    """
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


def process_segment(toks, effdir, piped=()):
    """Handle one simple command; return the new effective dir (cd) or None."""
    if not toks:
        return None
    if toks[0] == "cd" and len(toks) >= 2 and not toks[1].startswith("-"):
        return norm(effdir, toks[1])

    # GIT_DIR / GIT_WORK_TREE redirect the repo somewhere this parser cannot attribute,
    # so a commit under them is ambiguous.
    head, redirected = 0, False
    while head < len(toks) and is_env_assignment(toks[head]):
        if toks[head].split("=", 1)[0] in ("GIT_DIR", "GIT_WORK_TREE"):
            redirected = True
        head += 1

    # eval / sh -c payloads are code inside a quoted token this parser does not descend
    # into, so a commit-shaped payload is ambiguous. The same holds for code reaching a
    # shell's stdin: its own heredoc body (`bash <<EOF`) or a body piped in from the
    # previous segment (`cat <<EOF | bash`).
    h, env_s_cmd = head, None
    while h < len(toks):
        if toks[h] == "{":  # brace-group opener runs its body in this shell
            h += 1
            continue
        if os.path.basename(toks[h]) in (
            "env", "command", "builtin", "exec", "sudo", "nohup", "nice",
        ):
            h += 1
            while h < len(toks) and (
                toks[h].startswith("-") or is_env_assignment(toks[h])
            ):
                if toks[h] == "-S" and h + 1 < len(toks):
                    env_s_cmd = toks[h + 1]  # `env -S "bash -s"`: quoted command string
                    h += 2
                    continue
                if toks[h].startswith("-S") and len(toks[h]) > 2:
                    env_s_cmd = toks[h][2:]  # glued `env -Sbash`: the real command
                h += 1
            continue
        break
    hb = os.path.basename(toks[h]) if h < len(toks) else ""
    if env_s_cmd:
        parts = env_s_cmd.split()
        if parts:
            hb = os.path.basename(parts[0])
    stdin_code = " ".join(segment_bodies(toks)) + " " + " ".join(piped)
    rest = (env_s_cmd.split()[1:] if env_s_cmd else []) + toks[h + 1 :]
    if hb in ("eval", "sh", "bash", "zsh", "dash", "ksh", ".", "source"):
        # With a short -c flag the shell executes its ARGV string; stdin is
        # plain data then (`cat <<EOF | bash -c 'true'` must not go ambiguous).
        payload = " ".join(rest)
        if not any(re.match(r"-\w*c$", t) for t in rest):
            payload += " " + stdin_code
        if "git" in payload and "commit" in payload:
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
        if runs_stdin and "git" in stdin_code and "commit" in stdin_code:
            print("__AMBIGUOUS__")
        return None

    for i, tok in enumerate(toks):
        if tok != "git":
            continue
        gdir, verb, j = effdir, None, i + 1
        while j < len(toks):
            t = toks[j]
            if t == "-C" and j + 1 < len(toks):
                gdir = norm(gdir, toks[j + 1])
                j += 2
                continue
            if t in ("--git-dir", "--work-tree") or t.startswith(("--git-dir=", "--work-tree=")):
                redirected = True  # repo redirected away from gdir - can't attribute
                j += 2 if t in ("--git-dir", "--work-tree") else 1
                continue
            if t in ("-c", "--namespace", "--exec-path") and j + 1 < len(toks):
                j += 2
                continue
            if t.startswith("-"):
                j += 1
                continue
            verb = t
            break
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
                print("switch\t" + (gdir or "") + "\t" + target)
        break  # only the first git invocation of a segment counts
    return None


# Same set process_segment treats as interpreters: a command word whose stdin is
# executed as code, so a hidden `git commit` in an unresolvable body could run.
_INTERPRETERS = frozenset(
    ("eval", "sh", "bash", "zsh", "dash", "ksh", ".", "source",
     "python", "python3", "perl", "ruby", "node")
)


def _ambiguous_bears_git(raw_cmd):
    """When a heredoc delimiter is unresolvable, decide whether the command
    could still carry a git commit — so a benign data-sink heredoc (`cat
    <<$VAR`) is NOT falsely blocked, while a real or interpreter-hidden commit
    still is.

    Operates on the WHOLE raw command, not the ambiguity-truncated `cmd_text`:
    a real commit can sit AFTER the unresolvable heredoc (`cat <<$(echo EOF) …
    <close> … git commit`), which `cmd_text` (built only up to the ambiguity)
    would miss — a fail-OPEN. Tokenizing the raw command (newlines become `;`,
    quotes joined) surfaces a `git` word anywhere in it. Quote-AWARE (shlex
    joins `g''it` -> `git`) and one-directional: it only moves the verdict from
    block toward allow when there is provably no git surface at all, so it
    cannot reopen the quoting bypass. Heredoc BODY text is tokenized too, so a
    body that merely mentions `git commit` as prose over-blocks — fail-safe.
    """
    try:
        toks = tokenize(raw_cmd)
    except ValueError:
        return True  # can't parse -> can't reason -> fail closed
    if any(t == "git" for t in toks):
        return True
    if any(os.path.basename(t) in _INTERPRETERS for t in toks):
        return True
    return False


def _emit(cmd, default):
    global heredoc_bodies
    cmd_text, heredoc_bodies, ambiguous = extract_heredocs(cmd)
    if ambiguous:
        if _ambiguous_bears_git(cmd):
            print("__AMBIGUOUS__")
        return

    try:
        tokens = tokenize(cmd_text)
    except ValueError:
        # Only a commit-shaped command needs guarding here; odd quoting elsewhere (an
        # apostrophe in echo text) is not a fail-closed case. The bodies are checked as
        # well, so an interpreter-fed body cannot slip through.
        if ("git" in cmd_text and "commit" in cmd_text) or any(
            "git" in b and "commit" in b for b in heredoc_bodies
        ):
            print("__AMBIGUOUS__")
        return

    effdir = default
    scope_stack = []  # effdir saved at each '(' - a cd inside a subshell is scoped
    segment = []
    piped = []  # heredoc bodies flowing into the next segment's stdin via '|'
    for tok in tokens:
        if tok and not tok.strip("();<>|&"):  # pure-operator token -> boundary
            new_dir = process_segment(segment, effdir, piped)
            if new_dir is not None:
                effdir = new_dir
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
                    scope_stack.append(effdir)
                elif ch == ")" and scope_stack:
                    effdir = scope_stack.pop()
            continue
        segment.append(tok)
    new_dir = process_segment(segment, effdir, piped)


def emit_events(cmd: str, default_dir: str) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _emit(cmd, default_dir)
    return output.getvalue().rstrip("\n")


if __name__ == "__main__":
    print(emit_events(sys.argv[1], sys.argv[2]))

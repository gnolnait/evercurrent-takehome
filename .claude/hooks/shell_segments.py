"""Shared shell segmenter for the command-text guards.

`split_segments` turns a Bash command line into the list of top-level command
segments a guard must inspect. It is the ONE place that models the shell forms
which decide WHERE a command word starts:

  * quoting        - `'...'` / `"..."` are opaque, so a trigger word inside a
                     message or a doc is DATA, not a command
  * heredocs       - a terminated body is data and is dropped; an unterminated
                     one is rescanned as command text (fail closed)
  * arithmetic     - `<<` inside `$(( ))` / `(( ))` is a bitshift OPERATOR, not
                     a heredoc opener
  * comments       - `#` at command position hides the rest of the line
  * substitutions  - `$( )`, backticks, `${ ; }`, `( )`, `{ }` all start a new
                     segment, so a command hidden inside one is still inspected
  * REDIRECTIONS   - the operator AND its target word are removed, because bash
                     removes them BEFORE it resolves the command word

That last one is why this module exists. `no_merge_pr_impl` and
`no_bypass_signing_impl` each used to carry their own redirect handling, and the
two drifted: a form one guard removed correctly (a leading `2>&1`, a `>&word`
file target, a `>|` clobber) was a fail-open in the other. Every such form is
now handled once, here, and both guards consume it — so a new redirect form is
fixed in one place instead of two.

Redirection coverage, all measured against bash 5.3:

  `>f` `<f` `>>f` `<>f` `2>f`   plain redirections, with an optional io-number
  `>|f` `2>|f`                  force-clobber; the `|` binds to the OPERATOR and
                                is not a pipe
  `&>f` `&>>f`                   both-streams forms; the `&` binds to the operator
  `>&1` `2>&1` `<&3` `>&-` `<&-`  fd DUPLICATION / close - the operand is a
                                descriptor, not a filename
  `>&word` `&>word`             NOT duplication: bash sends both streams to the
                                FILE `word` and still runs the command
  `<<<WORD`                     here-string; feeds one word to stdin, opens no
                                body

Deliberately NOT swallowed: a redirection TARGET that contains a command
substitution (`` : >`cmd` ``, `: >$(cmd)`, `: >${ cmd; }`, `: > >(cmd)`). Bash
RUNS those to compute the filename, so the scan stops at the introducer and the
inner command becomes its own segment.

The redirection scan runs on RAW TEXT, before tokenizing, on purpose: it is
quote-aware, so a quoted look-alike (`'>/gh' pr merge`) is a command PATHNAME
and never reaches the redirect branch. The same strip applied after
`shlex.split` cannot tell the two apart and let that command through.

Two contracts a caller may rely on, kept deliberately open so URL-path and
stdin-payload matching can extend this module rather than fork it:

  * nested executable text is ALWAYS emitted as its own segment, so command
    inspection keeps working no matter what other projection is added;
  * the walker already knows every substitution SPAN and every heredoc /
    here-string body, so an outer-word projection (spans collapsed to one
    opaque non-slash sentinel, for `pulls/<sentinel>/merge` matching) and
    per-command body metadata are additive — neither needs the segment
    contract above to change.

Known residual (pre-existing, adversarial-only): arithmetic in BRACKET-delimited
contexts - legacy `$[ ... ]` and parameter-expansion subscripts (`${a[i<<2]}`) -
is not modeled, because the `]` delimiter collides with array-subscript `]` and
tracking it leaks a new fail-open per rule. A `<<` there is still misread as a
heredoc opener.

The same holds for WHAT a command word names. `command_name` reads `gh.exe`, `GH`
and a full Windows path as `gh`; all three guards call it.

The guards do NOT parse the text that `powershell`, `pwsh` or `cmd` runs. Those
shells quote, escape, comment and chain commands by rules unlike POSIX shell
(`'` is plain text in cmd, a backtick joins lines in PowerShell, `call` runs a
command), so a POSIX parse misses real commands. `windows_shell_text` gives each
guard the whole text, and the guard blocks when the text names what it guards
against: a merge, a bypass flag, or git and commit on a protected branch. Text
that only prints those words blocks too. That is the safe direction.
"""

import re

# Tools whose `command` is shell text that the guards must read.
SHELL_TOOLS = frozenset(
    {
        "Bash",
        "PowerShell",
    }
)

# Windows shells that run their argument text as a command.
WINDOWS_SHELLS = frozenset({"powershell", "pwsh", "cmd"})

_PATH_SEP = re.compile(r"[/\\]")

_HEREDOC_BARE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./!:@%+,=~-"
)


def _peek(s, i, k):
    r"""Up to k *logical* chars from s[i], collapsing `\`+newline continuations
    (bash removes them before tokenizing, so `$(\<nl>(` is `$((`). Returns
    (text, end) so a matched introducer can advance over the collapsed span."""
    text, j = [], i
    while j < len(s) and len(text) < k:
        if s[j] == "\\" and j + 1 < len(s) and s[j + 1] == "\n":
            j += 2
            continue
        text.append(s[j])
        j += 1
    return "".join(text), j


def _simple_param_expansion_end(cmd, i, n):
    """
    End of the plain `${var}` expansion at `i`, or -1 when it is not plain.

    Only a SIMPLE expansion may be glued into the surrounding word: brace
    depth counted with quotes honored, and no substitution inside. Anything
    else returns -1 and the caller must fall back to splitting on the braces
    — the pre-consolidation behavior, which is fail-CLOSED.

    The two shapes that must never be glued, because gluing hides a real
    command from every guard:

      * a QUOTED brace (`${x:-'}'} ; gh pr merge`) — counting literal `}`
        ends the expansion early, and the unbalanced quote left behind then
        swallows the whole trailing command into one word;
      * a nested substitution (`${x:-$(gh pr merge)}`, backtick, or a
        funsub `${ cmd; }`) — bash RUNS it, so it has to reach the walker
        and become its own segment.
    """
    k, depth = i + 2, 1
    while k < n:
        ch = cmd[k]
        if ch == "\\" and k + 1 < n:
            k += 2
            continue
        if ch == "`":
            return -1
        if ch == "$" and k + 1 < n and cmd[k + 1] == "(":
            return -1
        if ch == "$" and k + 1 < n and cmd[k + 1] == "{":
            if k + 2 < n and cmd[k + 2] in " \t\n|":
                return -1  # nested funsub runs code
            depth += 1
            k += 2
            continue
        if ch in "'\"":
            close = k + 1
            while close < n and cmd[close] != ch:
                # Inside "..." a backslash escapes the next char; inside
                # '...' nothing does, so only honor it for the double form.
                if ch == '"' and cmd[close] == "\\" and close + 1 < n:
                    close += 2
                    continue
                close += 1
            if close >= n:
                return -1  # unterminated quote
            k = close + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return -1  # unterminated expansion


def _scan_target_run(cmd, j, n):
    """
    End index of one redirect-target character run starting at `j`.

    Stops at whitespace / shell metacharacters, and BEFORE a substitution
    introducer (a backtick, `$(`, `${`) so [split_segments]'s main loop can
    tag the frame correctly — stopping AFTER the `$` mis-tags `$( )` as a
    bare subshell, whose glued-`#` comment rule then swallows a real later
    command. Consumes quoted spans whole; returns -1 on an unterminated
    quote (the caller abandons the redirection and rescans, fail-closed).
    """
    while j < n and cmd[j] not in " \t\n;|&<>(){}`":
        ch = cmd[j]
        if ch == "$" and j + 1 < n and cmd[j + 1] == "(":
            break
        if ch == "$" and j + 1 < n and cmd[j + 1] == "{":
            if j + 2 < n and cmd[j + 2] in " \t\n|":
                break  # funsub `${ cmd; }` RUNS code — the main loop frames it
            # Plain `${var}` expands as FILENAME text (`>${HOME}/x cmd` — the
            # word continues through and past it), so consume it whole;
            # stopping at the `$` would glue the rest of the line into one
            # segment whose command word is the expansion (a fail-open).
            # A non-simple expansion stops the scan instead: the walker then
            # splits on its braces and segments anything executable inside.
            end = _simple_param_expansion_end(cmd, j, n)
            if end < 0:
                break
            j = end
            continue
        if ch in "'\"":
            close = j + 1
            while close < n and cmd[close] != ch:
                close += 1
            if close >= n:
                return -1
            j = close + 1
        elif ch == "\\" and j + 1 < n:
            j += 2
        else:
            j += 1
    return j


def _target_continues(cmd, k, n):
    """True when position `k` sits on a substitution introducer that keeps a
    redirect-target word open: a backtick, `$(`, or a funsub `${ `/`${|`.
    Plain `${var}` never stops [_scan_target_run] (consumed inline), so it is
    deliberately not an introducer here."""
    if k >= n or cmd[k] not in "`$":
        return False
    if cmd[k] == "`":
        return True
    if k + 1 < n and cmd[k + 1] == "(":
        return True
    return k + 1 < n and cmd[k + 1] == "{" and k + 2 < n and cmd[k + 2] in " \t\n|"


def split_segments(cmd):
    """Split a shell command into top-level command segments.

    Quote-aware (never splits inside '...' or "...") and DROPS heredoc bodies so
    their lines never become segments. Splits on top-level ; & | newline and on
    ( ) { } ` group/substitution boundaries. Redirections (operator + target) are
    removed rather than treated as boundaries - see the module docstring.

    Arithmetic-aware: inside `$(( ))` / `(( ))` a `<<` is a bitshift operator,
    not a heredoc opener. A `ctx` stack tracks arithmetic vs command-substitution
    vs backtick vs subshell frames; only an `arith` frame reinterprets `<<`. A
    command substitution nested in arithmetic restores real heredoc parsing, so a
    genuine heredoc body there is still dropped as data.
    """
    segs = []
    buf = []
    heredocs = []  # (delimiter, strip_tabs) pairs awaiting their body
    ctx = []  # [kind, open-paren-count] frames: arith/cmdsub/backtick/subshell
    # Position + closed-frame kind of the most recent `)` that closed a ctx
    # frame — used to decide whether a `#` glued to that `)` is a comment (bash
    # treats `#` after a subshell `)` as a comment, but after a `$((..))`/`$(..)`
    # substitution `)` it is a literal word char).
    last_close_pos, last_close_kind = -1, None
    # Armed when a redirect-target word is interrupted by a substitution
    # (`>a$(x)#tail`): holds the ctx depth to resume the word at when the
    # substitution's frame closes. Bash resumes the FILENAME there; leaving
    # the glued residue as command text shifts the command word (fail-open).
    pending_target = None
    i, n = 0, len(cmd)

    def flush():
        s = "".join(buf).strip()
        if s:
            segs.append(s)
        del buf[:]

    def resume_target():
        # Re-enter a redirect-target word a substitution interrupted: the
        # glued residue is FILENAME text, not command text. Keeps the arm
        # when another substitution continues the same word; disarms on an
        # unterminated quote (main loop's quote handling is fail-closed).
        nonlocal i, pending_target
        k = _scan_target_run(cmd, i, n)
        if k < 0:
            pending_target = None
            return
        if not _target_continues(cmd, k, n):
            pending_target = None
        i = k

    while i < n:
        c = cmd[i]
        if c == "'":
            j = cmd.find("'", i + 1)
            if j == -1:
                j = n - 1
            buf.append(cmd[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            buf.append(c)
            i += 1
            while i < n and cmd[i] != '"':
                if cmd[i] == "\\" and i + 1 < n:
                    if cmd[i + 1] == "\n":
                        i += 2
                        continue
                    buf.append(cmd[i:i + 2])
                    i += 2
                    continue
                buf.append(cmd[i])
                i += 1
            if i < n:
                buf.append('"')
                i += 1
            continue
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] == "\n":
                i += 2
                continue
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        # A `#` at command position starts a comment: bash ignores it to end of
        # line, so a `<<EOF` inside it opens no heredoc and a comment-hidden
        # command on a following line must stay visible (dropping the comment as
        # a phantom heredoc body was a fail-open). A `#` inside a word (`x#y`) is
        # not a comment. A `)` is a word boundary only when it closed a SUBSHELL
        # (`( ... )`); a `)` closing a `$((..))`/`$(..)` substitution keeps the
        # word open, so `$((1))#` is a literal `1#`, not a comment — treating it
        # as one dropped a real later command (fail-open). The position check
        # rejects a quoted/escaped `)` that never closed a frame.
        after_subshell = (
            i > 0
            and cmd[i - 1] == ")"
            and i - 1 == last_close_pos
            and last_close_kind == "subshell"
        )
        if c == "#" and (i == 0 or cmd[i - 1] in " \t\n;|&(" or after_subshell):
            if pending_target is not None and len(ctx) <= pending_target:
                # A separator at (or above) the armed depth means the target
                # word can no longer resume — a stale arm must not fire at a
                # LATER close and swallow real command text.
                pending_target = None
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        # Enter arithmetic / command-substitution contexts. Introducers are
        # matched over collapsed `\`+newline continuations. `$((`/`((` open
        # arithmetic; `$(` a command substitution. Flushing preserves the
        # existing group-boundary behavior (a `(` already flushed).
        p3, e3 = _peek(cmd, i, 3)
        if p3 == "$((":
            ctx.append(["arith", 2])
            flush()
            i = e3
            continue
        p2, e2 = _peek(cmd, i, 2)
        if p2 == "((":
            ctx.append(["arith", 2])
            flush()
            i = e2
            continue
        if p2 == "$(":
            ctx.append(["cmdsub", 1])
            flush()
            i = e2
            continue
        if p2 == "${":
            nxt, _ = _peek(cmd, e2, 1)
            if nxt not in (" ", "\t", "\n", "|"):
                # Plain `${var}` parameter expansion — WORD text, not a
                # brace-group boundary. Consuming only `${` here left the
                # matching `}` to the unconditional `c in "{}"` flush handler
                # below, splitting the command word off its prefix
                # (`GIT_DIR=${HOME}/x git commit` segmented into `x` and
                # `git commit`, a fail-open — a funsub's OWN braces are still
                # a real boundary, so only the non-funsub form is glued here).
                # Only a SIMPLE expansion may be glued: a quoted brace or a
                # nested substitution inside it must keep the old splitting
                # behavior, or the leftover text swallows a real trailing
                # command. `_simple_param_expansion_end` draws that line.
                end = _simple_param_expansion_end(cmd, i, n)
                if end > 0:
                    buf.append(cmd[i:end])
                    i = end
                    continue
                buf.append(c)
                i += 1
                continue
            if nxt in (" ", "\t", "\n", "|"):
                # bash 5.3 "funsub" `${ cmd; }` / `${| cmd; }` RUNS cmd. Track
                # a frame so the closing `}` is attributable — a redirect-target
                # word interrupted by one must resume there. Plain `${var}`
                # deliberately keeps the old path (no frame).
                ctx.append(["bracesub", 1])
                flush()
                i = e2
                continue
        if ctx and ctx[-1][0] == "arith" and cmd[i:i + 2] == "<<":
            # arithmetic left-shift operator, not a heredoc opener
            i += 2
            continue
        if c == "<" and i + 1 < n and cmd[i + 1] == "<" and not (i + 2 < n and cmd[i + 2] == "<"):
            k = i + 2
            # `<<-` strips leading TABS from body lines AND from the close line;
            # a plain `<<` does neither. Which one it is decides whether an
            # indented delimiter look-alike closes the body, so carry it along.
            strip_tabs = False
            if k < n and cmd[k] == "-":
                strip_tabs = True
                k += 1
            while k < n and cmd[k] in " \t":
                k += 1
            if k < n and cmd[k] == "\\":
                k += 1
            q = ""
            if k < n and cmd[k] in ("'", '"'):
                q = cmd[k]
                k += 1
            if q:
                start = k
                close = cmd.find(q, k)
                if close > k:
                    delim = cmd[k:close]
                    k = close + 1
                else:
                    close = cmd.find("\n", k)
                    k = n if close < 0 else close
                    delim = cmd[start:k]
            else:
                ds = k
                while k < n and cmd[k] in _HEREDOC_BARE_CHARS:
                    k += 1
                delim = cmd[ds:k]
            if delim:
                heredocs.append((delim, strip_tabs))
                i = k  # drop the opener; the rest of the line is still scanned
                continue
        if (c in "<>" or (c == "&" and i + 1 < n and cmd[i + 1] == ">")) and not (
            i + 1 < n and cmd[i + 1] == "("
        ):
            # A redirection: bash removes the operator AND its target word before
            # it resolves the command word, so `>/dev/null gh pr merge` still runs
            # the merge. Treating the redirection as a segment boundary left the
            # target as the command word and the real command went unexamined (a
            # fail-open). `<(`/`>(` process substitution is excluded — it runs a
            # real command whose output becomes a file.
            j = i
            while j < n and cmd[j] in "<>":
                j += 1
            if j < n and cmd[j] == "|" and cmd[j - 1] == ">":
                # `>|` force-clobber: the `|` is part of the redirection
                # operator, NOT a pipe. Bash still runs the command after
                # clobbering the file, so leaving the `|` as a segment boundary
                # made `>|/tmp/x gh pr merge` tokenize as `/tmp/x`,`gh pr merge`
                # with the command hidden behind the pipe (a fail-open).
                j += 1
            if j < n and cmd[j] == "&" and j + 1 < n and (cmd[j + 1].isdigit() or cmd[j + 1] == "-"):
                # fd DUPLICATION (`>&1`, `2>&1`) or close (`>&-`): the operand is
                # a descriptor, not a filename, so consume it here. `>&word` is
                # NOT this - bash sends both streams to the FILE `word` and still
                # runs the command, so it falls through to the filename scan
                # below. Consuming it as a descriptor left `word` as the command
                # word and the real command went unexamined (a fail-open).
                j += 1
                while j < n and (cmd[j].isdigit() or cmd[j] == "-"):
                    j += 1
            else:
                if j < n and cmd[j] == "&":
                    j += 1  # `>&word` / `&>word`: the `&` binds to the operator
                while j < n and cmd[j] in " \t":
                    j += 1
                # A COMMAND SUBSTITUTION in the target is not inert text: bash
                # RUNS it to compute the filename (`: >`gh pr merge`` executes
                # the merge). Stop at a backtick / `$(` and leave the rest to the
                # substitution branches below, which segment the inner command so
                # it is still inspected. The stop must land ON the `$`, not the
                # `(`: a scan that eats the `$` and halts at `(` makes the main
                # loop tag the frame as a bare subshell, and a `#` glued after
                # that close then reads as a comment that swallows a real later
                # command (`>a$(true)#; gh pr merge` allowed — a fail-open),
                # while a cmdsub close correctly keeps the word open.
                # `{` stops the scan too: bash 5.3's alternate command
                # substitution `${ cmd; }` RUNS cmd to build the filename, and
                # swallowing it would hide that command. Stopping leaves the
                # brace-group branch below to flush it as its own segment.
                # `(` covers `$(`, process substitution and arithmetic; `<`/`>`
                # cover `>(`. Tilde and glob targets execute nothing.
                # UNTERMINATED quote (-1): consuming to EOF would swallow a
                # real later command as target text (a fail-open). Abandon the
                # redirection reading entirely: emit the operator char and
                # rescan, which hands `<<<'abc` to the heredoc branch whose
                # unterminated-body rewind inspects the rest. Bash errors on
                # this input, so over-blocking it is the safe direction.
                j = _scan_target_run(cmd, j, n)
                if j == -1:
                    buf.append(c)
                    i += 1
                    continue
            # Only now that the redirection is definitely being consumed: an
            # io-number binds to it (`2>f`, `0<<<W`) and is not a command word.
            while buf and buf[-1].isdigit():
                buf.pop()
            buf.append(" ")  # keep the words on either side apart
            if _target_continues(cmd, j, n):
                # The substitution CONTINUES the filename word (`>a$(x)#` is
                # ONE target). Arm the resume so the word is re-entered when
                # the frame closes, instead of its glued residue landing at
                # command position and shifting the command word.
                pending_target = len(ctx)
            i = j
            continue
        if c == "\n":
            if pending_target is not None and len(ctx) <= pending_target:
                pending_target = None  # same stale-arm rule as `;`
            if heredocs:
                i += 1
                body_start = i
                while heredocs and i < n:
                    le = cmd.find("\n", i)
                    if le == -1:
                        le = n
                    line = cmd[i:le]
                    i = le + 1 if le < n else n
                    # The close line must be EXACTLY the delimiter; only `<<-`
                    # allows leading TABS. Matching with a full .strip() closed
                    # the body EARLY on an indented look-alike (`    EOF` under a
                    # plain `<<EOF`, which bash treats as ordinary body data),
                    # ejecting the body's remaining lines into command position
                    # and false-blocking a trigger written INTO a file.
                    delim, strip_tabs = heredocs[0]
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        heredocs.pop(0)
                if heredocs:
                    # A delimiter never appeared as a close line, so the body ran
                    # to EOF. Either a misread non-heredoc `<<` (e.g. arithmetic
                    # bitshift `$((a << b))`, captured as delimiter `b`) or a
                    # genuinely unterminated heredoc. Dropping the body would
                    # hide a real command inside it (a fail-open), and keeping it
                    # as one blob would expose only its first word as the command
                    # word. Rewind and scan the body as ordinary command text so
                    # EVERY segment is inspected. This advances: each rewind
                    # starts strictly later than the previous one, so the loop
                    # terminates. Fail closed.
                    del heredocs[:]
                    flush()
                    i = body_start
                    continue
                flush()
                continue
            flush()
            i += 1
            continue
        if c == ";":
            if pending_target is not None and len(ctx) <= pending_target:
                # A separator at (or above) the armed depth means the target
                # word can no longer resume — a stale arm must not fire at a
                # LATER close and swallow real command text.
                pending_target = None
            flush()
            i += 1
            continue
        if c in "&|":
            if pending_target is not None and len(ctx) <= pending_target:
                # A separator at (or above) the armed depth means the target
                # word can no longer resume — a stale arm must not fire at a
                # LATER close and swallow real command text.
                pending_target = None
            flush()
            while i < n and cmd[i] in "&|":
                i += 1
            continue
        if c == "`":
            # Backtick command substitution (does not nest). Opening pushes a
            # sentinel; closing pops back through it, discarding any frames left
            # open inside (e.g. a stray `((` in `` `((` ``) so they can't leak an
            # arith context onto a later real heredoc.
            closing_backtick = any(f[0] == "backtick" for f in ctx)
            if closing_backtick:
                while ctx and ctx.pop()[0] != "backtick":
                    pass
            else:
                ctx.append(["backtick", 0])
            flush()
            i += 1
            if (
                closing_backtick
                and pending_target is not None
                and pending_target == len(ctx)
            ):
                # A backtick substitution interrupted a redirect-target word
                # (`` >a`x`# ``): resume the filename after the close.
                resume_target()
            continue
        if c == "(":
            if ctx and ctx[-1][0] == "arith":
                ctx[-1][1] += 1
            else:
                ctx.append(["subshell", 1])
            flush()
            i += 1
            continue
        if c == ")":
            last_close_pos, last_close_kind = i, None
            if ctx:
                ctx[-1][1] -= 1
                if ctx[-1][1] <= 0:
                    last_close_kind = ctx.pop()[0]
            flush()
            i += 1
            if (
                pending_target is not None
                and pending_target == len(ctx)
                and last_close_kind in ("cmdsub", "arith")
            ):
                # Resume the interrupted redirect-target word (never after a
                # SUBSHELL close — there bash's glued-`#` comment rule applies
                # and no target was pending at that depth anyway).
                resume_target()
            continue
        if c == "}":
            closed_bracesub = bool(ctx) and ctx[-1][0] == "bracesub"
            if closed_bracesub:
                ctx.pop()
            flush()
            i += 1
            if pending_target is not None and pending_target == len(ctx):
                # Closes either a funsub frame or a plain `${var}` (frameless,
                # same depth) inside a redirect target: the filename resumes.
                resume_target()
            continue
        if c == "{":
            flush()
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return segs


def tool_command(tool, cmd):
    """Return a shell tool's command in the POSIX form the guards parse."""
    return _powershell_text(cmd) if tool == "PowerShell" else cmd


def _powershell_text(text):
    """Return PowerShell text in the POSIX form the guards parse.

    A backslash is a path separator in PowerShell, so it becomes a slash and
    `C:\\x\\gh.exe` stays a path to `gh.exe`. A backtick is the escape
    character: at a line end it joins the two lines, and before any other
    character it keeps that character.
    """
    text = re.sub(r"`\r?\n", " ", text)
    return text.replace("`", "").replace("\\", "/")


def command_name(word):
    """Return the program a command word runs: the last path part, in lower
    case, without `.exe`. Windows runs `GIT.EXE`, `git.exe` and a full path to
    it as `git`."""
    name = _PATH_SEP.split(word)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def windows_shell_text(name, args):
    """Return the text that the Windows shell `name` (`powershell`, `pwsh` or
    `cmd`) gets in `args` after its own leading flags, in lower case, for a
    guard to search. Return None for an encoded PowerShell command, which
    cannot be read.

    A flag starts with `-`, `--` or `/`. Any PowerShell flag that is a prefix of
    `-EncodedCommand`, or `-ec`, names an encoded command, in any position.
    cmd also runs a command glued to its switch (`/cgh pr merge`, or
    `/c"gh pr merge & x"`), so that command is split off first.
    """
    if name == "cmd":
        split = []
        for arg in args:
            glued = re.fullmatch(r"(/+[ck])([^/].*)", arg, re.I | re.S)
            split += [glued.group(1), glued.group(2)] if glued else [arg]
        args = split
    else:
        for arg in args:
            flag = arg.lower().lstrip("-/") if arg[:1] in ("-", "/") else ""
            if flag == "ec" or (flag and "encodedcommand".startswith(flag)):
                return None
    i = 0
    while i < len(args) and args[i][:1] in ("-", "/"):
        i += 1
    return " ".join(args[i:]).lower()

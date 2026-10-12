#!/usr/bin/env python3
"""context_snapshot - work-state handoff across a compaction or a spawn.

Five modes. The `context-snapshot` bash fence launches the four hook modes:

- `snapshot` (PreCompact): write `notes/handoffs/auto-state.md` with the git,
  PR and notes state of the project. The model does not write this file, so it
  is current even when the model forgot to write a handoff.
- `print` (SessionStart, matcher `compact`): print a short preface, the
  auto-state file and the newest hand-written handoff. Claude Code adds this
  stdout to the resumed context.
- `startup` (SessionStart, matcher `startup`): fetch `main` from origin, then
  print how many commits the session HEAD is behind `origin/main` and the
  titles of the open PRs. A new session then sees work that already exists.
  The fetch updates the remote-tracking ref that all worktrees share.
- `spawn-hook` (PreToolUse on `mcp__ccd_session__spawn_task`): write a handoff
  under `notes/handoffs/spawn/` and add its path to the spawn prompt.
- `spawn` (run by the orchestrator): write the same handoff before the call
  and print the line to end the prompt with. The hook then skips that prompt.

`notes/` is gitignored and lives in the primary checkout only, so every mode
resolves the primary checkout from `git rev-parse --git-common-dir`. A session
in a linked worktree reads and writes the same files as one in the primary.

The hook modes must never block a compaction, a session start or a spawn.
Every path exits 0, and the bash fence also forces exit 0. The script reads
no file contents except the two handoff files that `print` shows. For the
other files under `notes/`, it reads only names, times and sizes.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

AUTO_STATE = "auto-state.md"
MAX_LINES = 150
NEWEST_NOTES = 10
MAX_WORKTREES = 30
MAX_BRANCHES = 30
MAX_PRS = 10
# Claude Code caps SessionStart stdout at 10,000 characters. Over the cap it
# shows the model only a 2,000-character preview, so stay well under it.
MAX_PRINT_CHARS = 9000
# Total time for all subprocess calls in one run, below each hook's settings
# timeout (60 s for PreCompact, 15 s for SessionStart, 30 s for spawn_task).
BUDGET_S = {"snapshot": 45.0, "print": 10.0, "startup": 10.0, "spawn-hook": 20.0, "spawn": 45.0}
# A fetch took 1 to 5 s on 2026-10-12 on a loaded machine. `gh pr list` runs
# at the same time, so the fetch can use most of the 10 s startup budget.
FETCH_TIMEOUT_S = 8.0
BASE = "main"

_deadline = [0.0]


def _proc(argv, cwd, timeout=10.0, env=None, capture=True):
    """The finished process, or None on a launch error, timeout or spent budget.

    `capture=False` discards the output. After a timeout, a captured run on
    Windows waits for every child that holds the pipe, with no time limit.
    """
    left = _deadline[0] - time.monotonic()
    if left <= 0.5:
        return None
    if capture:
        io = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    else:
        io = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        return subprocess.run(argv, cwd=cwd, timeout=min(timeout, left), env=env, **io)
    except (OSError, subprocess.SubprocessError):
        return None


def run(argv, cwd, timeout=10.0):
    """stdout of `argv`, or None on a launch error, timeout or spent budget.

    A non-zero exit still returns stdout: `gh pr checks` exits 8 while checks
    are pending and prints valid JSON all the same.
    """
    out = _proc(argv, cwd, timeout)
    return out.stdout if out else None


def primary_root(start):
    """The primary checkout that holds `notes/`, or None outside a git tree."""
    common = run(
        ["git", "-C", start, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        start,
    )
    common = (common or "").strip()
    if common and os.path.basename(common.rstrip("/\\")) == ".git":
        return os.path.dirname(common.rstrip("/\\"))
    top = (run(["git", "-C", start, "rev-parse", "--show-toplevel"], start) or "").strip()
    return top or None


def read_payload():
    """The hook payload as a dict; {} when stdin is a terminal or not JSON."""
    if sys.stdin.isatty():
        return {}
    try:
        # Bytes, so json decodes UTF-8: a text-mode stdin is cp1252 on Windows,
        # and a spawn prompt read that way went back to the tool garbled.
        data = json.loads(sys.stdin.buffer.read())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def start_dir(payload):
    """The session cwd from the payload, else the project dir, else cwd."""
    cwd = payload.get("cwd")
    for cand in (cwd, os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()):
        if isinstance(cand, str) and os.path.isdir(cand):
            return cand
    return os.getcwd()


def capped(lines, limit, noun):
    """At most `limit` lines, plus one line that counts the rest.

    Each section has its own cap, so a long section cannot push the later
    sections past MAX_LINES.
    """
    extra = len(lines) - limit
    if extra > 0:
        lines = lines[:limit] + ["- ... %d more %s" % (extra, noun)]
    return lines


def worktree_lines(root):
    out = run(["git", "worktree", "list", "--porcelain"], root)
    if out is None:
        return ["- git worktree list failed"]
    lines = []
    for block in out.strip().split("\n\n"):
        fields = {}
        for row in block.splitlines():
            key, _, value = row.partition(" ")
            fields[key] = value
        if "worktree" not in fields:
            continue
        if "branch" in fields:
            where = fields["branch"].removeprefix("refs/heads/")
        elif "bare" in fields:
            where = "bare"
        else:
            where = "detached"
        lines.append("- %s [%s] %s" % (fields["worktree"], where, fields.get("HEAD", "")[:7]))
    return capped(lines, MAX_WORKTREES, "worktrees")


def branch_lines(root):
    fmt = "%(refname:short)%09%(upstream:short)%09%(upstream:track,nobracket)"
    out = run(["git", "for-each-ref", "--format=" + fmt, "refs/heads"], root)
    if out is None:
        return ["- git for-each-ref failed"]
    lines = []
    for row in out.splitlines():
        name, _, rest = row.partition("\t")
        upstream, _, track = rest.partition("\t")
        if upstream:
            lines.append("- %s -> %s: %s" % (name, upstream, track or "even"))
    return capped(lines, MAX_BRANCHES, "branches") or ["- none"]


def check_summary(gh, root, number):
    out = run([gh, "pr", "checks", str(number), "--json", "bucket"], root)
    if out is None:
        return "checks: unknown (gh timed out)"
    try:
        rows = json.loads(out)
    except ValueError:
        return "checks: none"
    counts = Counter(
        row.get("bucket", "?") if isinstance(row, dict) else "?"
        for row in (rows if isinstance(rows, list) else [])
    )
    if not counts:
        return "checks: none"
    return "checks: " + ", ".join("%s %d" % kv for kv in sorted(counts.items()))


def pr_lines(root, checks=True):
    """One line per open PR. `checks=False` skips the `gh pr checks` call per PR."""
    gh = shutil.which("gh")
    if not gh:
        return ["- skipped: gh not found"]
    out = run(
        [gh, "pr", "list", "--state", "open", "--json", "number,title,headRefName,isDraft"],
        root,
        timeout=15.0,
    )
    try:
        prs = json.loads(out or "")
    except ValueError:
        return ["- skipped: gh pr list failed"]
    if not isinstance(prs, list):
        return ["- skipped: gh pr list failed"]
    lines = []
    for pr in prs[:MAX_PRS]:
        if not isinstance(pr, dict):
            continue
        number = pr.get("number")
        lines.append(
            "- #%s %s [%s]%s%s"
            % (
                number,
                str(pr.get("title", ""))[:80],
                pr.get("headRefName", ""),
                " draft" if pr.get("isDraft") else "",
                " " + check_summary(gh, root, number) if checks else "",
            )
        )
    if len(prs) > MAX_PRS:
        lines.append("- ... %d more PRs" % (len(prs) - MAX_PRS))
    return lines or ["- none"]


def files_by_mtime(directory):
    """Regular, non-hidden files in `directory`, newest first."""
    try:
        entries = [e for e in os.scandir(directory) if e.is_file() and not e.name.startswith(".")]
    except OSError:
        return []
    return sorted(entries, key=lambda e: e.stat().st_mtime, reverse=True)


def shown(path):
    """`path` with forward slashes, for display."""
    return path.replace("\\", "/")


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def notes_lines(root, sub):
    entries = files_by_mtime(os.path.join(root, "notes", sub))
    lines = []
    for e in entries[:NEWEST_NOTES]:
        st = e.stat()
        lines.append("- notes/%s/%s  %s  %d B" % (sub, e.name, stamp(st.st_mtime), st.st_size))
    return lines or ["- none"]


def newest_handoff(root):
    for e in files_by_mtime(os.path.join(root, "notes", "handoffs")):
        if e.name == AUTO_STATE:
            continue
        return e.path
    return None


def build_snapshot(root, trigger):
    """The auto-state file as a list of lines, at most MAX_LINES long."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    handoff = newest_handoff(root)
    lines = [
        "# Auto state snapshot",
        "",
        "Written by .claude/hooks/context_snapshot.py at %s (trigger: %s)." % (now, trigger),
        "Facts at that time. Verify before you act on them.",
        "",
        "## Worktrees",
        *worktree_lines(root),
        "",
        "## Branches with an upstream (local refs, no fetch)",
        *branch_lines(root),
        "",
        "## Open PRs",
        *pr_lines(root),
        "",
        "## Newest notes/dispatch",
        *notes_lines(root, "dispatch"),
        "",
        "## Newest notes/reviews",
        *notes_lines(root, "reviews"),
        "",
        "## Newest hand-written handoff",
        "- " + (shown(os.path.relpath(handoff, root)) if handoff else "none"),
    ]
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + ["- ... truncated at %d lines" % MAX_LINES]
    return lines


def snapshot(root, trigger):
    path = os.path.join(root, "notes", "handoffs", AUTO_STATE)
    lines = build_snapshot(root, trigger)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), ".%s.tmp" % AUTO_STATE)  # dot: never a handoff
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


PREFACE = (
    "Context was compacted. The files below hold the work state from before "
    "the compaction. The auto-state file is generated from git, gh and the "
    "notes directory; the handoff file was written by hand and can be older. "
    "Check both against the repo before you act.\n"
)


def section(path, limit):
    """A `=== path ===` header and the file, cut to `limit` characters in all.

    "" when the file cannot be read. A cut section ends with the file path, so
    the model can read the rest.
    """
    head = "=== %s ===\n" % path
    note = "\n\n[cut here; read the full file at %s]\n" % path
    room = limit - len(head)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(room + 1)
    except OSError:
        return ""
    if len(text) > room:
        return head + text[: max(room - len(note), 0)].rstrip() + note
    return head + text.rstrip() + "\n"


def age_note(path):
    """One line with the age of `path`, so a stale snapshot shows as stale."""
    try:
        minutes = int((time.time() - os.path.getmtime(path)) // 60)
    except OSError:
        return ""
    return "The auto-state file was written %d minutes ago.\n" % minutes


def render_resume(root):
    """The SessionStart text, or "" when there is nothing to show.

    The total stays under MAX_PRINT_CHARS. The auto-state file gets at most
    half of the budget when a handoff exists; the handoff gets the rest.
    """
    auto = shown(os.path.join(root, "notes", "handoffs", AUTO_STATE))
    handoff = newest_handoff(root)
    handoff = shown(handoff) if handoff else None
    preface = PREFACE + age_note(auto)
    parts = []
    budget = MAX_PRINT_CHARS - len(preface) - 2
    if os.path.isfile(auto):
        text = section(auto, budget // 2 if handoff else budget)
        if text:
            parts.append(text)
            budget -= len(text) + 1
    if handoff:
        text = section(handoff, budget)
        if text:
            parts.append(text)
    if not parts:
        return ""
    return preface + "\n" + "\n".join(parts)


def render_startup(start, root):
    """The startup report: commits behind origin/main and open PR titles.

    A failed or slow fetch adds a note; the count then uses the last fetch.
    """
    lines = ["Session start: freshness check. Read it before you start new work."]
    # The PR list does not need the fetch, so the two run at the same time.
    pool = ThreadPoolExecutor(max_workers=1)
    prs = pool.submit(pr_lines, root, False)
    pool.shutdown(wait=False)
    fetch = _proc(
        # An explicit destination: a single-branch clone does not map main.
        ["git", "fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (BASE, BASE)],
        start,
        timeout=FETCH_TIMEOUT_S,
        # A credential prompt waits for a person. Git Credential Manager
        # shows a sign-in window, which GIT_TERMINAL_PROMPT does not stop.
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never"),
        # git-remote-https outlives a killed git and holds a captured pipe.
        capture=False,
    )
    if fetch is None or fetch.returncode != 0:
        lines.append("- git fetch failed or timed out. The count below uses the last fetch.")
    out = (run(["git", "rev-list", "--count", "HEAD..origin/" + BASE], start) or "").strip()
    if not out.isdigit():
        lines.append("- No behind count: git rev-list HEAD..origin/%s failed." % BASE)
    elif out == "0":
        lines.append("- This checkout is up to date with origin/%s." % BASE)
    else:
        lines.append(
            "- This checkout is %s commit%s behind origin/%s. Update it before you build on it."
            % (out, "" if out == "1" else "s", BASE)
        )
    lines.append("- Open PRs. Make sure that none of them already does your task:")
    lines.extend("  " + line for line in prs.result())
    return "\n".join(lines) + "\n"


# Spawn handoffs go in a subdirectory: they belong to the child session, so
# the compaction printer must not take one as this session's newest handoff.
SPAWN_DIR = os.path.join(
    "notes",
    "handoffs",
    "spawn",
)


def slug(title):
    text = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return text[:40].rstrip("-") or "task"


def write_spawn_handoff(root, title, tldr, prompt):
    """Write the handoff for a spawned session and return its path."""
    now = datetime.now(timezone.utc)
    directory = os.path.join(root, SPAWN_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "%s-%s.md" % (now.strftime("%Y%m%dT%H%M%SZ"), slug(title)))
    lines = [
        "# Spawn handoff: %s" % title,
        "",
        "## Task",
        tldr or "-",
        "",
        "## Prompt",
        prompt.rstrip(),
        "",
        "## Work state at spawn time",
        # Drop the snapshot's own title; its "Written by" line stays.
        *build_snapshot(root, "spawn")[2:],
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def handoff_line(path):
    return "Handoff: read %s first." % shown(path)


def spawn_hook(root, payload):
    """PreToolUse output that adds a handoff to a spawn_task prompt, or None.

    None leaves the call unchanged: a malformed input, or a prompt that
    already names a spawn handoff (the orchestrator wrote one itself).
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    title, prompt = tool_input.get("title"), tool_input.get("prompt")
    if not isinstance(title, str) or not isinstance(prompt, str):
        return None
    if shown(SPAWN_DIR) + "/" in shown(prompt):
        return None
    tldr = tool_input.get("tldr")
    path = write_spawn_handoff(root, title, tldr if isinstance(tldr, str) else "", prompt)
    updated = dict(tool_input, prompt=prompt.rstrip() + "\n\n" + handoff_line(path))
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            # spawn_task only makes a chip; the user still starts the session.
            "permissionDecision": "allow",
            "permissionDecisionReason": "spawn handoff written to " + shown(path),
            "updatedInput": updated,
        }
    }


def spawn_cli(argv):
    """`spawn` mode: the orchestrator writes the handoff before spawn_task.

    Unlike the hook modes, this fails loudly: the caller needs the path.
    """
    parser = argparse.ArgumentParser(prog="context_snapshot.py spawn")
    parser.add_argument("--title", required=True)
    parser.add_argument("--tldr", default="")
    parser.add_argument("--prompt-file", required=True, help="a path, or - for stdin")
    args = parser.parse_args(argv)
    if args.prompt_file == "-":
        sys.stdin.reconfigure(encoding="utf-8")
        prompt = sys.stdin.read()
    else:
        with open(args.prompt_file, encoding="utf-8") as fh:
            prompt = fh.read()
    root = primary_root(os.getcwd())
    if not root:
        sys.stderr.write("context_snapshot.py spawn: not in a git checkout\n")
        return 1
    path = write_spawn_handoff(root, args.title, args.tldr, prompt)
    sys.stdout.write(handoff_line(path) + "\n")
    return 0


def main(argv):
    mode = argv[0] if argv else "snapshot"
    _deadline[0] = time.monotonic() + BUDGET_S.get(mode, 10.0)
    if mode == "spawn":
        return spawn_cli(argv[1:])
    try:
        payload = read_payload()
        start = start_dir(payload)
        root = primary_root(start)
        if not root:
            return 0
        if mode == "snapshot":
            trigger = payload.get("trigger")
            snapshot(root, trigger if isinstance(trigger, str) else "manual run")
        elif mode in ("print", "startup"):
            text = render_resume(root) if mode == "print" else render_startup(start, root)
            # On Windows a piped sys.stdout is cp1252, and one character
            # outside it (an arrow, a check mark) raised and lost the output.
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stdout.write(text)
        elif mode == "spawn-hook":
            out = spawn_hook(root, payload)
            if out:
                sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001 - a snapshot failure must not block compaction
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

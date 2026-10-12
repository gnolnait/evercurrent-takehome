#!/usr/bin/env python3
"""context_snapshot - work-state handoff across a context compaction.

Two modes, launched by the `context-snapshot` bash fence:

- `snapshot` (PreCompact): write `notes/handoffs/auto-state.md` with the git,
  PR and notes state of the project. The model does not write this file, so it
  is current even when the model forgot to write a handoff.
- `print` (SessionStart, matcher `compact`): print a short preface, the
  auto-state file and the newest hand-written handoff. Claude Code adds this
  stdout to the resumed context.

`notes/` is gitignored and lives in the primary checkout only, so both modes
resolve the primary checkout from `git rev-parse --git-common-dir`. A session
in a linked worktree reads and writes the same files as one in the primary.

The hook must never block a compaction or a session start. Every path exits 0,
and the bash fence also forces exit 0. The script reads no file contents except
the two handoff files that `print` shows. It lists only the names, times and
sizes of files under `notes/dispatch/` and `notes/reviews/`.
"""
import json
import os
import shutil
import subprocess
import sys
import time
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
# Total time for all subprocess calls in `snapshot`. The settings timeout is 60.
BUDGET_S = 45.0

_deadline = [0.0]


def run(argv, cwd, timeout=10.0):
    """stdout of `argv`, or None on a launch error, timeout or spent budget.

    A non-zero exit still returns stdout: `gh pr checks` exits 8 while checks
    are pending and prints valid JSON all the same.
    """
    left = _deadline[0] - time.monotonic()
    if left <= 0.5:
        return None
    try:
        out = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(timeout, left),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


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
        data = json.load(sys.stdin)
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
    try:
        rows = json.loads(out or "")
    except ValueError:
        return "checks: none"
    counts = {}
    for row in rows if isinstance(rows, list) else []:
        bucket = row.get("bucket", "?") if isinstance(row, dict) else "?"
        counts[bucket] = counts.get(bucket, 0) + 1
    if not counts:
        return "checks: none"
    return "checks: " + ", ".join("%s %d" % kv for kv in sorted(counts.items()))


def pr_lines(root):
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
            "- #%s %s [%s]%s %s"
            % (
                number,
                str(pr.get("title", ""))[:80],
                pr.get("headRefName", ""),
                " draft" if pr.get("isDraft") else "",
                check_summary(gh, root, number),
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
        if e.name != AUTO_STATE:
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
    tmp = path + ".tmp"
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


def render_resume(root):
    """The SessionStart text, or "" when there is nothing to show.

    The total stays under MAX_PRINT_CHARS. The auto-state file gets at most
    half of the budget when a handoff exists; the handoff gets the rest.
    """
    auto = shown(os.path.join(root, "notes", "handoffs", AUTO_STATE))
    handoff = newest_handoff(root)
    handoff = shown(handoff) if handoff else None
    parts = []
    budget = MAX_PRINT_CHARS - len(PREFACE) - 2
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
    return PREFACE + "\n" + "\n".join(parts)


def main(argv):
    _deadline[0] = time.monotonic() + BUDGET_S
    mode = argv[0] if argv else "snapshot"
    try:
        payload = read_payload()
        root = primary_root(start_dir(payload))
        if not root:
            return 0
        if mode == "snapshot":
            trigger = payload.get("trigger")
            snapshot(root, trigger if isinstance(trigger, str) else "manual run")
        elif mode == "print":
            sys.stdout.write(render_resume(root))
    except Exception:  # noqa: BLE001 - a snapshot failure must not block compaction
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Shared PreToolUse payload reader for the guard entry points.

Every entry point — the production `guard_runner` and the standalone shims —
used to carry its own copy of this parse-and-shape-check preamble. The copies
drifted, and the drift WAS the bug class:

- A shim that accepted what the runner rejected made its adversarial suite
  validate behavior production does not have (a non-object payload
  crashed the shims into their rc fence while the runner failed closed in
  python).
- Reading a field through an `or` default hid a wrong type instead of rejecting
  it. A FALSY non-string `tool_name` (`[]` / `0` / `false`) collapsed to `""`,
  every entry point read it as "not our tool", and the guard ALLOWED — never
  inspecting the command. This reached production `guard_runner`.

Each of those was fixed one layer at a time across three copies. Reading the
payload once, here, makes the divergence unrepresentable and makes the next
guard layer one edit instead of three.

Every rejection is a protocol violation, not a judgement call: a well-formed
PreToolUse payload is a JSON object whose `tool_name` is a string and whose
`tool_input`, when present, is an object. So every path here fails CLOSED —
exit 2, the blocking verdict — with a message naming the guard that refused.
Callers pass that name as `subject` ("guard_runner", "the no-merge-pr guard").
"""
import json
import sys


def _block(subject, problem):
    sys.stderr.write("BLOCKED: %s %s; failing closed.\n" % (subject, problem))
    sys.exit(2)


def _unusable_tool_name(tool):
    """True when `tool_name` cannot serve as the dispatch key.

    Blank counts as unusable, NOT as "some other tool". `""` and `"   "` are
    `str`, so an isinstance-only check passed them through, and every entry point
    then compared them against "Bash", missed, and exited 0 — the same silent
    ALLOW a falsy non-string produced through the old `or ""` default, for the
    same reason. An empty dispatch key is no more usable than a missing one.
    """
    if not isinstance(tool, str):
        return True
    return not tool.strip()


def _parse(subject, stream):
    # A well-formed payload is always valid JSON, so a parse failure means the
    # guard cannot see what it is guarding. Split out so the caller's binding
    # comes from a return value and is never possibly-unbound.
    try:
        return json.load(stream)
    except Exception:
        _block(subject, "could not parse the hook payload")


def read_payload(subject, stream=None):
    """Read and shape-check the payload on stdin.

    Returns `(data, tool_name, tool_input)` — `tool_input` normalized to a dict.
    Exits 2 on any protocol violation.
    """
    data = _parse(subject, sys.stdin if stream is None else stream)

    # A top-level array or scalar is valid JSON but not a payload. Without this
    # the `.get()` below raises AttributeError and python exits 1, which only
    # the bash rc fence turns into a block — with a raw traceback and a message
    # that blames the interpreter rather than naming the violation.
    if not isinstance(data, dict):
        _block(subject, "received a non-object hook payload")

    # `tool_name` must be a usable dispatch key. settings.json fires these hooks
    # only for the Bash, PowerShell and mcp merge matchers, so the invocation is
    # always a guarded one — if the guard cannot read the key it cannot tell what it is
    # guarding, which is the same protocol violation as an unparseable payload
    # and gets the same verdict. See _unusable_tool_name for why blank counts.
    tool = data.get("tool_name")
    if _unusable_tool_name(tool):
        _block(subject, "received a missing, blank, or non-string tool_name")

    # `tool_input` carries the ARGUMENTS, not the identity: absent means "no
    # arguments", which has a well-defined empty reading, so it normalizes to
    # {}. Present-but-wrongly-typed is still a violation.
    tool_input = data.get("tool_input")
    if tool_input is not None and not isinstance(tool_input, dict):
        _block(subject, "received a non-dict tool_input")

    return data, tool, tool_input or {}


def read_command(subject, tool_input):
    """Return `tool_input["command"]` as a string, `""` when absent.

    Exits 2 when present and not a string — a truthy non-string reached
    `.strip()` / `.startswith()` and crashed into the rc fence. Kept separate
    from `read_payload` because the entry points read `command` at different
    points: the merge guard only needs it on the shell-tool path, since its
    `evaluate()` returns before reading it for an mcp tool.
    """
    cmd = tool_input.get("command")
    if cmd is not None and not isinstance(cmd, str):
        _block(subject, "received a non-string tool_input.command")
    return cmd or ""

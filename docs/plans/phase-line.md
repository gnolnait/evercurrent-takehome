# Plan: the digest names the project phase and the next gate

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run.

## Goal

The brief asks for a digest that follows "project phases progression". The project
configuration holds phase windows and gates, and `ProjectConfig.phase_on` exists, but no
production code calls it. The digest never tells the reader which phase the project is in
or how close the next gate is.

## Why a line and not a ranking term (spike, 2026-10-13)

`notes/research/spikes/phase_focus_spike.py` multiplied the role prior by a DVT table per
item type (spec and BOM changes 1.5, blockers and schedule changes 1.3, risks 1.2,
decisions 0.8, fyi 0.7) on days 9 and 10, the first DVT days. No digest of any of the six
readers changed, not even in order. Sections group lines by item type, so every line in a
section gets the same multiplier, and no score crossed the threshold.

A second spike tested a rule that makes open changes carried into a new phase must-see for
their owners: six of the seven open change items on day 9 have no owner, and the seventh
belongs to a reader outside the demo. The rule would fire for nobody.

Both stay in `docs/later.md`. This unit ships the part that is true and visible: the
digest states the phase and the next gate.

## Rule

- `phase_line(world, day) -> str | None` in `daily/render.py`:
  - phase: `world.project.phase_on(day)`, and the `from_date` of that phase window;
  - next gate: the gate with the earliest `date` at or after `day`, ties broken by gate id;
  - text: `Phase: <phase> since <from_date>. Next gate: <gate id> <gate name>, <when>.`,
    where `<when>` is `today`, `in 1 working day` or `in <n> working days`, counted with
    `add_working_days`;
  - with no gate at or after `day`, the text ends after the phase: `Phase: <phase> since
    <from_date>.`;
  - before the first phase window, it returns None and no line is shown.
- Terminal: `render_digest` prints the phase line as line 3, under the window line.
- Slack: the window block holds the window line and the phase line, joined by a newline,
  each escaped. The block count does not change, so the 50-block arithmetic of W9 stays.
  The `text` fallback is the header, the window line and the phase line, one per line.

## Doc changes (first commit)

- SPEC section 5 step 7: the header names the window and the phase line.
- `docs/later.md`: the phase multiplier per item type and the carried-in rule, each with
  the spike result and a trigger (a world where changes land after a phase start, and items
  with owners).

## Behaviours

- PL1. Day 8 (EVT) and day 9 (DVT) of the bundled world give the exact texts:
  `Phase: EVT since 2026-08-03. Next gate: G-DVT1 DVT-1 go/no-go, in 1 working day.` and
  `Phase: DVT since 2026-10-22. Next gate: G-DVT1 DVT-1 go/no-go, today.`
- PL2. `<when>` counts working days across a weekend; one and many use the right noun.
- PL3. No gate at or after the day: the text ends after the phase.
- PL4. A day before the first phase window: None, and neither renderer shows a line.
- PL5. Terminal line 3 equals the phase line; Slack's window block text equals terminal
  lines 2 and 3 joined by a newline (parity, both escaped).
- PL6. The Slack `text` fallback holds the three lines; the 50-block tests do not change.

## Acceptance

- Tests in `tests/test_daily_render.py` and `tests/test_slack_post.py` cover PL1 to PL6;
  each new or changed test is RED-GREEN proven.
- `uv run digest demo --day 8` and `--day 9` show the two texts of PL1.
- The extraction eval report does not change.

## Not in scope

Phase re-weighting of ranking, per-subsystem phases (`subsystem_phases`), and checklist
changes at a phase boundary.

# Plan: the digest names the project phase and the next gate

Status: v0.2, 2026-10-13. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run. v0.2 folds in the advisor review and an independent Codex plan review.

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

- `phase_line(world, day) -> str | None` in `daily/render.py` returns plain text:
  - phase window: the latest window in `world.project.phases` whose `from_date` is at or
    before `day`; the line takes both the phase and the `from_date` from that window, so a
    repeated phase label uses its latest start;
  - next gate: among gates with `date` at or after `day`, the earliest `date`; ties go to
    the lowest gate id in string order, whatever the order in the config;
  - text: `Phase: <phase> since <from_date>. Next gate: <gate id> <gate name>, <when>.`,
    where `<when>` is `today` when the gate date is the day, else `in 1 working day` or
    `in <n> working days`, with `n` the smallest count for which `add_working_days(day, n)`
    is at or after the gate date;
  - whitespace in the gate name (including a line break) collapses to one space;
  - with no gate at or after `day`, the text ends after the phase: `Phase: <phase> since
    <from_date>.`;
  - before the first phase window, it returns None and neither renderer shows a line.
- Terminal: `render_digest` prints the phase line as line 3, under the window line, as
  plain text.
- Slack: the window block holds the window line and the phase line, each escaped once,
  joined by a newline. The block count does not change, so the 50-block arithmetic of W9
  stays. The `text` fallback is the escaped header, window line and phase line, one per
  line.
- Known limit: gate dates come from the project configuration, not from the corpus. The
  corpus moves the PN-51010 T1 gate to 2026-10-21; the configured G-TR1 date stays
  2026-10-20 until the ledger reads schedule claims. `docs/later.md` records it.

## Existing tests that change

The window block text and the fallback gain the phase line, so these assertions change
and each gets its own RED proof: `tests/test_daily_render.py` near lines 166 and 257;
`tests/test_slack_post.py` near lines 133, 140, 175, 206, 279 and 779 (the CLI post
fallback texts). In the block-limit cases, block counts, kept-line counts and omission
counts do not change. `docs/plans/m3-demo.md` B6.4 (the window block) changes with them.

## Doc changes (first commit)

- SPEC section 5 step 7: the header names the window and the phase line.
- `docs/later.md`: the phase multiplier per item type and the carried-in rule, each with
  the spike result and a trigger (a world where changes land after a phase start, and items
  with owners); and the known limit on configured gate dates.
- `docs/plans/m3-demo.md` B6.4: the window block holds the window line and the phase line.

## Behaviours

Feature checks:
- PL1. Day 8 (EVT) and day 9 (DVT) of the bundled world give the exact texts:
  `Phase: EVT since 2026-08-03. Next gate: G-DVT1 DVT-1 go/no-go, in 1 working day.` and
  `Phase: DVT since 2026-10-22. Next gate: G-DVT1 DVT-1 go/no-go, today.`
- PL2. `<when>` counts working days across a weekend (a Friday digest and a Monday gate:
  `in 1 working day`); one and many use the right noun.
- PL3. No gate at or after the day: the text ends after the phase.
- PL4. Gate choice: an unsorted fixture with two gates on the earliest date gives the lower
  id, and the same result with the fixture order reversed. A repeated phase label takes the
  latest window's start.
- PL5. Terminal line 3 equals the plain phase line. A gate name with `&`, `<`, `>` and a
  line break: the terminal shows it plain with the break collapsed; Slack shows it escaped
  once, in the block and in the fallback.
- PL6. Slack: the window block text is the escaped window line and the escaped phase line
  joined by a newline; the `text` fallback holds the three escaped lines.

Preservation checks (they pass today; the RED proof is a mutation after the helper exists):
- PL7. A day before the first phase window: `phase_line` returns None and neither renderer
  shows a line; a positive case beside it shows the line.
- PL8. The 50-block cases keep their block, kept-line and omission counts.

## Acceptance

- Tests in `tests/test_daily_render.py` and `tests/test_slack_post.py` cover PL1 to PL8 and
  the changed assertions; each new or changed test is RED-GREEN proven.
- `uv run digest demo --day 8` and `--day 9` show the two texts of PL1.
- The extraction eval report does not change.

## Not in scope

Phase re-weighting of ranking, per-subsystem phases (`subsystem_phases`), and checklist
changes at a phase boundary.

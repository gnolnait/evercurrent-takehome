---
name: testing
description: |
  How to pick a test kind + the RED-GREEN load-bearing-test principle, plus
  the run and coverage commands. Loads when editing any test file or conftest.
paths:
  - "tests/**/*.py"
  - "**/conftest.py"
---
# Testing — core

Before writing a test, scan this map and `tests/conftest.py`; the right fixture or
pattern usually already exists.


## Which kind of test?

Pick by source-of-truth × input-shape:

| Kind                                    | When                                                         |
|-----------------------------------------|--------------------------------------------------------------|
| **Example / unit**                      | a few hand-verifiable + boundary cases; documents intent     |
| **Golden**                              | large, structured, deterministic output you can't hand-write |
| **Differential**                        | two impls must agree (old↔new, fast↔slow)                    |
| **Property / metamorphic** (Hypothesis) | a clean invariant + a broad input space                      |
| **Oracle** (Hypothesis)                 | a perf-optimized impl vs an independent slow reference       |

They compose — each misses what the others catch: differential misses bugs in _shared_
logic; a golden file proves "unchanged," not "correct" (a wrong-but-blessed value
sticks); property/oracle needs a real invariant. Two traps: **don't golden small
output** (a 1–3-field object) — inline an explicit `assert == {...}` (documents intent,
can't be regenerated blind); and **"two paths" isn't always equivalence** — if paths
intentionally diverge, test each against its own oracle/invariant, not A==B.


## RED-GREEN-proven — the load-bearing rule

- Signature: new/modified test may pass even when the asserted behavior is broken.
- Fix: write the test; break the source under test; confirm failure for the asserted
  reason; restore source; confirm pass.
- Why: a test that passes broken and unbroken is not protecting the behavior.
- Example: blank the line that invokes the function under test; inner conditionals often
  leave assertions valid through another branch.
- Trap signatures: regex matches runner summary (`\d+ failed`), redundant
  keep/reject branches leave only one blankable line, integration test proves bug class
  but not which branch.

Record the proof: `prove-test` runs the RED-GREEN cycle for a test
and `red-prove` checks that each declared break of the source makes
a test fail. Both write `.test-proof/<sha>.json`.


## Test structure

**Tests live in `tests/`**, mirroring the `src/digest/` module they cover. A test that
needs no special harness is a regular test — don't reach for a heavyweight
fixture/marker out of imitation.

**Arrange / act / assert.** Structure non-trivial tests in three beats — a one-line
Setup → Action → Assert docstring makes intent reviewable — and name the test for the
behavior asserted (`test_<behavior>_<condition>`). Judgment, not a mandate: skip the
docstring on trivial tests.

**Rows, not clones.** The dominant growth mode of a test suite is _scenario accretion_:
each regression appended as a fully-constructed standalone test instead of a row in an
existing matrix. Counter-defaults:

- At the **third** test sharing an arrange-act-assert skeleton, collapse to
  `pytest.mark.parametrize`; later cases become rows unless their control flow genuinely
  differs. Before writing a new test in a file, check whether an existing matrix already
  fits it.
- **One scenario driver per stateful subsystem.** Lifecycle/state-machine suites get a
  single harness — events/timeline in, observable outcomes out — instead of each test
  re-choreographing connections, timers, mocks, and private fields. Don't initialize
  another test's private state by hand outside the driver.
- **Assertion boundary.** Exact full-object dumps only where serialization IS the
  contract; otherwise assert the behavioral projection you care about. Over-specified
  assertions are simultaneously longer and more brittle.
- A test file crossing **~1000 lines** is a consolidation-review trigger: before adding
  another standalone scenario, fold the file's repeats into matrices/drivers (or split
  it by concern). Large test files grow one reasonable test at a time.


## Cover the null case

`None` values are real values, not sentinels — code must work with them present, or
document and justify their absence. A test is what keeps null handling fixed. So when a
change touches an optional field, a join/filter key, or a transform that can emit
`None`/NaN, add a case that exercises it — happy-path rows alone won't catch a silent
drop, zero-fill, or `"None"`/`"NaN"` string.

- **What to add:** one input with `None` in the field under test (and, for floats, a
  NaN — they are distinct), then assert the documented behavior: the item survives / is
  bucketed / renders blank, NOT dropped, zeroed, or stringified.
- A share/ratio → assert a 0-or-`None` denominator doesn't poison the result; a "pick
  latest" → assert the pick is pinned by an explicit sort, not order-dependent.
- A source sentinel (`""`, `"N/A"`, `-1`) → assert ingest maps it to a real `None` and
  stores `None`, not the sentinel.

If a transform genuinely can't see a `None`, say so in a comment with the reason — a
documented, justified absence is allowed, and that comment is the alternative to the
test.


## Measuring coverage

- Signature: a changed-line coverage claim is needed.
- Fix: `uv run pytest --cov=digest --cov-report=term-missing`.
- Trap signatures: coverage claims without a report from a real run.


## Run what you changed, then push

**Before pushing, run the tests you wrote or changed, and run the formatters.** Push
early and often.

- Run the specific file(s) you touched: `uv run pytest tests/path/to/test_module.py`.
- Run the formatters and linter: `uv run ruff format` and `uv run ruff check`. Don't
  bypass pre-commit with `--no-verify`.
- RED-GREEN (above) still applies to every new or modified test: prove it fails for the
  right reason before it's allowed to pass.


## Yolo mode — only on explicit request

A user can ask for a PR to be developed "yolo-style." Only act on this when the user
says so explicitly, in those or clearly equivalent words — never infer it from urgency,
a small diff, or your own read of the risk.

When they do, trade test rigor for speed:

- Run only minimal tests before merge — enough to confirm the change runs, not the full
  suite for what you touched.
- Skip RED-GREEN proof on new or changed tests (above).

**Say so in the PR body.** Add a line to the Test Plan or Appendix naming the request
and the tradeoff — e.g. "yolo-style per request: minimal testing, no RED-GREEN proof" —
so a reviewer sees the speed/rigor tradeoff was asked for, not skipped silently.

Formatters and CI still run either way — yolo mode waives test rigor, not the rest of
the workflow.


## Skip / don't reach for

- Signature: generic mocks or reimplemented test utilities.
- Fix: prefer the existing fixture or library for the problem class — `respx` for httpx
  calls, `time-machine` for clocks, pytest's `caplog` / `tmp_path` / `monkeypatch`.
- Why: these already encode the integration behavior a hand-rolled mock gets wrong.
- Trap signatures: manual log capture, hand-rolled golden comparison, `MagicMock`
  standing in for a production instance.

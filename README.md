# Downstream: a role- and phase-aware daily digest for hardware teams on Slack

Thesis: a digest is the diff between what the project knows and what you were last told,
plus what should have happened but didn't.

## Status

The demo slice is built. `digest demo` prints the digests of three readers (ME, supply
chain, EM) for one day of the synthetic program Kestrel K2, offline, from recorded model
calls. A digest is a visibility filter, a must-see lane, a ranked lane, the absence lines
("not happened") and a plain-text renderer. Every quote is checked against its cited
message in code, and code builds every link.

The claims ledger and the stale-belief lines ("you were told Rev B; it is Rev C") are cut.
SPEC section 9 set a rule before the build: if claim keying precision is below 0.8, cut
the ledger to the ranker plus absences. The eval has no separate keying metric. Its claim
precision is stricter: it also needs an equal value on rev, gate date, qty and owner, and
it counts any claim the golden labels do not list as wrong, even a true one. That proxy measures
0.537 (n = 41), so the ledger is not in this slice. The design stays in `SPEC.md` and
`docs/decisions.md` (D19, D21).

Measured on the recorded corpus (`reports/eval-2026-10-23.md`):

| Metric | Value | n | Target |
| --- | --- | --- | --- |
| Citation validity | 1.0 | 73 | 1.0 (gate) |
| Triage recall | 1.0 | 30 | |
| Claim precision | 0.537 | 41 | 0.8 to keep the ledger |
| Claim recall | 0.629 | 35 | |
| Detected absences that are false | 0.304 | 23 | |
| Absence recall | 0.516 | 31 | |

About 30% of the detected absences are false, counted once per day, source and task
before the per-reader filter. The report labels this share the false-alarm
rate, but SPEC A6 defines that rate over simulated silences that were not stalls, and the
eval does not compute it yet. Silence is counted from task links that the extractor
writes, so a missed link makes an active task look silent.

Designed, not built: the claims ledger and linking, stale beliefs, the per-reader digest
window, phase re-weighting, feedback, live Slack delivery, the web preview and the MCP
server. `docs/later.md` holds the ordered list of next steps.

## Run

```bash
uv sync --all-extras --dev
uv run digest demo
uv run digest --help
uv run pytest
```

Everything runs offline by default (`DIGEST_MODE=replay`); no API keys are needed to
run the demo, the tests or CI.

## Read next

`SPEC.md` is the contract, `docs/decisions.md` holds the architecture decisions,
`docs/process.md` says how the work is built, and `ANSWERS.md` answers the five
technical questions.

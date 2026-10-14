# Plan: an ask absence reaches only the asker and the person asked

Status: v0.1, 2026-10-13. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run.

## Goal

SPEC section 5 step 5(b) says an unanswered ask "is an absence for the asker and the
owner". Today every other reader can get it too: `select` sends an ask absence that is not
must-see for the reader to the ranked lane, in `not_happened`, whatever its age. On day 6
the ME and supply chain digests both show "Grace Okonkwo asked Ana Folau, no reply for 3
working days", which concerns neither reader.

## The contradiction this fixes

- SPEC step 5(b): the audience is the asker and the owner (the person asked).
- SPEC step 6: a ranked-lane candidate includes, whatever its age, an "unanswered ask (in
  v1, the ask absence of step 5 once the reply cadence passes; an ask made of the reader is
  must-see)".
- `docs/plans/m3-demo.md` B5.5: the ask absence goes to `needs_you` for the person asked,
  "else to `not_happened`".
- `select` follows step 6 and B5.5. For the asker and the person asked, the must-see rules
  already catch the absence, so the step 6 clause only ever adds it for other readers.

## Rule

- An ask absence reaches only its asker and the person asked, through the must-see lane
  (unchanged rules: "an ask absence, you asked" and "an ask absence, you were asked").
- For any other reader it is left out before the ranked lane, the same way a checklist
  absence that is not must-see is left out. It does not count in `hidden`, which counts
  only the visibility filter.
- The ask item that the absence cites is unchanged. For another reader it follows its own
  rules: `fyi`, a ranked candidate only when its newest evidence falls in the window. The
  "one ask never shows twice" rule only applies when the absence line is shown.

## Doc changes (first commit)

- SPEC step 6: the ranked-lane candidate list drops the unanswered-ask clause, and says an
  ask absence never enters the ranked lane (step 5(b) names its audience).
- `docs/plans/m3-demo.md` B5.5: an ask absence goes to `needs_you` for the person asked and
  to `not_happened` for the asker; no other reader gets it.
- `select` docstring: the sentence on checklist absences covers ask absences too.

## Behaviours

- AA1. A reader who is neither the asker nor the person asked does not get the ask absence,
  at any age, in any section (paired test: the same absence reaches the asker).
- AA2. The asker gets it as must-see in `not_happened`; the person asked gets it as
  must-see in `needs_you` (preservation).
- AA3. The left-out absence does not change `hidden`.
- AA4. For another reader, the cited ask item still follows its own rules: shown in `fyi`
  when its newest evidence is in the window, absent when it is not.
- AA5. Checklist absences are unchanged (preservation).

## Acceptance

- `tests/test_select.py` covers AA1 to AA5; each new or changed test is RED-GREEN proven, and
  AA1 fails on today's code.
- `uv run digest demo`: the PR reports, per reader, total lines on days 5, 6 and 7 before
  and after, and names each removed line. Must-see lines do not change.
- The extraction eval report does not change.

## Not in scope

Absence sources (c) to (h), the `it happened` action, and the line wording of D27.

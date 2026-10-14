# Plan: an absence reaches only the readers its source names

Status: v0.2, 2026-10-13. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run. v0.2 folds in the advisor review and an independent Codex plan review.

## Goal

SPEC section 5 step 5(b) says an unanswered ask "is an absence for the asker and the
owner". Today every other reader can get it too: `select` sends an ask absence that is not
must-see for the reader to the ranked lane, in `not_happened`, whatever its age. On day 6
the ME and supply chain digests both show "Grace Okonkwo asked Ana Folau, no reply for 3
working days", which concerns neither reader.

## The contradiction this fixes

- SPEC step 5 names an audience for each absence source: (a) checklist, the task's role and
  the EM, per `m3-demo.md` B5.5; (b) ask, the asker and the owner (the person asked).
- SPEC step 6 lists, as ranked-lane candidates whatever their age, an "unanswered ask (in
  v1, the ask absence of step 5 ...)" and "absence".
- `docs/plans/m3-demo.md` B5.5 and `docs/plans/digest-window.md` (Rule, W3) send an ask
  absence to `not_happened` for other readers and keep old ones as candidates.
- `select` follows step 6. For the asker and the person asked, the must-see rules already
  catch the absence, so the step 6 clauses only ever add it for other readers.

## Rule

- General: an absence reaches only the readers its source names in step 5, through the
  must-see lane. No absence enters the ranked lane. The checklist rule already works this
  way; this unit applies it to ask absences.
- Ask absence: must-see for its asker ("an ask absence, you asked", section
  `not_happened`) and for the person asked ("an ask absence, you were asked", section
  `needs_you`), as today. For any other reader it is left out, whatever the reader's role,
  `affected_roles`, owned entities or EM status.
- Order: the visibility check runs first, as today. An absence the reader cannot see counts
  in `hidden`, as today, whoever it names. A visible absence left out by its audience does
  not count in `hidden`. An absence addressed to the reader that the reader cannot see
  stays hidden.
- The cited ask item: for another reader, a shown ask absence no longer replaces the ask's
  line, so the ask item follows its own rules (section `fyi`, a ranked candidate when its
  newest evidence falls in the window, subject to visibility, the threshold and the cap).
  Lines can therefore appear as well as disappear.

## Doc and prose changes (first commit)

- SPEC step 5(b): the asker and the owner are the only audience. Step 6: the must-see list
  names the ask-absence rules; the ranked-lane candidate list drops "unanswered ask" and
  "absence" and says no absence enters the ranked lane.
- `docs/plans/m3-demo.md` B5.5: an ask absence goes to `needs_you` for the person asked and
  to `not_happened` for the asker; no other reader gets it.
- `docs/plans/digest-window.md`: the Rule bullet and W3 say old open blockers stay
  candidates and ask absences reach only their audience (amended 2026-10-13, this plan).
- README line 34 example and the Scope paragraph (line 123); `select.py` module docstring,
  the `_section_of` docstring and the `aged` line.

## Existing tests that pin the old behaviour

The test writer changes these, and each changed test gets its own RED proof:
`tests/test_select.py` near lines 462 (third-party ask absences allowed), 543 (ranked
absences scored), 713 (an absence replaces a third party's ask) and 955 (W3: old ask
absences stay candidates); `tests/test_room.py` near line 412 (the role-change diff at 485
and 492 includes the third-party absence).

## Behaviours

- AA1. A third reader does not get a visible ask absence. The fixture gives the absence
  visible evidence, a score above the threshold and free cap space, with evidence both in
  and before the window; third readers include the EM, a reader in `affected_roles` and an
  owner of one of its entities. The same absence reaches the asker (paired). Fails on
  today's code.
- AA2. The asker gets it as must-see in `not_happened`; the person asked as must-see in
  `needs_you` (preservation).
- AA3. `hidden`: a visible absence left out by audience adds 0; an absence the reader
  cannot see adds 1; an absence addressed to the reader that the reader cannot see adds 1
  and is not shown.
- AA4. A third reader with the ask and its linked absence, both above the threshold, both
  visible, cap space free, and the ask's newest evidence in the window: today the ask line
  is replaced; after the change the ask shows in `fyi` and the absence does not. Paired
  with the ask's evidence outside the window: neither shows.
- AA5. Checklist absences are unchanged (preservation).

## Acceptance

- `tests/test_select.py` and `tests/test_room.py` cover AA1 to AA5 and the changed tests;
  each new or changed test is RED-GREEN proven.
- `uv run digest demo` on days 5, 6 and 7: the PR reports, per reader, the item ids added and
  removed with their section and lane, the total lines (selection entries) before and
  after, and the item overlap between readers. Must-see lines do not change.
- The extraction eval report does not change.

## Not in scope

Absence sources (c) to (h), the `it happened` action, and the line wording of D27.

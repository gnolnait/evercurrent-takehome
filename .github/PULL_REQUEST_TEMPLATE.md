```yaml release
category: feature | bugfix | hotfix | internal | revert
risk: low | medium | high — <what can break>
rollback: clean | migration | persisted-state — <what a revert leaves behind>
issue: #N | none
```

# Summary

<!-- One sentence, present tense, no "This PR". At most two more sentences, no bullets.
     Put Closes #N here, not in the fence. Put **Caution:** first on a line a reviewer must not miss. No emoji.
     Delete every template comment. -->

# Test Plan

<!-- Only evidence that CI does not produce: real data, a browser check, a measurement with its source.
     A before/after pair from the running app is the best evidence. If nothing goes beyond CI, write green CI.
     Name each gap as: not validated: <what> — <why acceptable>. No checklist. -->

- <what you ran beyond CI, and the result>

# Appendix

<!-- Optional; most PRs delete this heading. The rejected alternative, the blast radius, a deferred follow-up.
     One sentence per bullet, at most three. Each bullet must make sense without the rest of the PR. -->

---
name: anti-sprawl
description: |
  Compactness defaults for agent-written code — the recurring LLM-generation
  smells: single-use abstractions, pass-through layers, speculative defensive
  branches, and comment noise. Judgment rules, not gates; have a reason when you
  break one. Loads when editing any source or test file.
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---
# Anti-sprawl — write the compact version first

Generated code trends toward _more_: more layers, more guards, more comments, more
generality than any caller needs. Each addition looks defensible alone; the codebase
pays for the sum. Default to the compact form and let real demand — a second production
caller, a reachable failure, a genuine invariant — justify growth.


## Rule of N: abstractions must earn their surface

Inline until there is real multiplicity. Concretely:

- **No base class, protocol, or interface with one implementation.** Type the one
  concrete thing; extract when implementation #2 exists (test fakes don't count — a seam
  needed only by tests should be the narrowest possible boundary, not a mirrored
  production interface).
- **No helper / util with one consumer** exported from a shared location. Colocate it
  with its consumer; move it out at consumer #2–3. (This is the flip side of
  `code-placement.md`'s "create the module named for the capability" — that rule is for
  an independent domain abstraction with its own responsibility; a private helper of one
  caller stays with that caller.)
- **No parameter that every production caller passes identically.** A knob only tests
  vary is not configurability, it's a test seam wearing a costume — hardcode the value
  and give tests their seam another way.


## No pass-through layers

A function, method, endpoint, or module whose body only delegates — same arguments
through, result straight back, no translation of a documented boundary concern — is a
layer of indirection with no job. Call the real thing.

- Boundary translation is the legitimate exception (e.g. `ValueError` → `HTTPException`
  at an API edge) — but one boundary helper, not one wrapper per function.
- **Compatibility re-exports are moves that stalled.** After relocating code, update the
  importers in the same change. If a re-export must stay (external consumers), mark it
  `# compat re-export — remove after <condition/date>`; an unmarked one reads as API
  surface and never dies.


## Defensive code needs a reachable condition

Every guard implies "this state occurs." If you can't name the production path that
produces the state, don't handle it — assert it away or let it raise loudly.

- No `try/except` that can't fire, or that swallows into a warning the caller never
  checks. No re-validating what a typed boundary already guarantees (a `None` check on a
  field the model makes required implies a nullability the types deny).
- A branch commented "should be unreachable" is a deletion candidate, not a safety net:
  either encode the invariant structurally (types, constraint, assert) or delete the
  branch. Fabricated fallback values (a synthetic date for empty data) mask upstream
  bugs — fail visibly instead.


## Version suffixes are transition states, not addresses

A numbered sibling (`foo2`, `foo_v2`) is a migration in flight. Each stalled v1 keeps
recruiting new consumers, because generated code copies the nearest existing pattern.

- Creating `foo2` requires, in the same PR: mark `foo` deprecated with a removal
  condition, and stop new consumers (don't import a deprecated module in new code).
- When `foo`'s consumers hit zero, delete it in that PR — and give the replacement the
  plain name. `foo2` outliving `foo` by more than a migration window means the rename
  never finished.
- Before adding a consumer to any numbered/`DEPRECATED`-marked module, check whether the
  successor exists — the number is the hint that you're looking at the old one.


## Comments state what the code can't — rarely, briefly, plainly

A comment earns its line by carrying a constraint, invariant, side effect, or _why_ that
the code and types cannot show — never by narrating the next line, restating a
signature, or recording change history (git has it). First improve the name or
structure; then comment only what stays hidden. This applies to docstring bodies too.

A why-shaped comment can still be noise:

- **Necessary.** No comment is the default; most diffs add few or none. Do not annotate
  each field, branch, fixture, or assertion — when most members of a file carry a
  comment, that is annotation regardless of each comment's merit. State a shared reason
  once, next to the code it governs.
- **Brief.** Start with one sentence; add only what a reader needs to preserve or verify
  the constraint. A longer block is for a real contract — lock ordering, a data format,
  external tool behavior.
- **Plain.** Flat, literal words — the reader is skimming. Write "skip deliveries that
  have no reader", not "refused for want of a reader". A comment that needs a second
  read gets rewritten plainly or deleted.
- **Durable.** State the assumption this code depends on, not a tour of what another
  file currently does. "X reads this back, Y clamps it" rots when the neighbor changes,
  and stale comments cost their own fix PRs; "tolerates the backend not yet shipping
  this field" is a contract. Prefer a type, schema, or test to enforce it. No PR numbers
  as history; cite an issue only while it defines an active constraint.

Never write a comment whose audience is the reviewer. If a lens finding is wrong, refute
it in the PR thread. A comment justifies code only by stating a concrete constraint a
reviewer can verify.

Anchor: an audit of 40 merged PRs found 15% of added lines were comments, nearly all
why-shaped, whole files annotated field by field. Why-shaped is not the bar.


## When you touch sprawl, subtract

These rules apply retroactively at edit time: touching a file that carries a dead
export, an unused parameter, a one-consumer abstraction, or narration comments — and your
change is adjacent — fold the deletion in. Subtract > reuse > refactor > add.

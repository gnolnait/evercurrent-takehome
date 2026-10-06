# Lens selection policy

The authoritative rule for which review lenses a diff warrants. The orchestrator
(`ORCHESTRATOR.md`) and `/review-local` both read this file and apply what it says, so a
selection made by one is the selection made by the other.

Select lenses by risk surface, and dispatch the FEWEST that cover THIS change's actual
risk — not every lens that could conceivably apply. Calibrate: a typical PR warrants 1–2
lenses; 4 is already a lot; reserve 5+ only for genuinely load-bearing changes
(storage/schema, auth/security, concurrency, data transforms, or a change spanning many
layers). When unsure whether a specialized lens adds anything over review-adversarial's
general net, DON'T dispatch it — adversarial already covers its category, and a scoped
lens earns its slot only when its SPECIFIC risk is clearly and materially present.

Narrow lenses: review-correctness, review-security, review-concurrency,
review-silent-failure, review-tests, review-rule-specialist, review-consistency. Plus one
general lens: review-adversarial — the always-on safety net: it reports real bugs in ANY
category (it does not assume a specialist ran — most PRs dispatch few or none) plus the
out-of-bucket bugs + design problems the narrow scopes miss.

Secondary lenses live in `secondary/` (claim-evidence, envelope, information-loss,
instruction-drift, product-shape, rollout-cohort, subtraction, surface-census,
vacuity). They are NOT part of the default selection: dispatch one only when it is
requested by name (`/review-local --lenses vacuity`) or when its own frontmatter `gap:`
names a risk this diff clearly carries.


## By diff shape

- Docs/config/comment-only diff → 0–1 lenses (skip review-adversarial too).
- Typical implementation PR → review-adversarial alone (it covers correctness as a
  category; review-correctness is risk-matched below, not a default pair). Add
  review-tests if the PR changes runtime behavior and its test coverage is absent,
  unclear, or meaningfully affected; add review-silent-failure if it adds or changes
  error handling, I/O (network / filesystem / DB / subprocess), OR a branch /
  early-return that could skip a required side effect (a write, commit, or
  notification). Do NOT add the rest by reflex — each needs its specific risk-surface
  match below.
- Load-bearing (storage/schema, auth/security, concurrent code, data transforms, >5
  files across layers) → the full warranted subset.


## By risk surface

- review-adversarial on ANY non-trivial implementation PR (it's near always-on — the
  only exception is docs/config/comment-only diffs). It is the one lens that needs no
  risk-surface match; the others are scoped.
- review-correctness ONLY if the diff carries logic dense enough that a dedicated pass
  plausibly adds over review-adversarial's general net: an algorithmic or numerical
  change, intricate boundary / conditional logic, or a cross-layer contract change (API
  shape, serialization, schema mapping). Routine wiring, CRUD, and pattern-following
  refactors do NOT qualify — adversarial covers correctness as a category there.
- review-concurrency ONLY if the diff INTRODUCES or restructures concurrent
  coordination: locks / synchronization primitives, shared mutable state across tasks or
  threads, parallel writes, or ordering-sensitive async logic where a race would change
  the outcome. Merely touching async code does NOT qualify; adversarial covers
  concurrency as a category.
- review-security ONLY if the diff touches a trust boundary: parsing untrusted /
  external input, auth / token / secret / credential handling, SQL or command
  construction, redaction or logging of user-derived data, or file / path / URL
  handling. (review-adversarial still covers security as a category when this lens is
  not dispatched.)
- review-rule-specialist ONLY if at least one `.claude/rules/*.md` file's `paths:` globs
  match a changed file. Match them before you decide; no match means this lens has
  nothing to check. Its dispatch carries the PATH of each matched rule.
- review-consistency ONLY if the diff changes how a served or exported value is
  produced, or adds a second place that produces one. Any of these qualifies: a rule
  applied to a value (a unit conversion, a date bucket, a period or window bound, a
  filter, a rule for withholding); the grain or key of a join, view, or mapping that a
  breakdown, split, or share rides on; a value or label produced in two places at once —
  two queries, two endpoints, two output formats; a new surface, export, or status over
  a value that already had a reader. Judge this from the diff alone. Do NOT require
  yourself to name the second reader first — the twin lives outside the diff, and
  finding it is the lens's own work. Skip a diff that serves no value to a user and adds
  no reader of one: plumbing, types, styling, tooling, tests, and docs.
  review-correctness owns a wrong value; this lens owns two that disagree.

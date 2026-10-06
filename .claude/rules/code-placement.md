---
name: code-placement
description: |
  Where new code belongs. Loads when editing any source file under src/**.
  Guards against the "I'll just put this functionality in a random place"
  class of error — bolting behavior onto a convenient, already-imported, or
  lowest-layer module instead of the module whose responsibility it is.
paths:
  - "src/**/*.py"
  - "docs/**"
---
# Put functionality in its home

Before adding a function, class, method, or constant, ask **whose job is this?** — then
put it there. The single most common placement failure is bolting behavior onto a
module that merely happens to be _convenient_ (already imported, lowest in the layer
graph, or "close enough"), rather than the module whose **responsibility** it is.

A logging module owning a background-task scheduler; a source-specific quirk living in
a generic shared util; a constant duplicated into the file that reads it instead of the
module that defines it — these all read as "it was reachable from here," not "it
belongs here." The cost lands later: the next person looks for the capability under its
name and can't find it, or imports a whole unrelated package to reach one helper.


## The test, before you write it

1. **Does it already exist?** Before deciding _where_ a new helper / util goes, search
   for the existing owner — the house util or class that already does this. Placement
   is moot if you shouldn't be writing it at all. Skipping this search is how you get a
   second, untested copy of logic that already had a tested home. Adding a generic,
   exported, pure helper is itself the trigger to search by capability first.
2. **Name match.** Does the file / class / package name describe what you're adding? If
   you'd have to explain "it's in the logging module but it's not about logging," it's
   misplaced.
3. **Responsibility, not reachability.** Are you putting it here because this is the
   thing that _owns_ this concern, or because this is what you already had imported /
   the lowest layer that everything can see? Reachability is not ownership.
4. **Would someone find it here?** If a teammate searched by capability, is this the
   first place they'd look? If not, move it to where they'd look.
5. **One reason to change.** Adding this, does the module now have two unrelated reasons
   to change? If yes, it's two modules wearing one filename.
6. **No grab-bag growth.** A low-level shared module (a `utils` module) is not a dumping
   ground for "stuff every layer can import." If the only justification is "it had to
   go somewhere everyone can reach," that's a smell — find the real owner, or create a
   properly-named home.


## When there's genuinely no home

If the capability is real and — per test 1 above — you've confirmed no existing module
owns it, **create the module named for the capability** (no jargon in the name). Don't
wedge it into the nearest file. A new small well-named module beats a big mis-named one.


## Docs have a home too

Durable documentation lives in `docs/` — not scattered through code as long module
preambles. And a design doc carries **no PR-historical narrative** ("originally we
tried…", reviewer-round notes): that context is useless after merge; keep the timeless
design, move the rest to the PR description where history belongs.


## Behavior belongs on the domain class (Tell-Don't-Ask)

"Whose job is this?" applies to a _method_ as much as a module. State-mutating logic for
a domain entity belongs **on that entity's class**, not in a free function or an
endpoint that happens to hold a reference to it. If the method doesn't exist, add it —
even if that forces you to load the object first.

- **Pass the object, not its fields.** A helper takes the object and calls its own
  methods / mutates it through its own interface — it never reaches into another
  object's internal state from outside. Spraying an object's fields across a call
  signature is the asking-not-telling smell.
- **Endpoints orchestrate; domain classes behave.** A handler (CLI command, web route)
  loads the object, then calls methods on it. Business logic that lives inline in the
  handler (or in a free function beside it) is logic that can't be reused, tested, or
  kept invariant in one place.

Cost when ignored: the invariant can't be enforced in one place, callers couple to the
entity's internals, and the next person adds a _second_ way to do the same mutation.

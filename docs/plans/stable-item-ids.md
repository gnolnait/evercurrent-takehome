# Plan: stable item ids

Status: v0.1, 2026-10-12. Process stage 1 (`docs/process.md`), full process: Codex writes
the tests, an Opus subagent writes the code, every test is RED-GREEN proven, both review
lanes run. Source: the 2026-10-12 design review, which found that an item id depends on
the delta boundary and the model's output order.

## Goal

A new item's id names the item's first cited message, so the same fact gets the same id
when the thread is extracted in different deltas or the model lists items in a different
order. Seen records and feedback (SPEC section 4, UserProfile) will key on item ids, so
the id must be stable before they are built.

## Rule

- A new item (the model returns `item_id` null) gets
  `f"{thread_id}#{anchor_ts}#{k}"`.
- `anchor_ts` is the `message_ts` of the item's verified evidence entry with the lowest
  `ts_micros`, not the first entry in output order.
- `k` is the lowest integer, from 0, such that the id is not a key of the thread's
  existing items and not an id already given in this extraction.
- An updated item (the model returns an existing `item_id`) keeps its id, as today.

The id is stable against delta boundaries and output order. It is not stable when the
model cites a different first message for the same fact, or when two new items share an
anchor and the model swaps their order.

## Interface

No signature changes. `verify(extraction, delta, citable, glossary, tasks, existing)` in
`src/digest/extract/verify.py` changes how it builds a new id. The id format keeps the
`thread#ts#k` shape, so `absence.py`, `select.py` and the eval need no change.

## Behaviours

- B1. A new item's id uses the earliest cited message by `ts_micros`, whatever the order
  of its evidence entries in the output.
- B2. The same item extracted from a root-only delta and from a delta with the root and a
  later reply gets the same id when its earliest evidence is the root.
- B3. Two new items with the same anchor in one extraction get `#0` and `#1`.
- B4. A new item whose anchor id is already an existing item's id gets the next free `k`.
- B5. An updated item keeps its id (unchanged behaviour).

## Acceptance

- `tests/test_extract.py` covers B1 to B5; each new or changed test is RED-GREEN proven.
- The one recorded request that lists an existing item keeps a byte-identical id
  (`C0SUPCH:1792087200.010059#1792087200.010059#0`), so no cassette changes.
- `reports/eval-2026-10-23.md` is regenerated in the code commit. Only the unmatched-items
  row may change.
- `uv run digest demo` runs offline. Any change in line order within equal scores is
  noted in the PR.

## Not in scope

Item version history, a stable key for updated content, and seen records.

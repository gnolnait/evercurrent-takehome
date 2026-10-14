# Plan: M5 slice, the Slack digest layout (v6)

Status: v0.5, 2026-10-13. Process stage 1 (`docs/process.md`). The design comes from a design pass on the real day-6
payloads; three readers' mock payloads (v6) were posted to the user's DM in Scratch on
2026-10-13 and approved ("looks good"). This unit is one day of work. Section 15 lists what
it does not do.

## 1. Goal

Replace the digest's Slack layout with one a reader can scan in seconds: a short top message
with the next gate, the counts and only the items to read first, and the rest in one thread
reply. Each item is a title, one sentence and one meta line with one source link, plus file
links. No interactive element: the bot has no interaction handler yet (section 15).

## 2. Inputs and terms

- The reader's `Selection` (`profile`, `day`, `hidden`, `previous_digest`,
  `first_working_days`, `lines` with `item`, `section`, `must_see`, `reason`, `score`).
- The `World` (program, time zone, gates with date, buffer and checklist, personas, channels,
  entities with types).
- The seed map (`digest.seed.SeedMap`), optional.
- **Authors:** `authors: Mapping[str, str]` from a world message key to the persona `user_id`
  who posted it, for every evidence message of the selection's lines that the store holds.
  `cli._demo_selections` builds it from the store it already opens
  (`Store.events_in_thread(line.item.thread_id, as_of)`), so the renderer stays a pure
  function.

Terms used in every section below:

- **Key:** `<world channel>:<world ts>`. An evidence entry's key is
  `<channel_id>:<message_ts>`. The key of an item's thread root is its `thread_id`.
- **Selection order:** the index of a line in `selection.lines`. The lines are already in
  section order (`needs_you`, `not_happened`, `decisions`, `risks`, `fyi`).
- **Section title:** `render.SECTION_TITLES` (`Needs you`, `Not happened`, `Decisions`,
  `Risks`, `FYI`).
- **Gate code:** an entity id that is a gate id in `world.project.gates` or whose world
  entity type is `gate`.
- **Ask item:** an item of type `ask`, or an ask absence (`absence.source == "ask"`). **An
  ask to the reader:** an ask item whose `owner` is the reader.
- **Checklist absence:** an item with `absence.source == "checklist"`. Its task is the
  world checklist task with id `absence.task` in the gate `absence.gate`.
- **First name:** the first whitespace-separated word of the persona's `name`. An id with
  no persona gives `someone` (`Someone` at the start of a title or body).
- **Role text:** the role with `_` shown as a space: `supply_chain` gives `supply chain`.
- **Date text** `<Day d Mon>`: English three-letter weekday, day of month without a leading
  zero, English three-letter month: `Mon 19 Oct`. The date of a Slack ts is its local date
  in the world time zone.
- **Days text:** `1 working day`, else `N working days`.
- **P:** the end of `previous_digest` in the world time zone
  (`datetime.combine(previous_digest, time.max, zone)`). A first digest
  (`previous_digest is None`) has no P.
- **Separator:** the meta parts are joined by ` · ` (space, U+00B7, space).
- **Quote marks:** `“` (U+201C) and `”` (U+201D), as in the v6 mocks.

## 3. Top message

Block order with at least one item:

1. `header`, plain_text: `<program> · <Day d Mon>` of `selection.day`. Example:
   `Kestrel K2 · Mon 19 Oct`. With `reader_in_header=True`, the header adds
   ` · <first name> (<role text>)` of the reader, for example `Kestrel K2 · Mon 19 Oct ·
   Nadia (ME)`. `digest post` passes True on both paths, because each path sends the
   digests of several readers to one destination (one DM, or one webhook channel). A
   delivery to each reader's own DM passes False.
2. `context`: the lines of `render.context_lines(world, selection)`, word for word, joined
   by a newline: the window line (`Since your digest of 2026-10-16` or `First digest: the
   last 5 working days`), then the phase line when the world has a phase window on the
   day (`Phase: ... since ... . Next gate: ...`). The bold gate line (3.1) stays: it is the
   reader's gate with the buffer, and it may name the same gate as the phase line.
3. `section`: the summary: the gate line (3.1), a line break, the count line (3.2). With no
   gate, the count line only.
4. `divider`, then the top items (4.1), 2 blocks each.
5. `divider`, then one `context` foot (3.3). With no foot, no divider either.

Block order with no item: blocks 1 to 3, where the count line is `Nothing new for you.`.
When `hidden > 0`, a `divider` and the foot follow (5 blocks), else the message has 3
blocks. There is no thread reply.

### 3.1 Gate line

"Today" is `selection.day`. The candidates are the gates with date >= today. The first rule
that gives a gate wins; inside a rule, the earliest date wins, then the lower gate id:

1. A gate whose id is in `profile.owned_entities`.
2. A gate with a checklist task whose `owner` is the reader's role.
3. A gate whose id is in the `entities` of a kept item (after the fold, 4.3), not counting
   its members' entities.
4. Else no gate line.

Text: `*<name> (<id>) is <when>, <Day d Mon>.*`, then a space and the buffer clause when there
is one. `<name>` is the gate name with a trailing ` (...)` removed. `<when>` is `today`,
`tomorrow` or `in N days` (calendar days from today). The buffer clause:

1. `No buffer days.` when the gate has `buffer_days` 0. This wins over any absence data.
2. Else `U of B buffer days are used.`, where B is the gate's `buffer_days` and U is the
   largest `buffer_consumed` of a checklist absence of this gate in the selection.
3. Else nothing. Never print `buffer_days` alone: "7 buffer days" reads as "7 left".

| Reader | Gate line |
|---|---|
| Nadia | `*Top-cover tooling and texture release (G-TR1) is tomorrow, Tue 20 Oct.* No buffer days.` (rule 1) |
| Hamid | `*DVT-1 go/no-go (G-DVT1) is in 3 days, Thu 22 Oct.*` (rule 2; no G-DVT1 checklist absence in his selection) |
| Grace | `*DVT-1 go/no-go (G-DVT1) is in 3 days, Thu 22 Oct.* 4 of 7 buffer days are used.` (rule 1) |

### 3.2 Count line

Counts, all after the fold (4.3):

- A = all items. M = must-see items. K = items promoted to the top on a day with M = 0
  (`min(3, A)`), else 0. T = A - M - K, the thread items that are not must-see.
- N = items with the age tag `New` (5.6).

Parts:

- `<more>` = `, <T> more in the thread`. In the webhook form (11.2): `, <T> more below`.
  Omitted when T = 0.
- `<new>` = ` <N> of <A> are new.` Omitted in a first digest.
- `<rest>` = `the thread`. In the webhook form: `the next message`.

Templates, one per case:

| Case | Count line |
|---|---|
| 1 <= M <= 5 | `*<M> to read first*<more>.<new>` |
| M > 5 | `*<M> to read first*: 5 below, <M-5> at the top of <rest><more>.<new>` |
| M = 0, A > 0 | `Nothing needs you today. *<K> worth a look*<more>.<new>` |
| A = 0 | `Nothing new for you.` |

Examples: Nadia `*2 to read first*, 6 more in the thread. 4 of 8 are new.`; Hamid
`*4 to read first*, 5 more in the thread. 3 of 9 are new.`; Grace
`*4 to read first*, 4 more in the thread. 3 of 8 are new.`

### 3.3 Foot

One `context` block with one mrkdwn element. Two parts, each present only when it has
content, joined by `. `:

1. `In the thread: <list>`. In the webhook form: `Next message: <list>`. `<list>` is
   `Read first <M-5>` when M > 5, then `<section title> <count>` for each section with
   thread items, in section order, joined by `, `. Counts are before the reply cap (8).
2. `<hidden> items left out: from channels you cannot see.` (`1 item left out: ...` for
   one), when `hidden > 0`.

Example: `In the thread: Not happened 1, Risks 5`.

### 3.4 Notification text

`text` of the top message is the only string a phone shows. Parts:

- Gate part: `<gate id> <when>. ` from the gate line's gate. Omitted with no gate.
- M > 0: `<M> to read first, starting with <title>.`
- M = 0, A > 0: `Worth a look: <title>.`
- A = 0: `Nothing new for you.`

`<title>` is the displayed title (5.2) of the first top item, raw (before the escape). No `.`
is added when the title ends with `.`, `?` or `!`. The whole raw text is cut at a word
boundary (9.1) to the longest cut whose escaped form is at most 150 characters, then
escaped. Example: `G-TR1 tomorrow. 2 to read first, starting with Top-cover DFM review
open and blocking G-DVT1.` No bot name and no date: Slack shows both.

## 4. What goes where

### 4.1 Top items

- A day with M > 0: the top message holds must-see items only, at most 5, in top order
  (4.2). The place is the mark: no dot, no legend, no `[must see]` tag. Must-see items past
  5 open the thread reply (8).
- A day with M = 0 and A > 0: the top message holds the K items with the highest score
  (the kept line's `score`); ties go by selection order. They show in that order (score,
  then selection order), not in top order.

### 4.2 Order

- **Top order** (must-see items, and the overflow in the thread), ascending by:
  1. Section index.
  2. The nearest upcoming gate date: for a checklist absence, the date of `absence.gate`;
     for any other item, the earliest date >= today among the gate codes in the kept item's
     `entities`. An item with none sorts after every date.
  3. `absence.days_silent`, highest first. An item with no absence counts 0.
  4. Selection order.
- **Thread order** for the other thread items: section index, then `score` (highest
  first), then selection order.

Result: Grace starts with the TST-THM-02 gate task (G-DVT1, 4 days silent) before
TST-EMC-01 (G-DVT1, 3 days), then the ask absence (no gate), then the DFM review (risks).
Nadia starts with the DFM review (G-TR1, tomorrow) before the chassis base (no gate code).

### 4.3 Fold

Fold before every count and every order. Only two pair types fold. Walk the lines in
selection order. An **absorbable** line is a line in section `risks` whose item has no
absence. For each absorbable line R, the **absorber** is the first line X before R in
selection order that meets one rule (a line after R never absorbs it, so a fold never
moves an item to a later section; an `fyi` ask in R's thread stays a separate line):

- (a) X is a checklist absence, and an entity id of R occurs in the text of X's task. The id
  occurs when it is in the text with no letter, digit or `-` directly before or after it.
  `Pass the TST-EMC-01 EMC pre-scan` gives `TST-EMC-01`. A task not in the world matches
  nothing.
- (b) X is an ask item, X and R have the same `thread_id`, and they share an entity id that
  is not a gate code.

When R has an absorber, R becomes a **member** of X and leaves its section. Else R stays.
Because selection order is section order, "first in selection order" is "first in section
order, then selection order". Each absorbed item has one destination. An absorber is
never absorbable, so asks never absorb asks, two absences never fold, and folds do not chain.

The kept item (X with its members, in selection order):

- keeps X's section, `score`, `owner`, `first_seen`, link target (6) and entities, then the
  members' entities for codes (5.5);
- is must-see when X or any member is must-see;
- takes files from X and every member (6.2).

Text of a kept item with members:

- An ask item of type ask: the title of its first member, and its own ask body (5.3).
- An ask absence: its own title (5.2) and its own body (5.3). Its members add only
  must-see, files and codes: an absence keeps saying what was not found and where
  (SPEC section 5.7).
- A checklist absence: its own title (5.2), and the body of its best member: must-see
  first, then the higher score, then selection order.

Thread alone is not enough. Grace's DFM risk and THM risk share thread
`C0KPROG:1792423800.010077` but are two facts; they share only `G-DVT1`, a gate code, and
neither is an absorber, so they stay apart. Today's folds: Grace's EMC risk into the
TST-EMC-01 gate task (rule a), Grace's THM risk into the TST-THM-02 gate task (rule a),
Hamid's PN-31020 shortage into the PN-31020 ask (rule b). Hamid reads
`PN-31020 signal harness shortage for DVT-1 kit` / `Grace asked you: “can you give an ETA
for locate or replacement”`.

### 4.4 Caps

- Selection caps stay as `select.SECTION_CAPS` defines them, plus must-see lines.
- Top message: 5 must-see items, or K <= 3 ranked items on a day with M = 0.
- Thread reply: at most 50 blocks (8).

## 5. Item anatomy

Two blocks per item:

```
section  *<title>*\n<body>      one mrkdwn text object, verbatim
context  <meta line>            one mrkdwn element, verbatim
```

With an empty body, the section text is `*<title>*` alone. Every text passes the pipeline
of 9.1.

### 5.1 The ask message

One resolver gives the ask message of an ask item. Title, body, link (6.1) and the age tags
`Asked` and `You replied` (5.6) all use it.

- The ask message is the first entry of the item's `evidence` whose key is in `authors` and
  whose author is not the item's `owner`. This is the absence detector's rule
  (`authors.get(key, owner) != owner`): an entry with no known author counts as the
  owner's and is skipped.
- With no such entry, the item has no ask message: no quote, its body is the first sentence
  of its summary (5.3), its link target is the thread root, and it has no `Asked` and no
  `You replied` tag.
- **Asker:** the author of the ask message. For an ask absence with no ask message,
  `absence.asker`.

### 5.2 Title

- Any item not listed below: the data `title`.
- Checklist absence: `<task noun>: no progress evidence for <days text>`, with
  `absence.days_silent`. The noun is the task text without its first word and without a
  `the` that follows it: `Close the TST-THM-02 thermal review` gives
  `TST-THM-02 thermal review`. A task not in the world gives `absence.task` as the noun.
- Ask absence, by who asked (SPEC 5.7: the title says where the system looked):

| Case | Title |
|---|---|
| The reader asked (asker = reader) | `No reply from <owner first name> in this thread for <days text>` |
| The reader was asked (owner = reader) | `<Asker> has had no reply from you in this thread for <days text>` |

- A kept item with members: see 4.3.

Cut to 90 characters (9.1).

### 5.3 Body

- Ask item with an ask message: the prefix, then the cleaned quote of the ask message's
  `quote`, then `”`:
  - owner = reader: `<Asker> asked you: “`
  - an ask absence whose asker is the reader: `You asked: “`
  - else: `<Asker> asked: “`
- Ask item with no ask message: the first sentence of its summary.
- Checklist absence with members: the first sentence of the best member's summary (4.3).
- Checklist absence with no member: `Progress is expected every <days text>.` from the task's
  `expected_cadence_days`. A task not in the world gives an empty body.
- Any other item: the first sentence of its summary.

**First sentence:** flatten the summary (9.1), then split it after each `.`, `?` or `!` that
is followed by whitespace or by the end of the text. The mark stays with its sentence.
Strip each part; take the first part that is not empty. A summary with no such mark is one
sentence. An empty summary gives an empty body. No overlap filter: the body can repeat the
title.

**Quote cleanup**, after the flatten and before the cut, in this order:

1. A `<@reader id>` at the start, then any run of `,` `:` `;` `.` `!` `?` `-`, then any
   spaces: removed.
2. `<@U...>` or `<@U...|label>` of a persona: `@<first name>`. Of an unknown id:
   `@someone`.
3. `<#C...|name>`: `#name`.
4. `<url|text>`: `text`.
5. Any other `<...>` stays as text; the escape (9.1) makes it harmless.

The quote is cut to 100 characters (9.1). The limit counts the quote only, not the prefix
and not the closing `”`. Every other body is cut to 160 characters.

### 5.4 Label and owner

The label is shown on top items and on must-see overflow items in the thread, bold, first
in the meta line. No other thread item has a label. First row that matches wins:

| # | Case | Label |
|---|---|---|
| 1 | Ask item, owner = reader | `Asked of you` |
| 2 | Ask absence, asker = reader | `Your question` |
| 3 | Checklist absence, task `owner` = reader's role | `Your gate task` |
| 4 | Checklist absence, any other (the EM, or the gate's owner) | `Gate task` |
| 5 | Must-see, the kept item's entities include an owned entity | see below |
| 6 | Fallback, by section | `Needs you`, `Not happened`, `Decision`, `Risk`, `FYI` |

Row 5: c is the first owned entity in the kept item's `entities`, taking non-gate codes
before gate codes. When c is a gate code: `On your gate` when the displayed title holds c,
else `On your gate <c>`. When c is not a gate code: `You own this` when the displayed title
starts with c, else `On your <c>`.

Row 6 covers ranked items on a day with M = 0 and any must-see item no other row matches.

**Owner part:**

- Section `fyi`: no owner part.
- Checklist absence: the task's `owner` role. When exactly one human persona has that role:
  `Owner: <first name> (<role text>)`, or `Owner: you` when that persona is the reader. Else
  `Owner: <role text>`. This is a display convention: the checklist names a role, not a
  person. A task not in the world: no owner part.
- Any other item, by `owner`: the reader gives `Owner: you`; a human persona gives
  `Owner: <first name> (<role text>)`; a bot persona, an unknown id or `None` gives no owner
  part.
- `Owner: you` is never shown on an item that has a label.

### 5.5 Codes

At most 2 entity codes, plain text (backticks render red and look like errors), joined by
`, ` in one meta part.

- Candidates: the kept item's `entities`, then each member's `entities` in member order,
  without repeats (first seen wins).
- A candidate is a code when it matches `^[A-Z][A-Z0-9]*(-[A-Z0-9.]+)+$`, it is not a gate
  code, and its world entity type is not `vendor`, `subsystem` or `build`. An id with no
  world entity passes the type rule.
- A code is left out when it occurs as a substring of the displayed title, body or label:
  the raw text after its cut, before the escape.
- Order: codes in `profile.owned_entities` first, then candidate order. Keep the first 2.

### 5.6 Age tags: the diff

No age tag on an absence (checklist or ask): its title carries the days. For any other kept
item, at most two tags, in this order:

1. One of, first match:
   - `New`: not a first digest, and the kept item's `first_seen` is after P.
   - `Asked <Day d Mon>`: an ask to the reader with an ask message (5.1). The date is the ask
     message's date.
   - `Open since <Day d Mon>`: the date of `first_seen` is before `selection.day`. The date
     is the date of `first_seen`.
   - Else no tag.
2. `You replied`: an ask to the reader with an ask message, and an entry of the kept item's
   `evidence` with a later `message_ts` than the ask message whose author in `authors` is
   the reader. It can follow `New` or `Asked`.

No `Updated` tag (section 15). A first digest has no `New` tag; the other tags apply. Hamid
sees `Asked Thu 15 Oct · You replied` on the anodize PO ask and `Asked Tue 13 Oct · You
replied` on the spare-stock ask.

### 5.7 Meta line

The parts, in this order, each present only when it has content, joined by the separator:

1. `*<label>*` (5.4).
2. The owner part (5.4).
3. The age tags (5.6), one part each.
4. The codes (5.5).
5. Checklist absence whose gate is not the gate line's gate:
   `<gate id>: U of B buffer days used`, or `<gate id>: no buffer days` when B is 0. Omitted
   when `buffer_consumed` is `None` and B > 0.
6. The source part (6.1).
7. Checklist absence: `checked: #<world channel name> threads`, with the channel of its
   `thread_id`. It shows on every checklist absence, with or without members, so the search
   scope stays visible when a member supplies the body (SPEC 5.7).
8. The files part (6.2).

## 6. Source link and files

Each item has one source link, plus file links.

### 6.1 Source link

- **Target key:** for an ask item with an ask message, the ask message's key. For any other
  item, the thread root key (`thread_id`). A kept item with members uses its own target.
- **Mapped** (a seed map is given and `message_permalink(links, target)` is not `None`):
  `<permalink|Thread> in <#C...>`, with `Thread` when the target key is the `thread_id`,
  else `Message`. The channel is `channel_of(links, target)`. The permalink comes from
  `models.permalink_for`, so a mapped reply links with its `thread_ts`
  (`?thread_ts=<thread_ts>&cid=<channel>`). The raw `&` stays inside the URL part: the
  posted `current-*.json` payloads carry it and open.
- **Unmapped:** no link. `In <#C...>` with the Slack id from `links.channels` of the
  target's world channel, else `In #<world channel name>`. Never fall back to the world
  permalink: `kestrel-k2.slack.com` does not exist.
- No `<@U...>`: personas have no accounts. No `<!here>`.

Grace's TST-THM-02 gate task links its own thread (`C0ENGEF:1791911400.010019`), not the
program thread of its member.

### 6.2 Files

- Only seed-map files. With no seed map, no files part.
- The evidence keys: every evidence entry of the kept item, then of each member.
- The files: entries of `links.files` whose name starts with `<evidence key>/`, in map
  order, without repeats by file name (the part after the `/`).
- The part: `:paperclip: `, then the first 2 as `<permalink|<escaped name>>` joined by a
  space, then ` +N files` (` +1 file`) when N > 0 more distinct files remain.

Nadia's chassis item shows `PN-11020_FAI_photos.pdf` and `PN-11020_CMM_report.pdf`.

## 7. Each kind, from the reader's side

The meta column lists the parts that can show; the rules are in 5.4 to 5.7.

| Kind | Title | Body | Meta |
|---|---|---|---|
| Ask to you (needs_you) | data title, or its first member's title | `<Asker> asked you: “<quote>”` | `Asked of you` · `New` or `Asked <date>` · `You replied` · codes · `Message` or `Thread` |
| Ask absence, you asked | `No reply from Ana in this thread for 3 working days` | `You asked: “<quote>”` | `Your question` · `Owner: Ana (FW)` · `Thread` |
| Ask absence, asked of you | `Grace has had no reply from you in this thread for 3 working days` | `Grace asked you: “<quote>”` | `Asked of you` · `Thread` |
| Checklist absence | `TST-EMC-01 EMC pre-scan: no progress evidence for 3 working days` | best member's sentence, or the cadence | `Gate task` · `Owner: Tobias (EE)` · codes · other-gate buffer · `Thread` · `checked: #eng-elec-fw threads` |
| Blocker, risk | data title | first sentence | label when top · owner · age · codes · `Thread` · files |
| Decision | data title | first sentence | owner · age · codes · `Thread` · files |
| FYI (an ask to someone else too) | data title | first sentence, or `<Asker> asked: “<quote>”` | age · codes · `Thread` or `Message` · files |

## 8. Thread reply

- One reply per reader, never more. No reply when the thread holds no item.
- Groups, in order: the must-see overflow (top order) under `*Read first* · <n>`,
  then each section with thread items (thread order) under `*<section title>* · <n>`. `<n>`
  is the group's item count before the cap.
- Blocks: for each group, a `divider` (not before the first group), the heading `section`,
  then 2 blocks per item.
- Same item anatomy as the top message. Only the overflow items have a label.
- `text`: `More from your digest: <list>`, with the `<list>` of 3.3. Thread replies do not
  notify.
- **Cap:** when the blocks are more than 50, pack whole items in order and keep 1 block for
  a last `context` block `<n> more items not shown` (`1 more item not shown`), where n
  counts items, not blocks. An item fits when the blocks so far, its 2 blocks, its group's
  divider and heading when it is the group's first item, and the 1 kept block are at most
  50. Packing stops at the first item that does not fit. A heading is added only with its
  first item, so no heading is left empty. No second reply: no section is split across
  replies or repeated under a continued heading.

## 9. Text and safety

### 9.1 Text pipeline

Every data or model text goes through these steps, in this order:

1. **Flatten:** `" ".join(text.split())`.
2. **Clean** (quotes only, 5.3).
3. **Cut** the raw text: title 90, body 160, quote 100. A text at or under the limit is
   unchanged. Else the result is the longest prefix that ends at a word end (the next
   character in the text is a space), with trailing spaces removed, plus `…`; the `…`
   counts in the limit. When no such prefix is non-empty (one word longer than the limit),
   the result is the first limit - 1 characters plus `…`.
4. **Escape** `&`, `<`, `>` as `&amp;`, `&lt;`, `&gt;`.

Then every mrkdwn text object and element passes through the existing `slack._cut` at 3000
characters, which drops a partial link or entity at the cut.

The header is plain_text: flattened, not escaped, and cut to 150 characters as today
(`header[:149] + "…"`).

Text this module builds (labels, tags, templates) holds no `&`, `<` or `>` except the link
tokens below.

### 9.2 Safety rules

- Every text object except the header is `mrkdwn` with `verbatim: true`. The header is
  `plain_text` (Slack requires it).
- Only these go inside `<...>`: constructed message permalinks (`<url|Thread>`,
  `<url|Message>`), seed-map file permalinks (`<url|name>`), and channel ids
  (`<#C...>`, matching `<#C[A-Z0-9]+>`).
- `SeedMap` checks every value that goes inside `<...>` when it loads, so no later code
  needs to: `team_id` matches `T[A-Z0-9]+`; `workspace_url` matches
  `https://[a-z0-9-]+\.slack\.com`; each `channels` value and each message `channel`
  matches `C[A-Z0-9]+`; each message `ts` and `thread_ts` matches `[0-9]+\.[0-9]+`; each
  file `permalink` matches `https://[a-z0-9-]+\.slack\.com/files/[A-Za-z0-9_.%/-]+`. A
  value that does not match makes the map fail to load: `--seed-map` exits 1 with the
  field name only (the refusal of `docs/plans/m5-links.md`), before any Slack call.
- No buttons, no overflow menus, no `actions` blocks, no accessories. A URL button still
  sends an interaction payload, and the bot has no handler.

## 10. Mobile

- One column. No `fields`, no tables.
- Title at most 90 characters; body one sentence; meta one line on desktop.
- Measured on the v6 mocks at 38 characters per phone row: top messages take 24 (Nadia),
  35 (Hamid) and 36 (Grace) rows. About 1.5 screens.
- Dividers only around the item list and between thread groups; none between items.

## 11. Delivery

Both paths build every message of every reader before the first send, as today. `--seed-map`
behaves as in `docs/plans/m5-links.md` (loading, checks, refusals that print no map value,
the count line of unmapped sources, which still counts every evidence entry as
`_echo_unmapped` does today), except that an unmapped source now has no link (6.1).
`digest demo` (terminal) does not change.

### 11.1 DM path (`digest post --dm`)

- For each reader in order: `chat.postMessage` with the top message; then, when there is a
  reply, `chat.postMessage` with the reply and `thread_ts` = the `ts` of the top message's
  response. Then `posted the digest for <reader>`.
- A failure of the top message gives today's refusal: `cannot post the digest for
  <reader>: <failure><rerun note>`, with `_rerun_note(sent, total)`.
- A digest with no thread reply needs no `ts`: its top message alone is a success, even
  when the response has no string `ts`. Only when a reply is to be posted:
- a top response with no string `ts`, or a failure of the reply, gives:
  `cannot post the thread reply of the digest for <reader>: <failure>; its top message was
  sent<note>`. `<failure>` is `_dm_failure(...)`, or `the top message has no ts`. `<note>`
  is `, and a rerun sends all <total> again` when `sent` is 0, else `_rerun_note(sent,
  total)`. `sent` counts the readers whose messages all posted.
- A rerun sends all digests again, as today. This is demo delivery: SPEC's exposure on
  Slack's acceptance stays separate work.

### 11.2 Webhook path (`digest post` without `--dm`)

An incoming webhook returns no `ts`, so it cannot post a thread reply.

- For each reader in order: `send` with the top message, then, when there is a reply,
  `send` with the reply as a second plain message (no threading). Both are built with
  `threaded=False` (12): the foot reads `Next message: <list>`, the count line says
  `more below` and `at the top of the next message`.
- A failure of the top message gives today's refusals. A failure of the second message
  gives `cannot post the second message of the digest for <reader>: <exception type>; its
  first message was sent<note>`, or `Slack refused the second message of the digest for
  <reader>: status <status>, <shown body>; its first message was sent<note>`, with `<note>`
  as in 11.1 and `<shown body>` by today's rule.

## 12. Interface (stubs land before tests)

```python
# digest/daily/slack.py
def slack_digest(world: World, selection: Selection, *, links: SeedMap | None,
                 authors: Mapping[str, str], threaded: bool = True) -> list[dict[str, Any]]: ...
    # [top message] or [top message, thread reply]; each {"text": str, "blocks": list}.
    # threaded=False gives the webhook wording of 3.2 and 3.3.

# digest/cli.py
def _demo_selections(day: int) -> tuple[World, list[Selection], dict[str, str]]: ...
    # the third value: authors (2) for every evidence message of the returned selections
```

- `slack_message` is removed. Its callers `post` (cli.py:168) and `_post_dm` (cli.py:231)
  move to `slack_digest`.
- `_demo_selections` callers, updated by the implementer: `demo` (cli.py:107, ignores the
  third value), `post` (cli.py:167), `_post_dm` (cli.py:230). No other caller in `src/` or
  `tests/`.
- Test patches that return a 2-tuple, updated by the test author:
  `tests/test_slack_links.py:194` and `tests/test_slack_post.py:744`. The patches at
  `tests/test_slack_links.py:454` and `:508` return `None` on a refusal path that never
  unpacks the value; they need no change.
- The tests of `tests/test_slack_post.py` and `tests/test_slack_links.py` that pin the old
  layout are updated by the test author to the new layout, keeping every safety assertion
  (escaping, verbatim, cuts, token secrecy, refusals).
- The safety check is a test helper in `tests/`, not production code. Its rules are A17.

## 13. Acceptance

Each row is tested with hand-made selections, worlds, seed maps and `authors` unless it says
otherwise. The decision tags (D1 to D14) name the decision of the v0.2 brief each row
covers.

- A1 (3). Block order of the top message with items, with and without a foot; the no-item
  form with `hidden = 0` (3 blocks) and `hidden > 0` (5 blocks); the header
  `<program> · <Day d Mon>`; the `context_lines` text word for word in block 1, with and without a phase line; no reply with no
  item.
- A2 (3.1, D8). Each gate rule in turn, with a candidate for a later rule present; a past
  gate is never picked; equal dates go to the lower id; `today`, `tomorrow`, `in N days`;
  the trailing ` (...)` is removed. Buffer clause: `No buffer days.` on a 0-buffer gate even
  with a checklist absence that has `buffer_consumed`; the largest `buffer_consumed` wins;
  nothing otherwise; never `buffer_days` alone.
- A3 (3.2, D9). Each count template: 1 <= M <= 5, M > 5, M = 0 with K = 1, 2 and 3, A = 0;
  T = 0 drops the `<more>` part; a first digest drops `<new>` in every form; the webhook
  words.
- A4 (3.3, D9). The foot list with `Read first`; the hidden part alone, with the list, and
  singular; `Next message:` in the webhook form; no foot and no divider when both parts are
  empty.
- A5 (3.4, D9). Each notification form, with and without a gate; no doubled `.`; the cut
  keeps the escaped text at or under 150 characters.
- A6 (4.1, 4.2, D8). Top items are must-see only, at most 5; overflow opens the reply under
  `Read first` with labels, in top order; an M = 0 day takes the K best scores in
  score order with selection-order ties; each top sort key decides one pair (section, gate
  date, an item with no gate last, `days_silent`, selection order); thread order.
- A7 (4.3, D2). Rule (a) and rule (b) each fold; a risk with only a gate code in common in
  the same thread does not fold; a shared non-gate code in another thread does not fold by
  rule (b); two asks in one thread stay apart; two absences stay apart; a risk that matches
  two absorbers goes to the first; the kept item is must-see when a member is; a folded ask
  of type ask takes its first member's title and keeps its own body; a folded ask absence
  keeps its own title and body; a folded checklist absence takes
  its best member's sentence; counts use the kept items.
- A8 (5.1, D3). The ask message is the first entry by a known non-owner author; an entry
  whose key is not in `authors` is skipped; with no ask message, the body is the summary's
  first sentence, the link is the thread root, and no `Asked` or `You replied` tag shows.
  Title, body, link and tags all agree on the same entry.
- A9 (5.2, D6). The checklist absence title and noun rule; each of the two ask-absence
  titles; `1 working day`; `checked: #<channel> threads` on a checklist absence with and
  without a member.
- A10 (5.3, D10). First-sentence split at `.`, `?`, `!` before whitespace or the end, not
  inside `3.5` or `v1.2`; an empty summary; each cleanup rule, including the leading reader
  mention with its punctuation and an unknown id as `@someone`; each prefix; the 100 limit
  excludes the prefix.
- A11 (5.4, D11). Each label row, including `Gate task` for an EM who does not own the gate,
  each row-5 form and each section fallback; each owner case (one persona, two personas,
  reader, bot, unknown, `fyi`); `Owner: you` hidden under a label.
- A12 (5.5, D11). At most 2 codes; owned first; gate, vendor, subsystem and build codes left
  out; codes from members; repeats removed; a code in the cut title, body or label left
  out.
- A13 (5.6, D4). `New` after P and not at P; `Asked <date>` for an ask to the reader;
  `Open since <date>`; no tag on an item first seen on `selection.day` in a first digest;
  `You replied` after `New` and after `Asked`, and not from an earlier reader message; no
  `New` in a first digest; no tag on an absence; no `Updated` tag ever.
- A14 (6, D12). Target and label `Thread` or `Message` for a root, a reply and an ask
  message; a mapped reply's URL carries `thread_ts` and `cid`; an unmapped item has no link
  and prints `In <#C...>` or `In #<name>`; no world permalink ever; files from the item and
  its members in map order, repeats removed, 2 names then `+N files` and `+1 file`; no files
  without a seed map.
- A15 (8, D1). One reply with its `text`; groups with headings and dividers; the cap packs
  whole items, never leaves an empty heading, and ends with `<n> more items not shown`; no
  second reply.
- A16 (9.1, D7). Word-boundary cut with `…` in the limit; a long word cut hard; the escape
  after the cut (a `&` at the limit is never split); the 3000 bound on a long meta line; the
  header not escaped and cut to 150.
- A17 (9.2, safety helper). On every message the rows above build, the helper checks: at
  most 50 blocks; only `header`, `section`, `context`, `divider`; no accessory and no
  `actions`; a header only as block 0 of a top message, plain_text, at most 150 characters
  (the header is checked for type, place and length only: it is plain text, so a raw `&`,
  `<` or `>` in a program name is allowed there); every other text object mrkdwn, verbatim,
  1 to 3000 characters; a context block at most 10 elements; `text` at most 150 characters;
  every link URL starts with the seed map's `workspace_url`, and no `<http` link at all
  without a map; in each mrkdwn text and in `text`, after the allowed tokens of 9.2 are
  removed, no `<`, no `>`, and `&` only in `&amp;`, `&lt;`, `&gt;`; no `<@` and no `<!`. The helper fails on each kind of broken payload: a cut link, unescaped text, an
  accessory, a long header, a link to another host.
- A18 (11.2, D5). Webhook: per reader, the top message then the reply as a second send,
  with the webhook wording; nothing sent when a build fails; the second-message refusals.
- A19 (11.1, D13). DM: the reply posts with `thread_ts` = the top message's `ts`, per
  reader; the refusal for a failed reply and for a missing `ts`, with `sent` 0 and
  `sent` > 0; no output holds the token.
- A20 (12, D14). `_demo_selections` returns the authors of every evidence message;
  `digest demo` output does not change.
- A21 (real data). The day-6 demo selections, with a deterministic synthetic seed map
  built in the test from the replayed store (each evidence key mapped to channel = its world channel id, which
  already has the Slack form `C[A-Z0-9]+` that A17 accepts, ts = the world ts, thread_ts = the world thread_ts, and one file per world file
  name), build messages that pass the A17 helper. The real Scratch map stays in the manual
  check (A22). The comparison with the approved mocks in
  `notes/research/slack-layout/v6-*.json` is a manual check recorded in the PR (that folder
  is local scratch; the test builds from the store). Expected differences from v6: the
  absence titles (D6), `checked: #<channel> threads` on each checklist absence, and Grace's
  TST-THM-02 gate task linking its own thread (D12). Selection changes after the mocks
  (the ask audience of #74, and the new score order) change the reply block counts; the
  top messages keep their counts (10, 14 and 14 on 2026-10-13).
- A23 (4.3, fold direction). An `fyi` ask in the thread of a `risks` line that shares a
  non-gate entity with it does not absorb it: the risk stays in `risks`, the ask stays in
  `fyi`. A `needs_you` ask in the same position still absorbs the risk.
- A24 (2, header reader). `reader_in_header=False` (the default) gives today's header;
  `True` adds ` · <first name> (<role text>)`; the 150 cut still holds. `digest post` and
  `digest post --dm` each send headers that name their reader.
- A25 (9.2, seed map). For each field of the 9.2 list, a map whose value does not match
  fails to load: `digest post --seed-map` exits 1, the output names the field, does not
  hold the bad value, and no Slack call is made. A real-form map loads.
- A26 (12, authors). `digest post` and `digest post --dm`, with `_demo_selections`
  returning a non-empty authors map for an ask item, send the asker quote prefix and the
  `Asked of you` or `Your question` label that the authors decide.
- A22 (live, manual, recorded in the PR). `digest post --dm <member> --seed-map <map>` in
  Scratch; the user checks the three digests and their threads.

## 14. Test seams

- `slack_digest` with hand-made `Selection`, `World`, `SeedMap` and `authors`, as
  `tests/test_slack_links.py` builds them.
- The CLI with `CliRunner` and patched Slack clients, as `tests/test_slack_post.py` does.
- No test touches the network.

## 15. Not in this unit

- Reply splitting, with a section continued in a second reply: the cap in 8 cuts instead.
- The `Updated` tag: `Selection` does not keep each item's newest evidence time.
- The `stale_beliefs` section: `Line.section` has no such value.
- An overlap filter on bodies.
- A production `check()`: the safety rules live in a test helper.
- Buffer state per gate outside absence items (Hamid's gate line could then show it).
- An explicit ask-message field on `Item` (D26).
- Threaded webhook replies: the CLI has no lookup for the parent `ts`.
- Open, not verified: how Slack mobile wraps a context line with 2 file links; whether a raw
  `&` inside a link URL works in every client (the current posts suggest yes).

## Next

The interactive slice: a `digest bot` Socket Mode listener (slack-bolt), item menus
("Why this item?", "Mark done", "Snooze until tomorrow") that record SPEC's feedback events
and update the message in place. It needs its own plan.

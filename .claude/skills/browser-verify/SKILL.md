---
name: browser-verify
description: "Verify a user-visible change in a real browser: a digest as Slack shows it (Block Kit layout, DM delivery, buttons, App Home, seeded persona posts) in the Scratch test workspace, or a page of the local web preview. TRIGGER: 'check it in Slack', 'does the digest render', 'verify in the browser', 'click the button and see', 'what does the DM look like', 'open the preview page', or any change to Block Kit output, Slack interactivity, the seeder or a web preview template that tests cannot show. NOT for pipeline logic the replay tests cover, NOT for docs, NOT as a routine step on every PR."
---

# Verify a change in a real browser

The loop: **start what you verify, open a signed-in browser, drive the scenario,
cross-check against the app's own record, report what you saw with evidence.**

Replay tests prove what the pipeline decides. Only a browser proves what a reader sees:
how Slack lays out the blocks, whether a button reaches the app, whether a persona post
shows the right name. For those surfaces a green suite is not evidence. The change is
browser-verified or it is unverified.

## 0. What exists to verify

Check before you start; do not verify a surface that is not built.

| Surface | Milestone | How to reach it |
| --- | --- | --- |
| Bot posts and DMs in the Scratch workspace | M5 (live mode); the bot token and Socket Mode are proven by a smoke test | Slack web client |
| Seeded corpus (persona posts, threads, edits) | M5 (`digest seed`) | Slack web client |
| Web preview: digest, landing map, rationale, profile, open items | M4 | `http://localhost:<port>` |

Facts about the Slack app are in `notes/slack-app.md` (gitignored; app id, scopes,
which token is which). Tokens live only in `.env`. Never print them, paste them into a
page, or put them in a URL.

## 1. Start what you verify

- **Live bot.** Start live mode in a background job with the command SPEC section 3 names
  once M5 builds it. Wait for the app's own "connected" log event (Socket Mode) with a
  bounded poll, not a blind sleep.
- **Web preview.** Add or reuse a `.claude/launch.json` entry and start it with
  `preview_start`; that opens the in-app Browser pane on the page. Poll the port until it
  answers before you read anything.
- **Seeded corpus.** `digest seed` posts at a configurable pace. Seed a short slice (one
  world day) for a check; a full world takes hours.

Start only what you need, and record the background job ids so you can stop exactly
those at the end.

## 2. Sign in

- **Slack.** The user signs in. You never type a Slack password, never create a Slack
  account or workspace, and never accept a workspace invitation for the user. The user's
  own Chrome is usually already signed in to Scratch, so Claude in Chrome (path A) needs no
  sign-in step. In the in-app Browser pane (path B), ask the user to sign in in that pane
  themselves, then continue.
- **Web preview.** Local and without auth in v1.

Work only in the Scratch workspace. If the page shows any other workspace, stop and tell
the user.

## 3. Drive the browser

Pick by surface and session:

- **Path A: Claude in Chrome** (`mcp__claude-in-chrome__*`). The default for Slack. It
  uses the user's signed-in session, and the user can watch. Load the tool set in one
  `ToolSearch` call.
- **Path B: the in-app Browser pane** (`mcp__Claude_Browser__*`). The default for the
  web preview. It is separate from the user's Chrome, so Slack needs a sign-in there first.
- **Fallback: no browser.** Give the user the exact URL, a numbered click path and the
  expected observation, and ask them to report what they saw.

### Slack web client

- Open a conversation directly: `https://app.slack.com/client/<team_id>/<channel_id>`.
  `auth.test` gives `team_id`; `conversations.list` gives channel ids. A DM from the bot
  is under Apps, as Downstream.
- The client is a large single-page app. Prefer `find` and `get_page_text` over a full
  `read_page` tree, which is very large.
- In Slack dialogs, a click by `ref` sometimes does not register. Take a fresh screenshot
  and click by coordinate, then confirm the click took effect.
- Seeded posts show the persona's name with an APP tag, because the bot posts them under
  `chat:write.customize`. That is expected. Their Slack time is the real seeding time; the
  world time is in the message metadata.
- A button click is a real event to the app. Check the result in the app (step 4), not
  only on screen. An unchanged message after a click can mean the app never got the
  event.

### Web preview

- Confirm the page you loaded is the page you meant before you verify anything.
- A cache that lives in page memory is wiped by a full reload. To test its hit path,
  navigate the way the app does (click the in-app link), not with a fresh `navigate`.
- Read the console and network for each step. A page that looks right but logs a new
  error or a failed request fails the check. Take the baseline before the step under
  test, never after it.

### Tool trouble is not app trouble

A screenshot or click timeout is a statement about the browser tool, not a measurement of
the page. Before you report a freeze, check signals that do not pass through the tool:
the app's own logs, a local health request, or a plain reload that recovers.

### Writes in Slack

Clicking a digest button, posting or reacting is a write. Do it only in Scratch, and only
to drive the app flow the task tests. Do not post as the user in a channel, and do not
send a DM to a person, unless the user asks. Do not delete messages: list the test
messages' permalinks in the report, and the user decides.

## 4. Cross-check against the app's record

The screen is one channel. Check a second one that does not pass through the browser:

- The message as Slack stored it: `conversations.history` or `chat.getPermalink` with the
  bot token, from a short script that prints ids and booleans only, never the token.
- The app's structured log events for the step (for example the delivery event and its
  run id).
- The store: an exposure row for the reader after Slack accepted the message (SPEC
  section 5 step 9), or a feedback event after a button click (step 8).

Two channels that agree are evidence. One screenshot alone is not.

## 5. Report with evidence

State what you observed, not what should have happened: the surface, the action, the
result, and the evidence (a screenshot in context, the message permalink and `ts`, the
log event, the store row). "Opened the Downstream DM, clicked Snooze on the PN-31010
line, the line showed 'snoozed until Thu' and a feedback row with action snooze was
written" is a report. "Verified" is not.

If you could not verify (no sign-in, the surface is not built, the stack did not start),
say so plainly and give the user the fallback click path. Never downgrade to "tests
pass".

## 6. Clean up

Stop the background jobs you started, and only those. Delete throwaway scripts. Leave
Slack messages in place and list them for the user.

## When not to use this

- Ranking, extraction, ledger or absence logic: the replay tests and the eval cover it.
- Docs and comment-only changes.
- Every PR by reflex. Use it when the browser is the only faithful place to look, or when
  the user asks to see it work.

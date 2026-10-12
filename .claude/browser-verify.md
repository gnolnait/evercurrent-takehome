# Browser verification: surfaces of this project

The `browser-verify` skill (installed from the personal dev-kit) reads this file in its
step 0. It holds only what is specific to this repo.

## Surfaces

| Surface | Milestone | How to reach it |
| --- | --- | --- |
| Bot posts and DMs in the Scratch workspace | M5 (live mode). A smoke test has proven the bot token, the scopes and Socket Mode. | Slack web client |
| Seeded corpus (persona posts, threads, edits) | M5 (`digest seed`) | Slack web client |
| Web preview: digest, landing map, rationale, profile, open items | M4 | `http://localhost:<port>` |

Check `digest --help` before you start. A surface whose command is not there is not built.

## Start

- **Live bot.** Use the live-mode command from `digest --help` (M5 adds it) in a
  background job. Ready means the app's "connected" log event for Socket Mode.
- **Seeder.** Read `digest seed --help`. Use a pace or a day limit only if the command
  offers one (pacing is M5 stretch); otherwise seed the smallest world it accepts.
- **Web preview.** Add or reuse a `.claude/launch.json` entry and start it with
  `preview_start`.

## Sign in and tokens

- Slack: the user signs in. Their own Chrome is usually signed in to Scratch already, so
  Claude in Chrome is the default path. The in-app Browser pane needs the user to sign in
  there first.
- Web preview: local, no auth in v1.
- Tokens are in `.env` only (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`). App facts (app id,
  scopes, which token is which) are in `notes/slack-app.md`, which is gitignored.

## Where writes are allowed

Only the Scratch workspace. If Slack shows any other workspace, stop and tell the user.
Button clicks on Downstream's own messages are allowed for the flow under test. Do not
post as the user in a channel, and do not DM a person.

## Slack specifics

- Open a conversation at `https://app.slack.com/client/<team_id>/<channel_id>`.
  `auth.test` gives `team_id`; `conversations.list` gives channel ids. The bot's DM is
  under Apps, as Downstream.
- Seeded posts show a persona's name with an APP tag, because the bot posts them under
  `chat:write.customize`. That is expected. Their Slack time is the real seeding time; the
  world time is in the message metadata.
- Slack dialogs sometimes ignore a click by `ref`; click by coordinate from a fresh
  screenshot and confirm it took effect.

## The app's record (second channel)

- The stored message: `conversations.history`, or `conversations.replies` for a thread,
  with the bot token, from a short script that prints ids, block types and booleans only.
  `chat.getPermalink` gives only the link for the report.
- Structured log events for the step, such as the delivery event and its run id.
- Store rows: an exposure row for the reader after Slack accepted the message (SPEC
  section 5 step 9), or a feedback event after a button click (step 8).

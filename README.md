# Hermes Slack colleague mode

A Hermes platform plugin that makes the bundled Slack adapter behave more like a
colleague. It subclasses the bundled adapter and replaces the registered `slack`
platform while enabled. Do not enable it beside another plugin that overrides
`slack`.

## What it does

**Multiplayer-aware threads** (`solo_thread_follow`). Hermes keeps following a
thread without mentions while only one human is in it. Once a second human
writes there, or is mentioned there (including `@here`, `@channel` and user
groups), Hermes answers only when @mentioned. It never makes Hermes follow a
thread the bundled adapter would not follow (one where Hermes was mentioned,
posted, or has a session). DMs and free-response channels are unaffected.

**Context: inject the local conversation, reference everything else.**

| Injected (bounded) | Referenced only (IDs for the tools) |
| --- | --- |
| The current message, with forwards rendered inline, forwards of forwards included | Slack links and forward sources, with their threads |
| Forwarded files, through the bundled authenticated media pipeline | Files outside the current message |
| Thread context on first contact: the root plus the newest replies | Older thread messages ("N older messages are not shown") |
| Catch-up after a mention: everything since Hermes' last turn | Replies under backfilled channel messages |
| On a top-level mention: recent top-level channel messages | |
| One provenance line with IDs and permalink, with no API call per message | |

**Read-only Slack tools** in a `slack` toolset:

- `slack_read_thread`: opens a thread from a link or channel + ts, with forwards rendered recursively.
- `slack_read_channel`: reads recent top-level messages.
- `slack_list_channels`: lists channels and shows which ones Hermes can read.
- `slack_download_file`: saves a file locally for the file, document and vision tools.

The tools see exactly what the bot token sees. Registering them also switches
Hermes' Slack platform note from "no Slack access" to "has Slack tools". Set
`HERMES_SLACK_READ_TOOLS=0` in the gateway environment to skip them.

Access model: Slack's visibility is the boundary. Anything the bot token can
read is readable by every Hermes user, including other people's DMs with
Hermes. There is no per-requester check.

## Configuration

```yaml
plugins:
  enabled: [slack-context-forwarding]

slack:
  require_mention: true
  strict_mention: false          # must be false for solo_thread_follow
  thread_require_mention: false  # must be false for solo_thread_follow
  ignore_other_user_mentions: true

platforms:
  slack:
    extra:
      solo_thread_follow: true
      thread_context_max_messages: 30   # unset = bundled behaviour (oldest 30)
      history_backfill: true
      history_backfill_limit: 15
```

Scopes: the usual Hermes bot scopes, including `channels:history`,
`groups:history`, `files:read` and `users:read`. History works only in
channels the bot is a member of.

## Compatibility

Targets Hermes Agent 0.21.5–0.21.x (`requires_hermes: ">=0.21.5,<0.22.0"`). It
overrides private adapter methods (`_channel_gate_allows`,
`_fetch_thread_context`, `_conversations_replies_with_backoff`,
`_collect_inbound_media`, `_thread_context_line`). Treat each Hermes minor
release as a compatibility boundary: run the tests against it before widening
the range.

## Installation

Install a pinned commit so every instance runs identical code:

```bash
hermes plugins install https://github.com/hsmio/hermes-slack-context-forwarding.git \
  --enable --ref FULL_40_CHARACTER_COMMIT_SHA
hermes gateway restart
```

## Tests

From the Hermes installation root, so `gateway` and `plugins` import:

```bash
for t in forwarding context colleague scenarios; do python /path/to/plugin/tests/test_$t.py; done
```

`test_scenarios.py` reads as thread stories:

- one-on-one;
- a colleague joins untagged;
- you tag a colleague;
- you forget the tag;
- you rejoin after a long discussion.

## Removal

```bash
hermes plugins disable slack-context-forwarding
hermes gateway restart
```

## License

MIT — see `LICENSE`.

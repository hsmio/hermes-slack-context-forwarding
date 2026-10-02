# Hermes Slack Context & Forwarding

A standalone Hermes platform plugin that extends the bundled Slack adapter with:

- opt-in, bounded context from preceding **top-level** channel messages;
- native Slack forwarded-message text extraction without duplicate rendering;
- forwarded files routed through Hermes's existing authenticated Slack media pipeline;
- explicit file provenance so Hermes knows that a file belongs to the forwarded message.
- opt-out-free, bounded source-thread resolution for Slack message links and identifiable forwards.

It does not replace Slack setup or credentials. It subclasses the Slack adapter already shipped with Hermes and overrides the registered `slack` platform while enabled.

## Compatibility

This release targets **Hermes Agent 0.21.5–0.21.x** and declares:

```yaml
requires_hermes: ">=0.21.5,<0.22.0"
```

The plugin necessarily calls private Slack-adapter helpers because Hermes does not currently expose public extension hooks for inbound message normalization. Treat each new Hermes minor release as a compatibility boundary: update Hermes on a test instance, run the validation commands below, then widen the version constraint only after the Slack tests pass.

Do not enable this beside another plugin that overrides the `slack` platform registration.

## Recommended installation: pinned Git commit

Install the same immutable commit on every instance:

```bash
hermes plugins install https://github.com/hsmio/hermes-slack-context-forwarding.git \
  --enable \
  --ref FULL_40_CHARACTER_COMMIT_SHA
```

Pinning matters: it gives every Hermes instance identical code and prevents an unreviewed branch update from becoming active automatically.

For a private GitHub repository, configure `GITHUB_TOKEN` or `GH_TOKEN` in the target Hermes profile, authenticate `gh`, or use an existing Git credential helper/SSH agent. Hermes first attempts an anonymous clone and does not store credentials in the plugin Git configuration.

## Manual installation from the release archive

Extract the archive beneath the target profile's plugin directory:

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins/slack-context-forwarding"
tar -xzf hermes-slack-context-forwarding-0.3.0.tar.gz \
  --strip-components=1 \
  -C "$HERMES_HOME/plugins/slack-context-forwarding"
hermes plugins enable slack-context-forwarding
```

Git installation is preferable because Hermes records provenance and can update or remove the plugin cleanly.

## Configuration

Forwarded-message and linked-source handling are active whenever the plugin is enabled. Channel history is separately opt-in:

```bash
hermes config set platforms.slack.extra.history_backfill true
hermes config set platforms.slack.extra.history_backfill_limit 15
hermes config set platforms.slack.extra.linked_thread_max_messages 100
hermes gateway restart
```

Equivalent YAML:

```yaml
plugins:
  enabled:
    - slack-context-forwarding

platforms:
  slack:
    extra:
      history_backfill: true
      history_backfill_limit: 15
      linked_thread_max_messages: 100
```

The history limit is clamped to `0`–`100`; the assembled channel-context block is capped at 12,000 characters. The complete quoted forwarded-content block (text and file markers together) is separately capped at 12,000 characters. This is message-count based, not a rolling time window.

Linked source threads fetch up to 100 messages by default (configurable `1`–`200`), with at most two distinct references and three API pages per thread per turn. Some Slack app classes are limited to 15 results and one `conversations.replies` call per minute; in that case retrieval can be partial or rate-limited. When a thread exceeds the limit or context budget, the block says `TRUNCATED`; it does not pretend to contain the whole thread.

### Slack scopes

The existing Hermes Slack bot credentials are reused. Channel history requires:

- `channels:history` for public channels;
- `groups:history` for private channels;
- bot membership in the channel.

Forwarded files remain subject to Slack visibility, OAuth scopes, workspace membership, and Hermes's normal MIME and size limits. The plugin does not bypass Slack authorization and does not fetch file URLs through a separate downloader.
Source-thread retrieval uses the same bot token and requires the corresponding history scope and bot membership in the **source** conversation. Cross-channel public-channel resolution also requires `channels:read` for `conversations.info` to verify visibility; if that check fails the plugin does not fetch the thread. An inaccessible reference cannot be resolved from its URL alone.

## Behavior

### Channel context

On an explicitly addressed top-level channel message, the plugin scans backward through the top-level channel timeline. It:

- excludes the triggering message;
- stops at Hermes's preceding top-level message;
- preserves `[unverified]` labels for unauthorized participants;
- does not run for DMs, thread replies, or commands;
- does **not** traverse replies inside preceding threads;
- treats all recovered text as quoted, untrusted background;
- fails open if Slack history retrieval fails.

### Forwarded messages

The plugin recognizes explicit Slack native shared-message attachments (`is_share`), deduplicates equivalent flat-text and Block Kit representations, and ignores ordinary pasted-message previews (including automatic self-unfurl echoes). Nested files are merged into the ordinary Slack event file list **after** the bundled adapter's authorization and routing checks, with provenance-aware deduplication; the more complete Slack file record wins. Previews without `is_share` are deliberately not treated as forwards.

Forwarded material is framed as untrusted quoted content. A forwarded file is also named inside that frame, for example:

```text
Forwarded file: report.pdf (application/pdf)
```

### Linked and forwarded source threads

When an authorized message contains a Slack message permalink in authored text/blocks or an explicit native forward with a source link/ID, the plugin reads the source thread with Slack `conversations.replies`. It labels the exact shared message (`SHARED MESSAGE`), the thread root, and other replies. A link to a root with replies retrieves that thread; a link to a reply retrieves its parent and surrounding replies. Plain link unfurl previews do not count as native forwards, but the permalink in the author's text does count as a link.

If you reply to an earlier message and **mention Hermes in your reply**, the plugin also checks that thread's root for a link or forward. The root need not have mentioned Hermes. It does not recursively follow links found inside a retrieved source thread. Forwards with copied text but no usable original channel/message identifier can still display the quote, but cannot retrieve the original thread.

To avoid exposing a private source thread to a different destination, cross-channel sources must be confirmed public by Slack's `conversations.info`; an ID beginning with `C` alone is **not** proof of public visibility (Slack Connect can retain a `C` ID for a private channel). The destination must also be verified as an unshared internal channel: a public internal source cannot be quoted into a Slack Connect channel with external participants. Private-channel and DM references are not fetched across channels, even if the bot can read both. These checks fail closed when channel metadata or required read scopes are unavailable. Retrieval failure and an unverified target are explicitly marked. When a shared reply is beyond the configured thread scan, a targeted API lookup attempts to retrieve the specific reply; surrounding content is still marked truncated. Retrieved text is untrusted reference data, not a new user request. Source-thread file attachments are rendered as Slack message metadata; this feature does not automatically download every file from a fetched thread.

## Validation

Run these commands from the repository root with the target Hermes installation:

```bash
python -m py_compile __init__.py
python tests/test_plugin.py
```

On Hermes 0.21.5, also run:

```bash
hermes plugins doctor . --ci
hermes plugins validate .
```

If `pytest` is installed, the same assertions can also be run with `python -m pytest -q tests`.

On deployments that use a dedicated Hermes virtual environment, invoke that environment's `hermes` and `python` executables.

After installation, test these Slack cases:

1. A top-level mention following ordinary channel conversation receives bounded context.
2. Replies inside an earlier thread do not appear in that context.
3. A native text forward appears exactly once.
4. A forwarded file is available to Hermes.
5. A forwarded message that itself contains a file exposes both its text and file.
6. An ordinary direct file upload still works.
7. A private/inaccessible forwarded file fails safely.
8. Paste a permalink to a root and to a reply: both show the source thread and mark the precise shared message.
9. Forward a source message with a usable original permalink/ID and inspect its source thread.
10. Post an unaddressed root containing a source link/forward, then reply with a mention: the source thread appears.
11. Cross-channel private references fail closed; long threads are labeled truncated.

## Security and operational notes

- Slack events, history, forwards, blocks, attachments, filenames, and file contents are untrusted input.
- No Slack token or credential is stored in this repository.
- This plugin executes in the Hermes process with the same authority as Hermes itself; review and pin it like any other Python plugin.
- Enabling it replaces the bundled `slack` platform registration for that profile. Disable it before testing a competing Slack override.

## Removal

```bash
hermes plugins disable slack-context-forwarding
hermes plugins remove slack-context-forwarding
hermes gateway restart
```

Disabling/removing the plugin does not delete Slack credentials or conversation data.

## Upstream context

The forwarded-message behavior adapts useful ideas from [NousResearch/hermes-agent#96384](https://github.com/NousResearch/hermes-agent/issues/96384) and [PR #108478](https://github.com/NousResearch/hermes-agent/pull/108478), with additional deduplication, bounded text, self-unfurl handling, installed-version compatibility, and forwarded-file provenance.

## License

MIT — see `LICENSE`.

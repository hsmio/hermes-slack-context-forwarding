# Hermes Slack Context & Forwarding

A standalone Hermes platform plugin that extends the bundled Slack adapter with:

- opt-in, bounded context from preceding **top-level** channel messages;
- native Slack forwarded-message text extraction without duplicate rendering;
- forwarded files routed through Hermes's existing authenticated Slack media pipeline;
- explicit file provenance so Hermes knows that a file belongs to the forwarded message.

It does not replace Slack setup or credentials. It subclasses the Slack adapter already shipped with Hermes and overrides the registered `slack` platform while enabled.

## Compatibility

This release targets **Hermes Agent 0.21.5–0.21.x** and declares:

```yaml
requires_hermes: ">=0.21.5,<0.22.0"
```

The plugin necessarily calls private Slack-adapter helpers because Hermes does not currently expose public extension hooks for inbound message normalization. Treat each new Hermes minor release as a compatibility boundary: update Hermes on a test instance, run the validation commands below, then widen the version constraint only after the Slack tests pass.

Do not enable this beside another plugin that overrides the `slack` platform registration.

## Recommended installation: pinned Git commit

Publish this directory as the root of a Git repository, then install the same immutable commit on every instance:

```bash
hermes plugins install https://github.com/OWNER/hermes-slack-context-forwarding.git \
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
tar -xzf hermes-slack-context-forwarding-0.2.0.tar.gz \
  --strip-components=1 \
  -C "$HERMES_HOME/plugins/slack-context-forwarding"
hermes plugins enable slack-context-forwarding
```

Git installation is preferable because Hermes records provenance and can update or remove the plugin cleanly.

## Configuration

Forwarded-message handling is active whenever the plugin is enabled. Channel history is separately opt-in:

```bash
hermes config set platforms.slack.extra.history_backfill true
hermes config set platforms.slack.extra.history_backfill_limit 15
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
```

The history limit is clamped to `0`–`100`; the assembled channel-context block is capped at 12,000 characters. The complete quoted forwarded-content block (text and file markers together) is separately capped at 12,000 characters. This is message-count based, not a rolling time window.

### Slack scopes

The existing Hermes Slack bot credentials are reused. Channel history requires:

- `channels:history` for public channels;
- `groups:history` for private channels;
- bot membership in the channel.

Forwarded files remain subject to Slack visibility, OAuth scopes, workspace membership, and Hermes's normal MIME and size limits. The plugin does not bypass Slack authorization and does not fetch file URLs through a separate downloader.

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

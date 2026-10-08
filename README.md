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
tar -xzf hermes-slack-context-forwarding-0.3.1.tar.gz \
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

Linked source threads fetch up to 100 messages by default (configurable `1`–`200`), with at most two distinct references and three API pages per thread per turn. All linked enrichment shares a **15-second deadline per turn**, starting before destination-root reference inspection and covering requester access checks, source-thread reads, pagination, and exact-target lookup. Up to two references run concurrently; output keeps reference order and completed source contexts survive the deadline. Uncompleted references are explicitly marked timeout/unavailable. This deadline applies **only to optional linked enrichment**, not current-message provenance, channel-history backfill, forwarded-media handling, or the whole turn. Some Slack app classes are limited to 15 results and one `conversations.replies` call per minute; in that case retrieval can be partial or rate-limited. When a thread exceeds the limit or context budget, the block says `TRUNCATED`; it does not pretend to contain the whole thread.

### Slack scopes

The existing Hermes Slack bot credentials are reused. Channel history requires:

- `channels:history` for public channels;
- `groups:history` for private channels;
- bot membership in the channel.

Forwarded files remain subject to Slack visibility, OAuth scopes, workspace membership, and Hermes's normal MIME and size limits. The plugin does not bypass Slack authorization and does not fetch file URLs through a separate downloader.
Source-thread retrieval uses the existing workspace-selected client and token; it adds no credentials or user-token flow. Slack must permit that token to call `conversations.replies` in the **source** conversation, with `channels:history`, `groups:history`, `im:history`, or `mpim:history` as appropriate, and bot membership where Slack requires it. Proving the requester's access does not grant the bot access: Slack can still reject a read because of token type, membership, scopes, workspace policies, or rate limits. An inaccessible reference cannot be resolved from its URL alone.

Cross-conversation requester access checks require the corresponding read scope for both `conversations.info` and `conversations.members`:

| Source conversation | Read scope |
| --- | --- |
| Public channel | `channels:read` |
| Private channel | `groups:read` |
| DM (IM) | `im:read` |
| Group DM (MPIM) | `mpim:read` |

Public-channel checks also call `users.info`, requiring `users:read`. No email scope is needed. Missing scopes, API errors (including rate limits), timeouts, invalid responses, and unknown requester identity fail closed **before** reading the cross-conversation source thread. Reinstall/re-authorize the Slack app separately if its scopes need changing; this plugin never updates Slack permissions itself.

See Slack's method references for [conversation members](https://docs.slack.dev/reference/methods/conversations.members/), [user information](https://docs.slack.dev/reference/methods/users.info/), and [thread replies](https://docs.slack.dev/reference/methods/conversations.replies/).

## Behavior

### Current-message provenance (0.3.1)

Every accepted non-command, non-synthetic inbound message receives a separate provenance block **prepended to `event.channel_context`**. It identifies the current channel/message timestamp and permalink, and separately labels the thread-root timestamp/permalink (the current message itself for a top-level post). This does not alter `event.text`; existing forwarded-content rendering remains unchanged. Commands and `_hermes_force_process` events bypass plugin enrichment entirely.

Permalinks come only from Slack `chat.getPermalink` using the existing channel/team-selected workspace client. Each unique current/root timestamp gets one attempt bounded to two seconds, with SDK retries disabled on a shallow client copy, leaving the shared client's retry policy unchanged. The existing Slack URL parser must confirm the returned channel and timestamp. No URL is guessed. Failure or malformed identity is shown as an unavailable status and the message still runs; exception logging contains the class only, never raw exception data. A root equal to the current message reuses its result, including failures.

The block distinguishes current feedback from its thread anchor and linked/forwarded source references: cite the actual feedback message, not the source link it quotes; ask only when required identity or link is unavailable. Historical channel/thread lines retain validated channel/timestamp IDs, preserving native `[thread parent]`, `[assistant]`, and `[unverified]` tags, without a permalink API call for every historical line.

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

**Sharing policy: the current requester may share anything they can access, anywhere.** The plugin does not classify sharing intent, inspect destination metadata, verify recipient access, or restrict destination types. DMs, private channels, and Slack Connect/shared destinations are allowed. Posting a link/forward is sufficient to invoke bounded source resolution; no extra confirmation or intent classification is added.

For a source different from the accepted inbound conversation:

- Take the requester from the **current** `MessageEvent.source.user_id`, falling back only to the authenticated current raw Slack event's `user` when source identity is absent. Conflicting transport identities fail closed. Forwarded-message authors, source authors, and the destination thread-root author never substitute for the current requester, including when a later mention discovers a reference on that root.
- Verify source ID and conversation type through `conversations.info`. A `C` prefix alone is not evidence of public visibility.
- Require explicit requester membership from `conversations.members` for private channels, IMs, MPIMs, guests, and external users. Bot membership alone is never sufficient.
- Allow public-channel nonmembers only when `users.info` confirms the same-workspace, active, nonbot, non-app, non-guest user, and source `context_team_id` or `team_id` matches the inbound workspace (all supplied source team IDs must agree). Known stranger/pending-invitation users require membership. Absent account flags or absent workspace metadata conservatively require membership instead, as do valid differing workspace IDs. Supplied source `context_team_id`/`team_id` and public user `team_id` must be nonempty strings: a supplied empty or nonstring value fails closed even for a member. Other malformed metadata or a failed API lookup is also a denial, not a membership fallback.

If source and destination are the **same conversation**, the plugin relies on the bundled Slack adapter's already-accepted inbound authorization/routing checks and does not make redundant access lookups. This is not an independent authorization mechanism for callers bypassing the bundled inbound pipeline.

Every optional linked-enrichment API request (destination-root discovery, access checks, source reads, and exact-target lookup) uses a shallow copy of the existing channel/team-selected workspace client, with SDK retries disabled without changing the shared client. Each API attempt is bounded to the smaller of two seconds and the remaining shared 15-second budget. Membership scans request 200 IDs per page, stop on a verified member, and allow at most ten pages; an exhausted scan, repeated cursor, or invalid response fails closed. A member beyond those pages may therefore be denied. In the longest public fallback path, access verification makes at most twelve API calls per source (one source lookup, one user lookup, ten membership pages), subject to the overall deadline. Requester access results, including denials and concurrent in-flight duplicates, are memoized only within the current turn by workspace/source channel/requester. No permission cache survives into a later turn: access is rechecked then. Timeout and caller cancellation cancel and drain owned tasks; no enrichment task runs detached. Exceptions are logged by class only, without raw Slack response/error data or secrets.

Retrieval failure and an unverified target are explicitly marked. When a shared reply is beyond the configured thread scan, a targeted API lookup attempts to retrieve the specific reply within the remaining deadline; surrounding content is still marked truncated. Existing source-thread message/page/character bounds remain unchanged. Cross-conversation source reads never begin before requester access is verified. Access verification and the subsequent bot read are not an atomic Slack operation; membership could change between them. Retrieved text is untrusted reference data, not a new user request. Source-thread file attachments are rendered as Slack message metadata; this feature does not automatically download every file from a fetched thread.

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
11. Share private-channel/DM sources as a member into a DM or shared destination: source resolution is allowed without destination/recipient checks.
12. Try the same sources as a nonmember: resolution fails closed, even when the bot or forwarded/root author is a member.
13. Test a public nonmember internal user, a guest, and an external user: only the verified ordinary internal user bypasses membership.
14. Verify missing read/user scopes fail closed and long threads remain labeled truncated.

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

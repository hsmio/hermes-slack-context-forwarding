# Changelog

## 0.4.0 — Colleague mode

Rewrite around one rule: inject the local conversation, reference everything else.

- Add `solo_thread_follow`: thread follow-ups continue while one human is in the thread and need a mention once a second human writes or is mentioned there (`@here`/`@channel`/user groups count as several). Restricts, never extends, the bundled follow rules. Free-response channels and DMs are unaffected; lookup failures stay silent.
- Add `thread_context_max_messages`: thread context is the root plus the newest replies (not Slack's oldest page), and the catch-up after a mention contains every message since Hermes' last turn except its own replies (up to 200). Both say how many messages were left out and how to read them. Threads are paged with per-page timeouts; very long threads keep the newest 1000 in memory.
- Add read-only tools in a `slack` toolset: `slack_read_thread`, `slack_read_channel`, `slack_list_channels`, `slack_download_file`. Output is size-bounded; the token is sent only to the bundled adapter's Slack CDN hosts; downloads are byte-capped and expire after 7 days.
- Render forwards of forwards inline with their source IDs; promote their files too.
- Replace automatic linked-thread fetching with a one-line list of referenced Slack messages (`channel`/`ts`/`thread_ts`) for the tools. Remove `linked_thread_max_messages`.
- Replace the provenance block (two `chat.getPermalink` calls and ~900 characters per message) with one line: IDs plus a permalink built from a cached `auth.test` workspace URL.
- Remove the requester source-access layer (membership scans, shared deadlines, single-flight tasks): Slack's own visibility is the access boundary.
- Thread and channel lines carry `[ts=…]` instead of `[Slack message: channel=… ts=…]`; backfilled messages show their reply count.
- Rename the adapter class to `SlackColleagueAdapter`. Replace the 86 mock-heavy tests with 38 behaviour tests in four files, including thread scenarios.

## 0.3.1 — Inbound message provenance

- Prepend current message and distinct thread-root channel/timestamp/permalink provenance to `event.channel_context`, independently of linked/forwarded sources and without changing command text.
- Obtain permalinks using the channel/team-selected workspace client with a two-second timeout per unique message, disabled retries, validated returned channel/timestamp, and fail-open status. Never synthesize a Slack URL or log raw exceptions.
- Preserve native thread role/trust tags while appending validated historical message IDs; channel-history lines also retain validated IDs without per-line permalink API calls.
- Skip commands and synthetic force-processing events. Add regressions for feedback source-link confusion, replies/later turns, workspace routing, missing/invalid IDs, malformed API results, errors, timeout, and historical trust tags.

## 0.3.0 — Linked and forwarded source threads

- Resolve authored Slack message permalinks and native forwards with a usable source reference through the existing workspace-scoped bot client.
- Fetch bounded, paginated source threads and distinguish their root from the specific shared message.
- On a later mention in a thread, inspect its root for a link or forward even if the root never addressed Hermes.
- Reject cross-channel private/DM source lookups by default (checking `conversations.info`, not channel-ID prefixes); report inaccessible, unverifiable, or truncated references explicitly.
- Try an exact lookup for a shared reply beyond the bounded thread scan and reuse the bundled thread cache when inspecting a parent on a later mention.
- Add regressions for reply links, root links, forward references, delayed mentions, pagination, spoofed hosts, and privacy boundaries.

## 0.2.0 — Hermes 0.21.5 compatibility

- Use the bundled adapter's new `_collect_inbound_media` seam after normal authorization/routing instead of wrapping the full inbound handler.
- Register Slack's passive dependency probe and active dependency installer separately, matching the bundled platform registration.
- Require explicit `is_share` for forwarded text and files, so pasted Slack permalink previews are not treated as forwards or downloaded.
- Enforce one aggregate 12,000-character bound across forwarded text and file markers, prioritizing file provenance.
- Add end-to-end media and channel-context regressions against the installed Hermes runtime.

## 0.1.0 — 2026-09-25

- Add opt-in bounded top-level Slack channel context on explicit mentions.
- Exclude replies inside preceding threads.
- Restore native forwarded-message text with representation-aware deduplication.
- Route nested forwarded files through Hermes's existing Slack media pipeline.
- Label forwarded files inside the quoted forward block to preserve provenance.

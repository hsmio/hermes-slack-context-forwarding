# Changelog

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

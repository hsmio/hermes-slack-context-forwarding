# Changelog

## Unreleased — Requester-based source access

- Replace private-source/shared-destination bans with the policy that the current requester may share anything they can access, anywhere; do not classify intent or check destination/recipient access.
- Use the accepted current event's source user (authenticated current raw user fallback), never forwarded or thread-root authors, including references discovered by later mentions. Reject conflicting/missing transport identity for cross-conversation retrieval.
- Verify source metadata and bounded paginated requester membership for private channels, DMs, group DMs, guests, and external users. Public nonmembers require `users.info` evidence of an active ordinary internal user and matching source/workspace metadata; otherwise require membership.
- Fail closed before source-thread reads on lookup errors, missing scopes, malformed responses, or exhausted membership scans. Bound access API attempts to two seconds and membership pagination to ten pages of 200 IDs; disable SDK retries on a client copy without mutating the existing workspace client.
- Preserve same-conversation accepted-inbound access, source-thread bounds, exact-target/truncation behavior, provenance, forwarded media, and command/synthetic bypass. Document required read/user scopes, token/API limitations, non-atomic permission checks, and conservative denials; add requester/access regressions.

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

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("slack_context_forwarding", ROOT / "__init__.py")
assert SPEC and SPEC.loader
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


def test_forwarded_text_is_deduplicated_across_slack_representations():
    attachment = {
        "is_share": True,
        "is_msg_unfurl": True,
        "author_id": "U_OTHER",
        "author_name": "Alice",
        "text": "`forward-test-indigo-917`",
        "fallback": "[September 25th, 2026] Alice: forward-test-indigo-917",
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "forward-test-indigo-917"}],
                    }
                ],
            }
        ],
    }

    rendered = PLUGIN._forwarded_attachment_text(attachment, bot_uid="U_BOT")

    assert rendered == "From Alice: `forward-test-indigo-917`"
    assert rendered.count("forward-test-indigo-917") == 1
    assert "September" not in rendered


def test_automatic_self_unfurl_is_not_treated_as_a_forward():
    attachment = {
        "is_msg_unfurl": True,
        "author_id": "U_BOT",
        "text": "Hermes's prior reply",
    }

    assert PLUGIN._forwarded_attachment_text(attachment, bot_uid="U_BOT") == ""


def test_explicit_share_is_kept_even_when_the_original_author_is_hermes():
    attachment = {
        "is_share": True,
        "is_msg_unfurl": True,
        "author_id": "U_BOT",
        "text": "Hermes's explicitly forwarded reply",
    }

    assert "explicitly forwarded reply" in PLUGIN._forwarded_attachment_text(
        attachment, bot_uid="U_BOT"
    )


def test_nested_forwarded_file_is_merged_and_complete_record_wins():
    event = {
        "files": [{"id": "F1", "name": "report.pdf"}],
        "attachments": [
            {
                "is_share": True,
                "is_msg_unfurl": True,
                "author_id": "U_OTHER",
                "files": [
                    {
                        "id": "F1",
                        "name": "report.pdf",
                        "mimetype": "application/pdf",
                        "size": 1234,
                        "url_private_download": "https://files.slack.com/report.pdf",
                    },
                    {
                        "id": "F2",
                        "name": "notes.txt",
                        "mimetype": "text/plain",
                        "url_private": "https://files.slack.com/notes.txt",
                    },
                ],
            }
        ],
    }

    merged = PLUGIN._merge_forwarded_files(event, bot_uid="U_BOT")

    assert [item["id"] for item in merged] == ["F1", "F2"]
    assert merged[0]["mimetype"] == "application/pdf"
    assert merged[0]["url_private_download"].endswith("report.pdf")


def test_non_message_attachment_does_not_promote_nested_files():
    event = {
        "attachments": [
            {
                "title": "ordinary link preview",
                "files": [{"id": "F_PRIVATE", "name": "should-not-promote.txt"}],
            }
        ]
    }

    assert PLUGIN._merge_forwarded_files(event, bot_uid="U_BOT") == []


def test_pasted_message_permalink_does_not_download_nested_files():
    event = {"attachments": [{
        "is_msg_unfurl": True, "author_id": "U_OTHER", "text": "A preview",
        "files": [{"id": "F_PRIVATE", "name": "private.txt"}],
    }]}
    assert PLUGIN._merge_forwarded_files(event, bot_uid="U_BOT") == []
    assert PLUGIN._forwarded_attachment_text(event["attachments"][0], bot_uid="U_BOT") == ""


def test_combined_forwarded_context_has_a_single_aggregate_bound():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    event = MessageEvent(text="question", raw_message={"attachments": [
        {"is_share": True, "author_id": "U_OTHER", "text": "x" * 9000},
        {"is_share": True, "author_id": "U_OTHER", "text": "y" * 9000},
    ]}, metadata={"slack_team_id": "T_TEST"})
    with patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock):
        asyncio.run(adapter.handle_message(event))
    assert len(event.text) <= PLUGIN._FORWARDED_MAX_CHARS + len("question\n\n")


def test_new_media_seam_promotes_nested_file_after_routing():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    original = {
        "attachments": [{
            "is_share": True, "is_msg_unfurl": True, "author_id": "U_OTHER",
            "files": [{"id": "F1", "name": "report.pdf", "mimetype": "application/pdf"}],
        }],
        "files": [{"id": "F1", "name": "report.pdf"}],
    }
    media = AsyncMock(return_value=(["cached.pdf"], ["application/pdf"], [False], "question"))
    with patch.object(PLUGIN.bundled_slack.SlackAdapter, "_collect_inbound_media", media):
        result = asyncio.run(adapter._collect_inbound_media(
            original, "C_TEST", "T_TEST", "question", [], []))
    assert result[0] == ["cached.pdf"]
    delivered = media.await_args.args[0]
    assert [f["id"] for f in delivered["files"]] == ["F1"]
    assert delivered["files"][0]["mimetype"] == "application/pdf"


def test_forwarded_document_uses_bundled_authenticated_media_pipeline():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    event = {"attachments": [{
        "is_share": True, "is_msg_unfurl": True, "author_id": "U_OTHER",
        "files": [{
            "id": "F1", "name": "note.txt", "mimetype": "text/plain", "size": 7,
            "url_private_download": "https://files.slack.com/note.txt",
        }],
    }]}
    with (
        patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock, return_value=b"hello!\n") as download,
        patch.object(PLUGIN.bundled_slack, "cache_document_from_bytes_async", new_callable=AsyncMock, return_value="/cache/note.txt"),
    ):
        urls, types, inlined, text = asyncio.run(adapter._collect_inbound_media(
            event, "C_TEST", "T_TEST", "question", [], []))
    download.assert_awaited_once_with("https://files.slack.com/note.txt", team_id="T_TEST")
    assert urls == ["/cache/note.txt"]
    assert types == ["text/plain"]
    assert inlined == [True]
    assert "[Content of note.txt]:\nhello!" in text


def test_direct_file_still_flows_without_forwarded_attachments():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    event = {"files": [{
        "id": "F_DIRECT", "name": "note.txt", "mimetype": "text/plain", "size": 7,
        "url_private_download": "https://files.slack.com/note.txt",
    }]}
    with (
        patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock, return_value=b"hello!\n") as download,
        patch.object(PLUGIN.bundled_slack, "cache_document_from_bytes_async", new_callable=AsyncMock, return_value="/cache/note.txt"),
    ):
        urls, _, _, _ = asyncio.run(adapter._collect_inbound_media(
            event, "C_TEST", "T_TEST", "question", [], []))
    download.assert_awaited_once()
    assert urls == ["/cache/note.txt"]


def test_forwarded_text_and_file_provenance_in_message_event():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    raw = {"attachments": [{
        "is_share": True, "is_msg_unfurl": True, "author_id": "U_OTHER",
        "text": "note", "files": [{"id": "F1", "name": "report.pdf", "mimetype": "application/pdf"}],
    }]}
    event = MessageEvent(text="What is attached?", raw_message=raw, metadata={"slack_team_id": "T_TEST"})
    with patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock) as parent:
        asyncio.run(adapter.handle_message(event))
    parent.assert_awaited_once()
    assert "From U_OTHER: note" in event.text
    assert "Forwarded file: report.pdf (application/pdf)" in event.text


def test_channel_backfill_is_bounded_and_excludes_prior_thread_replies():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={
        "history_backfill": True, "history_backfill_limit": 15,
    }))
    adapter._bot_user_id = "U_BOT"
    client = type("Client", (), {})()
    client.conversations_history = AsyncMock(return_value={"messages": [
        {"ts": "9", "user": "U_HUMAN", "text": "latest top-level"},
        {"ts": "8", "thread_ts": "3", "user": "U_HUMAN", "text": "earlier thread reply"},
        {"ts": "7", "user": "U_BOT", "text": "prior bot turn"},
        {"ts": "6", "user": "U_HUMAN", "text": "too old"},
    ]})
    with (
        patch.object(adapter, "_get_client", return_value=client),
        patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
        patch.object(adapter, "_is_sender_authorized", return_value=True),
    ):
        context = asyncio.run(adapter._fetch_channel_history_context(
            channel_id="C_TEST", current_ts="10", team_id="T_TEST"))
    client.conversations_history.assert_awaited_once_with(
        channel="C_TEST", latest="10", inclusive=False, limit=15)
    assert "latest top-level" in context
    assert "earlier thread reply" not in context
    assert "too old" not in context


def test_history_does_not_fetch_for_thread_reply_or_command():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={
        "history_backfill": True,
    }))
    adapter._bot_user_id = "U_BOT"
    for message_type, thread_ts in ((MessageType.TEXT, "1"), (MessageType.COMMAND, "")):
        event = MessageEvent(
            text="hello", message_type=message_type, raw_message={
                "channel": "C_TEST", "ts": "2", "thread_ts": thread_ts,
                "text": "<@U_BOT> hello",
            }, metadata={"slack_team_id": "T_TEST"},
        )
        assert not adapter._eligible_for_history_backfill(event)


def test_linked_reply_fetches_thread_and_marks_exact_target():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    client = type("Client", (), {})()
    client.conversations_info = AsyncMock(side_effect=lambda channel: {
        "ok": True, "channel": {"id": channel, "is_private": False, "is_channel": True,
                                "is_shared": False, "is_ext_shared": False}})
    client.conversations_replies = AsyncMock(side_effect=[
        {"messages": [
            {"ts": "1234567890.000001", "user": "U_A", "text": "Root"},
            {"ts": "1234567891.000002", "thread_ts": "1234567890.000001", "user": "U_B", "text": "First"},
        ], "has_more": True, "response_metadata": {"next_cursor": "page2"}},
        {"messages": [
            {"ts": "1234567892.000003", "thread_ts": "1234567890.000001", "user": "U_C", "text": "Target"},
        ], "has_more": False},
    ])
    raw = {"channel": "C_HOME", "ts": "1234567893.000004", "text":
           "Look <https://acme.slack.com/archives/CSOURCE/p1234567892000003?thread_ts=1234567890.000001&cid=CSOURCE|here>"}
    event = MessageEvent(text=raw["text"], raw_message=raw,
                         metadata={"slack_team_id": "T_TEST"})
    with (
        patch.object(adapter, "_get_client", return_value=client),
        patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, side_effect=lambda user, **kw: user),
        patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock),
    ):
        asyncio.run(adapter.handle_message(event))
    assert client.conversations_replies.await_count == 2
    assert client.conversations_replies.await_args_list[0].kwargs["ts"] == "1234567890.000001"
    assert "Thread root" in event.channel_context
    assert "First" in event.channel_context
    assert "SHARED MESSAGE" in event.channel_context
    assert "Target" in event.channel_context


def test_later_mention_resolves_link_on_current_thread_root():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    client = type("Client", (), {})()
    client.conversations_info = AsyncMock(side_effect=lambda channel: {
        "ok": True, "channel": {"id": channel, "is_private": False, "is_channel": True,
                                "is_shared": False, "is_ext_shared": False}})
    client.conversations_replies = AsyncMock(side_effect=[
        {"messages": [{"ts": "1234567890.000001", "text":
            "See https://acme.slack.com/archives/CSOURCE/p1234567892000003"}]},
        {"messages": [
            {"ts": "1234567892.000003", "user": "U_A", "text": "Original source"},
        ], "has_more": False},
    ])
    event = MessageEvent(text="What do you think?", raw_message={
        "channel": "C_HOME", "ts": "1234567894.000005",
        "thread_ts": "1234567890.000001", "text": "<@U_BOT> What do you think?",
    }, metadata={"slack_team_id": "T_TEST"})
    with (
        patch.object(adapter, "_get_client", return_value=client),
        patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
        patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock),
    ):
        asyncio.run(adapter.handle_message(event))
    assert "Original source" in event.channel_context
    assert client.conversations_replies.await_args_list[0].kwargs["ts"] == "1234567890.000001"


def test_forward_reference_and_preview_are_distinct():
    forward = {"is_share": True, "channel_id": "CSOURCE", "message_ts": "1234567892.000003",
               "text": "Forwarded text"}
    preview = {"is_msg_unfurl": True, "channel_id": "C_PRIVATE", "message_ts": "1234567892.000003",
               "text": "preview"}
    references = PLUGIN._message_references({"attachments": [forward, preview]})
    assert [(r.channel, r.target_ts) for r in references] == [("CSOURCE", "1234567892.000003")]


def test_conflicting_forward_source_metadata_is_not_fetched():
    attachment = {"is_share": True,
                  "from_url": "https://acme.slack.com/archives/CSOURCE/p1234567892000003",
                  "channel_id": "COTHER", "message_ts": "1234567892.000003"}
    assert PLUGIN._message_references({"attachments": [attachment]}) == []
    assert PLUGIN._message_references({"attachments": [{"is_share": True,
        "message": {"channel": "CSOURCE", "ts": "1234567892.000003"}}]}) == []


def test_root_forward_reference_is_found_from_attached_source_url():
    refs = PLUGIN._message_references({"attachments": [{
        "is_share": True, "from_url": "https://acme.slack.com/archives/CSOURCE/p1234567890000001",
        "text": "A forwarded root without inline link",
    }]})
    assert refs == [PLUGIN._Reference("CSOURCE", "1234567890.000001")]


def test_block_message_mention_without_url_is_resolved():
    refs = PLUGIN._message_references({"blocks": [{"type": "rich_text", "elements": [{
        "type": "rich_text_section", "elements": [{
            "type": "message_mention", "channel_id": "CSOURCE",
            "message_ts": "1234567892.000003",
        }],
    }]}]})
    assert refs == [PLUGIN._Reference("CSOURCE", "1234567892.000003")]


def test_unfurl_text_is_not_a_reference_and_fake_slack_hostname_is_rejected():
    refs = PLUGIN._message_references({
        "text": "https://acme.slack.com.evil.example/archives/CSOURCE/p1234567892000003",
        "attachments": [{"is_msg_unfurl": True,
            "from_url": "https://acme.slack.com/archives/CSOURCE/p1234567892000003"}],
    })
    assert refs == []


def test_no_thread_reference_following_in_fetched_content():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    client = type("Client", (), {})()
    client.conversations_info = AsyncMock(side_effect=lambda channel: {
        "ok": True, "channel": {"id": channel, "is_private": False, "is_channel": True,
                                "is_shared": False, "is_ext_shared": False}})
    client.conversations_replies = AsyncMock(return_value={"messages": [{
        "ts": "1234567892.000003", "text":
        "https://acme.slack.com/archives/COTHER/p1234567894000005",
    }], "has_more": False})
    event = MessageEvent(text="link", raw_message={"channel": "CHOME", "ts": "1234567893.000004",
        "text": "https://acme.slack.com/archives/CSOURCE/p1234567892000003"},
        metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    client.conversations_replies.assert_awaited_once()


def test_cross_channel_private_reference_never_calls_slack():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    event = MessageEvent(text="see link", raw_message={"channel": "C_HOME", "ts": "1234567893.000004",
        "text": "https://acme.slack.com/archives/GSECRET/p1234567892000003"},
        metadata={"slack_team_id": "T_TEST"})
    with (
        patch.object(adapter, "_get_client", side_effect=AssertionError("private lookup")),
        patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock),
    ):
        asyncio.run(adapter.handle_message(event))
    assert "private" in event.channel_context.lower()


def test_private_c_prefixed_slack_connect_source_fails_closed():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    client = SimpleNamespace(
        conversations_info=AsyncMock(side_effect=lambda channel: {
            "ok": True, "channel": {"id": channel, "is_channel": True,
                                    "is_private": channel == "CSOURCE",
                                    "is_shared": False, "is_ext_shared": False}}),
        conversations_replies=AsyncMock(side_effect=AssertionError("private read")),
    )
    event = MessageEvent(text="link", raw_message={"channel": "CHOME", "ts": "1234567893.000004",
        "text": "https://acme.slack.com/archives/CSOURCE/p1234567892000003"},
        metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert client.conversations_info.await_count == 2
    client.conversations_replies.assert_not_awaited()
    assert "not verified public" in event.channel_context


def test_public_source_is_not_quoted_into_slack_connect_destination():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = SimpleNamespace(conversations_info=AsyncMock(return_value={
        "ok": True, "channel": {"id": "CHOME", "is_channel": True,
                                "is_shared": True, "is_ext_shared": True}}),
        conversations_replies=AsyncMock(side_effect=AssertionError("shared destination read")))
    context = asyncio.run(_context_with_client(adapter, client))
    assert "destination is shared or unverified" in context
    client.conversations_replies.assert_not_awaited()


def test_missing_conversation_info_scope_fails_closed():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = SimpleNamespace(conversations_info=AsyncMock(side_effect=PermissionError("missing_scope")),
                             conversations_replies=AsyncMock(side_effect=AssertionError("read")))
    context = asyncio.run(_context_with_client(adapter, client))
    assert "privacy could not be verified" in context
    client.conversations_replies.assert_not_awaited()


async def _context_with_client(adapter, client):
    with patch.object(adapter, "_get_client", return_value=client):
        return await adapter._linked_thread_context(
            PLUGIN._Reference("CSOURCE", "1234567892.000003"),
            team_id="T_TEST", destination_channel="CHOME")


def test_reply_after_message_cap_is_verified_with_targeted_lookup():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={
        "linked_thread_max_messages": 2}))
    client = SimpleNamespace(
        conversations_info=AsyncMock(side_effect=lambda channel: {
            "ok": True, "channel": {"id": channel, "is_channel": True, "is_private": False,
                                    "is_shared": False, "is_ext_shared": False}}),
        conversations_replies=AsyncMock(side_effect=[
            {"messages": [
                {"ts": "1234567890.000001", "text": "Root"},
                {"ts": "1234567891.000002", "thread_ts": "1234567890.000001", "text": "Early"},
            ], "has_more": True, "response_metadata": {"next_cursor": "more"}},
            {"messages": [{"ts": "1234567899.000009", "thread_ts": "1234567890.000001",
                           "text": "Later shared reply"}]},
        ]),
    )
    with (patch.object(adapter, "_get_client", return_value=client),):
        context = asyncio.run(adapter._linked_thread_context(
            PLUGIN._Reference("CSOURCE", "1234567899.000009", "1234567890.000001"),
            team_id="T_TEST", destination_channel="CHOME"))
    assert "SHARED MESSAGE" in context and "Later shared reply" in context
    assert "TRUNCATED" in context
    assert client.conversations_replies.await_args_list[1].kwargs["oldest"] == "1234567899.000009"


def test_short_slack_pages_continue_until_exact_shared_reply():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={
        "linked_thread_max_messages": 40}))
    first = [{"ts": f"1234567890.{i:06d}", "thread_ts": "1234567890.000000",
              "text": f"Earlier {i}"} for i in range(15)]
    second = [{"ts": "1234567890.000015", "thread_ts": "1234567890.000000",
               "text": "Selected reply"}]
    client = SimpleNamespace(conversations_replies=AsyncMock(side_effect=[
        {"messages": first, "has_more": True,
         "response_metadata": {"next_cursor": "cursor-2"}},
        {"messages": second, "has_more": False},
    ]))
    with patch.object(adapter, "_get_client", return_value=client):
        messages, more = asyncio.run(adapter._source_thread(
            PLUGIN._Reference("CHOME", "1234567890.000015", "1234567890.000000"),
            "T_TEST"))
    assert len(messages) == 16
    assert not more
    assert client.conversations_replies.await_args_list[1].kwargs["cursor"] == "cursor-2"


def test_later_mention_uses_native_thread_cache_without_extra_root_fetch():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    root = {"ts": "1234567890.000001", "text":
            "https://acme.slack.com/archives/CSOURCE/p1234567892000003"}
    key = adapter._thread_cache_key("CHOME", "1234567890.000001", "T_TEST")
    adapter._thread_context_cache[key] = SimpleNamespace(messages=[root])
    client = SimpleNamespace(
        conversations_info=AsyncMock(side_effect=lambda channel: {
            "ok": True, "channel": {"id": channel, "is_channel": True, "is_private": False,
                                    "is_shared": False, "is_ext_shared": False}}),
        conversations_replies=AsyncMock(return_value={"messages": [
            {"ts": "1234567892.000003", "text": "Shared source"}]}),
    )
    event = MessageEvent(text="read", raw_message={"channel": "CHOME",
        "ts": "1234567894.000005", "thread_ts": "1234567890.000001",
        "text": "<@U_BOT> read"}, metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    client.conversations_replies.assert_awaited_once()
    assert "Shared source" in event.channel_context


def test_later_mention_resolves_native_forward_on_root():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    root = {"ts": "1234567890.000001", "text": "", "attachments": [{
        "is_share": True, "from_url":
        "https://acme.slack.com/archives/CSOURCE/p1234567892000003",
        "text": "A forward with no top-level text",
    }]}
    key = adapter._thread_cache_key("CHOME", "1234567890.000001", "T_TEST")
    adapter._thread_context_cache[key] = SimpleNamespace(messages=[root])
    client = SimpleNamespace(
        conversations_info=AsyncMock(side_effect=lambda channel: {
            "ok": True, "channel": {"id": channel, "is_channel": True, "is_private": False,
                                    "is_shared": False, "is_ext_shared": False}}),
        conversations_replies=AsyncMock(return_value={"messages": [
            {"ts": "1234567892.000003", "text": "Forward's source thread"}]}),
    )
    event = MessageEvent(text="read", raw_message={"channel": "CHOME",
        "ts": "1234567894.000005", "thread_ts": "1234567890.000001",
        "text": "<@U_BOT> read"}, metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert "Forward's source thread" in event.channel_context


def test_root_audio_is_not_downloaded_when_inspecting_its_link():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    root = {"ts": "1234567890.000001", "text":
            "https://acme.slack.com/archives/CSOURCE/p1234567892000003",
            "files": [{"id": "FAUDIO", "mimetype": "audio/mp4"}]}
    key = adapter._thread_cache_key("CHOME", "1234567890.000001", "T_TEST")
    adapter._thread_context_cache[key] = SimpleNamespace(messages=[root])
    client = SimpleNamespace(
        conversations_info=AsyncMock(side_effect=lambda channel: {
            "ok": True, "channel": {"id": channel, "is_channel": True, "is_private": False,
                                    "is_shared": False, "is_ext_shared": False}}),
        conversations_replies=AsyncMock(return_value={"messages": [
            {"ts": "1234567892.000003", "text": "Source text"}]}),
    )
    event = MessageEvent(text="read", raw_message={"channel": "CHOME",
        "ts": "1234567894.000005", "thread_ts": "1234567890.000001",
        "text": "<@U_BOT> read"}, metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_download_slack_file_bytes", new_callable=AsyncMock) as download,
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    download.assert_not_awaited()
    assert "Source text" in event.channel_context


def test_incomplete_thread_is_labeled_and_inaccessible_source_fails_safe():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={
        "linked_thread_max_messages": 2,
    }))
    adapter._bot_user_id = "U_BOT"
    client = type("Client", (), {})()
    client.conversations_info = AsyncMock(side_effect=lambda channel: {
        "ok": True, "channel": {"id": channel, "is_private": False, "is_channel": True,
                                "is_shared": False, "is_ext_shared": False}})
    client.conversations_replies = AsyncMock(return_value={"messages": [
        {"ts": "1234567890.000001", "text": "Root"},
        {"ts": "1234567891.000002", "text": "First"},
    ], "has_more": True, "response_metadata": {"next_cursor": "more"}})
    event = MessageEvent(text="read", raw_message={"channel": "C_HOME", "ts": "1234567893.000004",
        "text": "https://acme.slack.com/archives/CSOURCE/p1234567891000002?thread_ts=1234567890.000001"},
        metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert "TRUNCATED" in event.channel_context
    assert "SHARED MESSAGE" in event.channel_context
    client.conversations_replies = AsyncMock(side_effect=PermissionError("no access"))
    event.channel_context = None
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert "could not" in event.channel_context.lower()


def _provenance_fixture(ts="1234567893.000004", root="", text="feedback"):
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = SimpleNamespace(retry_handlers=[object()], chat_getPermalink=AsyncMock(
        side_effect=lambda channel, message_ts: {"ok": True, "permalink":
            f"https://acme.slack.com/archives/{channel}/p{message_ts.replace('.', '')}"}))
    event = MessageEvent(text=text, message_id=ts, raw_message={
        "channel": "CHOME", "ts": ts, "thread_ts": root, "text": text},
        metadata={"slack_team_id": "TWORK"}, channel_context="existing native context")
    return adapter, client, event


def _deliver_provenance(adapter, client, event):
    with (patch.object(adapter, "_get_client", return_value=client) as select,
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock) as parent):
        asyncio.run(adapter.handle_message(event))
    parent.assert_awaited_once_with(event)
    return select


def test_current_feedback_link_is_not_the_linked_source():
    source = "https://acme.slack.com/archives/CHOME/p1234567890000001"
    adapter, client, event = _provenance_fixture(text=f"Record this feedback; source {source}")
    with patch.object(adapter, "_linked_thread_context", new_callable=AsyncMock, return_value="linked source"):
        select = _deliver_provenance(adapter, client, event)
    assert event.text == f"Record this feedback; source {source}"
    assert event.channel_context.startswith("[Current Slack message provenance")
    assert "Current message: channel=CHOME ts=1234567893.000004" in event.channel_context
    assert "https://acme.slack.com/archives/CHOME/p1234567893000004" in event.channel_context
    assert "actual feedback message" in event.channel_context
    assert "linked/forwarded" in event.channel_context
    assert "ask only" in event.channel_context
    assert "existing native context" in event.channel_context
    select.assert_called_with("CHOME", team_id="TWORK")
    client.chat_getPermalink.assert_awaited_once_with(channel="CHOME", message_ts=event.message_id)
    assert len(client.retry_handlers) == 1  # shared workspace client was not mutated


def test_reply_and_later_turn_have_separate_current_and_root_links():
    for ts in ("1234567893.000004", "1234567894.000005"):
        adapter, client, event = _provenance_fixture(ts=ts, root="1234567890.000001")
        _deliver_provenance(adapter, client, event)
        assert f"Current message: channel=CHOME ts={ts}" in event.channel_context
        assert "Thread root: channel=CHOME ts=1234567890.000001" in event.channel_context
        assert f"/p{ts.replace('.', '')}" in event.channel_context
        assert "/p1234567890000001" in event.channel_context
        assert client.chat_getPermalink.await_count == 2


def test_top_level_root_reuses_current_permalink():
    adapter, client, event = _provenance_fixture(root="1234567893.000004")
    _deliver_provenance(adapter, client, event)
    assert "Thread root: channel=CHOME ts=1234567893.000004" in event.channel_context
    client.chat_getPermalink.assert_awaited_once()


def test_commands_and_synthetic_force_processing_are_untouched():
    for command in (True, False):
        adapter, client, event = _provenance_fixture(text="/reset")
        event.raw_message["attachments"] = [{"is_share": True, "text": "quoted"}]
        if command:
            event.message_type = MessageType.COMMAND
        else:
            event.raw_message["_hermes_force_process"] = True
        _deliver_provenance(adapter, client, event)
        assert event.text == "/reset"
        assert event.channel_context == "existing native context"
        client.chat_getPermalink.assert_not_awaited()


def test_permalink_failures_are_fail_open_and_safe_to_log():
    for failure in (PermissionError("SECRET_TOKEN"), TimeoutError("SECRET_TOKEN")):
        adapter, client, event = _provenance_fixture()
        client.chat_getPermalink.side_effect = failure
        with patch.object(PLUGIN.logger, "info") as log:
            _deliver_provenance(adapter, client, event)
        assert "permalink=unavailable" in event.channel_context
        assert type(failure).__name__ in event.channel_context
        assert "SECRET_TOKEN" not in event.channel_context
        assert "SECRET_TOKEN" not in str(log.call_args_list)
        client.chat_getPermalink.assert_awaited_once()


def test_permalink_attempt_disables_sdk_retries_without_mutating_workspace_client():
    adapter, _, event = _provenance_fixture()
    attempts = []
    class Client:
        retry_handlers = [object()]
        async def chat_getPermalink(self, **kwargs):
            attempts.append((list(self.retry_handlers), kwargs))
            raise ConnectionError("SECRET_TOKEN")
    client = Client()
    _deliver_provenance(adapter, client, event)
    assert attempts == [([], {"channel": "CHOME", "message_ts": "1234567893.000004"})]
    assert len(client.retry_handlers) == 1
    assert event.channel_context.count("permalink=unavailable (ConnectionError)") == 2


def test_permalink_timeout_is_bounded_without_retries():
    adapter, client, event = _provenance_fixture()
    async def hanging(**kwargs):
        await asyncio.sleep(60)
    client.chat_getPermalink.side_effect = hanging
    with patch.object(PLUGIN, "_PERMALINK_TIMEOUT_SECONDS", 0.01):
        _deliver_provenance(adapter, client, event)
    assert "TimeoutError" in event.channel_context
    client.chat_getPermalink.assert_awaited_once()


def test_permalink_response_must_match_validated_channel_and_timestamp():
    for response in (None, {"ok": False, "error": "SECRET"}, {"ok": True},
                     {"ok": True, "permalink": "https://evil.example/archives/CHOME/p1234567893000004"},
                     {"ok": True, "permalink": "https://acme.slack.com/archives/COTHER/p1234567893000004"},
                     {"ok": True, "permalink": "https://acme.slack.com/archives/CHOME/p1234567890000001"}):
        adapter, client, event = _provenance_fixture()
        client.chat_getPermalink.side_effect = None
        client.chat_getPermalink.return_value = response
        _deliver_provenance(adapter, client, event)
        assert "permalink=unavailable" in event.channel_context
        assert "https://" not in event.channel_context
        client.chat_getPermalink.assert_awaited_once()


def test_invalid_inbound_identifiers_never_call_permalink_api():
    for key, value in (("channel", "C_BAD"), ("ts", "not-a-ts"), ("thread_ts", "bad-root")):
        adapter, client, event = _provenance_fixture()
        event.raw_message[key] = value
        _deliver_provenance(adapter, client, event)
        assert "invalid" in event.channel_context.lower()
        # Bad root does not prevent valid current-message provenance.
        assert client.chat_getPermalink.await_count == (1 if key == "thread_ts" else 0)


def test_metadata_fallback_selects_correct_workspace():
    adapter, client, event = _provenance_fixture()
    event.raw_message = {}
    event.metadata.update(slack_channel_id="CHOME", slack_thread_ts="1234567890.000001")
    select = _deliver_provenance(adapter, client, event)
    assert "Thread root: channel=CHOME ts=1234567890.000001" in event.channel_context
    assert all(call.args == ("CHOME",) and call.kwargs == {"team_id": "TWORK"}
               for call in select.call_args_list)


def test_native_thread_line_keeps_trust_tags_and_adds_validated_ids():
    adapter, client, _ = _provenance_fixture()
    adapter._bot_user_id = "UBOT"
    with (patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
          patch.object(adapter, "_is_sender_authorized", return_value=False)):
        for msg, parent, tag in (({"user": "UHUMAN"}, True, "[thread parent] [unverified]"),
                                 ({"user": "UBOT", "bot_id": "B1"}, False, "[assistant]")):
            msg["ts"] = "1234567890.000001"
            line = asyncio.run(adapter._thread_context_line(msg, "quoted", parent, "TWORK", "CHOME"))
            assert line.startswith(tag)
            assert "channel=CHOME ts=1234567890.000001" in line
        line = asyncio.run(adapter._thread_context_line({"ts": "bad"}, "quoted", False, "TWORK", "CHOME"))
        assert "ts=bad" not in line
    client.chat_getPermalink.assert_not_awaited()


def test_channel_history_keeps_trust_tags_and_validated_ids_without_permalink_calls():
    adapter, client, _ = _provenance_fixture()
    client.conversations_history = AsyncMock(return_value={"messages": [
        {"ts": "1234567890.000001", "user": "UHUMAN", "text": "historic"}]})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
          patch.object(adapter, "_is_sender_authorized", return_value=False)):
        context = asyncio.run(adapter._fetch_channel_history_context(
            channel_id="CHOME", current_ts="1234567893.000004", team_id="TWORK"))
    assert "[unverified] Alice:" in context
    assert "historic" in context
    assert "channel=CHOME ts=1234567890.000001" in context
    client.chat_getPermalink.assert_not_awaited()


def test_oversized_channel_history_preserves_identity_before_truncated_body():
    adapter, client, _ = _provenance_fixture()
    identity = "[Slack message: channel=CHOME ts=1234567890.000001]"
    client.conversations_history = AsyncMock(return_value={"messages": [
        {"ts": "1234567890.000001", "user": "UHUMAN", "text": "x" * 13000}]})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
          patch.object(adapter, "_is_sender_authorized", return_value=False)):
        context = asyncio.run(adapter._fetch_channel_history_context(
            channel_id="CHOME", current_ts="1234567893.000004", team_id="TWORK"))
    assert len(context) == PLUGIN._CONTEXT_MAX_CHARS == 12000
    assert context.startswith(PLUGIN._CONTEXT_HEADER + "\n[unverified] Alice:")
    assert context.endswith("...\n" + PLUGIN._CONTEXT_FOOTER)
    assert context.count(identity) == 1
    assert context.index(identity) < context.index("x" * 100)
    client.chat_getPermalink.assert_not_awaited()


def test_near_budget_channel_history_keeps_all_retained_identities():
    adapter, client, _ = _provenance_fixture()
    timestamps = ("1234567892.000003", "1234567891.000002", "1234567890.000001")
    identities = [f"[Slack message: channel=CHOME ts={ts}]" for ts in timestamps]
    body_budget = (PLUGIN._CONTEXT_MAX_CHARS - len(PLUGIN._CONTEXT_HEADER)
                   - len(PLUGIN._CONTEXT_FOOTER) - 2)
    # Two complete records exactly fill the budget; the older third must be dropped.
    prefix_chars = len("[unverified] Alice: ") + len(identities[0]) + 1
    newest_body = "x" * 100
    older_body = "y" * (body_budget - 2 * prefix_chars - len(newest_body) - 1)
    client.conversations_history = AsyncMock(return_value={"messages": [
        {"ts": ts, "user": "UHUMAN", "text": text}
        for ts, text in zip(timestamps, (newest_body, older_body, "excluded oldest"))]})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
          patch.object(adapter, "_is_sender_authorized", return_value=False)):
        context = asyncio.run(adapter._fetch_channel_history_context(
            channel_id="CHOME", current_ts="1234567893.000004", team_id="TWORK"))
    assert len(context) == PLUGIN._CONTEXT_MAX_CHARS == 12000
    lines = context[len(PLUGIN._CONTEXT_HEADER) + 1:-(len(PLUGIN._CONTEXT_FOOTER) + 1)].splitlines()
    assert len(lines) == 2
    for line, identity, body in zip(lines, reversed(identities[:2]), (older_body, newest_body)):
        assert line.startswith("[unverified] Alice:")
        assert line.count(identity) == 1
        assert line.index(identity) < line.index(body)
        assert line.endswith(body)
    assert identities[2] not in context
    assert "excluded oldest" not in context
    client.chat_getPermalink.assert_not_awaited()


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"plugin_smoke=passed tests={len(tests)}")

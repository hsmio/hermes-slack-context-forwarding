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
    ]}, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
    event = MessageEvent(text="What is attached?", raw_message=raw, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
            }, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"},
        )
        assert not adapter._eligible_for_history_backfill(event)


def test_linked_reply_fetches_thread_and_marks_exact_target():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = "U_BOT"
    client = type("Client", (), {})()
    client.conversations_info = AsyncMock(side_effect=lambda channel: {
        "ok": True, "channel": {"id": channel, "is_private": False, "is_channel": True,
                                "is_shared": False, "is_ext_shared": False}})
    client.conversations_members = AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]})
    client.users_info = _access_client().users_info
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
                         source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
    client.conversations_members = AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]})
    client.users_info = _access_client().users_info
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
    }, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
    client.conversations_members = AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]})
    client.users_info = _access_client().users_info
    client.conversations_replies = AsyncMock(return_value={"messages": [{
        "ts": "1234567892.000003", "text":
        "https://acme.slack.com/archives/COTHER/p1234567894000005",
    }], "has_more": False})
    event = MessageEvent(text="link", raw_message={"channel": "CHOME", "ts": "1234567893.000004",
        "text": "https://acme.slack.com/archives/CSOURCE/p1234567892000003"},
        source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    client.conversations_replies.assert_awaited_once()


def test_cross_channel_private_nonmember_never_reads_source():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client("GSECRET")
    client.conversations_members.return_value = {"ok": True, "members": ["UBOT", "UAUTHOR"]}
    with patch.object(adapter, "_get_client", return_value=client):
        context = asyncio.run(adapter._linked_thread_context(
            PLUGIN._Reference("GSECRET", "1234567892.000003"),
            team_id="T_TEST", destination_channel="DHOME", requester="UREQUESTER"))
    assert "not fetched" in context
    client.conversations_replies.assert_not_awaited()
    client.conversations_info.assert_awaited_once_with(channel="GSECRET")


def test_private_c_prefixed_slack_connect_member_can_share_source():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client(is_shared=True, is_ext_shared=True)
    assert "Verified source" in asyncio.run(_context_with_client(adapter, client))
    client.conversations_info.assert_awaited_once_with(channel="CSOURCE")
    client.conversations_members.assert_awaited_once()


def test_public_source_can_be_shared_into_slack_connect_destination_without_lookup():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client(is_private=False, context_team_id="T_TEST")
    # CHOME may be shared, private, or an IM: destination metadata is never consulted.
    assert "Verified source" in asyncio.run(_context_with_client(adapter, client))
    client.conversations_info.assert_awaited_once_with(channel="CSOURCE")
    client.conversations_members.assert_not_awaited()


def test_missing_conversation_info_scope_fails_closed():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client()
    client.conversations_info.side_effect = PermissionError("missing_scope SECRET")
    with patch.object(PLUGIN.logger, "info") as log:
        context = asyncio.run(_context_with_client(adapter, client))
    assert "access could not be verified" in context
    assert "SECRET" not in str(log.call_args_list) + context
    client.conversations_replies.assert_not_awaited()


async def _context_with_client(adapter, client):
    with patch.object(adapter, "_get_client", return_value=client):
        return await adapter._linked_thread_context(
            PLUGIN._Reference("CSOURCE", "1234567892.000003"),
            team_id="T_TEST", destination_channel="CHOME", requester="UREQUESTER")


def test_reply_after_message_cap_is_verified_with_targeted_lookup():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={
        "linked_thread_max_messages": 2}))
    client = SimpleNamespace(
        conversations_info=AsyncMock(side_effect=lambda channel: {
            "ok": True, "channel": {"id": channel, "is_channel": True, "is_private": False,
                                    "is_shared": False, "is_ext_shared": False}}),
        conversations_members=AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]}),
        users_info=_access_client().users_info,
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
            team_id="T_TEST", destination_channel="CHOME", requester="UREQUESTER"))
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
        conversations_members=AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]}),
        users_info=_access_client().users_info,
        conversations_replies=AsyncMock(return_value={"messages": [
            {"ts": "1234567892.000003", "text": "Shared source"}]}),
    )
    event = MessageEvent(text="read", raw_message={"channel": "CHOME",
        "ts": "1234567894.000005", "thread_ts": "1234567890.000001",
        "text": "<@U_BOT> read"}, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
        conversations_members=AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]}),
        users_info=_access_client().users_info,
        conversations_replies=AsyncMock(return_value={"messages": [
            {"ts": "1234567892.000003", "text": "Forward's source thread"}]}),
    )
    event = MessageEvent(text="read", raw_message={"channel": "CHOME",
        "ts": "1234567894.000005", "thread_ts": "1234567890.000001",
        "text": "<@U_BOT> read"}, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
        conversations_members=AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]}),
        users_info=_access_client().users_info,
        conversations_replies=AsyncMock(return_value={"messages": [
            {"ts": "1234567892.000003", "text": "Source text"}]}),
    )
    event = MessageEvent(text="read", raw_message={"channel": "CHOME",
        "ts": "1234567894.000005", "thread_ts": "1234567890.000001",
        "text": "<@U_BOT> read"}, source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
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
    client.conversations_members = AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]})
    client.users_info = _access_client().users_info
    client.conversations_replies = AsyncMock(return_value={"messages": [
        {"ts": "1234567890.000001", "text": "Root"},
        {"ts": "1234567891.000002", "text": "First"},
    ], "has_more": True, "response_metadata": {"next_cursor": "more"}})
    event = MessageEvent(text="read", raw_message={"channel": "C_HOME", "ts": "1234567893.000004",
        "text": "https://acme.slack.com/archives/CSOURCE/p1234567891000002?thread_ts=1234567890.000001"},
        source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_resolve_user_name", new_callable=AsyncMock, return_value="Alice"),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert "TRUNCATED" in event.channel_context
    assert "SHARED MESSAGE" in event.channel_context
    client.conversations_members = AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]})
    client.users_info = _access_client().users_info
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


def test_requester_can_share_private_source_into_dm():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = SimpleNamespace(
        conversations_info=AsyncMock(return_value={"ok": True, "channel": {
            "id": "GSECRET", "is_channel": True, "is_private": True}}),
        conversations_members=AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]}),
        conversations_replies=AsyncMock(return_value={"messages": [{
            "ts": "1234567892.000003", "text": "Private source thread"}]}),
    )
    event = MessageEvent(text="read this",
        raw_message={"channel": "DHOME", "user": "UREQUESTER", "ts": "1234567893.000004",
            "text": "https://acme.slack.com/archives/GSECRET/p1234567892000003"},
        source=SimpleNamespace(user_id="UREQUESTER"), metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert "Private source thread" in event.channel_context
    client.conversations_info.assert_awaited_once_with(channel="GSECRET")
    client.conversations_members.assert_awaited_once_with(channel="GSECRET", limit=200)


def _access_client(channel="CSOURCE", **source_fields):
    source = {"id": channel, "is_channel": True, "is_private": True}
    source.update(source_fields)
    return SimpleNamespace(
        retry_handlers=[object()],
        conversations_info=AsyncMock(return_value={"ok": True, "channel": source}),
        conversations_members=AsyncMock(return_value={"ok": True, "members": ["UREQUESTER"]}),
        users_info=AsyncMock(return_value={"ok": True, "user": {
            "id": "UREQUESTER", "team_id": "T_TEST", "deleted": False, "is_bot": False,
            "is_app_user": False, "is_restricted": False, "is_ultra_restricted": False}}),
        conversations_replies=AsyncMock(return_value={"messages": [{
            "ts": "1234567892.000003", "text": "Verified source"}]}),
    )


def test_requester_membership_continues_paginated_results():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client()
    client.conversations_members.side_effect = [
        {"ok": True, "members": ["UOTHER"], "response_metadata": {"next_cursor": "second"}},
        {"ok": True, "members": ["UREQUESTER"], "response_metadata": {"next_cursor": ""}},
    ]
    assert "Verified source" in asyncio.run(_context_with_client(adapter, client))
    assert client.conversations_members.await_args_list[1].kwargs == {
        "channel": "CSOURCE", "limit": 200, "cursor": "second"}


def test_active_internal_nonmember_can_access_verified_public_source():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client(is_private=False, context_team_id="T_TEST")
    client.conversations_members.return_value = {"ok": True, "members": []}
    assert "Verified source" in asyncio.run(_context_with_client(adapter, client))
    client.users_info.assert_awaited_once_with(user="UREQUESTER")
    client.conversations_members.assert_not_awaited()


def test_conflicting_transport_requester_fails_closed():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client()
    event = MessageEvent(text="read", source=SimpleNamespace(user_id="UREQUESTER"),
        raw_message={"channel": "DHOME", "user": "UOTHER", "ts": "1234567893.000004",
            "text": "https://acme.slack.com/archives/CSOURCE/p1234567892000003"},
        metadata={"slack_team_id": "T_TEST"})
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
        asyncio.run(adapter.handle_message(event))
    assert "not fetched" in event.channel_context
    client.conversations_info.assert_not_awaited()
    client.conversations_replies.assert_not_awaited()


def test_source_type_and_privacy_metadata_must_be_valid_before_membership():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for source in ({"id": "CSOURCE", "is_channel": True},
                   {"id": "CSOURCE", "is_channel": True, "is_private": "false"},
                   {"id": "CSOURCE", "is_channel": True, "is_im": True, "is_private": False}):
        client = _access_client()
        client.conversations_info.return_value = {"ok": True, "channel": source}
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_public_external_or_pending_user_needs_membership_even_with_matching_team():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for flag in ("is_stranger", "is_invited_user"):
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.users_info.return_value["user"][flag] = True
        client.conversations_members.return_value = {"ok": True, "members": []}
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_replies.assert_not_awaited()


def test_membership_cursor_cycles_and_page_cap_fail_closed():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for cursors, expected in ((["loop", "loop"], 2), (["a", "b", "a"], 3),
                              ([f"page{i}" for i in range(PLUGIN._ACCESS_MAX_PAGES + 1)],
                               PLUGIN._ACCESS_MAX_PAGES)):
        client = _access_client()
        client.conversations_members.side_effect = [
            {"ok": True, "members": [], "response_metadata": {"next_cursor": cursor}}
            for cursor in cursors]
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        assert client.conversations_members.await_count == expected
        client.conversations_replies.assert_not_awaited()


def test_invalid_membership_responses_fail_closed_before_thread_read():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for response in (None, {}, {"ok": False, "members": ["UREQUESTER"]},
                     {"ok": True, "members": "UREQUESTER"},
                     {"ok": True, "members": ["UREQUESTER", None]},
                     {"ok": True, "members": ["UREQUESTER"], "response_metadata": []},
                     {"ok": True, "members": ["UREQUESTER"], "response_metadata": {"next_cursor": 9}}):
        client = _access_client()
        client.conversations_members.return_value = response
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_replies.assert_not_awaited()


def test_public_guests_and_external_users_require_membership():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for changes in ({"is_restricted": True}, {"is_ultra_restricted": True},
                    {"team_id": "TOTHER"}):
        for member in (False, True):
            client = _access_client(is_private=False, context_team_id="T_TEST")
            client.users_info.return_value["user"].update(changes)
            client.conversations_members.return_value = {
                "ok": True, "members": ["UREQUESTER"] if member else []}
            context = asyncio.run(_context_with_client(adapter, client))
            assert ("Verified source" in context) is member
            assert client.conversations_replies.await_count == int(member)
            client.conversations_members.assert_awaited_once()


def test_public_missing_workspace_or_account_flags_requires_membership():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for missing in ("context_team_id", "team_id", "is_restricted", "is_bot", "deleted"):
        for member in (False, True):
            client = _access_client(is_private=False, context_team_id="T_TEST")
            target = (client.conversations_info.return_value["channel"] if missing == "context_team_id"
                      else client.users_info.return_value["user"])
            target.pop(missing)
            client.conversations_members.return_value = {
                "ok": True, "members": ["UREQUESTER"] if member else []}
            context = asyncio.run(_context_with_client(adapter, client))
            assert ("Verified source" in context) is member
            client.conversations_members.assert_awaited_once()


def test_public_deleted_and_bot_identity_never_grants_access():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for flag in ("deleted", "is_bot", "is_app_user"):
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.users_info.return_value["user"][flag] = True
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_invalid_user_lookup_fails_closed_without_membership_fallback():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for response in (None, {}, {"ok": False}, {"ok": True, "user": []},
                     {"ok": True, "user": {"id": "UOTHER"}}):
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.users_info.return_value = response
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_missing_or_invalid_requester_never_looks_up_source():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for requester in ("", None, "U_BAD", "urequester", " UREQUESTER", "UAUTHOR\n", {"id": "UREQUESTER"}):
        client = _access_client()
        with patch.object(adapter, "_get_client", return_value=client):
            context = asyncio.run(adapter._linked_thread_context(
                PLUGIN._Reference("CSOURCE", "1234567892.000003"),
                team_id="T_TEST", destination_channel="DHOME", requester=requester))
        assert "not fetched" in context
        client.conversations_info.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_source_metadata_identity_and_api_ok_are_required():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for response in (None, {}, {"ok": False}, {"ok": True}, {"ok": True, "channel": []},
                     {"ok": True, "channel": {"id": "COTHER", "is_channel": True, "is_private": True}},
                     {"ok": True, "channel": {"id": "CSOURCE"}}):
        client = _access_client()
        client.conversations_info.return_value = response
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_dm_mpim_and_legacy_private_group_members_can_share_anywhere():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for source in ({"id": "DSOURCE", "is_im": True},
                   {"id": "GSOURCE", "is_mpim": True, "is_group": True},
                   {"id": "GSOURCE", "is_group": True, "is_private": True}):
        for member in (False, True):
            for destination in ("DHOME", "GSHARED", "CSHARED"):
                client = _access_client()
                client.conversations_info.return_value = {"ok": True, "channel": source}
                client.conversations_members.return_value = {
                    "ok": True, "members": ["UREQUESTER"] if member else ["UBOT"]}
                with patch.object(adapter, "_get_client", return_value=client):
                    context = asyncio.run(adapter._linked_thread_context(
                        PLUGIN._Reference(source["id"], "1234567892.000003"),
                        team_id="T_TEST", destination_channel=destination, requester="UREQUESTER"))
                assert ("Verified source" in context) is member
                client.conversations_info.assert_awaited_once_with(channel=source["id"])
                client.users_info.assert_not_awaited()


def test_same_conversation_relies_on_accepted_inbound_access():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client()
    with patch.object(adapter, "_get_client", return_value=client):
        context = asyncio.run(adapter._linked_thread_context(
            PLUGIN._Reference("CSOURCE", "1234567892.000003"),
            team_id="T_TEST", destination_channel="CSOURCE"))
    assert "Verified source" in context
    client.conversations_info.assert_not_awaited()
    client.conversations_members.assert_not_awaited()
    client.users_info.assert_not_awaited()


def test_access_api_errors_do_not_log_response_secrets_or_read_thread():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for method in ("conversations_info", "conversations_members", "users_info"):
        client = _access_client(is_private=(method != "users_info"), context_team_id="T_TEST")
        getattr(client, method).side_effect = PermissionError("missing_scope SECRET_TOKEN")
        with patch.object(PLUGIN.logger, "info") as log:
            context = asyncio.run(_context_with_client(adapter, client))
        assert "not fetched" in context
        assert "SECRET_TOKEN" not in context + str(log.call_args_list)
        client.conversations_replies.assert_not_awaited()


def test_each_access_api_attempt_has_timeout():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    async def hanging(**kwargs):
        await asyncio.sleep(60)
    for method in ("conversations_info", "conversations_members", "users_info"):
        client = _access_client(is_private=(method != "users_info"), context_team_id="T_TEST")
        getattr(client, method).side_effect = hanging
        with patch.object(PLUGIN, "_ACCESS_TIMEOUT_SECONDS", 0.01):
            context = asyncio.run(_context_with_client(adapter, client))
        assert "not fetched" in context
        client.conversations_replies.assert_not_awaited()


def test_current_requester_not_root_or_forward_author_grants_source_access():
    for delayed in (False, True):
        for member in (False, True):
            adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
            adapter._bot_user_id = "UBOT"
            client = _access_client()
            client.conversations_members.return_value = {
                "ok": True, "members": ["UAUTHOR", "UROOT"] + (["UREQUESTER"] if member else [])}
            root = {"user": "UROOT", "ts": "1234567890.000001", "attachments": [{
                "is_share": True, "author_id": "UAUTHOR", "text": "quote",
                "channel_id": "CSOURCE", "message_ts": "1234567892.000003"}]}
            raw = {"channel": "DHOME", "user": "UREQUESTER", "ts": "1234567893.000004",
                   "text": "<@UBOT> read"}
            if delayed:
                raw["thread_ts"] = root["ts"]
                key = adapter._thread_cache_key("DHOME", root["ts"], "T_TEST")
                adapter._thread_context_cache[key] = SimpleNamespace(messages=[root])
            else:
                raw["attachments"] = root["attachments"]
            event = MessageEvent(text="read", source=SimpleNamespace(user_id="UREQUESTER"),
                                 raw_message=raw, metadata={"slack_team_id": "T_TEST"})
            with (patch.object(adapter, "_get_client", return_value=client),
                  patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
                asyncio.run(adapter.handle_message(event))
            assert ("Verified source" in event.channel_context) is member
            assert client.conversations_replies.await_count == int(member)


def test_authenticated_raw_current_user_fallback_and_missing_identity():
    for user in ("UREQUESTER", ""):
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client()
        event = MessageEvent(text="read", raw_message={"channel": "DHOME", "user": user,
            "ts": "1234567893.000004", "text": "https://acme.slack.com/archives/CSOURCE/p1234567892000003",
            "attachments": [{"is_share": True, "author_id": "UREQUESTER", "text": "quote"}]},
            metadata={"slack_team_id": "T_TEST"})
        with (patch.object(adapter, "_get_client", return_value=client),
              patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
            asyncio.run(adapter.handle_message(event))
        assert ("Verified source" in event.channel_context) is bool(user)
        assert client.conversations_replies.await_count == int(bool(user))


def test_auth_and_source_read_use_inbound_workspace_not_primary_or_channel_mapping():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client()
    primary = _access_client()
    adapter._team_clients = {"T_TEST": client, "TOTHER": primary}
    adapter._channel_team = {"CSOURCE": "TOTHER"}
    adapter._app = SimpleNamespace(client=primary)
    context = asyncio.run(adapter._linked_thread_context(
        PLUGIN._Reference("CSOURCE", "1234567892.000003"), team_id="T_TEST",
        destination_channel="DHOME", requester="UREQUESTER"))
    assert "Verified source" in context
    client.conversations_info.assert_awaited_once_with(channel="CSOURCE")
    client.conversations_replies.assert_awaited_once()
    primary.conversations_info.assert_not_awaited()
    primary.conversations_replies.assert_not_awaited()
    assert len(client.retry_handlers) == 1


def test_conflicting_source_workspace_metadata_requires_membership():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    client = _access_client(is_private=False, context_team_id="T_TEST", team_id="TOTHER")
    client.conversations_members.return_value = {"ok": True, "members": []}
    assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
    client.conversations_replies.assert_not_awaited()
    client.conversations_members.assert_awaited_once()


def test_malformed_source_boolean_flags_fail_closed():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for flag in ("is_channel", "is_group", "is_im", "is_mpim"):
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.conversations_info.return_value["channel"][flag] = "false"
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_malformed_user_account_flags_fail_closed_even_for_member():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for flag in ("deleted", "is_bot", "is_app_user", "is_restricted", "is_ultra_restricted",
                 "is_stranger", "is_invited_user"):
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.users_info.return_value["user"][flag] = "false"
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_access_attempts_disable_retries_without_mutating_workspace_client():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    attempts = []
    class Client:
        retry_handlers = [object()]
        async def conversations_info(self, **kwargs):
            attempts.append(("info", list(self.retry_handlers), kwargs))
            return {"ok": True, "channel": {"id": "CSOURCE", "is_channel": True,
                    "is_private": False, "context_team_id": "T_TEST"}}
        async def users_info(self, **kwargs):
            attempts.append(("user", list(self.retry_handlers), kwargs))
            return {"ok": True, "user": {"id": "UREQUESTER", "is_restricted": True}}
        async def conversations_members(self, **kwargs):
            attempts.append(("members", list(self.retry_handlers), kwargs))
            return {"ok": True, "members": ["UREQUESTER"]}
    client = Client()
    with patch.object(adapter, "_get_client", return_value=client):
        assert asyncio.run(adapter._requester_source_access(
            PLUGIN._Reference("CSOURCE", "1234567892.000003"), "T_TEST", "UREQUESTER"))
    assert attempts == [("info", [], {"channel": "CSOURCE"}),
                        ("user", [], {"user": "UREQUESTER"}),
                        ("members", [], {"channel": "CSOURCE", "limit": 200})]
    assert len(client.retry_handlers) == 1


def test_verified_public_nonmember_can_share_to_dm_or_shared_destination():
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
    for destination in ("DHOME", "CSHARED", "GSHARED"):
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.conversations_members.return_value = {"ok": True, "members": []}
        with patch.object(adapter, "_get_client", return_value=client):
            context = asyncio.run(adapter._linked_thread_context(
                PLUGIN._Reference("CSOURCE", "1234567892.000003"), team_id="T_TEST",
                destination_channel=destination, requester="UREQUESTER"))
        assert "Verified source" in context
        client.conversations_info.assert_awaited_once_with(channel="CSOURCE")
        client.users_info.assert_awaited_once_with(user="UREQUESTER")
        client.conversations_members.assert_not_awaited()


def test_supplied_malformed_source_workspace_fails_closed():
    for private in (False, True):
        for key in ("context_team_id", "team_id"):
            for value in (None, "", 0, False, [], {}, ["T_TEST"]):
                adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
                client = _access_client(is_private=private, context_team_id="T_TEST")
                client.conversations_info.return_value["channel"][key] = value
                assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
                client.conversations_members.assert_not_awaited()
                client.conversations_replies.assert_not_awaited()


def test_supplied_malformed_public_user_workspace_fails_closed():
    for value in (None, "", 0, False, [], {}, ["T_TEST"]):
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client(is_private=False, context_team_id="T_TEST")
        client.users_info.return_value["user"]["team_id"] = value
        assert "not fetched" in asyncio.run(_context_with_client(adapter, client))
        client.conversations_members.assert_not_awaited()
        client.conversations_replies.assert_not_awaited()


def test_valid_differing_source_workspaces_preserve_membership_fallback():
    for key in ("context_team_id", "team_id"):
        for member in (False, True):
            adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
            client = _access_client(is_private=False, context_team_id="T_TEST")
            client.conversations_info.return_value["channel"][key] = "TOTHER"
            client.conversations_members.return_value = {"ok": True, "members": ["UREQUESTER"] if member else []}
            assert ("Verified source" in asyncio.run(_context_with_client(adapter, client))) is member
            client.conversations_members.assert_awaited_once()


def _latency_event(channels=("CSOURCE", "COTHER"), root=False, targets=None):
    targets = targets or ["1234567892.000003"] * len(channels)
    text = "<@UBOT> read " + " ".join(
        f"https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}"
        for channel, ts in zip(channels, targets))
    raw = {"channel": "CHOME", "ts": "1234567893.000004", "user": "UREQUESTER", "text": text}
    if root:
        raw["thread_ts"] = "1234567890.000001"
    return MessageEvent(text=text, raw_message=raw, source=SimpleNamespace(user_id="UREQUESTER"),
                        metadata={"slack_team_id": "T_TEST"})


async def _latency_deliver(adapter, client, event, budget=0.05):
    with (patch.object(adapter, "_get_client", return_value=client),
          patch.object(adapter, "_inbound_provenance", new_callable=AsyncMock, return_value="provenance"),
          patch.object(PLUGIN, "_LINKED_TIMEOUT_SECONDS", budget, create=True),
          patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock) as parent):
        start = asyncio.get_running_loop().time()
        await adapter.handle_message(event)
        elapsed = asyncio.get_running_loop().time() - start
        parent.assert_awaited_once_with(event)
        return elapsed


def _multi_source_client():
    client = _access_client()
    client.conversations_info.side_effect = lambda channel: {
        "ok": True, "channel": {"id": channel, "is_channel": True, "is_private": True}}
    return client


def test_shared_linked_deadline_default_is_fifteen_seconds():
    assert getattr(PLUGIN, "_LINKED_TIMEOUT_SECONDS", None) == 15.0


def test_linked_sources_run_concurrently_with_ordered_output_and_two_limit():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _multi_source_client()
        active = peak = 0
        both = asyncio.Event()
        async def replies(channel, ts, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both.set()
            try:
                await asyncio.wait_for(both.wait(), 0.1)
                await asyncio.sleep(0.01 if channel == "CSOURCE" else 0)
                return {"messages": [{"ts": ts, "text": f"source-{channel}"}]}
            finally:
                active -= 1
        client.conversations_replies.side_effect = replies
        event = _latency_event(("CSOURCE", "COTHER", "CTHIRD"))
        await _latency_deliver(adapter, client, event, 0.3)
        assert peak == 2 and active == 0
        assert event.channel_context.index("source-CSOURCE") < event.channel_context.index("source-COTHER")
        assert "CTHIRD" not in event.channel_context
        assert client.conversations_replies.await_count == 2
        assert client.conversations_info.await_count == client.conversations_members.await_count == 2
    asyncio.run(scenario())


def test_shared_deadline_retains_completed_source_and_marks_pending_in_order():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _multi_source_client()
        cancelled = []
        async def replies(channel, ts, **kwargs):
            if channel == "CSOURCE":
                try:
                    await asyncio.sleep(0.15)
                finally:
                    cancelled.append(channel)
            return {"messages": [{"ts": ts, "text": f"source-{channel}"}]}
        client.conversations_replies.side_effect = replies
        event = _latency_event()
        elapsed = await _latency_deliver(adapter, client, event)
        assert elapsed < 0.12
        assert "source-COTHER" in event.channel_context and "source-CSOURCE" not in event.channel_context
        assert "timeout" in event.channel_context.lower()
        assert event.channel_context.index("CSOURCE") < event.channel_context.index("source-COTHER")
        assert cancelled == ["CSOURCE"]
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


def test_deadline_starts_before_delayed_destination_root_discovery():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        adapter._bot_user_id = "UBOT"
        client = _multi_source_client()
        async def replies(channel, ts, **kwargs):
            if channel == "CHOME":
                await asyncio.sleep(0.15)
                return {"messages": [{"ts": ts, "text": "root"}]}
            return {"messages": [{"ts": ts, "text": "must not read"}]}
        client.conversations_replies.side_effect = replies
        event = _latency_event(root=True)
        assert await _latency_deliver(adapter, client, event) < 0.12
        assert "timeout" in event.channel_context.lower()
        client.conversations_info.assert_not_awaited()
        assert client.conversations_replies.await_count == 1
    asyncio.run(scenario())


def test_delayed_root_discovery_spends_source_remaining_budget():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        adapter._bot_user_id = "UBOT"
        client = _multi_source_client()
        async def replies(channel, ts, **kwargs):
            await asyncio.sleep(0.035)
            return {"messages": [{"ts": ts, "text": "root" if channel == "CHOME" else "too late"}]}
        client.conversations_replies.side_effect = replies
        event = _latency_event(("CSOURCE",), root=True)
        assert await _latency_deliver(adapter, client, event) < 0.085
        assert "too late" not in event.channel_context
        assert "timeout" in event.channel_context.lower()
    asyncio.run(scenario())


def test_shared_deadline_covers_access_and_never_reads_unverified_source():
    async def scenario():
        for method in ("conversations_info", "conversations_members", "users_info"):
            adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
            client = _access_client(is_private=method != "users_info", context_team_id="T_TEST")
            async def hanging(**kwargs):
                await asyncio.sleep(0.15)
            getattr(client, method).side_effect = hanging
            event = _latency_event(("CSOURCE",))
            assert await _latency_deliver(adapter, client, event) < 0.12
            assert "timeout" in event.channel_context.lower()
            client.conversations_replies.assert_not_awaited()
        # The deadline is shared across pagination, not reset for each attempt.
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client()
        async def delayed_page(**kwargs):
            await asyncio.sleep(0.03)
            return {"ok": True, "members": [], "response_metadata": {"next_cursor": "more"}}
        client.conversations_members.side_effect = delayed_page
        event = _latency_event(("CSOURCE",))
        assert await _latency_deliver(adapter, client, event) < 0.085
        assert "timeout" in event.channel_context.lower()
        assert client.conversations_members.await_count == 2
        client.conversations_replies.assert_not_awaited()
    asyncio.run(scenario())


def test_exact_target_lookup_uses_remaining_deadline():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={"linked_thread_max_messages": 1}))
        client = _access_client()
        async def replies(**kwargs):
            if "oldest" in kwargs:
                await asyncio.sleep(0.15)
                return {"messages": [{"ts": kwargs["ts"], "text": "too late"}]}
            await asyncio.sleep(0.02)
            return {"messages": [{"ts": "1234567890.000001", "text": "root"}], "has_more": True}
        client.conversations_replies.side_effect = replies
        event = _latency_event(("CSOURCE",))
        assert await _latency_deliver(adapter, client, event) < 0.12
        assert "timeout" in event.channel_context.lower() and "too late" not in event.channel_context
        assert client.conversations_replies.await_count == 2
        assert client.conversations_replies.await_args.kwargs["oldest"] == "1234567892.000003"
    asyncio.run(scenario())


def test_optional_linked_api_copies_disable_retries_including_root_and_exact():
    attempts = []
    class Client:
        retry_handlers = [object()]
        async def conversations_info(self, **kwargs):
            attempts.append(("info", list(self.retry_handlers)))
            return {"ok": True, "channel": {"id": kwargs["channel"], "is_channel": True, "is_private": True}}
        async def conversations_members(self, **kwargs):
            attempts.append(("members", list(self.retry_handlers)))
            return {"ok": True, "members": ["UREQUESTER"]}
        async def conversations_replies(self, **kwargs):
            attempts.append(("replies", list(self.retry_handlers)))
            if kwargs["channel"] == "CHOME":
                return {"messages": [{"ts": kwargs["ts"], "text": "root"}]}
            if "oldest" in kwargs:
                return {"messages": [{"ts": kwargs["ts"], "text": "exact"}]}
            return {"messages": [{"ts": "1234567890.000001", "text": "source root"}], "has_more": True}
    adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={"linked_thread_max_messages": 1}))
    adapter._bot_user_id = "UBOT"
    client = Client()
    event = _latency_event(("CSOURCE",), root=True)
    asyncio.run(_latency_deliver(adapter, client, event, 0.3))
    assert attempts == [("replies", []), ("info", []), ("members", []), ("replies", []), ("replies", [])]
    assert len(client.retry_handlers) == 1
    assert "SHARED MESSAGE" in event.channel_context and "exact" in event.channel_context


def test_concurrent_duplicate_channel_access_single_flight_and_next_turn_rechecks():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client()
        async def info(channel):
            await asyncio.sleep(0.01)
            return {"ok": True, "channel": {"id": channel, "is_channel": True, "is_private": True}}
        client.conversations_info.side_effect = info
        client.conversations_replies.side_effect = lambda ts, **kwargs: {"messages": [{"ts": ts, "text": "allowed"}]}
        targets = ["1234567892.000003", "1234567891.000002"]
        event = _latency_event(("CSOURCE", "CSOURCE"), targets=targets)
        await _latency_deliver(adapter, client, event, 0.3)
        assert event.channel_context.count("SHARED MESSAGE") == 2
        assert client.conversations_info.await_count == client.conversations_members.await_count == 1
        client.conversations_members.return_value = {"ok": True, "members": []}
        event = _latency_event(("CSOURCE", "CSOURCE"), targets=targets)
        await _latency_deliver(adapter, client, event, 0.3)
        assert event.channel_context.count("not fetched") == 2
        assert client.conversations_info.await_count == client.conversations_members.await_count == 2
        assert client.conversations_replies.await_count == 2
        # Concurrent turns on the same adapter must not share in-flight permissions,
        # even when source channel is the same and team/requester differ.
        client.conversations_members.return_value = {"ok": True, "members": ["UREQUESTER"]}
        allowed = _latency_event(("CSOURCE",))
        denied = _latency_event(("CSOURCE",))
        denied.source.user_id = denied.raw_message["user"] = "UOTHER"
        denied.metadata["slack_team_id"] = "TOTHER"
        with (patch.object(adapter, "_get_client", return_value=client) as select,
              patch.object(adapter, "_inbound_provenance", new_callable=AsyncMock, return_value="provenance"),
              patch.object(PLUGIN, "_LINKED_TIMEOUT_SECONDS", 0.3),
              patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
            await asyncio.gather(adapter.handle_message(allowed), adapter.handle_message(denied))
        assert "allowed" in allowed.channel_context and "not fetched" in denied.channel_context
        assert client.conversations_info.await_count == client.conversations_members.await_count == 4
        assert client.conversations_replies.await_count == 3
        assert {call.kwargs["team_id"] for call in select.call_args_list} == {"T_TEST", "TOTHER"}
    asyncio.run(scenario())


def test_linked_cancellation_propagates_and_drains_all_children():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client()
        started = asyncio.Event()
        ended = []
        async def info(**kwargs):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                ended.append(True)
        client.conversations_info.side_effect = info
        event = _latency_event(("CSOURCE", "CSOURCE"), targets=["1234567892.000003", "1234567891.000002"])
        task = asyncio.create_task(_latency_deliver(adapter, client, event, 0.3))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("caller cancellation swallowed")
        assert ended == [True]
        client.conversations_replies.assert_not_awaited()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


def test_per_attempt_source_read_timeout_is_bounded_independently():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client()
        async def hanging(**kwargs):
            await asyncio.sleep(0.15)
        client.conversations_replies.side_effect = hanging
        event = _latency_event(("CSOURCE",))
        with patch.object(PLUGIN, "_ACCESS_TIMEOUT_SECONDS", 0.01):
            assert await _latency_deliver(adapter, client, event, 0.3) < 0.08
        assert "timeout" in event.channel_context.lower()
    asyncio.run(scenario())


def test_linked_budget_does_not_bound_provenance_history_or_entire_turn():
    async def scenario():
        adapter = PLUGIN.SlackHistoryBackfillAdapter(PlatformConfig(extra={}))
        client = _access_client()
        event = _latency_event(("CSOURCE",))
        async def slow_provenance(*args):
            await asyncio.sleep(0.03)
            return "slow provenance"
        with (patch.object(adapter, "_get_client", return_value=client),
              patch.object(adapter, "_inbound_provenance", side_effect=slow_provenance),
              patch.object(PLUGIN, "_LINKED_TIMEOUT_SECONDS", 0.01, create=True),
              patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock)):
            await adapter.handle_message(event)
        assert "slow provenance" in event.channel_context and "Verified source" in event.channel_context
    asyncio.run(scenario())


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"plugin_smoke=passed tests={len(tests)}")

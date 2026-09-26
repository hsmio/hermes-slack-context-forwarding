from __future__ import annotations

import importlib.util
import asyncio
from pathlib import Path
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


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"plugin_smoke=passed tests={len(tests)}")

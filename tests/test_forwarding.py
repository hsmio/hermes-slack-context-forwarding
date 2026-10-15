"""Forwarded Slack messages: text is quoted once, files reach the media pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent

from helpers import PLUGIN, run, run_tests

PDF = {"id": "F1", "name": "report.pdf", "mimetype": "application/pdf", "size": 1234,
       "url_private_download": "https://files.slack.com/report.pdf"}


def adapter():
    instance = PLUGIN.SlackColleagueAdapter(PlatformConfig(extra={}))
    instance._bot_user_id = "U_BOT"
    return instance


def handled(raw, text="What is this?"):
    event = MessageEvent(text=text, raw_message=raw, source=SimpleNamespace(user_id="U_ME"),
                         metadata={"slack_team_id": "T1"})
    with (patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock),
          patch.object(PLUGIN.SlackColleagueAdapter, "_workspace_url", new=AsyncMock(return_value=""))):
        run(adapter().handle_message(event))
    return event


def test_forward_text_appears_once_across_slack_representations():
    forward = {
        "is_share": True, "is_msg_unfurl": True, "author_name": "Alice",
        "text": "`forward-test-indigo-917`",
        "fallback": "[September 25th, 2026] Alice: forward-test-indigo-917",
        "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "text", "text": "forward-test-indigo-917"}]}]}],
    }
    text = handled({"attachments": [forward]}).text
    assert "From Alice: `forward-test-indigo-917`" in text
    assert text.count("forward-test-indigo-917") == 1
    assert "September" not in text


def test_pasted_link_preview_is_not_a_forward():
    preview = {"is_msg_unfurl": True, "author_id": "U_OTHER", "text": "A preview",
               "files": [{"id": "F_PRIVATE", "name": "private.txt"}]}
    assert handled({"attachments": [preview]}).text == "What is this?"
    assert PLUGIN._merge_forwarded_files({"attachments": [preview]}) == []


def test_forward_of_a_forward_shows_both_texts_sources_and_files():
    raw = {"attachments": [{
        "is_share": True, "author_name": "Alice", "text": "see below",
        "from_url": "https://acme.slack.com/archives/C0SOURCE1/p1700000000000200",
        "attachments": [{"is_share": True, "author_name": "Bob", "text": "the original",
                         "files": [{"id": "F2", "name": "b.png", "mimetype": "image/png"}]}],
    }]}
    text = handled(raw).text
    assert "From Alice [channel=C0SOURCE1 ts=1700000000.000200]: see below" in text
    assert "↳ From Bob: the original" in text
    assert "Forwarded file: b.png (image/png)" in text
    assert [f["id"] for f in PLUGIN._merge_forwarded_files(raw)] == ["F2"]


def test_forwarded_files_merge_with_direct_files_and_the_complete_record_wins():
    event = {"files": [{"id": "F1", "name": "report.pdf"}],
             "attachments": [{"is_share": True, "files": [PDF, {"id": "F3", "name": "notes.txt"}]}]}
    merged = PLUGIN._merge_forwarded_files(event)
    assert [f["id"] for f in merged] == ["F1", "F3"]
    assert merged[0]["mimetype"] == "application/pdf"


def test_forwarded_document_is_downloaded_by_the_bundled_pipeline():
    event = {"attachments": [{"is_share": True, "files": [
        {"id": "F1", "name": "note.txt", "mimetype": "text/plain", "size": 7,
         "url_private_download": "https://files.slack.com/note.txt"}]}]}
    instance = adapter()
    with (patch.object(instance, "_download_slack_file_bytes", new=AsyncMock(return_value=b"hello!\n")) as download,
          patch.object(PLUGIN.bundled_slack, "cache_document_from_bytes_async",
                       new=AsyncMock(return_value="/cache/note.txt"))):
        urls, types, _, text = run(instance._collect_inbound_media(event, "C1", "T1", "question", [], []))
    download.assert_awaited_once_with("https://files.slack.com/note.txt", team_id="T1")
    assert urls == ["/cache/note.txt"] and types == ["text/plain"]
    assert "[Content of note.txt]:\nhello!" in text


def test_huge_forwards_are_bounded():
    raw = {"attachments": [{"is_share": True, "text": "x" * 9000}, {"is_share": True, "text": "y" * 9000}]}
    text = handled(raw, text="question").text
    assert len(text) <= PLUGIN._FORWARDED_MAX_CHARS + len("question\n\n")
    assert "[forwarded content truncated]" in text


if __name__ == "__main__":
    run_tests(globals(), "forwarding")

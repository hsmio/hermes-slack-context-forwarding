from __future__ import annotations

import importlib.util
from pathlib import Path


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


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"plugin_smoke=passed tests={len(tests)}")

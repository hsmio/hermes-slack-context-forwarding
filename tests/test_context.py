"""What goes into the model's context for a Slack message, and what is only referenced."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType

from helpers import PLUGIN, run, run_tests


def adapter(**extra):
    instance = PLUGIN.SlackColleagueAdapter(PlatformConfig(extra=extra))
    instance._bot_user_id = "U_BOT"
    return instance


def handled(raw, instance=None, message_type=MessageType.TEXT, workspace="https://acme.slack.com/"):
    instance = instance or adapter()
    event = MessageEvent(text=raw.get("text", ""), message_type=message_type, raw_message=raw,
                         source=SimpleNamespace(user_id="U_ME"), metadata={"slack_team_id": "T1"})
    event.channel_context = "[Thread context — bundled]"
    with (patch.object(PLUGIN.bundled_slack.SlackAdapter, "handle_message", new_callable=AsyncMock),
          patch.object(PLUGIN.SlackColleagueAdapter, "_workspace_url", new=AsyncMock(return_value=workspace))):
        run(instance.handle_message(event))
    return event


def test_provenance_is_one_line_with_ids_and_a_permalink_and_no_api_calls():
    event = handled({"channel": "C0ABCDEF1", "ts": "1700000000.000200", "thread_ts": "1700000000.000100",
                     "text": "hi"})
    first_line = event.channel_context.splitlines()[0]
    assert first_line == ("[Current Slack message: channel=C0ABCDEF1 ts=1700000000.000200 "
                          "thread_ts=1700000000.000100 permalink=https://acme.slack.com/archives/C0ABCDEF1/"
                          "p1700000000000200?thread_ts=1700000000.000100&cid=C0ABCDEF1]")
    assert event.channel_context.endswith("[Thread context — bundled]")


def test_provenance_without_workspace_url_still_has_ids():
    event = handled({"channel": "C0ABCDEF1", "ts": "1700000000.000200", "text": "hi"}, workspace="")
    assert event.channel_context.startswith("[Current Slack message: channel=C0ABCDEF1 ts=1700000000.000200]")


def test_slack_links_are_referenced_not_fetched():
    raw = {"channel": "C0HERE001", "ts": "1700000000.000300",
           "text": "see <https://acme.slack.com/archives/C0OTHER01/p1700000000000100?thread_ts=1699999999.000001>"
                   " and https://evil.example/archives/C0OTHER02/p1700000000000100"}
    instance = adapter()
    instance._get_client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Slack reads"))
    context = handled(raw, instance).channel_context
    assert ("[Slack messages referenced by the current message: channel=C0OTHER01 ts=1700000000.000100 "
            "thread_ts=1699999999.000001.") in context
    assert "C0OTHER02" not in context
    assert "slack_read_thread" in context


def test_message_mention_blocks_count_as_references():
    raw = {"channel": "C0HERE001", "ts": "1700000000.000300", "text": "",
           "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
               {"type": "message_mention", "channel_id": "C0OTHER01", "message_ts": "1700000000.000100"}]}]}]}
    assert "channel=C0OTHER01 ts=1700000000.000100" in handled(raw).channel_context


def test_commands_are_untouched():
    event = handled({"channel": "C0ABCDEF1", "ts": "1700000000.000200", "text": "/new"},
                    message_type=MessageType.COMMAND)
    assert event.channel_context == "[Thread context — bundled]"


def test_workspace_url_is_looked_up_once_per_workspace():
    instance = adapter()
    client = SimpleNamespace(auth_test=AsyncMock(return_value={"url": "https://acme.slack.com/"}))
    instance._get_client = lambda *a, **k: client
    assert run(instance._workspace_url("C1", "T1")) == "https://acme.slack.com/"
    assert run(instance._workspace_url("C1", "T1")) == "https://acme.slack.com/"
    assert client.auth_test.await_count == 1


def history_adapter(messages):
    instance = adapter(history_backfill=True, history_backfill_limit=15)
    client = SimpleNamespace(conversations_history=AsyncMock(return_value={"messages": messages}))
    instance._get_client = lambda *a, **k: client
    instance._resolve_user_name = AsyncMock(side_effect=lambda uid, **kw: {"U_A": "Alice"}.get(uid, uid))
    instance._is_sender_authorized = lambda *a, **k: True
    return instance, client


def test_top_level_mention_gets_recent_channel_messages_back_to_hermes_last_post():
    instance, client = history_adapter([  # Slack returns newest first
        {"ts": "1700000009.000000", "user": "U_A", "text": "latest", "reply_count": 4},
        {"ts": "1700000008.000000", "thread_ts": "1700000003.000000", "user": "U_A", "text": "a thread reply"},
        {"ts": "1700000007.000000", "user": "U_BOT", "text": "Hermes' previous answer"},
        {"ts": "1700000006.000000", "user": "U_A", "text": "too old"},
    ])
    context = run(instance._channel_history_context(channel_id="C1", current_ts="1700000010.000000"))
    assert "Alice [ts=1700000009.000000] (4 replies): latest" in context
    assert "a thread reply" not in context and "too old" not in context
    assert client.conversations_history.await_args.kwargs["limit"] == 15


def test_backfill_is_only_for_top_level_mentions():
    instance = adapter(history_backfill=True)
    def eligible(raw, message_type=MessageType.TEXT):
        return instance._eligible_for_history_backfill(MessageEvent(
            text="", message_type=message_type, raw_message=raw, source=SimpleNamespace(user_id="U_ME"),
            metadata={"slack_team_id": "T1"}))
    top = {"channel": "C1", "ts": "1700000002.000000", "text": "<@U_BOT> hello"}
    assert eligible(top)
    assert not eligible({**top, "thread_ts": "1700000001.000000"})
    assert not eligible({**top, "text": "hello"})
    assert not eligible({**top, "channel": "D1"})
    assert not eligible(top, MessageType.COMMAND)


if __name__ == "__main__":
    run_tests(globals(), "context")

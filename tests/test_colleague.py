from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig

from helpers import PLUGIN, TOOLS, run_tests

BOT = "UBOT"
HUGO = "UHUGO"
ALICE = "UALICE"


def _adapter(**extra):
    adapter = PLUGIN.SlackColleagueAdapter(PlatformConfig(extra={"solo_thread_follow": True, **extra}))
    adapter._bot_user_id = BOT
    return adapter


def _msg(ts, user=None, text="", **kw):
    msg = {"ts": ts, "thread_ts": "1700000000.000001", "text": text, **kw}
    if user:
        msg["user"] = user
        msg.setdefault("client_msg_id", f"c-{ts}")
    return msg


def _client(messages):
    client = SimpleNamespace()
    client.calls = []

    async def replies(**params):
        client.calls.append(params)
        return {"ok": True, "messages": messages, "has_more": False}
    client.conversations_replies = replies
    return client


def _gate(adapter, client, *, user=HUGO, text="ok", mentioned=False, thread=True, base=True):
    async def run():
        with (patch.object(PLUGIN.bundled_slack.SlackAdapter, "_channel_gate_allows",
                           new=AsyncMock(return_value=base)),
              patch.object(adapter, "_get_client", return_value=client),
              patch.object(adapter, "_resolve_user_is_bot", new=AsyncMock(return_value=False))):
            return await adapter._channel_gate_allows(
                channel_id="C1", routing_text=text, bot_uid=BOT, is_mentioned=mentioned,
                is_thread_reply=thread, event_thread_ts="1700000000.000001", user_id=user,
                team_id="T1", is_dm=False, force_process=False)
    return asyncio.run(run())


SOLO = [
    _msg("1700000000.000001", HUGO, f"<@{BOT}> what is X?"),
    _msg("1700000000.000002", BOT, "X is…", bot_id="B1"),
    _msg("1700000000.000003", HUGO, "and Y?"),
]


def test_solo_thread_follows_without_mention():
    assert _gate(_adapter(), _client(SOLO)) is True


def test_other_human_reply_makes_thread_mention_only():
    thread = SOLO + [_msg("1700000000.000004", ALICE, "I disagree")]
    adapter = _adapter()
    assert _gate(adapter, _client(thread), user=ALICE, text="I disagree") is False
    adapter.__dict__.pop("_scf_thread_cache", None)
    assert _gate(adapter, _client(thread), text="what do you think Alice") is False


def test_mentioning_a_colleague_mid_sentence_makes_thread_mention_only():
    thread = SOLO + [_msg("1700000000.000004", HUGO, f"what do you think <@{ALICE}>?")]
    assert _gate(_adapter(), _client(thread), text=f"what do you think <@{ALICE}>?") is False


def test_group_mention_makes_thread_mention_only():
    thread = SOLO + [_msg("1700000000.000004", HUGO, "<!here> look")]
    assert _gate(_adapter(), _client(thread), text="<!here> look") is False


def test_bot_mentions_do_not_count_as_humans():
    thread = SOLO + [_msg("1700000000.000004", BOT, f"Done, <@{HUGO}> and <@{ALICE}>", bot_id="B1")]
    assert _gate(_adapter(), _client(thread)) is True


def test_explicit_mention_still_answers_in_multiplayer_thread():
    thread = SOLO + [_msg("1700000000.000004", ALICE, "hm")]
    client = _client(thread)
    assert _gate(_adapter(), client, mentioned=True, text=f"<@{BOT}> summarize") is True
    assert client.calls == []  # mentions never need the participant read


def test_base_gate_rejection_is_final_and_disabled_mode_is_untouched():
    assert _gate(_adapter(), _client(SOLO), base=False) is False
    adapter = PLUGIN.SlackColleagueAdapter(PlatformConfig(extra={}))
    adapter._bot_user_id = BOT
    multi = SOLO + [_msg("1700000000.000004", ALICE, "hm")]
    assert _gate(adapter, _client(multi)) is True


def test_participant_lookup_failure_requires_a_mention():
    client = SimpleNamespace(conversations_replies=AsyncMock(side_effect=RuntimeError("boom")))
    assert _gate(_adapter(), client) is False


def test_free_response_channel_is_not_restricted():
    multi = SOLO + [_msg("1700000000.000004", ALICE, "hm")]
    with patch.object(PLUGIN.SlackColleagueAdapter, "_slack_free_response_channels",
                      return_value={"C1"}):
        assert _gate(_adapter(), _client(multi)) is True


def test_thread_context_keeps_root_and_newest_replies():
    messages = [_msg("1700000000.000001", HUGO, "root")] + [
        _msg(f"1700000001.{i:06d}", HUGO, f"reply {i}") for i in range(1, 151)]
    pages = [messages[:100], messages[100:]]

    async def replies(**params):
        page = pages.pop(0)
        return {"ok": True, "messages": page, "has_more": bool(pages),
                "response_metadata": {"next_cursor": "next" if pages else ""}}

    adapter = _adapter(thread_context_max_messages=10)
    client = SimpleNamespace(conversations_replies=replies)

    async def run():
        with patch.object(adapter, "_get_client", return_value=client):
            return await adapter._conversations_replies_with_backoff("C1", "1700000000.000001", 31, "T1")
    result = asyncio.run(run())
    kept = result["messages"]
    assert kept[0]["text"] == "root"
    assert kept[-1]["text"] == "reply 150"
    assert len(kept) == 11  # root + 10 newest (the configured maximum, incl. the current message)


def test_thread_context_is_untouched_when_not_configured():
    adapter = PLUGIN.SlackColleagueAdapter(PlatformConfig(extra={}))
    base = AsyncMock(return_value={"ok": True, "messages": ["bundled"]})

    async def run():
        with patch.object(PLUGIN.bundled_slack.SlackAdapter, "_conversations_replies_with_backoff", new=base):
            return await adapter._conversations_replies_with_backoff("C1", "1700000000.000001", 31, "T1")
    assert asyncio.run(run())["messages"] == ["bundled"]


def test_tool_reference_parsing():
    assert TOOLS.parse_reference(
        "https://acme.slack.com/archives/C0ABCDEF1/p1700000000123456?thread_ts=1699999999.000100&cid=C0ABCDEF1"
    ) == ("C0ABCDEF1", "1700000000.123456", "1699999999.000100")
    try:
        TOOLS.parse_reference("https://evil.example/archives/C0ABCDEF1/p1700000000123456")
    except TOOLS.ToolError:
        pass
    else:
        raise AssertionError("non-Slack host accepted")


def test_tool_renders_nested_forwards_with_files_and_sources():
    with patch.object(TOOLS, "_user", side_effect=lambda client, uid: ({"UA": "Alice"}.get(uid, uid), False)):
        rendered = TOOLS.render_message(None, {
            "ts": "1.000001", "user": "UA", "text": "fyi <@UA>",
            "attachments": [{
                "is_share": True, "author_id": "UA", "channel_id": "C0SOURCE1", "ts": "1700000000.000200",
                "from_url": "https://acme.slack.com/archives/C0SOURCE1/p1700000000000200",
                "text": "outer forward",
                "files": [{"id": "F0OUTER1", "name": "a.pdf", "url_private": "https://files.slack.com/x"}],
                "message": {"attachments": [{
                    "is_share": True, "author_name": "Bob", "text": "inner forward",
                    "files": [{"id": "F0INNER1", "name": "b.png"}]}]},
            }],
        })
    share = rendered["forwarded_or_linked"][0]
    assert rendered["text"] == "fyi @Alice"
    assert share["channel"] == "C0SOURCE1" and share["link"].endswith("p1700000000000200")
    assert share["files"][0]["id"] == "F0OUTER1" and "url_private" not in share["files"][0]
    assert share["contains"][0]["text"] == "inner forward"
    assert share["contains"][0]["files"][0]["id"] == "F0INNER1"


def test_download_refuses_non_slack_hosts():
    assert TOOLS._safe_cdn("https://files.slack.com/files-pri/T1-F1/x.png")
    assert not TOOLS._safe_cdn("https://slack-edge.evil.example/x")
    assert not TOOLS._safe_cdn("https://files.slack.com.evil.example/x")
    assert not TOOLS._safe_cdn("http://files.slack.com/x")


def test_tool_errors_are_json_without_secrets():
    with patch.object(TOOLS, "_token", return_value=""):
        out = json.loads(TOOLS.read_thread({"link": "https://a.slack.com/archives/C0ABCDEF1/p1700000000123456"}))
    assert "token" in out["error"].lower() and "xox" not in out["error"]


class FakeSlack:
    """Answers WebClient calls from canned data."""

    def __init__(self, **responses):
        self.responses = responses

    def __getattr__(self, method):
        def call(**params):
            value = self.responses[method]
            return value(**params) if callable(value) else value
        return call


def _tools(fake, **patches):
    return patch.multiple(TOOLS, _client=lambda: fake, _token=lambda: "xoxb-test",
                          _user=lambda client, uid: (uid.lower(), False), **patches)


def test_read_thread_from_a_reply_link_returns_root_and_newest_within_budget():
    replies = [{"ts": "1700000000.000001", "user": "UROOT", "text": "root", "reply_count": 300}] + [
        {"ts": f"1700000001.{i:06d}", "user": "UA", "thread_ts": "1700000000.000001", "text": "x" * 1500}
        for i in range(300)]
    fake = FakeSlack(conversations_replies={"messages": replies, "has_more": False},
                     conversations_info={"channel": {"name": "general", "is_member": True}})
    with _tools(fake):
        out = json.loads(TOOLS.read_thread({
            "link": "https://acme.slack.com/archives/C0ABCDEF1/p1700001000000299?thread_ts=1700000000.000001"}))
    texts = [m["text"] for m in out["messages"]]
    assert texts[0] == "root" and out["messages"][-1]["ts"] == "1700000001.000299"
    assert out["omitted_middle_messages"] > 0
    assert len(json.dumps(out)) < TOOLS._MAX_OUTPUT + 5_000


def test_channel_names_resolve_once():
    calls = []

    def channels(**params):
        calls.append(params)
        return {"channels": [{"id": "C0GENERAL1", "name": "general"}]}
    fake = FakeSlack(conversations_list=channels)
    TOOLS._channel_ids.clear()
    assert TOOLS._resolve_channel(fake, "general") == "C0GENERAL1"
    assert TOOLS._resolve_channel(fake, "general") == "C0GENERAL1"
    assert len(calls) == 1
    assert TOOLS._resolve_channel(fake, "C0GENERAL1") == "C0GENERAL1"


def _download(tmp, handler, info=None):
    import httpx
    fake = FakeSlack(files_info={"file": info or {
        "id": "F0FILE0001", "name": "report final.pdf", "mimetype": "application/pdf", "size": 3,
        "url_private_download": "https://files.slack.com/files-pri/T1-F0FILE0001/report.pdf"}})
    with (_tools(fake, _http=lambda: httpx.Client(transport=httpx.MockTransport(handler))),
          patch.dict("os.environ", {"HERMES_HOME": str(tmp)}),
          patch("hermes_constants.get_hermes_home", return_value=tmp)):
        return json.loads(TOOLS.download_file({"file_id": "F0FILE0001"}))


def test_download_saves_file_and_sends_token_only_to_slack(tmp_path=None):
    import tempfile
    import httpx
    tmp = Path(tempfile.mkdtemp())
    seen = []

    def ok(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200, content=b"PDF")
    out = _download(tmp, ok)
    assert Path(out["path"]).read_bytes() == b"PDF" and Path(out["path"]).parent == tmp / "cache" / "slack-files"
    assert seen == [("files.slack.com", "Bearer xoxb-test")]

    def evil_redirect(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})
    assert "unexpected host" in _download(tmp, evil_redirect)["error"]
    assert "evil.example" not in seen

    def sign_in(request):
        return httpx.Response(200, content=b"<!DOCTYPE html><html>sign in</html>")
    assert "sign-in page" in _download(tmp, sign_in)["error"]


if __name__ == "__main__":
    run_tests(globals(), "colleague")

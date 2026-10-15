"""Thread stories from real use, against a fake Slack thread.

Each test reads as: people post in a thread, then we check whether Hermes
answers an untagged message, and what thread context it would receive.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig

from helpers import PLUGIN, run_tests

HERMES, ME, ALICE = "UHERMES", "UME", "UALICE"
ROOT_TS = "1700000000.000000"


class Thread:
    """A Slack thread plus a Hermes adapter that reads it."""

    def __init__(self):
        self.messages: list[dict] = []
        self.hermes = PLUGIN.SlackColleagueAdapter(PlatformConfig(extra={"solo_thread_follow": True, "thread_context_max_messages": 60}))
        self.hermes._bot_user_id = HERMES

    def post(self, user: str, text: str) -> str:
        ts = f"1700000000.{len(self.messages):06d}"
        msg = {"ts": ts, "thread_ts": ROOT_TS, "user": user, "text": text}
        if user == HERMES:
            msg["bot_id"] = "BHERMES"
        else:
            msg["client_msg_id"] = ts
        self.messages.append(msg)
        return ts

    async def _replies(self, **params):
        return {"ok": True, "messages": list(self.messages), "has_more": False}

    def _run(self, coro_fn):
        client = type("Client", (), {"conversations_replies": staticmethod(self._replies)})()
        self.hermes.__dict__.pop("_scf_thread_cache", None)

        async def go():
            with (patch.object(self.hermes, "_get_client", return_value=client),
                  patch.object(self.hermes, "_resolve_user_is_bot", new=AsyncMock(return_value=False)),
                  patch.object(self.hermes, "_resolve_user_name",
                               new=AsyncMock(side_effect=lambda uid, **kw: uid)),
                  patch.object(PLUGIN.bundled_slack.SlackAdapter, "_channel_gate_allows",
                               new=AsyncMock(return_value=True))):  # bundled wake rules: "active thread"
                return await coro_fn()
        return asyncio.run(go())

    def answers(self, user: str, text: str, tagged: bool = False) -> bool:
        """Post a message and report whether Hermes would answer it."""
        self.post(user, f"<@{HERMES}> {text}" if tagged else text)
        return self._run(lambda: self.hermes._channel_gate_allows(
            channel_id="C1", routing_text=self.messages[-1]["text"], bot_uid=HERMES,
            is_mentioned=tagged, is_thread_reply=True, event_thread_ts=ROOT_TS, user_id=user,
            team_id="T1", is_dm=False, force_process=False))

    def context(self, after_ts: str = "") -> str:
        """Thread context Hermes gets for the latest message (catch-up when after_ts is given)."""
        return self._run(lambda: self.hermes._fetch_thread_context(
            "C1", ROOT_TS, self.messages[-1]["ts"], "T1", after_ts=after_ts, force_refresh=True))


def test_one_on_one_thread_needs_no_tags():
    thread = Thread()
    thread.post(ME, f"<@{HERMES}> what is our refund policy?")
    thread.post(HERMES, "30 days.")
    assert thread.answers(ME, "and for digital goods?")


def test_colleague_replying_untagged_makes_the_thread_mention_only():
    thread = Thread()
    thread.post(ME, f"<@{HERMES}> what is our refund policy?")
    thread.post(HERMES, "30 days.")
    assert not thread.answers(ALICE, "that changed last month")   # nobody tagged anyone
    assert not thread.answers(ME, "oh really? since when?")       # still humans talking
    assert thread.answers(ME, "update your answer", tagged=True)


def test_tagging_a_colleague_makes_the_thread_mention_only():
    thread = Thread()
    thread.post(ME, f"<@{HERMES}> draft the announcement")
    thread.post(HERMES, "Draft: …")
    assert not thread.answers(ME, f"<@{ALICE}> can you review this?")
    assert not thread.answers(ALICE, "looks good")


def test_forgot_to_tag_then_tag_in_a_follow_up():
    thread = Thread()
    thread.post(ALICE, "Q3 numbers are in")
    assert not thread.answers(ME, "can you summarize the main risks?")  # forgot the tag
    assert thread.answers(ME, "^", tagged=True)
    context = thread.context()  # first time in this thread: cold start
    assert "can you summarize the main risks?" in context and "Q3 numbers are in" in context


def test_rejoining_after_a_long_human_discussion_sees_all_of_it_once():
    thread = Thread()
    thread.post(ME, f"<@{HERMES}> plan the offsite")
    last_seen = thread.post(ME, "budget is 5k")              # Hermes' last processed message
    thread.post(HERMES, "LONG HERMES ANSWER")
    for i in range(80):
        thread.post(ALICE if i % 2 else ME, f"discussion point {i}")
    assert thread.answers(ME, "what did we decide?", tagged=True)
    catch_up = thread.context(after_ts=last_seen)
    assert all(f"discussion point {i}" in catch_up for i in range(80))
    assert "LONG HERMES ANSWER" not in catch_up                # already in its history
    assert "plan the offsite" not in catch_up                  # already in its history


def test_very_long_catch_up_says_what_was_left_out():
    thread = Thread()
    last_seen = thread.post(ME, f"<@{HERMES}> start")
    for i in range(250):
        thread.post(ALICE, f"msg {i}")
    thread.post(ME, "summarize")
    catch_up = thread.context(after_ts=last_seen)
    assert "msg 249" in catch_up and "msg 10 " not in catch_up
    assert "50 older thread messages are not shown" in catch_up
    assert "slack_read_thread channel=C1" in catch_up


if __name__ == "__main__":
    run_tests(globals(), "scenarios")

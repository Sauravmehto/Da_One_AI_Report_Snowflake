"""Condensing a long chat's earlier turns (app/agent/history.py).

The saving is only worth having if nothing a user relies on changes: every
message stays stored and on screen, the model is never handed a history the API
rejects, the recent turns reach it word for word, and a summary that fails or
comes back cut off leaves the turn to go ahead exactly as it did before.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from app.agent import history
from app.agent.loop import run_turn
from app.conversation import Conversation
from app.conversation.base import SUMMARY_PREFIX
from app.engine import llm
from app.engine.connectors.snowflake import client as sc
from tests.test_token_accounting import CTX, _FakeClient, _final, _text_block, _usage


def _settings(trigger=1, keep=2):
    return SimpleNamespace(ANTHROPIC_MODEL_AGENT="test-model", AGENT_MAX_ITERATIONS=3,
                           AGENT_MAX_TOKENS=16000, CHAT_SUMMARY_TRIGGER_TOKENS=trigger,
                           CHAT_SUMMARY_KEEP_TURNS=keep)


def _chat(turns=4):
    """User question -> tool call -> tool result -> answer, `turns` times."""
    c = Conversation(conversation_id="conv_7", owner="U", database="ECOMMERCE_DB")
    for t in range(turns):
        c.append("user", [{"type": "text", "text": f"question {t}"}])
        c.append("assistant", [
            {"type": "thinking", "thinking": f"private reasoning {t}", "signature": "sig"},
            {"type": "tool_use", "id": f"tu_{t}", "name": "plan_dashboard",
             "input": {"description": f"dashboard {t}"}},
        ])
        c.append("user", [{"type": "tool_result", "tool_use_id": f"tu_{t}",
                           "content": json.dumps({"plan_id": f"plan_{t}", "panel_count": 12})}])
        c.append("assistant", [{"type": "text", "text": f"answer {t}: report_id rpt_{t}"}])
    return c


class _Summariser:
    def __init__(self, text="- Dashboard rpt_0 planned (plan_0).", stop_reason="end_turn", fail=False):
        self.text, self.stop_reason, self.fail = text, stop_reason, fail
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("overloaded")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)],
                               stop_reason=self.stop_reason, usage=None)


@pytest.fixture(autouse=True)
def no_audit_writes(monkeypatch):
    rows = []
    monkeypatch.setattr(sc, "write_audit", rows.append)
    return rows


# --- what the model is sent ------------------------------------------------------------


def test_without_a_summary_nothing_changes():
    c = _chat(3)
    assert c.to_model_messages() == [{"role": m.role, "content": m.content} for m in c.messages]


def test_a_summary_replaces_the_turns_it_covers_and_leads_the_next_user_turn():
    c = _chat(4)
    c.summary, c.summary_through = "- rpt_0 planned", 8   # covers turns 0 and 1
    sent = c.to_model_messages()
    assert sent[0]["role"] == "user"
    assert sent[0]["content"][0]["text"] == SUMMARY_PREFIX + "- rpt_0 planned"
    assert sent[0]["content"][1]["text"] == "question 2"
    assert "question 0" not in json.dumps(sent) and "question 1" not in json.dumps(sent)
    assert len(sent) == 8


def test_every_message_is_still_stored_for_the_screen():
    c = _chat(4)
    c.summary, c.summary_through = "- s", 8
    c.to_model_messages()
    assert len(c.messages) == 16
    assert c.messages[0].content[0]["text"] == "question 0"


def test_the_prefix_is_identical_turn_to_turn_so_the_cache_holds():
    c = _chat(4)
    c.summary, c.summary_through = "- s", 8
    first = c.to_model_messages()
    c.append("user", [{"type": "text", "text": "one more"}])
    assert c.to_model_messages()[:len(first)] == first


def test_a_summary_ending_before_an_assistant_message_still_starts_with_the_user():
    c = _chat(2)
    c.summary, c.summary_through = "- s", 3   # an odd cut: the assistant's answer is next
    sent = c.to_model_messages()
    assert sent[0]["role"] == "user" and sent[0]["content"][0]["text"].startswith(SUMMARY_PREFIX)
    assert [m["role"] for m in sent[:2]] == ["user", "assistant"]


def test_a_summary_past_the_end_is_harmless():
    c = _chat(1)
    c.summary, c.summary_through = "- s", 99
    assert c.to_model_messages() == [
        {"role": "user", "content": [{"type": "text", "text": SUMMARY_PREFIX + "- s"}]}
    ]


# --- when to condense -----------------------------------------------------------------------


def test_a_short_chat_is_left_alone():
    assert history.summary_cut(_chat(4), _settings(trigger=1_000_000)) is None


def test_condensing_can_be_switched_off():
    assert history.summary_cut(_chat(8), _settings(trigger=0)) is None


def test_the_recent_turns_are_always_kept():
    assert history.summary_cut(_chat(2), _settings(trigger=1, keep=2)) is None


def test_the_cut_falls_on_a_user_turn_so_no_tool_call_loses_its_result():
    c = _chat(5)
    cut = history.summary_cut(c, _settings(trigger=1, keep=2))
    assert cut == 12
    message = c.messages[cut]
    assert message.role == "user" and message.content[0]["type"] == "text"
    # Every tool call before the cut is answered before the cut.
    before = json.dumps([m.content for m in c.messages[:cut]])
    assert all(before.count(f'"tu_{t}"') == 2 for t in range(3))


def test_an_existing_summary_is_not_redone_for_the_same_turns():
    c = _chat(5)
    c.summary, c.summary_through = "- s", 12
    assert history.summary_cut(c, _settings(trigger=1, keep=2)) is None


# --- writing the summary -----------------------------------------------------------------------


def test_the_summary_is_written_from_the_older_turns_only(monkeypatch):
    model = _Summariser()
    monkeypatch.setattr(llm, "_get_client", lambda: model)
    c = _chat(4)
    assert history.condense(c, _settings(), 8)
    assert (c.summary, c.summary_through) == ("- Dashboard rpt_0 planned (plan_0).", 8)
    sent = model.requests[0]["messages"][0]["content"]
    assert "question 1" in sent and "rpt_1" in sent and "plan_dashboard" in sent
    assert "question 2" not in sent
    assert "private reasoning" not in sent
    assert model.requests[0]["model"] == llm.MODEL_SUMMARY


def test_a_second_summary_carries_the_first_forward(monkeypatch):
    model = _Summariser(text="- newer")
    monkeypatch.setattr(llm, "_get_client", lambda: model)
    c = _chat(6)
    c.summary, c.summary_through = "- rpt_0 planned", 8
    assert history.condense(c, _settings(), 16)
    sent = model.requests[0]["messages"][0]["content"]
    assert "EARLIER SUMMARY" in sent and "- rpt_0 planned" in sent
    assert "question 0" not in sent and "question 2" in sent


@pytest.mark.parametrize("model", [
    _Summariser(fail=True), _Summariser(text=""), _Summariser(stop_reason="max_tokens"),
])
def test_a_failed_empty_or_cut_off_summary_changes_nothing(monkeypatch, model):
    monkeypatch.setattr(llm, "_get_client", lambda: model)
    c = _chat(4)
    assert not history.condense(c, _settings(), 8)
    assert (c.summary, c.summary_through) == (None, 0)


def test_the_summary_call_is_audited_against_the_conversation(monkeypatch, no_audit_writes):
    monkeypatch.setattr(llm, "_get_client", lambda: _Summariser())
    from app.engine import audit
    with audit.conversation("conv_7"):
        history.condense(_chat(4), _settings(), 8)
    assert no_audit_writes[0]["tool_name"] == "ai:chat_summary"
    assert no_audit_writes[0]["conversation_id"] == "conv_7"


# --- in a real turn ----------------------------------------------------------------------------


async def _turn(conversation, settings, client):
    return [e async for e in run_turn(client=client, settings=settings, conversation=conversation,
                                      user_message="and now?", ctx=CTX)]


async def test_a_long_chat_is_condensed_before_the_turn_and_the_turn_uses_it(monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: _Summariser(text="- earlier facts"))
    chat = _chat(4)
    client = _FakeClient([_final([_text_block("Done")], "end_turn", _usage(10, 5))])
    events = await _turn(chat, _settings(trigger=1, keep=2), client)

    kinds = [e.type if hasattr(e, "type") else e[0] for e in events]
    assert kinds[0] == "message_start" and kinds[1] == "thinking"
    sent = client.messages.calls[0]["messages"]
    assert sent[0]["content"][0]["text"].startswith(SUMMARY_PREFIX)
    assert "question 0" not in json.dumps(sent)
    assert "question 3" in json.dumps(sent) and "and now?" in json.dumps(sent)
    assert chat.summary == "- earlier facts" and len(chat.messages) == 18


async def test_if_the_summary_fails_the_turn_goes_ahead_with_the_whole_history(monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: _Summariser(fail=True))
    chat = _chat(4)
    client = _FakeClient([_final([_text_block("Done")], "end_turn", _usage(10, 5))])
    await _turn(chat, _settings(trigger=1, keep=2), client)
    sent = json.dumps(client.messages.calls[0]["messages"])
    assert "question 0" in sent and SUMMARY_PREFIX[:20] not in sent
    assert chat.summary is None


async def test_rounds_within_a_turn_are_never_condensed_between(monkeypatch):
    summariser = _Summariser(text="- s")
    monkeypatch.setattr(llm, "_get_client", lambda: summariser)
    from tests.test_token_accounting import _tool_block
    client = _FakeClient([
        _final([_tool_block("tu_x", "explain_concept", {"term": "GMV"})], "tool_use", _usage(10, 5)),
        _final([_text_block("Done")], "end_turn", _usage(10, 5)),
    ])
    await _turn(_chat(4), _settings(trigger=1, keep=2), client)
    assert len(summariser.requests) == 1

    def unmarked(messages):
        text = json.dumps(messages)
        return json.loads(text.replace(', "cache_control": {"type": "ephemeral", "ttl": "1h"}', ""))

    first, second = (unmarked(c["messages"]) for c in client.messages.calls)
    assert second[:len(first)] == first      # round two extends round one; nothing was re-cut
    assert second[0]["content"][0]["text"].startswith(SUMMARY_PREFIX)


# --- stored safely ------------------------------------------------------------------------------


def test_the_summary_survives_a_save_and_old_chats_still_load():
    c = _chat(2)
    c.summary, c.summary_through = "- s", 4
    again = Conversation.model_validate_json(c.model_dump_json())
    assert (again.summary, again.summary_through) == ("- s", 4)

    old = json.loads(_chat(1).model_dump_json())
    old.pop("summary"), old.pop("summary_through")
    loaded = Conversation.model_validate(old)
    assert (loaded.summary, loaded.summary_through) == (None, 0)


def test_the_real_settings_turn_it_on_with_sane_defaults():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["CHAT_SUMMARY_TRIGGER_TOKENS"].default == 50000
    assert fields["CHAT_SUMMARY_KEEP_TURNS"].default == 4


# --- how often it condenses ---------------------------------------------------------
#
# A chat building dashboards has turns so large that the turns it always keeps
# are over the trigger by themselves. Condensing then left it over the trigger,
# and the next turn condensed again: every turn re-wrote the kept history to the
# cache at the write price and re-summarised its own summary.


def _big_turn(c, t, rounds=10, chars=4000):
    c.append("user", [{"type": "text", "text": f"build {t}"}])
    for r in range(rounds):
        c.append("assistant", [{"type": "tool_use", "id": f"tu_{t}_{r}",
                                "name": "run_safe_sql", "input": {"sql": "SELECT 1"}}])
        c.append("user", [{"type": "tool_result", "tool_use_id": f"tu_{t}_{r}",
                           "content": "x" * chars}])
    c.append("assistant", [{"type": "text", "text": f"done {t}"}])


def _condensed_turns(settings, turns=20):
    c = Conversation(conversation_id="conv_big", owner="U", database="ECOMMERCE_DB")
    fired = []
    for t in range(1, turns + 1):
        _big_turn(c, t)
        cut = history.summary_cut(c, settings)
        if cut is not None:
            c.summary, c.summary_through = "- facts so far", cut
            fired.append(t)
    return fired


def test_a_build_chat_is_not_condensed_on_every_turn():
    one_turn = history.estimated_tokens(
        [{"role": "user", "content": [{"type": "tool_result", "content": "x" * 4000}]}] * 10
    )
    # Four kept turns come to well over this trigger on their own.
    fired = _condensed_turns(_settings(trigger=one_turn * 3, keep=4))

    assert fired, "a long chat must still be condensed"
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert all(gap >= 2 for gap in gaps), f"condensed on consecutive turns: {fired}"


def test_a_summary_absorbs_at_least_a_trigger_of_new_history():
    c = _chat(6)
    trigger = history.estimated_tokens(
        [{"role": m.role, "content": m.content} for m in c.messages]
    )  # the whole chat is exactly one trigger's worth
    settings = _settings(trigger=trigger, keep=2)

    # Everything before the last two turns is less than that, so nothing is cut.
    assert history.summary_cut(c, settings) is None

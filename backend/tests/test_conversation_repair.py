"""A chat interrupted mid-tool must not be broken for good.

Reported from testing: every message in a long conversation failed with

    400 messages.164: `tool_use` ids were found without `tool_result` blocks
    immediately after

The loop stored the model's tool call before running the tool, and stored the
result only after. A turn cut off in between (tab closed, Stop pressed, a proxy
dropping a quiet stream during a slow dashboard edit) was saved with the call
and no result. The API refuses that history, so the conversation could never
be used again.

Two halves, both tested: histories already in that state are repaired on the
way to the model, and the loop no longer writes new ones.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent import loop as loop_mod
from app.agent.loop import run_turn
from app.agent.tools import ToolContext
from app.conversation.base import (
    INTERRUPTED_TOOL_RESULT,
    Conversation,
    repair_tool_pairs,
)


def user(*blocks):
    return {"role": "user", "content": list(blocks)}


def assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def text(t):
    return {"type": "text", "text": t}


def call(tid, name="edit_dashboard"):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def result(tid, content="ok"):
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def results_in(message):
    return [b for b in message["content"] if b.get("type") == "tool_result"]


# --- repair on the way to the model ------------------------------------------


def test_the_reported_shape_is_repaired():
    """A call with no result, then the user's next message: exactly the history
    that returned the 400 on every send."""
    history = [
        user(text("add the panels")),
        assistant(text("Preparing the edit"), call("toolu_01Kz6")),
        user(text("yes, retry now")),
    ]
    repaired = repair_tool_pairs(history)

    following = repaired[2]
    assert following["role"] == "user"
    assert [b["tool_use_id"] for b in results_in(following)] == ["toolu_01Kz6"]
    assert results_in(following)[0]["is_error"] is True
    assert results_in(following)[0]["content"] == INTERRUPTED_TOOL_RESULT
    # Results come first, and the user's words survive.
    assert following["content"][0]["type"] == "tool_result"
    assert following["content"][-1] == text("yes, retry now")


def test_a_call_left_at_the_end_gets_an_answer_message():
    repaired = repair_tool_pairs([user(text("go")), assistant(call("t1"))])
    assert repaired[-1]["role"] == "user"
    assert results_in(repaired[-1])[0]["tool_use_id"] == "t1"


def test_only_the_unanswered_call_of_several_is_filled():
    history = [
        user(text("go")),
        assistant(call("t1"), call("t2")),
        user(result("t1", "done")),
    ]
    got = results_in(repair_tool_pairs(history)[2])
    assert [b["tool_use_id"] for b in got] == ["t1", "t2"]
    assert got[0]["content"] == "done"
    assert got[1]["content"] == INTERRUPTED_TOOL_RESULT


def test_a_result_that_answers_nothing_is_dropped():
    """The mirror-image 400: a tool_result whose call is not directly before it."""
    history = [user(text("hi")), assistant(text("hello")), user(result("ghost"), text("and?"))]
    repaired = repair_tool_pairs(history)
    assert repaired[2]["content"] == [text("and?")]


def test_a_message_of_only_stray_results_disappears():
    history = [user(text("hi")), assistant(text("hello")), user(result("ghost"))]
    assert len(repair_tool_pairs(history)) == 2


def test_a_healthy_history_is_unchanged():
    history = [
        user(text("go")),
        assistant(text("running"), call("t1")),
        user(result("t1")),
        assistant(text("done")),
    ]
    assert repair_tool_pairs(history) == history


def test_stored_messages_are_not_modified():
    following = user(text("again"))
    history = [user(text("go")), assistant(call("t1")), following]
    repair_tool_pairs(history)
    assert following == user(text("again"))


def test_the_repair_is_stable_so_caching_still_works():
    """Prompt caching matches on exact bytes. A repair that differed turn to turn
    would bust the cache for the whole conversation after the break."""
    history = [user(text("go")), assistant(call("t1")), user(text("again"))]
    assert repair_tool_pairs(history) == repair_tool_pairs(history)


def test_the_conversation_sends_the_repaired_history():
    conv = Conversation(conversation_id="c1", owner="U")
    conv.append("user", [text("go")])
    conv.append("assistant", [call("t1")])
    conv.append("user", [text("again")])
    sent = conv.to_model_messages()
    assert results_in(sent[2])[0]["tool_use_id"] == "t1"
    # ...while what is stored stays as it happened.
    assert conv.messages[2].content == [text("again")]


# --- the loop never stores a call without its answer -----------------------


CTX = ToolContext(database="PRIVATECREDIT", snowflake_user="U", role="R")
SETTINGS = SimpleNamespace(ANTHROPIC_MODEL_AGENT="m", AGENT_MAX_ITERATIONS=3,
                           AGENT_MAX_TOKENS=16000, CHAT_SUMMARY_TRIGGER_TOKENS=0,
                           CHAT_SUMMARY_KEEP_TURNS=4)


def _block(kind, **kw):
    return SimpleNamespace(type=kind, **kw,
                           model_dump=lambda **_: {"type": kind, **kw})


class _Stream:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            if False:
                yield None
        return gen()

    async def get_final_message(self):
        return self._final


def _client(*finals):
    queue = list(finals)
    return SimpleNamespace(messages=SimpleNamespace(stream=lambda **_: _Stream(queue.pop(0))))


def _tool_turn(*ids):
    return SimpleNamespace(
        content=[_block("tool_use", id=i, name="edit_dashboard", input={}) for i in ids],
        stop_reason="tool_use", usage=None)


async def _drain(client, conv):
    return [e async for e in run_turn(client=client, settings=SETTINGS,
                                      conversation=conv, user_message="edit", ctx=CTX)]


def _stored_ok(conv):
    """What the API checks: every call answered in the very next message."""
    history = [{"role": m.role, "content": m.content} for m in conv.messages]
    return repair_tool_pairs(history) == history


async def test_a_turn_cancelled_mid_tool_still_stores_an_answer(monkeypatch):
    """The disconnect path. Cancellation is not an Exception, so it went straight
    past dispatch's own error handling."""
    async def hangs_up(name, tool_input, ctx):
        raise asyncio.CancelledError

    monkeypatch.setattr(loop_mod, "dispatch", hangs_up)
    conv = Conversation(conversation_id="c1", owner="U")

    with pytest.raises(asyncio.CancelledError):
        await _drain(_client(_tool_turn("t1")), conv)

    assert conv.messages[-1].role == "user"
    assert conv.messages[-1].content[0]["tool_use_id"] == "t1"
    assert conv.messages[-1].content[0]["content"] == INTERRUPTED_TOOL_RESULT
    assert _stored_ok(conv)


async def test_the_finished_call_keeps_its_real_result(monkeypatch):
    """Two calls, the second interrupted: the first's genuine result must not be
    overwritten by the stand-in."""
    async def second_hangs_up(name, tool_input, ctx):
        if second_hangs_up.calls:
            raise asyncio.CancelledError
        second_hangs_up.calls += 1
        return {"status": "ok", "value": 42}
    second_hangs_up.calls = 0

    monkeypatch.setattr(loop_mod, "dispatch", second_hangs_up)
    conv = Conversation(conversation_id="c1", owner="U")

    with pytest.raises(asyncio.CancelledError):
        await _drain(_client(_tool_turn("t1", "t2")), conv)

    first, second = conv.messages[-1].content
    assert first["tool_use_id"] == "t1" and "42" in first["content"]
    assert second["tool_use_id"] == "t2" and second["content"] == INTERRUPTED_TOOL_RESULT
    assert _stored_ok(conv)


async def test_an_unexpected_error_after_the_tool_still_stores_an_answer(monkeypatch):
    async def fine(name, tool_input, ctx):
        return {"status": "ok"}

    def explodes(result):
        raise RuntimeError("boom")

    monkeypatch.setattr(loop_mod, "dispatch", fine)
    monkeypatch.setattr(loop_mod, "truncate_tool_result", explodes)
    conv = Conversation(conversation_id="c1", owner="U")

    with pytest.raises(RuntimeError):
        await _drain(_client(_tool_turn("t1")), conv)
    assert _stored_ok(conv)


async def test_a_reply_cut_off_inside_a_tool_call_stores_an_answer():
    """max_tokens can stop a reply partway through writing a tool call. The loop
    ends the turn there, so the call is answered as never having run."""
    cut = SimpleNamespace(
        content=[_block("text", text="Adding the"),
                 _block("tool_use", id="t1", name="edit_dashboard", input={})],
        stop_reason="max_tokens", usage=None)
    conv = Conversation(conversation_id="c1", owner="U")

    events = await _drain(_client(cut), conv)

    assert any(e.type == "error" for e in events)
    assert conv.messages[-1].content[0]["tool_use_id"] == "t1"
    assert "never run" in conv.messages[-1].content[0]["content"]
    assert _stored_ok(conv)


async def test_a_normal_tool_round_is_unchanged(monkeypatch):
    async def fine(name, tool_input, ctx):
        return {"status": "ok"}

    monkeypatch.setattr(loop_mod, "dispatch", fine)
    done = SimpleNamespace(content=[_block("text", text="Done")],
                           stop_reason="end_turn", usage=None)
    conv = Conversation(conversation_id="c1", owner="U")

    await _drain(_client(_tool_turn("t1"), done), conv)

    assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    assert conv.messages[2].content[0]["tool_use_id"] == "t1"
    assert "is_error" not in conv.messages[2].content[0]

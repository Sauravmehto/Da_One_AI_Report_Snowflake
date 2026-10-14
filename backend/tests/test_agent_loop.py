"""Agent loop mechanics, with the model faked.

Covers the parts that break silently in production: tool-result plumbing back
into the next request, the iteration ceiling, and failure handling. A model
error must surface as an event, never as an exception — an exception would
abort the SSE stream mid-answer and leave the UI hanging.
"""

from types import SimpleNamespace

import pytest

from app.agent.loop import Event, format_sse, run_turn
from app.agent.tools import ToolContext
from app.conversation.base import Conversation

CTX = ToolContext(database="PRIVATECREDIT", snowflake_user="SVC_USER", role="R")


class _FakeStream:
    """Mirrors the SDK's MessageStream closely enough for the loop.

    The loop iterates the stream itself rather than `text_stream`, because that
    helper yields text deltas only and the loop also forwards the model's
    reasoning. So the double has to be async-iterable and emit the same tagged
    events the SDK does: `.type` of "text" with `.text`, or "thinking" with
    `.thinking`.
    """

    def __init__(self, texts, final, thinking=()):
        self._texts = texts
        self._thinking = thinking
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for t in self._thinking:
                yield SimpleNamespace(type="thinking", thinking=t)
            for t in self._texts:
                yield SimpleNamespace(type="text", text=t)
        return gen()

    @property
    def text_stream(self):
        async def gen():
            for t in self._texts:
                yield t
        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, scripted):
        # scripted: list of (texts, final_message) — one entry per model round
        self._scripted = list(scripted)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        texts, final = self._scripted.pop(0)
        return _FakeStream(texts, final)


class _FakeClient:
    def __init__(self, scripted):
        self.messages = _FakeMessages(scripted)


def _text_block(text):
    return SimpleNamespace(type="text", text=text,
                           model_dump=lambda **k: {"type": "text", "text": text})


def _tool_block(tid, name, tool_input):
    return SimpleNamespace(
        type="tool_use", id=tid, name=name, input=tool_input,
        model_dump=lambda **k: {"type": "tool_use", "id": tid,
                                "name": name, "input": tool_input},
    )


def _final(blocks, stop_reason, usage=None):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)


SETTINGS = SimpleNamespace(
    ANTHROPIC_MODEL_AGENT="test-model",
    AGENT_MAX_ITERATIONS=3,
    AGENT_MAX_TOKENS=16000,
    CHAT_SUMMARY_TRIGGER_TOKENS=0,  # condensing is covered in test_chat_summary.py
    CHAT_SUMMARY_KEEP_TURNS=4,
)


async def _collect(client, conversation, message="hi"):
    return [
        e async for e in run_turn(
            client=client, settings=SETTINGS, conversation=conversation,
            user_message=message, ctx=CTX,
        )
    ]


async def test_text_only_turn_streams_and_stops():
    client = _FakeClient([(["Hel", "lo"], _final([_text_block("Hello")], "end_turn"))])
    conv = Conversation(conversation_id="c1", owner="SVC_USER", database="PRIVATECREDIT")

    events = await _collect(client, conv)
    types = [e.type for e in events]

    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert "".join(e.data["delta"] for e in events if e.type == "text") == "Hello"
    # user turn + assistant reply recorded
    assert [m.role for m in conv.messages] == ["user", "assistant"]


async def test_tool_result_is_fed_back_into_the_next_request():
    """The failure this guards: results not reaching the follow-up request, so
    the model answers as if the tool never ran."""
    client = _FakeClient([
        ([], _final([_tool_block("tu_1", "explain_concept", {"term": "DSCR"})], "tool_use")),
        (["Done"], _final([_text_block("Done")], "end_turn")),
    ])
    conv = Conversation(conversation_id="c1", owner="SVC_USER", database="PRIVATECREDIT")

    events = await _collect(client, conv)
    assert [e.type for e in events].count("tool_start") == 1
    assert [e.type for e in events].count("tool_end") == 1

    # Second model call must carry the tool_result.
    second_request = client.messages.calls[1]["messages"]
    tool_results = [
        b for m in second_request for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "tu_1"
    assert "DSCR" in tool_results[0]["content"]


async def test_failed_tool_is_marked_is_error_for_the_model():
    client = _FakeClient([
        ([], _final([_tool_block("tu_1", "no_such_tool", {})], "tool_use")),
        (["ok"], _final([_text_block("ok")], "end_turn")),
    ])
    conv = Conversation(conversation_id="c1", owner="SVC_USER", database="PRIVATECREDIT")

    events = await _collect(client, conv)
    end = next(e for e in events if e.type == "tool_end")
    assert end.data["ok"] is False

    blocks = [b for m in client.messages.calls[1]["messages"] for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert blocks[0].get("is_error") is True


async def test_iteration_ceiling_terminates_with_an_explicit_error():
    """A model that keeps asking for tools must stop and SAY it stopped —
    silently truncating would present an incomplete answer as final."""
    looping = [
        ([], _final([_tool_block(f"tu_{i}", "explain_concept", {"term": "DSCR"})], "tool_use"))
        for i in range(SETTINGS.AGENT_MAX_ITERATIONS)
    ]
    client = _FakeClient(looping)
    conv = Conversation(conversation_id="c1", owner="SVC_USER", database="PRIVATECREDIT")

    events = await _collect(client, conv)
    last = events[-1]
    assert last.type == "error"
    assert str(SETTINGS.AGENT_MAX_ITERATIONS) in last.data["message"]


async def test_model_failure_becomes_an_error_event_not_an_exception():
    class Boom:
        def stream(self, **kwargs):
            raise RuntimeError("api down")

    client = SimpleNamespace(messages=Boom())
    conv = Conversation(conversation_id="c1", owner="SVC_USER", database="PRIVATECREDIT")

    events = await _collect(client, conv)
    assert events[-1].type == "error"
    assert "api down" in events[-1].data["message"]
    # The user's message is still recorded, so the turn is not lost.
    assert conv.messages[0].role == "user"


async def test_chart_is_persisted_and_kept_out_of_the_model_context(monkeypatch):
    """Two things at once, because they are the same design decision.

    The panel must be stored on the conversation, or a reopened chat shows the
    commentary with nothing above it. And it must NOT reach the model, which
    gets a description of what was plotted instead of the rows.
    """
    panel = {
        "title": "Principal by Status",
        "chart_type": "bar",
        "x": "LOAN_STATUS",
        "y": "PRINCIPAL",
        "columns": ["LOAN_STATUS", "PRINCIPAL"],
        "rows": [["CURRENT", 4321], ["DEFAULT", 99]],
        "row_count": 2,
    }

    async def fake_dispatch(name, tool_input, ctx):
        return {"panel": panel, "sql": "SELECT 1", "row_count": 2}

    monkeypatch.setattr("app.agent.loop.dispatch", fake_dispatch)

    client = _FakeClient([
        ([], _final([_tool_block("tu_1", "show_chart", {})], "tool_use")),
        (["Mostly current."], _final([_text_block("Mostly current.")], "end_turn")),
    ])
    conv = Conversation(conversation_id="c1", owner="SVC_USER", database="PRIVATECREDIT")

    events = await _collect(client, conv)

    artifact = next(e for e in events if e.type == "artifact")
    assert artifact.data["kind"] == "chart"
    assert artifact.data["panel"]["rows"] == panel["rows"]

    # Survives the turn, keyed by the tool call that drew it.
    assert conv.artifacts["tu_1"]["panel"]["title"] == "Principal by Status"

    # A round-trip through storage must not drop it.
    assert Conversation.model_validate_json(
        conv.model_dump_json()
    ).artifacts["tu_1"]["panel"]["row_count"] == 2

    # The model sees the summary, not the data.
    sent = next(
        b for m in client.messages.calls[1]["messages"] for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert "4321" not in sent["content"]
    assert "rendered" in sent["content"]


def test_sse_framing():
    frame = format_sse(Event("tool_start", {"name": "run_safe_sql"}))
    assert frame.startswith("event: tool_start\n")
    assert frame.endswith("\n\n")
    assert '"name": "run_safe_sql"' in frame


# --- the silent ending -----------------------------------------------------
#
# A turn that ran out of room used to stop dead: stop_reason "max_tokens" was
# reported as a plain message_stop, the UI had nothing to say about it, and the
# reply simply ceased mid-thought. Measured against the real model, the first
# turn of a dashboard request spends ~5.6k output tokens — most of it reasoning
# before the tool call — so the old 4096 ceiling truncated it every time and the
# app looked like it had hung.

def _conv():
    return Conversation(conversation_id="c1", owner="SVC_USER", database="LOGISTICS_DB")


async def test_running_out_of_tokens_is_reported_not_swallowed():
    client = _FakeClient([([], _final([_text_block("Half a thou")], "max_tokens"))])
    events = await _collect(client, _conv())

    assert [e.type for e in events][-1] == "error", (
        "a truncated turn must say so; message_stop renders as nothing at all"
    )
    assert "cut off" in events[-1].data["message"]


async def test_a_normal_turn_still_ends_quietly():
    client = _FakeClient([([], _final([_text_block("done")], "end_turn"))])
    events = await _collect(client, _conv())

    assert [e.type for e in events][-1] == "message_stop"
    assert not [e for e in events if e.type == "error"]


async def test_reasoning_is_streamed_so_the_wait_is_visible():
    """Adaptive thinking runs before the first word — against the real model,
    4.7k characters of it. Without forwarding that, the user watches an empty
    pane for the whole of it and concludes the app is stuck."""
    client = _FakeClient([([], _final([_text_block("hi")], "end_turn"))])
    client.messages.stream = lambda **kw: _FakeStream(
        ["hi"],
        _final([_text_block("hi")], "end_turn"),
        thinking=["weighing the options"],
    )
    events = await _collect(client, _conv())

    thinking = [e for e in events if e.type == "thinking"]
    assert thinking, "reasoning was not forwarded to the client"
    assert thinking[0].data["delta"] == "weighing the options"


async def test_the_request_asks_for_room_and_for_visible_reasoning():
    """Both are settings on the request itself, and both were the bug: thinking
    was left implicit (so it streamed as empty blocks) and max_tokens was small
    enough that reasoning alone could exhaust it."""
    client = _FakeClient([([], _final([_text_block("hi")], "end_turn"))])
    await _collect(client, _conv())

    sent = client.messages.calls[0]
    assert sent["max_tokens"] == SETTINGS.AGENT_MAX_TOKENS
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}

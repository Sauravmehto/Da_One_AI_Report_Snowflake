"""What a turn costs: caching the transcript, and recording the bill.

Both of these fail silently, which is why they are tested here rather than
trusted. A cache breakpoint that is never read looks exactly like no caching at
all — a slightly larger invoice at the end of the month. And a turn that is not
audited leaves no trace to notice it by.

The sharp edge is in the middle test: `to_model_messages()` hands back the SAME
list objects the Conversation holds, so marking the last block in place would
persist the breakpoint, replay it next turn, and accumulate one dead marker per
turn until the request exceeds the four breakpoints the API allows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.loop import _with_cache_breakpoint, run_turn
from app.agent.tools import ToolContext
from app.conversation.base import Conversation
from app.engine import audit
from app.engine.connectors.snowflake import client as sc

CTX = ToolContext(database="PRIVATECREDIT", snowflake_user="SVC_USER", role="R")

SETTINGS = SimpleNamespace(
    ANTHROPIC_MODEL_AGENT="test-model",
    AGENT_MAX_ITERATIONS=3,
    AGENT_MAX_TOKENS=16000,
    CHAT_SUMMARY_TRIGGER_TOKENS=0,  # condensing is covered in test_chat_summary.py
    CHAT_SUMMARY_KEEP_TURNS=4,
)


# --- doubles ----------------------------------------------------------------


class _FakeStream:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            return
            yield  # pragma: no cover - makes this an async generator
        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self._scripted.pop(0))


class _FakeClient:
    def __init__(self, scripted):
        self.messages = _FakeMessages(scripted)


class _Boom:
    """A client whose stream raises, to exercise the error exit."""

    def __init__(self):
        self.messages = self

    def stream(self, **kwargs):
        raise RuntimeError("upstream exploded")


def _text_block(text):
    return SimpleNamespace(
        type="text", text=text,
        model_dump=lambda **k: {"type": "text", "text": text},
    )


def _tool_block(tid, name, tool_input):
    return SimpleNamespace(
        type="tool_use", id=tid, name=name, input=tool_input,
        model_dump=lambda **k: {"type": "tool_use", "id": tid,
                                "name": name, "input": tool_input},
    )


def _usage(inp, out, read=0, write=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=read, cache_creation_input_tokens=write,
    )


def _final(blocks, stop_reason, usage=None):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)


def _conversation():
    return Conversation(
        conversation_id="conv_1", owner="SVC_USER", database="PRIVATECREDIT"
    )


@pytest.fixture
def audited(monkeypatch):
    """Capture audit rows instead of writing them to Snowflake."""
    rows: list[dict] = []

    def fake_log_event(tool, status, snowflake_user=None, snowflake_role=None,
                       **kwargs):
        rows.append({"tool": tool, "status": status,
                     "snowflake_user": snowflake_user, **kwargs})

    monkeypatch.setattr(audit, "log_event", fake_log_event)
    return rows


async def _collect(client, conversation, message="hi"):
    return [
        e async for e in run_turn(
            client=client, settings=SETTINGS, conversation=conversation,
            user_message=message, ctx=CTX,
        )
    ]


def _breakpoints(messages):
    return [
        block
        for m in messages
        for block in m["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]


# --- the helper in isolation ------------------------------------------------


def test_the_breakpoint_lands_on_the_final_block():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "one"}]},
        {"role": "user", "content": [
            {"type": "text", "text": "two"},
            {"type": "text", "text": "three"},
        ]},
    ]
    marked = _with_cache_breakpoint(messages)

    assert marked[-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert len(_breakpoints(marked)) == 1


# --- one-hour chat cache ------------------------------------------------------
#
# Building a dashboard runs the planner inside a turn, and readers pause over a
# plan or a chart; a five-minute entry had expired by the next round either way,
# and the whole conversation was re-sent at full price ("$0.33 for a one-line
# reply after a 10-minute break").


def _markers(blocks):
    return [b["cache_control"] for b in blocks if isinstance(b, dict) and "cache_control" in b]


def test_every_chat_cache_marker_lives_an_hour(monkeypatch):
    """All of them, system and conversation alike: the API rejects a one-hour
    entry that follows a five-minute one, so one lagging marker would fail
    every chat request."""
    from app.agent import prompts

    monkeypatch.setattr(prompts.metadata_store, "build_context", lambda db: "ctx", raising=False)
    monkeypatch.setattr(prompts.metadata_store, "dashboard_heuristics", lambda db: "", raising=False)
    conversation = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    markers = (
        _markers(prompts.build_system_prompt("ECOMMERCE_DB", "ctx"))
        + _markers(prompts.build_dashboard_system_prompt("ECOMMERCE_DB", "panels"))
        + _markers(_with_cache_breakpoint(conversation)[-1]["content"])
    )
    assert len(markers) == 3
    assert all(m == {"type": "ephemeral", "ttl": "1h"} for m in markers)


async def test_the_request_sent_carries_one_hour_markers_only(audited):
    client = _FakeClient([_final([_text_block("Done")], "end_turn", _usage(10, 5))])
    await _collect(client, _conversation())
    request = client.messages.calls[0]
    found = _markers(request["system"]) + [
        m for msg in request["messages"] if isinstance(msg["content"], list)
        for m in _markers(msg["content"])
    ]
    assert found and all(m.get("ttl") == "1h" for m in found)


def test_the_assistant_is_told_not_to_probe_before_building():
    from app.agent import prompts

    text = prompts.build_system_prompt("ECOMMERCE_DB", "ctx")[0]["text"]
    assert "Do not run test or connection queries" in text


def test_the_helper_does_not_touch_what_it_was_given():
    block = {"type": "text", "text": "one"}
    messages = [{"role": "user", "content": [block]}]

    _with_cache_breakpoint(messages)

    assert "cache_control" not in block
    assert _breakpoints(messages) == []


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "user", "content": []}],
    [{"role": "user", "content": "a plain string"}],
])
def test_shapes_with_nothing_to_mark_are_returned_unchanged(messages):
    assert _with_cache_breakpoint(messages) is messages


# --- the loop ---------------------------------------------------------------


async def test_every_round_sends_exactly_one_breakpoint(audited):
    """One per request, at the end. Two would waste a budget of four; a stale
    one from an earlier turn would read nothing."""
    client = _FakeClient([
        _final([_tool_block("tu_1", "explain_concept", {"term": "DSCR"})],
               "tool_use", _usage(100, 10)),
        _final([_text_block("Done")], "end_turn", _usage(120, 20)),
    ])

    await _collect(client, _conversation())

    assert len(client.messages.calls) == 2
    for call in client.messages.calls:
        marks = _breakpoints(call["messages"])
        assert len(marks) == 1, "exactly one breakpoint per request"
        assert call["messages"][-1]["content"][-1] is marks[0], "must be last"


async def test_the_breakpoint_is_never_written_into_stored_history(audited):
    """The trap. Stored content is the same object the request borrows, so a
    breakpoint set in place would persist, replay, and accumulate."""
    client = _FakeClient([
        _final([_tool_block("tu_1", "explain_concept", {"term": "DSCR"})],
               "tool_use", _usage(100, 10)),
        _final([_text_block("Done")], "end_turn", _usage(120, 20)),
    ])
    conv = _conversation()

    await _collect(client, conv)

    stored = conv.to_model_messages()
    assert _breakpoints(stored) == [], "a breakpoint leaked into saved history"


async def test_a_later_turn_still_sends_exactly_one(audited):
    """What the leak would actually cost: turn N carrying N breakpoints, and a
    400 once that passes four."""
    conv = _conversation()
    for reply in ("first", "second", "third", "fourth", "fifth"):
        client = _FakeClient([
            _final([_text_block(reply)], "end_turn", _usage(100, 10))
        ])
        await _collect(client, conv, message=reply)
        assert len(_breakpoints(client.messages.calls[0]["messages"])) == 1


# --- the audit row ----------------------------------------------------------


async def test_a_turn_is_audited_with_its_totals(audited):
    client = _FakeClient([
        _final([_tool_block("tu_1", "explain_concept", {"term": "DSCR"})],
               "tool_use", _usage(100, 10, read=0, write=900)),
        _final([_text_block("Done")], "end_turn", _usage(120, 20, read=900)),
    ])

    await _collect(client, _conversation(), message="what is DSCR?")

    assert len(audited) == 1
    row = audited[0]
    assert row["tool"] == "agent_turn"
    assert row["status"] == "ok"
    assert row["model"] == "test-model"
    assert row["model_calls"] == 2
    assert row["input_tokens"] == 220
    assert row["output_tokens"] == 30
    assert row["cache_read_tokens"] == 900
    assert row["cache_write_tokens"] == 900
    assert row["conversation_id"] == "conv_1"
    assert row["question"] == "what is DSCR?"
    assert row["snowflake_user"] == "SVC_USER"


async def test_a_turn_that_spends_nothing_writes_no_row(audited):
    """A model call that never happened is not a turn worth a Snowflake INSERT."""
    await _collect(_Boom(), _conversation())
    assert audited == []


async def test_a_failed_turn_is_still_audited(audited):
    """The tokens were spent before it failed. A turn that only appears in the
    log when it succeeds understates the bill."""
    client = _FakeClient([
        _final([_tool_block("tu_1", "explain_concept", {"term": "DSCR"})],
               "tool_use", _usage(100, 10)),
    ])
    conv = _conversation()

    # Second round has nothing scripted, so the stream call raises.
    await _collect(client, conv)

    assert len(audited) == 1
    assert audited[0]["status"] == "error"
    assert audited[0]["input_tokens"] == 100
    assert audited[0]["model_calls"] == 1


async def test_hitting_the_token_ceiling_is_recorded_as_truncated(audited):
    client = _FakeClient([
        _final([_text_block("half an ans")], "max_tokens", _usage(100, 16000)),
    ])

    await _collect(client, _conversation())

    assert audited[0]["status"] == "truncated"
    assert audited[0]["output_tokens"] == 16000


# --- the write, against a table that may predate the columns ----------------


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *args, **kwargs):
        self.conn.executed.append(sql)
        if self.conn.reject and "INPUT_TOKENS" in sql:
            raise Exception(
                "001038 (42601): SQL compilation error: "
                "invalid identifier 'INPUT_TOKENS'"
            )
        return self

    def close(self):
        pass


class _Conn:
    def __init__(self, reject=False):
        self.reject = reject
        self.executed: list[str] = []

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _reset_token_column_state():
    original = sc._audit_token_columns
    sc._audit_token_columns = None
    yield
    sc._audit_token_columns = original


ROW = {"tool_name": "agent_turn", "status": "ok", "input_tokens": 220,
       "model": "test-model", "conversation_id": "conv_1"}


def test_a_migrated_table_records_the_tokens(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(sc, "get_connection", lambda: conn)

    sc.write_audit(ROW)

    assert len(conn.executed) == 1
    assert "INPUT_TOKENS" in conn.executed[0]
    assert sc._audit_token_columns is True


def test_an_unmigrated_table_still_gets_the_row(monkeypatch):
    """The failure this prevents: naming a column that is not there fails the
    INSERT, and because auditing is best-effort the audit trail would simply
    stop appearing with nothing to say why."""
    conn = _Conn(reject=True)
    monkeypatch.setattr(sc, "get_connection", lambda: conn)

    sc.write_audit(ROW)

    assert len(conn.executed) == 2, "rejected, then retried without the columns"
    assert "INPUT_TOKENS" not in conn.executed[1]
    assert "TOOL_NAME" in conn.executed[1]
    assert sc._audit_token_columns is False


def test_the_missing_columns_are_only_discovered_once(monkeypatch):
    conn = _Conn(reject=True)
    monkeypatch.setattr(sc, "get_connection", lambda: conn)

    sc.write_audit(ROW)
    sc.write_audit(ROW)
    sc.write_audit(ROW)

    # One wasted statement in total, not one per call.
    assert len(conn.executed) == 4
    assert [s for s in conn.executed if "INPUT_TOKENS" in s] == [conn.executed[0]]


def test_an_unrelated_failure_is_not_retried(monkeypatch):
    """Only a compile error can be retried safely — nothing executed, so no row
    can be duplicated. Anything else must surface on the first attempt."""
    conn = _Conn()
    monkeypatch.setattr(sc, "get_connection", lambda: conn)

    def boom(self, sql, *args, **kwargs):
        self.conn.executed.append(sql)
        raise Exception("250002: connection is closed / socket dead")

    monkeypatch.setattr(_Cursor, "execute", boom)

    with pytest.raises(Exception, match="250002"):
        sc.write_audit(ROW)

    assert len(conn.executed) == 1


# --- plans record how many panels survived -----------------------------------
#
# A plan that drops failing panels is a success, so it wrote no audit row and
# the drop rate a repair step would be decided from was never recorded.


def _plan_result(kept, skipped):
    return {"plan_id": "p1", "plan": {"title": "T", "panels": []},
            "panel_count": kept, "skipped_panels": skipped}


async def _run_plan_turn(monkeypatch, result):
    from app.agent import tools

    monkeypatch.setitem(tools._IMPLEMENTATIONS, "plan_dashboard",
                        lambda ctx, **k: result)
    client = _FakeClient([
        _final([_tool_block("tu_p", "plan_dashboard", {"description": "20 panels"})],
               "tool_use", _usage(10, 5)),
        _final([_text_block("Here is the plan")], "end_turn", _usage(10, 5)),
    ])
    await _collect(client, _conversation(), message="build it")


async def test_a_plan_that_dropped_panels_is_recorded_as_partial(audited, monkeypatch):
    await _run_plan_turn(monkeypatch, _plan_result(17, [
        {"title": "Claims Ratio", "reason": "SQL failed to compile: invalid identifier"},
        {"title": "DSO", "reason": "parameter error"},
        {"title": "Leakage", "reason": "SQL rejected by safety layer"},
    ]))

    rows = [r for r in audited if r["tool"] == "plan_dashboard"]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "partial"
    assert row["row_count"] == 17
    assert row["error"].startswith("3 of 20 panels dropped")
    assert "Claims Ratio" in row["error"]
    assert row["conversation_id"] == "conv_1"


async def test_a_complete_plan_is_recorded_too(audited, monkeypatch):
    """The drop rate needs the plans that dropped nothing as its denominator."""
    await _run_plan_turn(monkeypatch, _plan_result(12, []))

    rows = [r for r in audited if r["tool"] == "plan_dashboard"]
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["row_count"] == 12
    assert rows[0]["error"] is None


async def test_a_failed_plan_is_recorded_once_as_an_error(audited, monkeypatch):
    await _run_plan_turn(monkeypatch, {"error": "No panels could be validated."})

    rows = [r for r in audited if r["tool"] == "plan_dashboard"]
    assert [r["status"] for r in rows] == ["error"]

"""A stopped Edit-panel request stops the model call, not just the browser's wait.

The assistant's Stop button aborted the browser's request, but the server kept
the model call running to the end — generating, and billing, a reply nobody
would read. A stop signal now closes the stream at the next chunk.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.api.v1.routes import dashboards as routes
from app.engine import audit, llm


class _Block:
    type = "text"
    text = '{"reply": "done"}'


@pytest.fixture
def fake_stream(monkeypatch):
    """A streamed reply of a few chunks, recording how far it was read, whether
    it was closed, and each audit row."""
    state = {"chunks": 5, "read": 0, "closed": False, "on_chunk": None,
             "opened": 0, "audits": []}

    class _Stream:
        def __enter__(self):
            state["opened"] += 1
            return self

        def __exit__(self, *exc):
            state["closed"] = True
            return False

        def __iter__(self):
            for i in range(state["chunks"]):
                state["read"] += 1
                if state["on_chunk"]:
                    state["on_chunk"](i)
                yield object()

        def get_final_message(self):
            return type("Resp", (), {"content": [_Block()], "stop_reason": "end_turn"})()

    class _Messages:
        def stream(self, **kw):
            return _Stream()

    monkeypatch.setattr(llm, "_get_client",
                        lambda: type("Client", (), {"messages": _Messages()})())
    monkeypatch.setattr(audit, "log_ai_call",
                        lambda what, model, resp=None, **kw: state["audits"].append(
                            {"what": what, "error": kw.get("error")}))
    return state


def _call():
    return llm._create("dashboard_edit_assist", stream=True, model="m", max_tokens=10,
                       messages=[])


def test_a_stop_part_way_closes_the_stream_and_reads_no_further(fake_stream):
    signal = threading.Event()
    fake_stream["on_chunk"] = lambda i: signal.set() if i == 1 else None

    with llm.cancellable(signal), pytest.raises(llm.LLMCancelled):
        _call()

    # Stopped at the chunk after the signal, not at the end of the reply.
    assert fake_stream["read"] == 2
    assert fake_stream["closed"] is True
    # A stopped call still leaves its audit row, marked as failed.
    assert fake_stream["audits"][-1]["what"] == "dashboard_edit_assist"
    assert isinstance(fake_stream["audits"][-1]["error"], llm.LLMCancelled)


def test_a_request_stopped_before_it_starts_never_opens_a_stream(fake_stream):
    signal = threading.Event()
    signal.set()

    with llm.cancellable(signal), pytest.raises(llm.LLMCancelled):
        _call()

    assert fake_stream["opened"] == 0


def test_a_signal_that_is_never_set_returns_the_whole_reply(fake_stream):
    with llm.cancellable(threading.Event()):
        resp = _call()

    assert fake_stream["read"] == 5
    assert llm._text(resp) == '{"reply": "done"}'


def test_calls_outside_a_stoppable_block_are_unchanged(fake_stream):
    """Every other streamed call (plans, edits) reads the final message whole."""
    resp = _call()

    assert fake_stream["read"] == 0
    assert llm._text(resp) == '{"reply": "done"}'


def test_the_signal_does_not_leak_past_its_block(fake_stream):
    with llm.cancellable(threading.Event()):
        pass
    assert llm._cancel_signal.get() is None


def test_a_cancelled_call_is_still_an_llm_error():
    """Callers that catch LLMError keep working when a request is stopped."""
    assert issubclass(llm.LLMCancelled, llm.LLMError)


def test_a_dropped_browser_sets_the_stop_signal():
    class _Request:
        def __init__(self):
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks >= 2

    signal = threading.Event()
    asyncio.run(asyncio.wait_for(routes._stop_on_disconnect(_Request(), signal), 5))

    assert signal.is_set()


def test_a_stoppable_job_runs_its_model_calls_under_the_signal():
    signal = threading.Event()
    seen = []

    def job(value):
        seen.append(llm._cancel_signal.get())
        return value * 2

    assert routes._stoppable(signal, job)(21) == 42
    assert seen == [signal]
    assert llm._cancel_signal.get() is None


def test_the_assistant_model_defaults_to_the_dashboard_model(monkeypatch):
    """Nothing changes until someone chooses a lighter model on purpose."""
    if os.environ.get("MCP_MODEL_DASHBOARD_ASSIST"):
        pytest.skip("overridden in this environment")
    assert llm.MODEL_DASHBOARD_ASSIST == llm.MODEL_DASHBOARD

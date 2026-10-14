"""A dashboard plan that runs out of room says so.

A plan is one JSON object carrying every panel's SQL and chart config, and an
edit re-emits the whole thing. At 8192 output tokens a 20-panel request stopped
mid-object, and the parser reported "unparseable output" -- which reads as a
malformed answer worth retrying. It was retried, three times, and stopped at the
same place each time, each attempt billing a full ceiling of output.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import llm

COMPLETE = json.dumps({
    "dashboard_title": "GCC E-Commerce Performance & Profitability",
    "panels": [{
        "title": "GMV by Channel", "sql": "SELECT CHANNEL, SUM(GMV_AED) AS GMV_AED FROM T GROUP BY 1",
        "chart_type": "bar", "x": "CHANNEL", "y": "GMV_AED", "display": "chart",
    }],
})
# What the failing run actually returned: well-formed right up to the cut.
CUT_OFF = COMPLETE[:120]

CURRENT = {"title": "T", "panels": json.loads(COMPLETE)["panels"]}

CALLS = {
    "generate": lambda: llm.generate_dashboard_plan("20 panels", "ctx"),
    "generate_file": lambda: llm.generate_file_dashboard_plan("20 panels", "ctx"),
    "edit": lambda: llm.edit_dashboard_plan(CURRENT, "add 8 panels", "ctx"),
    "edit_file": lambda: llm.edit_file_dashboard_plan(CURRENT, "add 8 panels", "ctx"),
}


@pytest.fixture
def fake_model(monkeypatch):
    """A client whose next response is whatever the test sets, recording what
    each request asked for."""
    state = {"text": COMPLETE, "stop_reason": "end_turn", "requests": []}

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Messages:
        def create(self, **kw):
            state["requests"].append(kw)
            return type("Resp", (), {
                "content": [_Block(state["text"])],
                "stop_reason": state["stop_reason"],
            })()

        def stream(self, **kw):
            # Plan calls stream; the final message is the same response.
            response = self.create(**kw)
            return type("Stream", (), {
                "__enter__": lambda s: s,
                "__exit__": lambda s, *exc: False,
                "get_final_message": lambda s: response,
            })()

    monkeypatch.setattr(llm, "_get_client",
                        lambda: type("Client", (), {"messages": _Messages()})())
    return state


@pytest.mark.parametrize("name", CALLS)
def test_a_cut_off_plan_says_it_ran_out_of_room(fake_model, name):
    """Not "unparseable": that sends the reader, or an agent, back to retry a
    request that cannot succeed."""
    fake_model.update(text=CUT_OFF, stop_reason="max_tokens")

    with pytest.raises(llm.LLMError) as err:
        CALLS[name]()

    message = str(err.value)
    assert "ran out of room" in message
    assert "fewer panels" in message
    assert "unparseable" not in message


@pytest.mark.parametrize("name", CALLS)
def test_every_plan_call_gets_the_raised_ceiling(fake_model, name):
    """All four, including the edits: an edit re-emits every panel, so a
    dashboard can only be as large as the smallest ceiling on this path."""
    CALLS[name]()
    assert fake_model["requests"][-1]["max_tokens"] == llm._PLAN_MAX_TOKENS
    assert llm._PLAN_MAX_TOKENS >= 16000


def test_a_complete_plan_still_parses(fake_model):
    plan = llm.generate_dashboard_plan("20 panels", "ctx")
    assert plan.title == "GCC E-Commerce Performance & Profitability"
    assert len(plan.panels) == 1


def test_malformed_but_finished_output_keeps_its_own_error(fake_model):
    """The two failures need different responses, so they must not merge: a
    response that ENDED normally and still is not JSON is a genuine parse
    problem."""
    fake_model.update(text="Here is your dashboard: {not json", stop_reason="end_turn")

    with pytest.raises(llm.LLMError) as err:
        llm.generate_dashboard_plan("20 panels", "ctx")

    assert "unparseable" in str(err.value)
    assert "ran out of room" not in str(err.value)


def test_an_unparseable_plan_error_shows_where_it_broke_and_how_it_ended(fake_model):
    """It used to quote the first 200 characters: a well-formed opening that
    made every failure look as though it broke at the same place."""
    body = '{"dashboard_title": "T", "panels": [' + ('{"title": "p"}, ' * 60) + 'BROKEN_TAIL'
    fake_model.update(text=body, stop_reason="end_turn")

    with pytest.raises(llm.LLMError) as err:
        llm.generate_dashboard_plan("20 panels", "ctx")

    message = str(err.value)
    assert "BROKEN_TAIL" in message, "the end of the output must be visible"
    assert "line" in message and "column" in message, "the parser's location must be kept"

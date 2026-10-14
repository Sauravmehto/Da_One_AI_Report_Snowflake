"""Dashboards of up to 25 panels, built on request.

The planner defaults to 8-12 panels. Users sometimes ask for 18-20, and the
path had two limits that would break such a request: a planner rule of "8 to
12", and a 16000-token reply ceiling that a 25-panel plan with long SQL could
overrun, failing as "ran out of room". The ceiling is now 32000, which the SDK
only allows on a streamed request, so whole-plan calls stream.

Everything downstream of the planner must take the larger dashboard whole:
validation, tabs, what the chat is shown, and the summary.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from anthropic import Anthropic

from app.agent import prompts
from app.conversation.truncate import MAX_TOOL_RESULT_CHARS, truncate_tool_result
from app.engine import audit, dashboards, llm
from app.engine.connectors.snowflake import client as sc
from app.engine.dashboard_sections import MAX_TABS


def _panel(i, section="Overview"):
    return {"title": f"Panel {i} — GMV by Channel and Month", "chart_type": "bar",
            "x": "CHANNEL", "y": "GMV_AED", "section": section, "display": "chart",
            "sql": f"SELECT CHANNEL, SUM(GMV_AED) AS GMV_AED FROM ECOMMERCE WHERE K = {i} GROUP BY 1"}


# --- the rules the planner and the chat are given -----------------------------------


def test_the_default_stays_8_to_12_and_a_request_can_go_to_25():
    rules = llm._DASHBOARD_RULES
    assert "Generate between 8 and 12 panels" in rules
    assert "up to 25" in rules and "Never exceed 25 panels" in rules
    assert llm.MAX_REQUESTED_PANELS == 25


def test_the_chat_knows_the_cap_before_it_plans():
    text = prompts._BASE_PROMPT if hasattr(prompts, "_BASE_PROMPT") else json.dumps(
        prompts.build_system_prompt("ECOMMERCE_DB", "ctx"))
    assert "at most 25" in text


# --- the plan call itself ---------------------------------------------------------------


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def _streamed_reply(text, stop_reason="end_turn"):
    return _sse([
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 9000,
                      "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[:50]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[50:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 14000}},
        {"type": "message_stop"},
    ])


@pytest.fixture
def real_sdk(monkeypatch):
    """The real Anthropic client, answering from a fake network: exercises the
    SDK's streaming call exactly as production makes it, with no credits."""
    state = {"reply": "", "stop_reason": "end_turn", "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_streamed_reply(state["reply"], state["stop_reason"]))

    client = Anthropic(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    rows = []
    monkeypatch.setattr(sc, "write_audit", rows.append)
    state["audit"] = rows
    return state


def test_a_25_panel_plan_arrives_whole_through_the_real_sdk(real_sdk):
    real_sdk["reply"] = json.dumps({"dashboard_title": "GCC E-Commerce",
                                    "panels": [_panel(i) for i in range(25)]})
    plan = llm.generate_dashboard_plan("a 25-panel e-commerce dashboard", "ctx")
    assert len(plan.panels) == 25
    request = real_sdk["requests"][0]
    assert request["stream"] is True
    assert request["max_tokens"] == 32000


@pytest.mark.parametrize("call", [
    lambda: llm.generate_dashboard_plan("x", "ctx"),
    lambda: llm.generate_file_dashboard_plan("x", "ctx"),
    lambda: llm.edit_dashboard_plan({"title": "T", "panels": [_panel(0)]}, "add a panel", "ctx"),
    lambda: llm.edit_file_dashboard_plan({"title": "T", "panels": [_panel(0)]}, "add a panel", "ctx"),
])
def test_every_whole_plan_call_streams_with_the_raised_ceiling(real_sdk, call):
    real_sdk["reply"] = json.dumps({"dashboard_title": "T", "panels": [_panel(1)],
                                    "operations": [{"op": "add", "panel": _panel(1)}]})
    call()
    assert real_sdk["requests"][0]["stream"] is True
    assert real_sdk["requests"][0]["max_tokens"] == llm._PLAN_MAX_TOKENS == 32000


def test_a_streamed_call_is_still_audited_with_its_tokens(real_sdk):
    real_sdk["reply"] = json.dumps({"dashboard_title": "T", "panels": [_panel(1)]})
    llm.generate_dashboard_plan("x", "ctx")
    row = real_sdk["audit"][0]
    assert row["tool_name"] == "ai:dashboard_plan" and row["status"] == "ok"
    assert row["output_tokens"] == 14000
    assert row["cache_read_tokens"] == 9000


def test_a_cut_off_streamed_plan_still_says_it_ran_out_of_room(real_sdk):
    real_sdk.update(reply='{"dashboard_title": "T", "panels": [{"title": "Pan', stop_reason="max_tokens")
    with pytest.raises(llm.LLMError, match="ran out of room"):
        llm.generate_dashboard_plan("x", "ctx")


def test_a_failed_stream_is_audited_and_raised(monkeypatch):
    def handler(request):
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error",
                                                                     "message": "Overloaded"}})

    client = Anthropic(api_key="k", max_retries=0,
                       http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    rows = []
    monkeypatch.setattr(sc, "write_audit", rows.append)
    with pytest.raises(Exception, match="Overloaded"):
        llm.generate_dashboard_plan("x", "ctx")
    assert rows[0]["status"] == "error"


def test_short_calls_do_not_stream(real_sdk, monkeypatch):
    """Only whole plans stream; the summary and the rest keep their request as is."""
    seen = {}

    class _Messages:
        def create(self, **kw):
            seen["create"] = kw
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=None)

        def stream(self, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("summary streamed")

    monkeypatch.setattr(llm, "_get_client", lambda: SimpleNamespace(messages=_Messages()))
    llm.summarize_dashboard("T", [], [])
    assert "stream" not in seen["create"]


# --- downstream of the planner ----------------------------------------------------------


def test_create_plan_keeps_all_25_panels_on_at_most_four_tabs(monkeypatch):
    sections = ["Overview", "Growth", "Efficiency", "Operations", "Customers", "Detail"]
    panels = [_panel(i, section=sections[i % len(sections)]) for i in range(25)]
    monkeypatch.setattr(dashboards.metadata_store, "build_context", lambda db: "ctx")
    monkeypatch.setattr(dashboards.metadata_store, "dashboard_heuristics", lambda db: "")
    monkeypatch.setattr(llm, "generate_dashboard_plan",
                        lambda d, c, h="": llm.DashboardPlan(title="T", panels=panels))
    monkeypatch.setattr(sc, "dry_run_query", lambda sql: ["CHANNEL", "GMV_AED"])

    result = dashboards.create_plan("25 panels", "ECOMMERCE_DB")
    assert result["panel_count"] == 25
    assert result["skipped_panels"] == []
    assert len({p["section"] for p in result["plan"]["panels"]}) <= MAX_TABS


def test_the_chat_sees_every_panel_title_and_the_true_count():
    """A 25-panel plan is far over the chat's result budget. What survives must
    still say 25 and list every title, or the assistant re-plans to find out."""
    payload = {"plan_id": "p1", "panel_count": 25, "skipped_panels": [],
               "plan": {"title": "T", "panels": [_panel(i) for i in range(25)]}}
    content, shortened = truncate_tool_result(payload)
    assert shortened and len(content) <= MAX_TOOL_RESULT_CHARS + 200
    shown = json.loads(content)
    assert shown["panel_count"] == 25
    titles = json.dumps(shown["plan"]["panels"])
    assert all(f"Panel {i} " in titles for i in range(25))


def test_the_summary_covers_all_25_charts():
    from app.api.v1.routes import dashboards as routes

    snapshots = [{"title": f"Chart {i}", "columns": ["CHANNEL", "GMV_AED"], "x": "CHANNEL",
                  "y": "GMV_AED", "rows": [["Web", 10], ["App", 5]], "row_count": 2}
                 for i in range(25)]
    _, _, panels = routes._narrative_payload("T", [], snapshots, "AED")
    assert len(panels) == 25

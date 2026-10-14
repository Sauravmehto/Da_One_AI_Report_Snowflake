"""What the audit log can see: every model call's cost, every failed tool, and
the person behind each row.

Before this, only the chat turn recorded its spend. The dashboard planner and
editor (the most expensive calls), summaries and panel descriptions wrote
nothing; a failed dashboard edit left no row of its own while the turn said
"ok"; and every row named the server's machine login and one session id per
process rather than the signed-in user and their login.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.api.deps import get_current_session
from app.engine import audit, llm
from app.engine.connectors.snowflake import client as sc
from app.session.base import SessionData

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def rows(monkeypatch):
    """Rows as they would reach the INSERT, with the thread that wrote each."""
    written: list[dict] = []

    def fake_write(row):
        written.append({**row, "_thread": threading.get_ident()})

    monkeypatch.setattr(sc, "write_audit", fake_write)
    return written


def _usage(inp=10, out=5, read=100, write=20):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_read_input_tokens=read, cache_creation_input_tokens=write)


class _Client:
    def __init__(self, fail=False):
        self.messages = self
        self.fail = fail
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("overloaded")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                               usage=_usage(), stop_reason="end_turn")


# --- 2.1: every model call writes one row ---------------------------------------


def test_a_model_call_records_what_it_cost(rows, monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: _Client())
    llm._create("dashboard_plan", model="claude-opus-4-8", max_tokens=10, messages=[])
    assert len(rows) == 1
    row = rows[0]
    assert row["tool_name"] == "ai:dashboard_plan"
    assert row["status"] == "ok"
    assert row["model"] == "claude-opus-4-8"
    assert (row["input_tokens"], row["output_tokens"]) == (10, 5)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (100, 20)
    assert row["model_calls"] == 1
    assert row["duration_ms"] >= 0


def test_a_failed_model_call_is_recorded_and_still_raises(rows, monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: _Client(fail=True))
    with pytest.raises(RuntimeError, match="overloaded"):
        llm._create("dashboard_edit", model="m", max_tokens=10, messages=[])
    assert rows[0]["status"] == "error"
    assert "overloaded" in rows[0]["error_message"]


def test_a_broken_audit_never_breaks_the_model_call(monkeypatch):
    def boom(row):
        raise RuntimeError("Snowflake down")

    monkeypatch.setattr(sc, "write_audit", boom)
    monkeypatch.setattr(llm, "_get_client", lambda: _Client())
    assert llm._create("dashboard_summary", model="m", max_tokens=10, messages=[]) is not None


def test_the_summary_and_planner_are_among_the_recorded_calls(rows, monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: _Client())
    llm.summarize_dashboard("T", [], [])
    llm.describe_panel("T", "bar", ["A", "B"], "A", ["B"])
    assert [r["tool_name"] for r in rows] == ["ai:dashboard_summary", "ai:panel_description"]


def test_no_model_call_bypasses_the_audit():
    """A new call written as client.messages.create would cost money the audit
    never sees. Only the one wrapper in llm.py, the chat loop (audited per turn),
    and the two places that record explicitly may call the API."""
    allowed = {
        APP / "engine" / "llm.py": 2,              # _create itself: create, and stream for plans
        APP / "engine" / "excel_mapping.py": 1,    # recorded on the caller's thread
        APP / "services" / "ai_query.py": 1,       # recorded around the await
    }
    for path in APP.rglob("*.py"):
        if path.parts[-3:-1] == ("app", "agent") or path.name == "loop.py":
            continue
        found = len(re.findall(r"messages\.(?:create|stream)\(", path.read_text(encoding="utf-8")))
        assert found == allowed.get(path, 0), f"{path.relative_to(APP)}: {found} unaudited call(s)"


def test_excel_mapping_rows_are_written_by_the_request_thread(rows, monkeypatch):
    """Pool threads carry no request connection or login; their calls must be
    recorded from the thread that has them."""
    from app.engine import excel_mapping

    class _Mapper(_Client):
        def create(self, **request):
            super().create(**request)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"regions": []}')],
                                   usage=_usage())

    monkeypatch.setattr(excel_mapping, "_get_client", lambda: _Mapper())
    monkeypatch.setattr(excel_mapping, "_has_mappable_fields", lambda shape: True)
    packet = {"shapes": [{"n": i} for i in range(4)]}
    try:
        excel_mapping.propose_mapping(packet)
    except llm.LLMError:  # "nothing mapped" is fine; the rows are the point
        pass
    assert len(rows) == 4
    assert {r["tool_name"] for r in rows} == {"ai:excel_mapping"}
    assert {r["_thread"] for r in rows} == {threading.get_ident()}


# --- 2.3 / 2.4: the person and the login ------------------------------------------


def test_a_row_names_the_signed_in_user_not_the_machine(rows):
    with sc.use_connection(object(), identity=("ALICE", "ANALYST")):
        audit.log_ai_call("dashboard_plan", "m", started=0.0)
    assert rows[0]["audit_user"] == "ALICE"
    assert rows[0]["snowflake_user"] == "ALICE"
    assert rows[0]["snowflake_role"] == "ANALYST"


def test_a_row_names_the_login_but_never_carries_the_cookie(rows):
    async def request():
        audit.bind_session("secret-cookie-value", "oauth")
        await asyncio.to_thread(audit.log_event, "run_safe_sql", "ok")

    asyncio.run(request())
    assert rows[0]["session_id"] == audit.session_ref("secret-cookie-value")
    assert "secret-cookie-value" not in str(rows[0])
    assert rows[0]["auth_mode"] == "oauth"


def test_two_logins_are_two_sessions():
    assert audit.session_ref("a") != audit.session_ref("b")
    assert audit.session_ref("a") == audit.session_ref("a")


def test_outside_a_request_the_old_fallbacks_still_apply(rows):
    audit.log_event("run_safe_sql", "ok")
    assert rows[0]["session_id"] == audit.SESSION_ID
    assert rows[0]["auth_mode"] == audit.AUTH_MODE
    assert rows[0]["audit_user"]


def test_the_login_reaches_streamed_replies_and_worker_threads(rows):
    """Set in the session dependency, it must survive into a StreamingResponse
    (the chat) and asyncio.to_thread (every tool and engine call)."""
    session = SessionData(session_id="cookie-1", snowflake_user="BOB", access_token="t",
                          refresh_token_encrypted="e", token_expires_at="2030-01-01T00:00:00Z",
                          auth_mode="oauth")

    class _Store:
        async def get(self, session_id):
            return session if session_id == "cookie-1" else None

    app = FastAPI()
    app.state.session_store = _Store()

    @app.get("/stream")
    async def stream(_: SessionData = Depends(get_current_session)):
        async def body():
            await asyncio.to_thread(audit.log_event, "agent_turn", "ok")
            yield "done"
        return StreamingResponse(body())

    from app.core.config import settings
    client = TestClient(app)
    client.cookies.set(settings.SESSION_COOKIE_NAME, "cookie-1")
    assert client.get("/stream").text == "done"
    assert rows[0]["session_id"] == audit.session_ref("cookie-1")
    assert rows[0]["auth_mode"] == "oauth"


def test_the_conversation_tag_does_not_outlive_its_block(rows):
    with audit.conversation("conv_9"):
        audit.log_event("ai:dashboard_plan", "ok")
    audit.log_event("run_safe_sql", "ok")
    assert [r["conversation_id"] for r in rows] == ["conv_9", None]


# --- 2.2: a failed tool is visible ---------------------------------------------------


async def test_a_failed_dashboard_edit_writes_an_error_row(monkeypatch):
    from app.agent import loop
    from tests.test_token_accounting import CTX, SETTINGS, _conversation, _FakeClient, _final
    from tests.test_token_accounting import _text_block, _tool_block, _usage as turn_usage

    logged: list[dict] = []
    monkeypatch.setattr(audit, "log_event",
                        lambda tool, status, *a, **k: logged.append({"tool": tool, "status": status, **k}))
    tags: list[str | None] = []

    async def failing_dispatch(name, tool_input, ctx):
        tags.append(audit._conversation.get())
        return {"error": "No previewed edit found for this dashboard"}

    monkeypatch.setattr(loop, "dispatch", failing_dispatch)
    client = _FakeClient([
        _final([_tool_block("tu_1", "edit_dashboard", {"report_id_or_title": "x"})],
               "tool_use", turn_usage(10, 5)),
        _final([_text_block("It failed")], "end_turn", turn_usage(10, 5)),
    ])
    async for _ in loop.run_turn(client=client, settings=SETTINGS, conversation=_conversation(),
                                 user_message="rename the chart", ctx=CTX):
        pass

    errors = [r for r in logged if r["tool"] == "edit_dashboard"]
    assert len(errors) == 1
    assert errors[0]["status"] == "error"
    assert "No previewed edit" in errors[0]["error"]
    assert errors[0]["conversation_id"] == "conv_1"
    assert tags == ["conv_1"]  # rows written inside the tool carry the conversation
    assert audit._conversation.get() is None


async def test_a_tool_that_audits_itself_is_not_recorded_twice(monkeypatch):
    from app.agent import loop

    logged: list[str] = []
    monkeypatch.setattr(audit, "log_event", lambda tool, *a, **k: logged.append(tool))
    await loop._record_tool_error("run_safe_sql", {"error": "bad"}, "q",
                                  SimpleNamespace(snowflake_user="U", role="R"),
                                  SimpleNamespace(conversation_id="c"))
    assert logged == []


# --- 2.6 -----------------------------------------------------------------------------


def test_recent_activity_is_ordered_by_time(monkeypatch):
    seen = {}

    def fake_read(query, limit):
        seen["q"] = query
        return {"columns": [], "rows": []}

    monkeypatch.setattr(sc, "read_audit", fake_read)
    audit.recent()
    assert "ORDER BY EVENT_TS DESC" in seen["q"]


# --- the two routes that call the model directly ----------------------------------
#
# Found in the live check: the summary route called the model with no connection
# bound, so its audit row failed with "Missing required environment variable".


async def test_the_summary_is_audited_as_the_signed_in_user(monkeypatch):
    from app.api.v1.routes import dashboards as routes
    from app.engine import storage as storage_mod

    seen = []
    monkeypatch.setattr(storage_mod, "get_json", lambda k: None)
    monkeypatch.setattr(storage_mod, "put_json", lambda k, v: None)

    async def connection(request, session):
        return "user-connection"

    def summarize(title, kpis, panels):
        seen.append((sc._request_connection.get(), sc.bound_identity()))
        return "fine"

    monkeypatch.setattr(routes, "_bound_connection", connection)
    monkeypatch.setattr(llm, "summarize_dashboard", summarize)
    session = SessionData(session_id="s", snowflake_user="CAROL", role="ANALYST", access_token="t",
                          refresh_token_encrypted="e", token_expires_at="2030-01-01T00:00:00Z")
    await routes.dashboard_narrative(report_id="r1", request=None, session=session,
                                     payload={"title": "T", "kpis": [], "panels": []})
    assert seen == [("user-connection", ("CAROL", "ANALYST"))]


async def test_the_panel_description_is_audited_as_the_signed_in_user(monkeypatch):
    from app.api.v1.routes import dashboards as routes
    from app.engine import storage as storage_mod

    seen = []
    monkeypatch.setattr(storage_mod, "get_json", lambda k: None)
    monkeypatch.setattr(storage_mod, "put_json", lambda k, v: None)

    async def connection(request, session):
        return "user-connection"

    def describe(*args):
        seen.append(sc.bound_identity())
        return "fine"

    monkeypatch.setattr(routes, "_bound_connection", connection)
    monkeypatch.setattr(llm, "describe_panel", describe)
    session = SessionData(session_id="s", snowflake_user="CAROL", role="ANALYST", access_token="t",
                          refresh_token_encrypted="e", token_expires_at="2030-01-01T00:00:00Z")
    await routes.panel_description(payload={"title": "T", "chart_type": "bar"}, session=session)
    assert seen == [("CAROL", "ANALYST")]


async def test_an_unreachable_warehouse_costs_the_audit_row_not_the_summary(monkeypatch):
    from app.api.v1.routes import dashboards as routes

    async def no_connection(request, session):
        raise RuntimeError("Snowflake unreachable")

    monkeypatch.setattr(routes, "_bound_connection", no_connection)
    session = SessionData(session_id="s", snowflake_user="CAROL", access_token="t",
                          refresh_token_encrypted="e", token_expires_at="2030-01-01T00:00:00Z")
    assert await routes._run_ai_call(None, session, lambda: "summary") == "summary"


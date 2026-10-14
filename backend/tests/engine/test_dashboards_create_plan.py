"""Regression tests for dashboards.create_plan's partial-success behavior:
panels that fail validation are dropped and reported in skipped_panels (not
fatal), unless ALL fail. Hermetic — the planner and the Snowflake dry-run are
stubbed, so no LLM or DB calls.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Minimal env so importing snowflake_client doesn't fail.
os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards
from app.engine import llm
from app.engine.connectors.snowflake import client as snowflake_client
from app.engine.dashboards import DashboardError
from app.engine.sql_safety import UnsafeSQLError


def _panel(title, sql, **kw):
    p = {"title": title, "sql": sql, "chart_type": "bar", "x": "a", "y": "b",
         "color": None, "orientation": "v", "display": "both"}
    p.update(kw)
    return p


@pytest.fixture
def stub_context(monkeypatch):
    """Stub the metadata lookups so create_plan needs no config/DB."""
    monkeypatch.setattr(dashboards.metadata_store, "build_context", lambda db: "ctx")
    monkeypatch.setattr(dashboards.metadata_store, "dashboard_heuristics", lambda db: "")


def _set_plan(monkeypatch, panels, title="T"):
    monkeypatch.setattr(
        llm, "generate_dashboard_plan",
        lambda desc, ctx, heur="": llm.DashboardPlan(title=title, panels=panels),
    )


def test_bad_panel_dropped_and_reported_not_fatal(stub_context, monkeypatch):
    _set_plan(monkeypatch, [
        _panel("Good A", "SELECT 1"),
        _panel("Bad KPI", "BADSQL"),
        _panel("Good B", "SELECT 2"),
    ])

    def dry(sql):
        if "BAD" in sql:
            raise UnsafeSQLError("not a select")
        return ["c"]
    monkeypatch.setattr(snowflake_client, "dry_run_query", dry)

    res = dashboards.create_plan("x", "LOAN")

    assert res["panel_count"] == 2
    assert [p["title"] for p in res["plan"]["panels"]] == ["Good A", "Good B"]
    assert len(res["skipped_panels"]) == 1
    assert res["skipped_panels"][0]["title"] == "Bad KPI"
    assert "not a select" in res["skipped_panels"][0]["reason"]
    # The cached plan (what save_dashboard persists) contains only kept panels.
    cached = dashboards.get_cached_plan(res["plan_id"])["plan"]
    assert cached == res["plan"]
    assert all(p["title"] != "Bad KPI" for p in cached["panels"])


def test_panel_without_sql_is_skipped(stub_context, monkeypatch):
    _set_plan(monkeypatch, [
        _panel("Has SQL", "SELECT 1"),
        {"title": "No SQL", "sql": None, "chart_type": "bar", "display": "both"},
    ])
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["c"])

    res = dashboards.create_plan("x", "LOAN")
    assert res["panel_count"] == 1
    assert res["skipped_panels"] == [{"title": "No SQL", "reason": "panel has no SQL"}]


def test_all_panels_valid_has_no_skips(stub_context, monkeypatch):
    _set_plan(monkeypatch, [_panel("A", "SELECT 1"), _panel("B", "SELECT 2")])
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["c"])

    res = dashboards.create_plan("x", "LOAN")
    assert res["panel_count"] == 2
    assert res["skipped_panels"] == []


def test_all_panels_fail_raises_with_reasons(stub_context, monkeypatch):
    _set_plan(monkeypatch, [_panel("A", "x"), _panel("B", "y")])
    monkeypatch.setattr(
        snowflake_client, "dry_run_query",
        lambda sql: (_ for _ in ()).throw(UnsafeSQLError("nope")),
    )
    with pytest.raises(DashboardError) as excinfo:
        dashboards.create_plan("x", "LOAN")
    msg = str(excinfo.value)
    assert "No panels could be validated" in msg
    assert "'A'" in msg and "'B'" in msg

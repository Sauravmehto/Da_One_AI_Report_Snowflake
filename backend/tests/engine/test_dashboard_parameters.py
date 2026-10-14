"""Hermetic tests for parameterized dashboards. Snowflake and the LLM are
stubbed. Covers: the shared sql_params renderer, param-aware panel validation
(still catching unsafe SQL, not just filling placeholders), run substitution
with value/default, and the edit_dashboard parameter-value redirect."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards
from app.engine.connectors.snowflake import client as snowflake_client
from app.engine import sql_params
from app.engine.sql_safety import UnsafeSQLError, validate_sql


# --- shared renderer (same module Excel uses) --------------------------------

def test_sql_params_render_and_default_and_escape():
    out = sql_params.render_sql(
        "SELECT * FROM t WHERE d <= {{as_of}} AND region = {{r}}",
        [{"name": "as_of", "type": "date", "default": "2026-01-01"},
         {"name": "r", "type": "string"}],
        {"r": "O'Brien"},
    )
    assert out == "SELECT * FROM t WHERE d <= '2026-01-01' AND region = 'O''Brien'"


def test_sql_params_number_type_rejects_injection():
    with pytest.raises(sql_params.ParameterError):
        sql_params.render_sql("SELECT {{n}}", [{"name": "n", "type": "number"}],
                              {"n": "1; DROP TABLE t"})


def test_excel_still_uses_shared_renderer():
    # excel_reports.render_sql is now a thin wrapper over sql_params but must keep
    # raising its own ExcelReportError type.
    from app.engine import excel_reports as er
    assert er.render_sql("SELECT {{y}}", [{"name": "y", "type": "number", "default": 7}], {}) == "SELECT 7"
    with pytest.raises(er.ExcelReportError):
        er.render_sql("SELECT {{y}}", [{"name": "y", "type": "number"}], {})


# --- param-aware validation still gates unsafe SQL ---------------------------

def test_validate_substitutes_then_still_rejects_unsafe(monkeypatch):
    # dry_run_query delegates to the REAL validate_sql, so a rendered non-SELECT
    # is still rejected — substitution only fills placeholders, it is not a bypass.
    def real_dry_run(sql):
        validate_sql(sql)          # raises UnsafeSQLError for non-read-only
        return ["c"]
    monkeypatch.setattr(snowflake_client, "dry_run_query", real_dry_run)

    params = [{"name": "as_of", "type": "date", "default": "2026-01-01"}]
    good = [{"title": "ok", "sql": "SELECT * FROM t WHERE d <= {{as_of}}"}]
    bad = [{"title": "danger", "sql": "DELETE FROM t WHERE d <= {{as_of}}"}]

    kept, skipped = dashboards._validate_panels(good, params)
    assert [p["title"] for p in kept] == ["ok"] and skipped == []

    kept2, skipped2 = dashboards._validate_panels(bad, params)
    assert kept2 == [] and len(skipped2) == 1
    assert "safety layer" in skipped2[0]["reason"]


def test_validate_reports_unfilled_placeholder(monkeypatch):
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["c"])
    # {{missing}} is not declared -> render fails -> reported, panel skipped.
    panels = [{"title": "p", "sql": "SELECT * FROM t WHERE x = {{missing}}"}]
    kept, skipped = dashboards._validate_panels(panels, [{"name": "as_of", "type": "date", "default": "2026-01-01"}])
    assert kept == [] and "parameter error" in skipped[0]["reason"]


# --- run substitution: value else default, no version created ----------------

def _stub_run(monkeypatch, captured):
    monkeypatch.setattr(snowflake_client, "touch_saved_dashboard_run", lambda rid: (5, "2026-01-01T00:00:00"))
    def _run_query(sql, max_rows=None):
        captured.append(sql)
        return {"columns": ["N"], "rows": [[1]], "row_count": 1, "sql": sql}
    monkeypatch.setattr(snowflake_client, "run_query", _run_query)


def test_run_uses_passed_value(monkeypatch):
    plan = {"title": "T", "parameters": [{"name": "as_of", "type": "date", "default": "2026-01-01"}],
            "panels": [{"title": "p", "sql": "SELECT COUNT(*) AS N FROM t WHERE d <= {{as_of}}",
                        "chart_type": "kpi", "x": None, "y": None, "color": None,
                        "size": None, "orientation": None, "display": "kpi"}]}
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard", lambda rid: {"DASHBOARD_PLAN": plan})
    captured = []
    _stub_run(monkeypatch, captured)

    res = dashboards.run_dashboard("rid", {"as_of": "2026-03-31"})
    assert captured == ["SELECT COUNT(*) AS N FROM t WHERE d <= '2026-03-31'"]
    assert res["parameter_values"] == {"as_of": "2026-03-31"}


def test_run_falls_back_to_default(monkeypatch):
    plan = {"title": "T", "parameters": [{"name": "as_of", "type": "date", "default": "2026-01-01"}],
            "panels": [{"title": "p", "sql": "SELECT COUNT(*) AS N FROM t WHERE d <= {{as_of}}",
                        "chart_type": "kpi", "display": "kpi"}]}
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard", lambda rid: {"DASHBOARD_PLAN": plan})
    captured = []
    _stub_run(monkeypatch, captured)

    res = dashboards.run_dashboard("rid")   # no values -> default
    assert captured == ["SELECT COUNT(*) AS N FROM t WHERE d <= '2026-01-01'"]
    assert res["parameter_values"] == {"as_of": "2026-01-01"}


# --- edit_dashboard value-change redirect ------------------------------------

def test_value_change_heuristic():
    yes = dashboards._looks_like_value_change
    assert yes("show data for the 24th instead of the 23rd")
    assert yes("run it for 2026-03-31")
    assert yes("as of 2026-03-31")
    # structural / add-parameter requests are NOT value changes
    assert not yes("add an as_of_date date parameter and use it in the panels")
    assert not yes("add a panel showing principal by sector")
    assert not yes("change the chart type to a line chart")
    assert not yes("narrow the delinquency filter to DELQ_10")


def _stub_resolve(monkeypatch, plan):
    monkeypatch.setattr(snowflake_client, "resolve_report_ids",
                        lambda q: [{"report_id": "rid1", "report_title": "D", "version": 1}])
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"REPORT_ID": "rid1", "VERSION": 1, "REPORT_TITLE": "D",
                                     "DESCRIPTION": None, "TAGS": [], "DASHBOARD_PLAN": plan})


def test_redirect_suggests_parameter_when_none_declared(monkeypatch):
    _stub_resolve(monkeypatch, {"title": "D", "panels": [], "parameters": []})
    r = dashboards.preview_edit("rid1", "show data for the 24th instead of the 23rd")
    assert r["status"] == "suggest_parameter"
    assert "AS A PARAMETER" in r["message"]


def test_redirect_uses_parameter_when_already_declared(monkeypatch):
    _stub_resolve(monkeypatch, {"title": "D", "panels": [],
                                "parameters": [{"name": "as_of_date", "type": "date", "default": "2026-01-01"}]})
    r = dashboards.preview_edit("rid1", "run it for 2026-03-31 instead")
    assert r["status"] == "use_parameter"
    assert "as_of_date" in str(r["parameters"])


# --- a placeholder the planner wrapped in quotes -----------------------------
#
# From a real plan: ('{{trade_lane}}' = 'All' OR TRADE_LANE = '{{trade_lane}}')
# rendered as (''All'' = 'All' ...), and the whole panel failed to parse.

_LANE = [{"name": "trade_lane", "type": "enum", "default": "All",
          "options": ["All", "GCC-EU"]}]


def test_a_quoted_placeholder_renders_the_same_as_an_unquoted_one():
    quoted = sql_params.render_sql(
        "SELECT 1 FROM T WHERE ('{{trade_lane}}' = 'All' OR LANE = '{{trade_lane}}')",
        _LANE, {})
    bare = sql_params.render_sql(
        "SELECT 1 FROM T WHERE ({{trade_lane}} = 'All' OR LANE = {{trade_lane}})",
        _LANE, {})
    assert quoted == bare
    assert "''All''" not in quoted
    validate_sql(quoted)  # parses: the defect was that it did not


def test_spacing_inside_quoted_braces_is_tolerated():
    out = sql_params.render_sql("SELECT '{{ trade_lane }}'", _LANE, {"trade_lane": "GCC-EU"})
    assert out == "SELECT 'GCC-EU'"


def test_a_value_containing_a_quote_is_still_escaped():
    out = sql_params.render_sql("SELECT '{{trade_lane}}'", _LANE, {"trade_lane": "O'Neil"})
    assert out == "SELECT 'O''Neil'"


def test_a_quoted_number_placeholder_is_left_as_written():
    """'5' is valid SQL and compares as text; stripping it would change what the
    query means rather than fix a broken one."""
    out = sql_params.render_sql("SELECT '{{n}}'", [{"name": "n", "type": "number"}], {"n": 5})
    assert out == "SELECT '5'"


def test_a_quoted_undeclared_placeholder_is_still_reported():
    with pytest.raises(sql_params.ParameterError):
        sql_params.render_sql("SELECT '{{nope}}'", _LANE, {})

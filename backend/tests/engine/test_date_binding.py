"""Metadata-driven date-parameter binding: the column a date parameter filters
must come from the semantic layer's date_roles, never be guessed. Resolver +
binding-validation are hermetic (real TOML config, sqlglot parse, no Snowflake).
The create_plan paths stub the LLM and the dry-run."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards
from app.engine import llm
from app.engine.connectors.snowflake import client as snowflake_client
from app.engine.metadata_loader import metadata_store as ms

PC = "PRIVATECREDIT"
T_MARKS = "PRIVATECREDIT.PUBLIC.PC_MARKS"
T_DEAL = "PRIVATECREDIT.PUBLIC.PRIVATE_CREDIT"
T_RISK = "PRIVATECREDIT.PUBLIC.PC_RISK_LIMITS"
T_FIN = "PRIVATECREDIT.PUBLIC.PC_BORROWER_FINANCIALS"


# --- resolver (semantic layer is the authority) -----------------------------

def test_resolver_ok_by_role():
    assert ms.resolve_date_column(PC, T_MARKS, "snapshot_date")["column"] == "MARK_DATE"
    assert ms.resolve_date_column(PC, T_DEAL, "maturity_date")["column"] == "MATURITY_DATE"


def test_resolver_unsupported_for_dateless_tables():
    # PC_RISK_LIMITS and PC_BORROWER_FINANCIALS have no date column at all.
    assert ms.resolve_date_column(PC, T_RISK, "snapshot_date")["status"] == "unsupported"
    assert ms.resolve_date_column(PC, T_FIN, "snapshot_date")["status"] == "unsupported"


def test_resolver_ambiguous_without_role():
    # PRIVATE_CREDIT has four date columns — a bare "as of" is ambiguous.
    r = ms.resolve_date_column(PC, T_DEAL)
    assert r["status"] == "ambiguous" and len(r["candidates"]) == 4


def test_resolver_role_not_found():
    assert ms.resolve_date_column(PC, T_MARKS, "maturity_date")["status"] == "role_not_found"


# --- SQL binding extraction --------------------------------------------------

def test_extract_binding_column_various_shapes():
    ex = dashboards._extract_placeholder_columns
    assert ex("SELECT 1 FROM t WHERE MARK_DATE <= {{as_of}}", "as_of") == {"MARK_DATE"}
    assert ex("SELECT 1 FROM t WHERE DATE(MARK_DATE) <= {{as_of}} AND x={{n}}", "as_of") == {"MARK_DATE"}
    assert ex("SELECT 1 FROM t WHERE MATURITY_DATE BETWEEN {{as_of}} AND CURRENT_DATE", "as_of") == {"MATURITY_DATE"}


# --- binding validation (the enforcement) ------------------------------------

def _param(name, bindings):
    return {"name": name, "type": "date", "default": "2026-06-30", "bindings": bindings}


def test_multitable_correct_bindings_pass():
    params = [_param("as_of_date", [
        {"table": T_DEAL, "role": "maturity_date"},
        {"table": T_MARKS, "role": "snapshot_date"}])]
    panels = [
        {"title": "Maturing deals", "sql": f"SELECT 1 FROM {T_DEAL} WHERE MATURITY_DATE >= {{{{as_of_date}}}}"},
        {"title": "Marks", "sql": f"SELECT 1 FROM {T_MARKS} WHERE MARK_DATE <= {{{{as_of_date}}}}"},
    ]
    assert dashboards._validate_date_bindings(PC, params, panels) == []


def test_wrong_column_per_table_is_flagged():
    # binding declares maturity_date on PRIVATE_CREDIT but the SQL filters ORIGINATION_DATE
    params = [_param("as_of_date", [{"table": T_DEAL, "role": "maturity_date"}])]
    panels = [{"title": "Deals", "sql": f"SELECT 1 FROM {T_DEAL} WHERE ORIGINATION_DATE <= {{{{as_of_date}}}}"}]
    problems = dashboards._validate_date_bindings(PC, params, panels)
    assert problems and "ORIGINATION_DATE" in problems[0]


def test_unsupported_table_binding_is_flagged():
    params = [_param("as_of_date", [{"table": T_RISK, "role": "snapshot_date"}])]
    panels = [{"title": "Risk", "sql": f"SELECT 1 FROM {T_RISK} WHERE 1=1"}]
    problems = dashboards._validate_date_bindings(PC, params, panels)
    assert problems and "no date column" in problems[0] and "PC_RISK_LIMITS" in problems[0]


def test_param_used_without_binding_is_flagged():
    params = [{"name": "as_of_date", "type": "date", "default": "2026-06-30"}]  # no bindings
    panels = [{"title": "Marks", "sql": f"SELECT 1 FROM {T_MARKS} WHERE MARK_DATE <= {{{{as_of_date}}}}"}]
    problems = dashboards._validate_date_bindings(PC, params, panels)
    assert problems and "no metadata bindings" in problems[0]


# --- create_plan wiring ------------------------------------------------------

def _stub(monkeypatch, plan):
    monkeypatch.setattr(llm, "generate_dashboard_plan", lambda d, c, h="": plan)
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["c"])


def test_create_plan_stops_on_unsupported_binding(monkeypatch):
    plan = llm.DashboardPlan(
        title="T",
        panels=[{"title": "Risk", "sql": f"SELECT 1 FROM {T_RISK} WHERE LIMIT_THRESHOLD_PCT <= {{{{as_of_date}}}}"}],
        parameters=[_param("as_of_date", [{"table": T_RISK, "role": "snapshot_date"}])])
    _stub(monkeypatch, plan)
    res = dashboards.create_plan("as of a date", PC)
    assert res["status"] == "needs_clarification"
    assert any("PC_RISK_LIMITS" in q for q in res["questions"])


def test_create_plan_surfaces_planner_clarification(monkeypatch):
    plan = llm.DashboardPlan(title="T", panels=[{"title": "p", "sql": "SELECT 1"}],
                             needs_clarification="Which date column should as_of use on PC_RISK_LIMITS?")
    _stub(monkeypatch, plan)
    res = dashboards.create_plan("x", PC)
    assert res["status"] == "needs_clarification"
    assert "PC_RISK_LIMITS" in res["questions"][0]


def test_create_plan_ok_for_correct_multitable(monkeypatch):
    plan = llm.DashboardPlan(
        title="T",
        panels=[
            {"title": "Maturing", "sql": f"SELECT 1 FROM {T_DEAL} WHERE MATURITY_DATE >= {{{{as_of_date}}}}",
             "chart_type": "bar", "x": "a", "y": "b", "display": "both"},
            {"title": "Marks", "sql": f"SELECT 1 FROM {T_MARKS} WHERE MARK_DATE <= {{{{as_of_date}}}}",
             "chart_type": "bar", "x": "a", "y": "b", "display": "both"},
        ],
        parameters=[_param("as_of_date", [
            {"table": T_DEAL, "role": "maturity_date"},
            {"table": T_MARKS, "role": "snapshot_date"}])])
    _stub(monkeypatch, plan)
    res = dashboards.create_plan("multi", PC)
    assert res.get("status") != "needs_clarification"
    assert res["panel_count"] == 2
    assert res["parameters"][0]["bindings"][0]["role"] == "maturity_date"

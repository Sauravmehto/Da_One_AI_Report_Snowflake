"""The tab rules run where the AI shapes a dashboard, and nowhere else.

test_dashboard_sections pins what the rules do; this pins that they are
actually applied: to a new plan in full, to an AI edit only for the tabs it
invents. Both engines, because they are separate code paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards, file_dashboards, llm
from app.engine.connectors.snowflake import client as snowflake_client


def chart(title, section):
    return {"title": title, "sql": f"SELECT 1 AS a, 2 AS b -- {title}", "chart_type": "bar",
            "x": "a", "y": "b", "display": "chart", "section": section}


def sections(panels):
    seen = []
    for p in panels:
        if p.get("section") not in seen:
            seen.append(p.get("section"))
    return seen


# The tester's freight dashboard: nine charts spread over four tabs.
SPRAWL = ([chart(f"o{i}", "Overview") for i in range(3)]
          + [chart(f"s{i}", "Service Quality") for i in range(2)]
          + [chart(f"c{i}", "Cost & D&D") for i in range(2)]
          + [chart(f"f{i}", "Cost & Finance") for i in range(2)])


# --- warehouse ------------------------------------------------------------------


@pytest.fixture
def warehouse(monkeypatch, tmp_path):
    # Plans and staged edits are cached through the storage backend; point it at a
    # throwaway folder so these tests never write into the real local cache.
    from app.engine import storage as storage_mod
    storage_mod.set_storage(storage_mod.LocalStorage(tmp_path / "storage"))
    dashboards._EDIT_CACHE.clear()
    yield from _warehouse_stubs(monkeypatch)
    storage_mod.set_storage(None)
    dashboards._EDIT_CACHE.clear()


def _warehouse_stubs(monkeypatch):
    monkeypatch.setattr(dashboards.metadata_store, "build_context", lambda db: "ctx")
    monkeypatch.setattr(dashboards.metadata_store, "dashboard_heuristics", lambda db: "")
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["a", "b"])
    yield


def test_a_new_warehouse_plan_gets_its_tabs_consolidated(warehouse, monkeypatch):
    monkeypatch.setattr(llm, "generate_dashboard_plan",
                        lambda d, c, h="": llm.DashboardPlan(title="Freight", panels=SPRAWL))
    result = dashboards.create_plan("freight", "LOAN")
    assert sections(result["plan"]["panels"]) == ["Overview"]
    assert "Layout rule applied" in result["layout_note"]


def test_a_warehouse_edit_cannot_open_a_one_chart_tab(warehouse, monkeypatch):
    current = {"title": "Sales", "panels": [chart(f"s{i}", "Sales") for i in range(5)]
               + [chart(f"p{i}", "Pricing") for i in range(4)]}
    dashboards._EDIT_CACHE.clear()
    monkeypatch.setattr(snowflake_client, "resolve_report_ids",
                        lambda q: [{"report_id": "rid1", "report_title": "Sales", "version": 1}])
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"REPORT_ID": "rid1", "VERSION": 1, "REPORT_TITLE": "Sales",
                                     "DASHBOARD_PLAN": current})
    edited = current["panels"] + [chart("RFQs and Bookings by Trade Lane", "Demand")]
    monkeypatch.setattr(llm, "edit_dashboard_plan",
                        lambda cur, chg, ctx, h="": llm.DashboardPlan(title="Sales", panels=edited))

    result = dashboards.preview_edit("rid1", "add RFQs and bookings by lane")

    assert result["status"] == "preview"
    assert sections(result["proposed_plan"]["panels"]) == ["Sales", "Pricing"]
    assert '"Demand"' in result["layout_note"]


def test_a_manual_layout_save_is_never_consolidated(warehouse, monkeypatch):
    """A tab someone arranged on purpose is theirs, however small."""
    current = {"title": "Mine", "panels": [chart("a", "Main"), chart("b", "Main")]}
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"REPORT_ID": "rid1", "VERSION": 1, "REPORT_TITLE": "Mine",
                                     "DASHBOARD_PLAN": current})
    monkeypatch.setattr(snowflake_client, "session_identity", lambda: ("U", "R"))
    stored = {}
    monkeypatch.setattr(snowflake_client, "insert_dashboard_version",
                        lambda row: stored.update(row))
    monkeypatch.setattr(dashboards, "_invalidate_run_cache", lambda *a, **k: None, raising=False)

    # Layout entries are matched to panels by title.
    dashboards.update_layout("rid1", [{"title": "a", "section": "Main"},
                                      {"title": "b", "section": "Just One"}])

    plan = stored.get("dashboard_plan") or stored.get("DASHBOARD_PLAN")
    assert plan is not None, sorted(stored)
    assert sections(plan["panels"]) == ["Main", "Just One"]


# --- file dashboards --------------------------------------------------------------


class _Connector:
    def dry_run_query(self, sql):
        return ["a", "b"]

    def run_query(self, sql):
        return {"columns": ["a", "b"], "rows": [[1, 2]], "row_count": 1, "sql": sql}

    def session_identity(self):
        return ("alice", "ROLE")


@pytest.fixture
def storage(tmp_path):
    from app.engine import storage as storage_mod
    storage_mod.set_storage(storage_mod.LocalStorage(tmp_path / "storage"))
    file_dashboards._EDIT_CACHE.clear()
    yield
    storage_mod.set_storage(None)
    file_dashboards._EDIT_CACHE.clear()


def test_a_new_file_plan_gets_its_tabs_consolidated(storage, monkeypatch):
    monkeypatch.setattr(llm, "generate_file_dashboard_plan",
                        lambda d, c: llm.DashboardPlan(title="Freight", panels=SPRAWL))
    result = file_dashboards.create_plan("freight", "FOLDER:abc", "ctx", _Connector())
    assert sections(result["plan"]["panels"]) == ["Overview"]
    assert "Layout rule applied" in result["layout_note"]


def test_a_file_edit_cannot_open_a_one_chart_tab(storage, monkeypatch):
    start = [chart(f"s{i}", "Sales") for i in range(12)]
    monkeypatch.setattr(llm, "generate_file_dashboard_plan",
                        lambda d, c: llm.DashboardPlan(title="Sales", panels=start))
    planned = file_dashboards.create_plan("sales", "FOLDER:abc", "ctx", _Connector())
    saved = file_dashboards.save_plan(planned["plan_id"], "Sales", created_by="alice")

    edited = start + [chart("Demand by Lane", "Demand")]
    monkeypatch.setattr(llm, "edit_file_dashboard_plan",
                        lambda cur, chg, ctx: llm.DashboardPlan(title="Sales", panels=edited))

    result = file_dashboards.preview_edit(saved["report_id"], "add demand", _Connector(), "ctx")

    assert result["status"] == "preview"
    assert sections(result["proposed_plan"]["panels"]) == ["Sales"]
    assert '"Demand"' in result["layout_note"]


def test_a_plan_that_needed_no_change_carries_no_note(warehouse, monkeypatch):
    fits = [chart(f"s{i}", "Sales") for i in range(6)] + [chart(f"o{i}", "Ops") for i in range(6)]
    monkeypatch.setattr(llm, "generate_dashboard_plan",
                        lambda d, c, h="": llm.DashboardPlan(title="Fits", panels=fits))
    assert "layout_note" not in dashboards.create_plan("fits", "LOAN")

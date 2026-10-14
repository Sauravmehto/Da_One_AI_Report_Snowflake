"""Hermetic tests for dashboard editing + version history orchestration in
dashboards.py. The LLM editor and all Snowflake access are stubbed, so these run
with no network: they exercise resolution (0/1/many), the narrow-edit diff, the
preview->commit staging, the drop-a-panel guard, and revert.
"""

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
from app.engine.dashboards import DashboardError


def _panel(title, sql, **kw):
    p = {"title": title, "sql": sql, "chart_type": "bar", "x": "STATE", "y": "N",
         "color": None, "size": None, "orientation": "v", "display": "both"}
    p.update(kw)
    return p


CURRENT_PLAN = {
    "title": "Loan Risk",
    "panels": [
        _panel("Loans by State", "SELECT STATE, COUNT(*) AS N FROM LOAN.PUBLIC.SERVICER1 GROUP BY STATE"),
        _panel("Delinquent Balance", "SELECT STATE, SUM(PRIN_BAL) AS N FROM LOAN.PUBLIC.SERVICER1 WHERE LOAN_STATUS LIKE 'DELQ%' GROUP BY STATE"),
    ],
}


@pytest.fixture(autouse=True)
def _clear_edit_cache():
    dashboards._EDIT_CACHE.clear()
    yield
    dashboards._EDIT_CACHE.clear()


@pytest.fixture
def stub_db(monkeypatch):
    """One current dashboard, resolvable by id or title; stubbed reads/writes."""
    state = {"inserted": None, "reverted": None}
    monkeypatch.setattr(snowflake_client, "resolve_report_ids",
                        lambda q: ([{"report_id": "rid1", "report_title": "Loan Risk", "version": 3}]
                                   if q.upper() in ("RID1", "LOAN RISK") else []))
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"REPORT_ID": "rid1", "VERSION": 3,
                                     "REPORT_TITLE": "Loan Risk", "DESCRIPTION": None,
                                     "TAGS": [], "DASHBOARD_PLAN": CURRENT_PLAN})
    monkeypatch.setattr(snowflake_client, "session_identity", lambda: ("SVC", "ROLE"))
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["c"])
    monkeypatch.setattr(dashboards.metadata_store, "build_context", lambda db: "ctx")
    monkeypatch.setattr(dashboards.metadata_store, "dashboard_heuristics", lambda db: "")
    monkeypatch.setattr(dashboards.metadata_store, "documented_databases", lambda: ["LOAN", "PRIVATECREDIT"])

    def _insert(row):
        state["inserted"] = row
    monkeypatch.setattr(snowflake_client, "insert_dashboard_version", _insert)
    return state


def _stub_editor(monkeypatch, new_plan):
    monkeypatch.setattr(llm, "edit_dashboard_plan",
                        lambda cur, chg, ctx, heur="": llm.DashboardPlan(
                            title=new_plan["title"], panels=new_plan["panels"]))


# --- resolution -------------------------------------------------------------

def test_resolve_not_found(stub_db):
    r = dashboards.preview_edit("Nope", "change x")
    assert r["status"] == "not_found"


def test_resolve_ambiguous(stub_db, monkeypatch):
    monkeypatch.setattr(snowflake_client, "resolve_report_ids",
                        lambda q: [{"report_id": "a", "report_title": "Dup", "version": 1},
                                   {"report_id": "b", "report_title": "Dup", "version": 1}])
    r = dashboards.preview_edit("Dup", "change x")
    assert r["status"] == "ambiguous"
    assert len(r["matches"]) == 2


# --- narrow edit + diff -----------------------------------------------------

def test_preview_narrow_edit_reports_only_the_changed_field(stub_db, monkeypatch):
    # Edit ONLY panel 2's WHERE clause; everything else identical.
    import copy
    edited = copy.deepcopy(CURRENT_PLAN)
    edited["panels"][1]["sql"] = edited["panels"][1]["sql"].replace("DELQ%", "DELQ_10%")
    _stub_editor(monkeypatch, edited)

    r = dashboards.preview_edit("Loan Risk", "narrow the delinquency filter to DELQ_10")
    assert r["status"] == "preview"
    assert r["current_version"] == 3 and r["new_version"] == 4
    diff = r["diff"]
    # exactly one panel changed, one field (sql), nothing added/removed
    assert diff["panels_added"] == [] and diff["panels_removed"] == []
    assert diff["title_changed"] is None
    assert len(diff["panels_changed"]) == 1
    ch = diff["panels_changed"][0]
    assert ch["index"] == 1 and list(ch["fields"].keys()) == ["sql"]
    # nothing was written on preview
    assert stub_db["inserted"] is None


def test_preview_flags_out_of_scope_change(stub_db, monkeypatch):
    # A sloppy editor also renamed panel 1 and dropped a panel — the diff must
    # surface both so Claude can flag them (panel-9-style regression guard).
    edited = {"title": "Loan Risk", "panels": [
        _panel("Loans by STATE (renamed)", CURRENT_PLAN["panels"][0]["sql"]),
    ]}
    _stub_editor(monkeypatch, edited)
    r = dashboards.preview_edit("rid1", "tweak the delinquency filter")
    diff = r["diff"]
    assert any(f == "title" for ch in diff["panels_changed"] for f in ch["fields"])
    assert len(diff["panels_removed"]) == 1  # panel 2 disappeared -> flagged


def test_edit_rejected_when_a_panel_fails_validation(stub_db, monkeypatch):
    edited = {"title": "Loan Risk", "panels": [
        CURRENT_PLAN["panels"][0],
        _panel("Broken", "SELECT bad"),
    ]}
    _stub_editor(monkeypatch, edited)
    monkeypatch.setattr(snowflake_client, "dry_run_query",
                        lambda sql: (_ for _ in ()).throw(Exception("boom")) if "bad" in sql else ["c"])
    with pytest.raises(DashboardError) as e:
        dashboards.preview_edit("rid1", "add a panel")
    assert "not staged" in str(e.value) and "no panel is dropped" in str(e.value)


# --- preview -> commit ------------------------------------------------------

def test_commit_requires_matching_preview(stub_db):
    with pytest.raises(DashboardError) as e:
        dashboards.commit_edit("rid1", "some change")
    assert "No previewed edit" in str(e.value)


def test_commit_inserts_new_version_row(stub_db, monkeypatch):
    import copy
    edited = copy.deepcopy(CURRENT_PLAN)
    edited["panels"][1]["sql"] += " ORDER BY N DESC"
    _stub_editor(monkeypatch, edited)

    dashboards.preview_edit("rid1", "order the delinquency panel")
    res = dashboards.commit_edit("rid1", "order the delinquency panel")

    assert res["status"] == "committed"
    assert res["version"] == 4 and res["version_id"] == "rid1_v4"
    row = stub_db["inserted"]
    assert row["report_id"] == "rid1"
    assert row["version_id"] == "rid1_v4" and row["version"] == 4
    assert row["edit_note"] == "order the delinquency panel"
    assert row["report_title"] == "Loan Risk"           # title carried forward
    assert row["dashboard_plan"]["panels"][1]["sql"].endswith("ORDER BY N DESC")
    # cache consumed
    assert "rid1" not in dashboards._EDIT_CACHE


# --- revert -----------------------------------------------------------------

def test_revert_ok(monkeypatch):
    seen = {}
    monkeypatch.setattr(snowflake_client, "revert_dashboard_version",
                        lambda rid, v: seen.update(rid=rid, v=v) or True)
    res = dashboards.revert("rid1", 2)
    assert res["reverted"] is True and res["reverted_to_version"] == 2
    assert seen == {"rid": "rid1", "v": 2}


def test_revert_missing_version_raises(monkeypatch):
    monkeypatch.setattr(snowflake_client, "revert_dashboard_version", lambda rid, v: False)
    with pytest.raises(DashboardError):
        dashboards.revert("rid1", 99)

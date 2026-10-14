"""Hermetic tests for file_dashboards preview_edit / commit_edit / revert."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import file_dashboards, llm
from app.engine.dashboards import DashboardError


def _panel(title, sql, **kw):
    p = {
        "title": title,
        "sql": sql,
        "chart_type": "bar",
        "x": "a",
        "y": "b",
        "color": None,
        "orientation": "v",
        "display": "both",
    }
    p.update(kw)
    return p


class _Connector:
    def dry_run_query(self, sql: str):
        return ["a", "b"]

    def run_query(self, sql: str):
        return {"columns": ["a", "b"], "rows": [[1, 1]], "row_count": 1, "sql": sql}

    def session_identity(self):
        return ("alice", "ROLE")


CURRENT_PLAN = {
    "title": "Mortgage overview",
    "panels": [
        _panel("Amounts", "SELECT value_num AS a, 1 AS b FROM facts"),
        _panel("Counts", "SELECT COUNT(*) AS a, 1 AS b FROM facts"),
    ],
}


@pytest.fixture
def storage_root(tmp_path):
    """A throwaway storage backend, INJECTED rather than configured by env var.

    set_storage(None) only clears the cached backend; the next get_storage()
    rebuilds it from EXCEL_STORAGE_DIR, which is read at import time and under a
    different name (MCP_EXCEL_STORAGE_DIR). Setting an env var here therefore did
    nothing, and these tests wrote their fixtures into the real storage
    directory. Handing over an explicit backend cannot drift from what the code
    reads, and the teardown puts the process back as it found it.
    """
    root = tmp_path / "storage"
    from app.engine import storage as storage_mod

    storage_mod.set_storage(storage_mod.LocalStorage(root))
    yield root
    storage_mod.set_storage(None)


@pytest.fixture(autouse=True)
def _clear_edit_cache():
    file_dashboards._EDIT_CACHE.clear()
    yield
    file_dashboards._EDIT_CACHE.clear()


@pytest.fixture
def saved_dashboard(storage_root, monkeypatch):
    monkeypatch.setattr(
        llm,
        "generate_file_dashboard_plan",
        lambda desc, ctx: llm.DashboardPlan(title="Mortgage overview", panels=CURRENT_PLAN["panels"]),
    )
    planned = file_dashboards.create_plan(
        "overview", "FOLDER:abc", "ctx", _Connector()
    )
    saved = file_dashboards.save_plan(
        planned["plan_id"], "Mortgage overview", created_by="alice"
    )
    return saved["report_id"]


def _stub_editor(monkeypatch, new_plan):
    monkeypatch.setattr(
        llm,
        "edit_file_dashboard_plan",
        lambda cur, chg, ctx: llm.DashboardPlan(
            title=new_plan["title"], panels=new_plan["panels"]
        ),
    )


def test_preview_narrow_edit_reports_only_changed_field(
    storage_root, monkeypatch, saved_dashboard
):
    edited = copy.deepcopy(CURRENT_PLAN)
    edited["panels"][1]["sql"] = "SELECT COUNT(*) AS a, 2 AS b FROM facts"
    _stub_editor(monkeypatch, edited)

    r = file_dashboards.preview_edit(
        saved_dashboard, "change panel 2 y to 2", _Connector(), "ctx"
    )
    assert r["status"] == "preview"
    assert r["source_type"] == "file"
    assert r["current_version"] == 1 and r["new_version"] == 2
    diff = r["diff"]
    assert diff["panels_added"] == [] and diff["panels_removed"] == []
    assert len(diff["panels_changed"]) == 1
    assert list(diff["panels_changed"][0]["fields"].keys()) == ["sql"]


def test_edit_rejected_when_a_panel_fails_validation(
    storage_root, monkeypatch, saved_dashboard
):
    edited = {
        "title": "Mortgage overview",
        "panels": [CURRENT_PLAN["panels"][0], _panel("Broken", "BAD")],
    }
    _stub_editor(monkeypatch, edited)

    class BadConnector(_Connector):
        def dry_run_query(self, sql: str):
            if "BAD" in sql:
                raise Exception("boom")
            return ["a", "b"]

    with pytest.raises(DashboardError) as exc:
        file_dashboards.preview_edit(
            saved_dashboard, "add broken panel", BadConnector(), "ctx"
        )
    assert "not staged" in str(exc.value)


def test_commit_requires_matching_preview(storage_root, saved_dashboard):
    with pytest.raises(DashboardError) as exc:
        file_dashboards.commit_edit(saved_dashboard, "some change")
    assert "No previewed edit" in str(exc.value)


def test_commit_bumps_version_and_archives_previous(
    storage_root, monkeypatch, saved_dashboard
):
    edited = copy.deepcopy(CURRENT_PLAN)
    edited["panels"][0]["sql"] += " LIMIT 10"
    _stub_editor(monkeypatch, edited)

    file_dashboards.preview_edit(
        saved_dashboard, "limit amounts panel", _Connector(), "ctx"
    )
    res = file_dashboards.commit_edit(saved_dashboard, "limit amounts panel")

    assert res["status"] == "committed"
    assert res["version"] == 2
    rec = file_dashboards.read_record(saved_dashboard)
    assert rec["version"] == 2
    assert rec["dashboard_plan"]["panels"][0]["sql"].endswith("LIMIT 10")

    versions = file_dashboards.list_versions(saved_dashboard)
    assert versions["count"] == 2
    assert versions["versions"][0]["version"] == 1
    assert versions["versions"][1]["is_current"] is True


def test_revert_restores_archived_version(storage_root, monkeypatch, saved_dashboard):
    edited = copy.deepcopy(CURRENT_PLAN)
    edited["panels"][0]["title"] = "Changed"
    _stub_editor(monkeypatch, edited)
    file_dashboards.preview_edit(saved_dashboard, "rename", _Connector(), "ctx")
    file_dashboards.commit_edit(saved_dashboard, "rename")

    res = file_dashboards.revert(saved_dashboard, 1)
    assert res["reverted"] is True
    rec = file_dashboards.read_record(saved_dashboard)
    assert rec["dashboard_plan"]["panels"][0]["title"] == "Amounts"
    assert rec["version"] == 3

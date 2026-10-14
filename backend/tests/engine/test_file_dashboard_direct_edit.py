"""Direct UI edits for file-source dashboards (update_plan)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import file_dashboards, llm
from app.engine.dashboards import DashboardError


CURRENT = {
    "title": "Mortgage overview",
    "panels": [
        {
            "title": "Amounts",
            "chart_type": "bar",
            "x": "a",
            "y": "b",
            "color": None,
            "orientation": "v",
            "display": "both",
            "sql": "SELECT value_num AS a, 1 AS b FROM facts",
        },
    ],
    "parameters": [],
}


class _Connector:
    def dry_run_query(self, sql: str):
        if "BROKEN" in sql:
            raise RuntimeError("nope")
        return ["a", "b"]

    def run_query(self, sql: str):
        return {"columns": ["a", "b"], "rows": [[1, 1]], "row_count": 1, "sql": sql}

    def session_identity(self):
        return ("alice", "ROLE")


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


@pytest.fixture
def report_id(storage_root, monkeypatch):
    monkeypatch.setattr(
        llm,
        "generate_file_dashboard_plan",
        lambda desc, ctx: llm.DashboardPlan(title=CURRENT["title"], panels=CURRENT["panels"]),
    )
    planned = file_dashboards.create_plan("x", "FOLDER:abc", "ctx", _Connector())
    saved = file_dashboards.save_plan(
        planned["plan_id"], "Mortgage overview", created_by="alice"
    )
    return saved["report_id"]


def test_direct_edit_is_saved_as_a_new_version(report_id):
    result = file_dashboards.update_plan(
        report_id,
        _Connector(),
        {"title": "Mortgage overview", "panels": [{**CURRENT["panels"][0], "chart_type": "line"}]},
        edit_note="bar to line",
    )

    assert result["version"] == 2
    assert result["saved"] is True
    rec = file_dashboards.read_record(report_id)
    assert rec["dashboard_plan"]["panels"][0]["chart_type"] == "line"
    assert rec["last_edit_note"] == "bar to line"


def test_sql_is_validated_on_direct_edit(report_id):
    with pytest.raises(DashboardError, match="failed validation"):
        file_dashboards.update_plan(
            report_id,
            _Connector(),
            {"panels": [{**CURRENT["panels"][0], "sql": "SELECT BROKEN FROM facts"}]},
        )

    rec = file_dashboards.read_record(report_id)
    assert rec["version"] == 1


def test_cannot_empty_a_dashboard(report_id):
    with pytest.raises(DashboardError, match="at least one panel"):
        file_dashboards.update_plan(report_id, _Connector(), {"panels": []})

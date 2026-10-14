"""Tests for file_dashboards — DuckDB upload-source dashboards."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import file_dashboards, llm
from app.engine import document_store
from app.engine.connectors.duckdb import client as duckdb_client
from app.engine.dashboards import DashboardError
from app.engine.sql_safety import UnsafeSQLError


class _Connector:
    def __init__(self, dry_run=None, run=None):
        self._dry_run = dry_run or (lambda sql: ["c"])
        self._run = run or (
            lambda sql: {"columns": ["c"], "rows": [[1]], "row_count": 1, "sql": sql}
        )

    def dry_run_query(self, sql: str):
        return self._dry_run(sql)

    def run_query(self, sql: str):
        return self._run(sql)

    def session_identity(self):
        return ("TEST_USER", None)


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
def documents_db(tmp_path):
    path = tmp_path / "documents.duckdb"
    conn = duckdb.connect(str(path))
    document_store.ensure_schema(conn)
    entity = document_store.resolve_entity(conn, "Property A")
    conn.execute(
        "INSERT INTO facts (fact_id, entity_id, metric, value_num, unit, "
        "source_file, source_page) VALUES ('f1', ?, 'loan_amount', 1000000, 'usd', "
        "'a.pdf', 1)",
        [entity],
    )
    conn.commit()
    conn.close()
    return path


def test_create_plan_validates_and_caches(storage_root, monkeypatch):
    monkeypatch.setattr(
        llm,
        "generate_file_dashboard_plan",
        lambda desc, ctx: llm.DashboardPlan(
            title="Loan amounts",
            panels=[_panel("By property", "SELECT 1")],
        ),
    )
    connector = _Connector()

    res = file_dashboards.create_plan(
        "loan amounts by property",
        "FOLDER:abc123",
        "# Folder context\nTable: facts",
        connector,
    )

    assert res["source_type"] == "file"
    assert res["panel_count"] == 1
    cached = file_dashboards.get_cached_plan(res["plan_id"])
    assert cached["source_key"] == "FOLDER:abc123"


def test_bad_panel_dropped_not_fatal(storage_root, monkeypatch):
    monkeypatch.setattr(
        llm,
        "generate_file_dashboard_plan",
        lambda desc, ctx: llm.DashboardPlan(
            title="T",
            panels=[_panel("Good", "SELECT 1"), _panel("Bad", "BAD")],
        ),
    )

    def dry(sql):
        if "BAD" in sql:
            raise UnsafeSQLError("not a select")
        return ["c"]

    res = file_dashboards.create_plan("x", "EXCEL:ds1", "ctx", _Connector(dry_run=dry))
    assert res["panel_count"] == 1
    assert len(res["skipped_panels"]) == 1


def test_save_and_run_roundtrip(storage_root, monkeypatch, documents_db):
    monkeypatch.setattr(
        llm,
        "generate_file_dashboard_plan",
        lambda desc, ctx: llm.DashboardPlan(
            title="Facts",
            panels=[_panel("Amounts", "SELECT value_num FROM facts")],
        ),
    )

    conn = duckdb.connect(str(documents_db), read_only=True)

    def dry(sql):
        conn.execute(sql)
        return ["value_num"]

    def run(sql):
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return {"columns": cols, "rows": rows, "row_count": len(rows), "sql": sql}

    connector = _Connector(dry_run=dry, run=run)
    planned = file_dashboards.create_plan("amounts", "FOLDER:c1", "ctx", connector)
    saved = file_dashboards.save_plan(
        planned["plan_id"], "My folder dashboard", created_by="alice"
    )

    assert saved["source_type"] == "file"
    assert file_dashboards.exists(saved["report_id"])

    ran = file_dashboards.run_dashboard(saved["report_id"], connector)
    assert ran["panels"][0]["row_count"] == 1
    assert ran["run_count"] == 1

    listed = file_dashboards.list_dashboards(source_key="FOLDER:c1")
    assert any(d["report_id"] == saved["report_id"] for d in listed)

    config = file_dashboards.get_dashboard_config(saved["report_id"])
    assert config["source_key"] == "FOLDER:c1"
    assert config["panels"][0]["sql"]


def test_rejects_non_upload_source():
    with pytest.raises(DashboardError, match="not an uploaded"):
        file_dashboards.create_plan("x", "LOAN", "ctx", _Connector())


def test_collection_plan_with_facts_table(documents_db):
    """Integration: dry-run against real document DuckDB via connector."""
    from app.engine import collection_query

    plan = collection_query.plan([], documents_db)
    sources, views = plan.as_binding()
    with duckdb_client.use_collection(sources, views, documents=documents_db):
        sql = (
            "SELECT e.canonical_name, MAX(f.value_num) AS amount "
            "FROM facts f JOIN entities e ON f.entity_id = e.entity_id "
            "WHERE f.metric = 'loan_amount' GROUP BY 1"
        )
        duckdb_client.dry_run_query(sql)
        result = duckdb_client.run_query(sql)
        assert result["row_count"] == 1

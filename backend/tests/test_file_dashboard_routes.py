"""HTTP surface for file-source saved dashboards.

Engine logic lives in tests/engine/test_file_dashboards.py. This proves the
route layer picks the file path (not Snowflake), merges list results, and runs
panels through bind_upload_source without needing a live warehouse connection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_session
from app.engine import file_dashboards, llm
from app.main import app
from app.session.base import SessionData


@pytest.fixture
def session() -> SessionData:
    return SessionData(
        session_id="dash-routes",
        snowflake_user="ANALYST",
        role="READ_WRITE_LOAN_DB_ROLE",
        access_token="token",
        refresh_token_encrypted="",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_mode="oauth",
    )


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
def client(monkeypatch, session, storage_root) -> TestClient:
    async def fake_snowflake_engine(request, sess, fn, *args):
        if fn.__name__ == "list_dashboards":
            return []
        return fn(*args)

    class _RouteConn:
        @staticmethod
        def dry_run_query(sql: str):
            return ["a", "b"]

        @staticmethod
        def run_query(sql: str):
            return {"columns": ["a", "b"], "rows": [[42, 1]], "row_count": 1, "sql": sql}

        @staticmethod
        def session_identity():
            return ("ANALYST", "READ_WRITE_LOAN_DB_ROLE")

    async def fake_file_dashboard(request, sess, report_id, fn, *args):
        return fn(report_id, _RouteConn(), *args)

    monkeypatch.setattr(
        "app.api.v1.routes.dashboards._run_snowflake_engine", fake_snowflake_engine
    )
    monkeypatch.setattr(
        "app.api.v1.routes.dashboards._run_file_dashboard", fake_file_dashboard
    )
    app.dependency_overrides[get_current_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_current_session, None)


def _panel(title: str, sql: str) -> dict[str, Any]:
    return {
        "title": title,
        "sql": sql,
        "chart_type": "bar",
        "x": "a",
        "y": "b",
        "color": None,
        "orientation": "v",
        "display": "both",
    }


def _seed_file_dashboard(
    monkeypatch,
    *,
    source_key: str = "FOLDER:abc123",
    created_by: str = "ANALYST",
) -> str:
    monkeypatch.setattr(
        llm,
        "generate_file_dashboard_plan",
        lambda desc, ctx: llm.DashboardPlan(
            title="Route test",
            panels=[_panel("Count", "SELECT 42 AS a, 1 AS b")],
        ),
    )

    class _Conn:
        @staticmethod
        def dry_run_query(sql: str):
            return ["a", "b"]

        @staticmethod
        def run_query(sql: str):
            return {"columns": ["a", "b"], "rows": [[42, 1]], "row_count": 1, "sql": sql}

        @staticmethod
        def session_identity():
            return ("ANALYST", "READ_WRITE_LOAN_DB_ROLE")

    planned = file_dashboards.create_plan("test", source_key, "ctx", _Conn())
    saved = file_dashboards.save_plan(
        planned["plan_id"], "Route E2E", created_by=created_by
    )
    return saved["report_id"]


def test_list_merges_file_dashboards(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)

    response = client.get("/api/v1/dashboards")

    assert response.status_code == 200
    body = response.json()
    ids = [d["report_id"] for d in body["dashboards"]]
    assert report_id in ids
    file_row = next(d for d in body["dashboards"] if d["report_id"] == report_id)
    assert file_row["source_type"] == "file"
    assert file_row["source_key"] == "FOLDER:abc123"


def test_get_file_dashboard_runs_panels(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)

    response = client.get(f"/api/v1/dashboards/{report_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["source_type"] == "file"
    assert body["run_count"] == 1
    assert len(body["panels"]) == 1
    assert body["panels"][0]["row_count"] == 1
    assert not body["panels"][0].get("error")


def test_post_run_increments_run_count(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)
    client.get(f"/api/v1/dashboards/{report_id}")

    response = client.post(f"/api/v1/dashboards/{report_id}/run", json={})

    assert response.status_code == 200
    assert response.json()["run_count"] == 2


def test_file_dashboard_versions_stub(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)

    response = client.get(f"/api/v1/dashboards/{report_id}/versions")

    assert response.status_code == 200
    versions = response.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["is_current"] is True


def test_file_dashboard_revert(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)
    from app.engine import file_dashboards, llm
    import copy

    edited = {
        "title": "Route test",
        "panels": [
            dict(
                title="Count",
                sql="SELECT 99 AS a, 1 AS b",
                chart_type="bar",
                x="a",
                y="b",
                color=None,
                orientation="v",
                display="both",
            )
        ],
    }
    monkeypatch.setattr(
        llm,
        "edit_file_dashboard_plan",
        lambda cur, chg, ctx: llm.DashboardPlan(
            title=edited["title"], panels=edited["panels"]
        ),
    )

    class _Conn:
        @staticmethod
        def dry_run_query(sql: str):
            return ["a", "b"]

        @staticmethod
        def run_query(sql: str):
            return {"columns": ["a", "b"], "rows": [[42, 1]], "row_count": 1, "sql": sql}

        @staticmethod
        def session_identity():
            return ("ANALYST", "READ_WRITE_LOAN_DB_ROLE")

    file_dashboards.preview_edit(report_id, "change sql", _Conn(), "ctx")
    file_dashboards.commit_edit(report_id, "change sql")

    response = client.post(
        f"/api/v1/dashboards/{report_id}/revert", json={"version": 1}
    )

    assert response.status_code == 200
    assert response.json()["reverted"] is True


def test_file_dashboard_direct_edit_saves(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)
    from app.core.config import settings

    monkeypatch.setattr(settings, "ADMIN_ROLES", ["READ_WRITE_LOAN_DB_ROLE"])

    response = client.put(
        f"/api/v1/dashboards/{report_id}",
        json={
            "plan": {
                "title": "Route E2E",
                "panels": [
                    {
                        "title": "Count",
                        "sql": "SELECT 42 AS a, 1 AS b",
                        "chart_type": "line",
                        "x": "a",
                        "y": "b",
                        "color": None,
                        "orientation": "v",
                        "display": "both",
                    }
                ],
            },
            "edit_note": "bar to line",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] is True
    assert body["version"] == 2
    assert body["source_type"] == "file"


def test_file_dashboard_direct_edit_rejects_empty_panels(client, monkeypatch, storage_root):
    report_id = _seed_file_dashboard(monkeypatch)
    from app.core.config import settings

    monkeypatch.setattr(settings, "ADMIN_ROLES", ["READ_WRITE_LOAN_DB_ROLE"])

    response = client.put(
        f"/api/v1/dashboards/{report_id}",
        json={"plan": {"panels": []}},
    )

    assert response.status_code == 400
    assert "at least one panel" in response.json()["detail"]

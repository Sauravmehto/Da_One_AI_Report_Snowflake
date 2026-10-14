"""Who may author semantic layers, and who may delete shared work.

Rules worth pinning because they fail silently in the dangerous direction if
they regress — an authorisation check that stops running does not error, it
just starts saying yes.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_session
from app.core.config import Settings
from app.session.base import SessionData


def _settings(**overrides) -> Settings:
    return Settings(SNOWFLAKE_ACCOUNT="test-account", **overrides)


def test_no_configured_roles_means_nobody_is_admin():
    """The default must be closed.

    A deployment that never sets ADMIN_ROLES should refuse admin access, not
    hand it to everyone — the failure has to be a locked door, not an open one.
    """
    settings = _settings()
    assert settings.is_admin_role("ANY_ROLE") is False
    assert settings.is_admin_role(None) is False


def test_admin_role_matches_case_insensitively():
    """Snowflake reports roles uppercased. A lower-case entry in the
    environment that never matched would look like a permissions bug."""
    settings = _settings(ADMIN_ROLES=["dataalpha_admin"])
    assert settings.is_admin_role("DATAALPHA_ADMIN") is True
    assert settings.is_admin_role("  DataAlpha_Admin  ") is True


def test_non_admin_role_is_refused():
    settings = _settings(ADMIN_ROLES=["DATAALPHA_ADMIN"])
    assert settings.is_admin_role("PRIVATE_CREDIT_ANALYST") is False


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("DATAALPHA_ADMIN", ["DATAALPHA_ADMIN"]),
        ("A,B , C", ["A", "B", "C"]),
        ('["A", "B"]', ["A", "B"]),
        ("", []),
    ],
)
def test_admin_roles_parsed_from_the_environment(monkeypatch, raw, expected):
    """Set through the ENVIRONMENT, not as a constructor argument.

    The two take different paths: pydantic-settings JSON-decodes a list field
    inside the env source, before any field validator runs. An earlier version
    of this test passed the value as a kwarg, missed that entirely, and the
    plain `ADMIN_ROLES=DATAALPHA_ADMIN` spelling crashed the app at import.
    """
    monkeypatch.setenv("ADMIN_ROLES", raw)
    assert _settings().ADMIN_ROLES == expected


# ── Deleting shared work ──────────────────────────────────────────────────
#
# Anyone signed in may RUN a saved dashboard or report; only an administrator
# may remove one. The distinction matters because these are shared: a delete
# takes the thing away from everybody, not just from the person clicking.


@pytest.fixture
def analyst() -> SessionData:
    return SessionData(
        session_id="s1",
        snowflake_user="ANALYST",
        role="PRIVATE_CREDIT_ANALYST",
        access_token="token",
        refresh_token_encrypted="",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_mode="keypair",
    )


@pytest.fixture
def client(analyst) -> TestClient:
    from app.main import app

    app.dependency_overrides[get_current_session] = lambda: analyst
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_current_session, None)


def _as_admin(monkeypatch, role: str) -> None:
    """Make the signed-in role an administrator for this test only."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ADMIN_ROLES", [role])


@pytest.mark.parametrize(
    "path",
    ["/api/v1/dashboards/d1", "/api/v1/excel/reports/r1"],
)
def test_a_non_admin_cannot_delete(client, path, monkeypatch):
    """No ADMIN_ROLES configured, so nobody is an admin — and the refusal must
    happen before any engine call, not after a Snowflake round trip."""
    called: list[str] = []
    monkeypatch.setattr(
        "app.engine.dashboards.delete_dashboard",
        lambda rid: called.append(rid),
    )
    monkeypatch.setattr(
        "app.engine.excel_reports.delete_report",
        lambda rid: called.append(rid),
    )

    response = client.delete(path)

    assert response.status_code == 403
    assert "administrator role" in response.json()["detail"]
    assert called == [], "the engine ran despite the role being refused"


def test_an_admin_can_delete_a_dashboard(client, analyst, monkeypatch):
    _as_admin(monkeypatch, analyst.role)
    seen: dict = {}

    def fake_delete(report_id):
        seen["id"] = report_id
        return {"report_id": report_id, "report_title": "Q3 Exposure", "deleted": True}

    monkeypatch.setattr("app.engine.dashboards.delete_dashboard", fake_delete)

    async def inline(request, session, fn, *args):
        return fn(*args)

    monkeypatch.setattr("app.api.v1.routes.dashboards._run_snowflake_engine", inline)

    response = client.delete("/api/v1/dashboards/d1")

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert seen["id"] == "d1"


def test_an_admin_can_delete_an_excel_report(client, analyst, monkeypatch):
    _as_admin(monkeypatch, analyst.role)
    monkeypatch.setattr(
        "app.engine.excel_reports.delete_report",
        lambda rid: {"report_id": rid, "report_title": "Sales", "deleted": True},
    )

    async def inline(request, session, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.routes.excel_reports._run_engine", inline)

    response = client.delete("/api/v1/excel/reports/r1")

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True


def test_deleting_something_that_is_not_there_is_a_404(client, analyst, monkeypatch):
    """Distinguishable from a refusal: 403 means "not you", 404 means "not
    there". Collapsing them would make a typo look like a permissions problem."""
    from app.engine import excel_reports as engine

    _as_admin(monkeypatch, analyst.role)

    def missing(rid):
        raise engine.ExcelReportError(f"No active saved report with report_id '{rid}'.")

    monkeypatch.setattr(engine, "delete_report", missing)

    async def inline(request, session, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.routes.excel_reports._run_engine", inline)

    response = client.delete("/api/v1/excel/reports/nope")

    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_a_non_admin_cannot_edit_a_dashboard(client, monkeypatch):
    """Editing is shared-state surgery, same as deleting: it changes what
    everyone sees. And the refusal must land before any engine work."""
    called: list = []
    monkeypatch.setattr("app.engine.dashboards.update_plan",
                        lambda *a, **k: called.append(a))

    response = client.put("/api/v1/dashboards/d1", json={"plan": {"panels": []}})

    assert response.status_code == 403
    assert "administrator role" in response.json()["detail"]
    assert called == []


def test_an_admin_edit_reaches_the_engine_with_the_plan(client, analyst, monkeypatch):
    _as_admin(monkeypatch, analyst.role)
    seen: dict = {}

    def fake_update(report_id, plan, edit_note):
        seen.update(report_id=report_id, plan=plan, note=edit_note)
        return {"report_id": report_id, "version": 4, "saved": True}

    monkeypatch.setattr("app.engine.dashboards.update_plan", fake_update)

    async def inline(request, session, fn, *args):
        return fn(*args)

    monkeypatch.setattr("app.api.v1.routes.dashboards._run_snowflake_engine", inline)

    response = client.put("/api/v1/dashboards/d1", json={
        "plan": {"panels": [{"title": "P", "sql": "SELECT 1"}]},
        "edit_note": "renamed",
    })

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 4
    assert seen["plan"]["panels"][0]["sql"] == "SELECT 1"
    assert seen["note"] == "renamed"


def test_a_panel_that_will_not_compile_is_a_400_not_a_500(client, analyst, monkeypatch):
    from app.engine import dashboards as dash

    _as_admin(monkeypatch, analyst.role)

    def boom(*args):
        raise dash.DashboardError("1 panel(s) failed validation ... invalid identifier")

    monkeypatch.setattr(dash, "update_plan", boom)

    async def inline(request, session, fn, *args):
        return fn(*args)

    monkeypatch.setattr("app.api.v1.routes.dashboards._run_snowflake_engine", inline)

    response = client.put("/api/v1/dashboards/d1", json={"plan": {"panels": []}})

    assert response.status_code == 400
    assert "failed validation" in response.json()["detail"]


def test_panel_sql_is_not_readable_by_a_non_admin(client, monkeypatch):
    """The run endpoints deliberately return render config and rows but no SQL.
    The config endpoint is the only HTTP surface that exposes how a dashboard is
    built, so it must not widen who can see that."""
    monkeypatch.setattr(
        "app.engine.dashboards.get_dashboard_config",
        lambda rid: {"panels": [{"sql": "SELECT SECRET FROM VAULT"}]},
    )

    response = client.get("/api/v1/dashboards/d1/config")

    assert response.status_code == 403
    assert b"VAULT" not in response.content


def test_the_run_response_still_carries_no_sql(monkeypatch):
    """Pins the property the check above depends on: if a refactor started
    including SQL in the run payload, gating /config would protect nothing."""
    from app.engine import dashboards as dash

    monkeypatch.setattr(dash.snowflake_client, "read_saved_dashboard", lambda rid: {
        "REPORT_ID": rid, "REPORT_TITLE": "T", "VERSION": 1,
        "DASHBOARD_PLAN": {"title": "T", "parameters": [], "panels": [
            {"title": "P", "chart_type": "bar", "x": "A", "y": "B",
             "sql": "SELECT SECRET FROM VAULT"}]},
    })
    monkeypatch.setattr(dash.snowflake_client, "run_query",
                        lambda sql, max_rows=None: {"columns": ["A"], "rows": [[1]], "row_count": 1})
    monkeypatch.setattr(dash.snowflake_client, "touch_saved_dashboard_run",
                        lambda rid: (1, None))

    out = dash.run_dashboard("d1", None)

    assert "sql" not in out["panels"][0], "the run payload started leaking SQL"

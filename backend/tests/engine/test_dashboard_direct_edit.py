"""Editing a dashboard's configuration directly, without the model.

update_plan is the sibling of preview_edit/commit_edit for a caller that knows
exactly what it wants. The whole risk is that "skips the LLM" quietly becomes
"skips the checks" — these pin that it does not.
"""

from __future__ import annotations

import pytest

from app.engine import dashboards as dash


CURRENT = {
    "title": "Loan Portfolio",
    "panels": [
        {"title": "By sector", "chart_type": "bar", "x": "SECTOR", "y": "TOTAL",
         "sql": "SELECT SECTOR, SUM(PRINCIPAL) AS TOTAL FROM LOAN.PUBLIC.L GROUP BY 1"},
    ],
    "parameters": [],
}


@pytest.fixture
def saved(monkeypatch):
    """A stored dashboard, with every Snowflake call stubbed."""
    written: dict = {}
    monkeypatch.setattr(dash.snowflake_client, "read_saved_dashboard", lambda rid: {
        "REPORT_ID": rid, "REPORT_TITLE": "Loan Portfolio", "VERSION": 3,
        "DESCRIPTION": "d", "TAGS": [], "DASHBOARD_PLAN": CURRENT,
    })
    monkeypatch.setattr(dash.snowflake_client, "session_identity", lambda: ("U", "R"))
    monkeypatch.setattr(dash.snowflake_client, "insert_dashboard_version",
                        lambda row: written.update(row))
    monkeypatch.setattr(dash.snowflake_client, "dry_run_query", lambda sql: None)
    monkeypatch.setattr(dash, "_validate_date_bindings",
                        lambda db, params, panels: [])
    return written


def test_an_edit_is_saved_as_a_new_version(saved):
    """Never an overwrite. The previous version stays, demoted, so a bad edit is
    a revert rather than a loss."""
    result = dash.update_plan("d1", {
        "title": "Loan Portfolio",
        "panels": [{**CURRENT["panels"][0], "chart_type": "line"}],
    }, edit_note="bar to line")

    assert result["version"] == 4, "did not build on the stored version"
    assert saved["version_id"] == "d1_v4"
    assert saved["edit_note"] == "bar to line"
    assert saved["dashboard_plan"]["panels"][0]["chart_type"] == "line"


def test_sql_is_still_validated_when_the_model_is_skipped(saved, monkeypatch):
    """The gate belongs to the plan, not to whoever wrote it. A hand-edited
    panel that will not compile must be refused exactly like a generated one."""
    def wont_compile(sql):
        raise RuntimeError("invalid identifier 'NOPE'")

    monkeypatch.setattr(dash.snowflake_client, "dry_run_query", wont_compile)

    with pytest.raises(dash.DashboardError, match="failed validation"):
        dash.update_plan("d1", {
            "panels": [{**CURRENT["panels"][0], "sql": "SELECT NOPE FROM L"}],
        })

    assert saved == {}, "a failing edit was written anyway"


def test_one_bad_panel_rejects_the_whole_edit(saved, monkeypatch):
    """An edit must never quietly shrink a dashboard — the good panels are not
    saved without the bad one."""
    def only_the_second_fails(sql):
        if "BROKEN" in sql:
            raise RuntimeError("nope")

    monkeypatch.setattr(dash.snowflake_client, "dry_run_query", only_the_second_fails)

    with pytest.raises(dash.DashboardError, match="no panel is dropped"):
        dash.update_plan("d1", {
            "panels": [
                CURRENT["panels"][0],
                {"title": "Bad", "sql": "SELECT BROKEN FROM L"},
            ],
        })

    assert saved == {}


def test_an_edit_cannot_empty_a_dashboard(saved):
    with pytest.raises(dash.DashboardError, match="at least one panel"):
        dash.update_plan("d1", {"panels": []})

    assert saved == {}


def test_unknown_panel_keys_do_not_reach_stored_json(saved):
    """The plan arrives from a client. Only known fields are carried across, so
    nothing downstream ends up trusting a key nobody validated."""
    dash.update_plan("d1", {
        "panels": [{**CURRENT["panels"][0],
                    "onclick": "alert(1)", "is_admin": True}],
    })

    stored = saved["dashboard_plan"]["panels"][0]
    assert "onclick" not in stored
    assert "is_admin" not in stored
    assert stored["sql"] == CURRENT["panels"][0]["sql"], "the SQL was lost"


def test_parameters_are_kept_when_the_edit_does_not_mention_them(saved, monkeypatch):
    """A form that edits panels should not silently drop the date control."""
    monkeypatch.setattr(dash.snowflake_client, "read_saved_dashboard", lambda rid: {
        "REPORT_ID": rid, "REPORT_TITLE": "T", "VERSION": 1, "TAGS": [],
        "DASHBOARD_PLAN": {**CURRENT,
                           "parameters": [{"name": "as_of", "type": "date"}]},
    })

    dash.update_plan("d1", {"panels": CURRENT["panels"]})

    assert saved["dashboard_plan"]["parameters"] == [{"name": "as_of", "type": "date"}]


def test_editing_a_dashboard_that_is_not_there_is_refused(monkeypatch):
    monkeypatch.setattr(dash.snowflake_client, "read_saved_dashboard", lambda rid: None)

    with pytest.raises(dash.DashboardError, match="No active saved dashboard"):
        dash.update_plan("gone", {"panels": CURRENT["panels"]})


def test_a_saved_panel_naming_a_column_its_query_lacks_is_reported(saved, monkeypatch):
    """Saved anyway: the query compiles and the edit may be half done. The save
    says which chart will not draw, rather than leaving it to the dashboard."""
    monkeypatch.setattr(dash.snowflake_client, "dry_run_query", lambda sql: ["SECTOR", "TOTAL"])

    result = dash.update_plan("d1", {
        "panels": [{**CURRENT["panels"][0], "x": "INDUSTRY"}],
    })

    assert saved["version_id"] == "d1_v4"
    assert result["warnings"] == [
        "'By sector': x \"INDUSTRY\" is not a column the query returns (SECTOR, TOTAL)"]


def test_an_unchanged_panel_is_not_reported_on_every_save(saved, monkeypatch):
    monkeypatch.setattr(dash.snowflake_client, "dry_run_query", lambda sql: ["OTHER"])

    result = dash.update_plan("d1", {"title": "Renamed", "panels": CURRENT["panels"]})

    assert result["warnings"] == []

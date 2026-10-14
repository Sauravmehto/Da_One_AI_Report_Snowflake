"""Saving which tab a panel sits on and how much of the grid it occupies.

update_layout exists so a drag does not pay for update_plan's full re-validation.
The risk in that shortcut is it becoming a way to change things it has no
business changing — these pin what it can and cannot touch.
"""

from __future__ import annotations

import pytest

from app.engine import dashboards as dash


PANEL_SQL = "SELECT SECTOR, SUM(PRINCIPAL) AS TOTAL FROM LOAN.PUBLIC.L GROUP BY 1"

CURRENT = {
    "title": "Loan Portfolio",
    "panels": [
        {"title": "By sector", "chart_type": "bar", "x": "SECTOR", "y": "TOTAL",
         "sql": PANEL_SQL},
        {"title": "By vintage", "chart_type": "bar", "x": "VINTAGE", "y": "TOTAL",
         "sql": PANEL_SQL},
        {"title": "Detail", "display": "table", "sql": PANEL_SQL},
    ],
    "parameters": [{"name": "as_of", "type": "date", "default": "2026-01-31"}],
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
    return written


def test_layout_is_saved_as_a_new_version(saved):
    """Revertible like any other edit, so a rearrangement is never a one-way door."""
    result = dash.update_layout("d1", [
        {"title": "By sector", "section": "Overview", "span": 4, "row_span": 2},
    ])

    assert result["version"] == 4, "did not build on the stored version"
    assert saved["edit_note"] == "layout changed"
    panel = saved["dashboard_plan"]["panels"][0]
    assert (panel["section"], panel["span"], panel["row_span"]) == ("Overview", 4, 2)


def test_sql_is_never_dry_run(saved, monkeypatch):
    """The whole point of the separate path. A layout change cannot alter SQL, so
    paying for a dry-run per panel would be latency for nothing."""
    def fail(sql):
        raise AssertionError("update_layout must not dry-run SQL")

    monkeypatch.setattr(dash.snowflake_client, "dry_run_query", fail)
    dash.update_layout("d1", [{"title": "By sector", "span": 4}])


def test_sql_and_parameters_survive_untouched(saved):
    """A client sends only presentation fields; anything else it happens to put in
    the payload must not reach the stored plan."""
    dash.update_layout("d1", [
        {"title": "By sector", "span": 2, "sql": "SELECT 1", "chart_type": "pie"},
    ])

    panel = saved["dashboard_plan"]["panels"][0]
    assert panel["sql"] == PANEL_SQL, "layout save rewrote a panel's SQL"
    assert panel["chart_type"] == "bar", "layout save changed how a panel renders"
    assert saved["dashboard_plan"]["parameters"] == CURRENT["parameters"]


def test_out_of_range_values_are_clamped(saved):
    """The grid is 4 columns. A span of 9 must become a wide panel, never a
    panel the renderer cannot place."""
    dash.update_layout("d1", [
        {"title": "By sector", "span": 9, "row_span": 0},
        {"title": "By vintage", "span": "wide"},
    ])

    panels = saved["dashboard_plan"]["panels"]
    assert (panels[0]["span"], panels[0]["row_span"]) == (4, 1)
    assert panels[1]["span"] is None, "unparseable span should fall back to derived"


def test_entry_order_becomes_panel_order(saved):
    """Position is a layout decision too, so the order the client displays is the
    order that gets stored."""
    dash.update_layout("d1", [
        {"title": "Detail", "span": 4},
        {"title": "By vintage", "span": 2},
        {"title": "By sector", "span": 2},
    ])

    assert [p["title"] for p in saved["dashboard_plan"]["panels"]] == [
        "Detail", "By vintage", "By sector"
    ]


def test_a_panel_the_client_did_not_know_about_is_kept(saved):
    """A stale client must not be able to drop a panel added since it loaded."""
    dash.update_layout("d1", [{"title": "By sector", "span": 4}])

    titles = [p["title"] for p in saved["dashboard_plan"]["panels"]]
    assert titles == ["By sector", "By vintage", "Detail"], (
        "panels missing from the payload were dropped"
    )


def test_an_unknown_title_is_ignored_not_fatal(saved):
    """A renamed panel should cost that one entry, not the whole rearrangement."""
    dash.update_layout("d1", [
        {"title": "Was renamed", "span": 4},
        {"title": "By sector", "span": 3},
    ])

    panels = {p["title"]: p for p in saved["dashboard_plan"]["panels"]}
    assert panels["By sector"]["span"] == 3
    assert len(panels) == 3


def test_no_change_saves_nothing(saved):
    """Opening Arrange and closing it again should not add a version."""
    result = dash.update_layout("d1", [{"title": "By sector"}])

    assert result["saved"] is False
    assert saved == {}, "a no-op wrote a version"


def test_a_missing_dashboard_is_an_error(monkeypatch):
    monkeypatch.setattr(dash.snowflake_client, "read_saved_dashboard", lambda rid: None)
    with pytest.raises(dash.DashboardError):
        dash.update_layout("nope", [{"title": "x", "span": 2}])


def test_repeated_titles_are_matched_in_order():
    """Two panels can share a title. The second entry must land on the second
    panel rather than overwriting the first. (Widths of 2 and 3: a chart is
    never narrower than half the grid, so a width of 1 would come back as 2.)"""
    panels = [
        {"title": "Exposure", "sql": PANEL_SQL},
        {"title": "Exposure", "sql": PANEL_SQL},
    ]
    out, changed = dash._apply_layout(panels, [
        {"title": "Exposure", "span": 2},
        {"title": "Exposure", "span": 3},
    ])

    assert [p["span"] for p in out] == [2, 3]
    assert changed == 2


# --- display vs chart_type -------------------------------------------------
#
# The two fields can contradict each other and the renderer used to settle it by
# discarding the chart: `display == "table"` won before `chart_type` was even
# read, so a panel that had chosen a funnel was drawn as a grid of numbers. It
# looked like the planner never picking good chart types, which sent us to the
# prompt instead of here.

def test_a_table_display_does_not_discard_a_chosen_chart():
    panel = dash._normalise_layout(
        {"display": "table", "chart_type": "funnel", "x": "STAGE", "y": "N"}
    )
    assert panel["display"] == "both", (
        "a panel naming a drawable chart must keep it; the rows stay underneath"
    )


def test_a_genuine_table_panel_stays_a_table():
    panel = dash._normalise_layout({"display": "table", "chart_type": None})
    assert panel["display"] == "table"


def test_a_chart_display_with_nothing_to_draw_becomes_a_table():
    panel = dash._normalise_layout({"display": "chart", "chart_type": None})
    assert panel["display"] == "table"


def test_kpi_survives_a_contradictory_display():
    panel = dash._normalise_layout({"display": "table", "chart_type": "kpi"})
    assert panel["display"] == "kpi"


def test_an_invented_display_mode_is_dropped():
    """A value the renderer does not understand looks configured and is not."""
    panel = dash._normalise_layout(
        {"display": "sparkline", "chart_type": "bar"}
    )
    assert panel["display"] in dash.DISPLAY_MODES


def test_display_is_normalised_for_case_and_padding():
    panel = dash._normalise_layout(
        {"display": "  TABLE ", "chart_type": "Treemap"}
    )
    assert panel["display"] == "both"

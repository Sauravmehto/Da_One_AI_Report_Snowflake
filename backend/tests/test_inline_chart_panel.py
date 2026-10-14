"""An inline chart in chat is built like a dashboard panel, so it draws like one.

The chat renders show_chart's panel with the dashboard's own card. What made
the two look different was the panel: no currency (AED money read as USD), no
column formats, and one value column for every chart except a stacked bar, so
a measure against its target came out as a single line.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import tools
from app.agent.tools import ToolContext
from app.engine import dashboards
from app.engine.metadata_loader import metadata_store


class _Connector:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns, self.rows = columns, rows

    def session_identity(self):
        return ("U", "R")

    def run_query(self, sql: str, max_rows: int | None = None) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows,
                "row_count": len(self.rows), "sql": sql}


@pytest.fixture
def chart(monkeypatch):
    monkeypatch.setattr(tools.audit, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(metadata_store, "currency", lambda db: "AED" if db == "FINTECH_DB" else None)

    def run(columns, rows, **kwargs):
        connector = _Connector(columns, rows)
        monkeypatch.setattr(ToolContext, "connector", property(lambda self: connector))
        ctx = ToolContext(database="FINTECH_DB")
        return tools._show_chart(ctx, sql="SELECT 1", user_question="q", **kwargs)

    return run


NPL = (["WEEK_START", "NPL_PCT", "TARGET_NPL_PCT"], [["2025-01-06", 0.031, 0.03]])


def test_several_value_columns_are_drawn_for_any_chart_type(chart):
    out = chart(*NPL, title="NPL vs target", chart_type="area_line",
                x="week_start", y="NPL_PCT, TARGET_NPL_PCT")
    assert out["panel"]["y"] == ["NPL_PCT", "TARGET_NPL_PCT"]
    assert out["panel"]["x"] == "WEEK_START"


def test_a_list_of_value_columns_is_accepted_too(chart):
    out = chart(*NPL, title="t", chart_type="combo", x="WEEK_START",
                y=["NPL_PCT", "TARGET_NPL_PCT"])
    assert out["panel"]["y"] == ["NPL_PCT", "TARGET_NPL_PCT"]


def test_one_value_column_stays_a_plain_name(chart):
    out = chart(["SEGMENT", "BALANCE_AED"], [["SME", 10]], title="t", chart_type="bar",
                x="SEGMENT", y="BALANCE_AED")
    assert out["panel"]["y"] == "BALANCE_AED"


def test_money_reads_in_the_databases_currency(chart):
    out = chart(["SEGMENT", "BALANCE_AED"], [["SME", 10]], title="t", chart_type="bar",
                x="SEGMENT", y="BALANCE_AED")
    assert out["panel"]["default_currency"] == "AED"


def test_column_formats_are_kept_and_cleaned_like_a_dashboards(chart):
    out = chart(*NPL, title="t", chart_type="line", x="WEEK_START", y="NPL_PCT",
                column_formats={"NPL_PCT": {"format": "percent", "decimals": 2},
                                "JUNK": {"format": "sparkles"}})
    assert out["panel"]["column_formats"] == {"NPL_PCT": {"format": "percent", "decimals": 2}}


def test_a_chart_that_cannot_draw_what_it_names_is_sent_back(chart):
    out = chart(*NPL, title="t", chart_type="combo", x="WEEK_START", y="NPL_PCT")
    assert "error" in out and "combo needs at least 2 y columns" in out["error"]

    out = chart(*NPL, title="t", chart_type="bar", x="WEEK_START", y="NPL_PCT,GONE")
    assert "error" in out and "y (GONE)" in out["error"]


def test_a_missing_split_column_is_reported_not_dropped(chart):
    out = chart(*NPL, title="t", chart_type="line", x="WEEK_START", y="NPL_PCT",
                color="MARKET")
    assert "error" in out and "color (MARKET)" in out["error"]


def test_the_inline_panel_matches_a_dashboard_run_of_the_same_panel(chart, monkeypatch):
    """Same config, same rows: chat and dashboard hand the renderer the same panel."""
    config = {"title": "NPL vs target", "chart_type": "area_line", "x": "WEEK_START",
              "y": ["NPL_PCT", "TARGET_NPL_PCT"], "color": None, "size": None,
              "orientation": None, "display": "chart",
              "column_formats": {"NPL_PCT": {"format": "percent", "decimals": 2}},
              "sql": "SELECT 1 FROM FINTECH_DB.PUBLIC.FINTECH"}
    columns, rows = NPL
    monkeypatch.setattr(dashboards.snowflake_client, "read_saved_dashboard", lambda rid: {
        "DASHBOARD_PLAN": {"title": "D", "panels": [config], "parameters": []}})
    monkeypatch.setattr(dashboards.snowflake_client, "run_query", lambda sql, max_rows=None: {
        "columns": columns, "rows": rows, "row_count": len(rows), "sql": sql})
    monkeypatch.setattr(dashboards.snowflake_client, "touch_saved_dashboard_run",
                        lambda rid: (1, None))
    dashboard_panel = dashboards.run_dashboard("d1")["panels"][0]

    inline = chart(columns, rows, title=config["title"], chart_type="area_line",
                   x="WEEK_START", y="NPL_PCT,TARGET_NPL_PCT",
                   column_formats=config["column_formats"])["panel"]

    # A dashboard also says whether its row cap was hit; chat has no such cap flag.
    dashboard_panel.pop("truncated")
    assert inline == dashboard_panel

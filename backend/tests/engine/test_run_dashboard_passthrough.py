"""Regression guard: run_dashboard must return panel rows EXACTLY as Snowflake
produced them — no server-side shaping. This replaces the old
test_dashboards_shaping.py (shape_panel_rows was removed; year/quarter handling
now lives in the panel SQL). Hermetic — Snowflake access is stubbed.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards
from app.engine.connectors.snowflake import client as snowflake_client


def test_run_dashboard_passes_rows_through_unchanged(monkeypatch):
    plan = {
        "title": "T",
        "panels": [{
            "title": "By Fiscal Year", "chart_type": "bar",
            "x": "FISCAL_YEAR", "y": "REVENUE", "color": None,
            "orientation": "v", "display": "both",
            "sql": "SELECT FISCAL_YEAR, SUM(REVENUE) AS REVENUE FROM t GROUP BY FISCAL_YEAR",
        }],
    }
    # Raw result as Snowflake returns it: FISCAL_YEAR is an int, a quarter label
    # comes back in the SQL's (already-ordered) row order.
    raw = {
        "columns": ["FISCAL_YEAR", "REVENUE"],
        "rows": [[2023, 100], [2024, 200], [2025, 150]],
        "row_count": 3,
        "sql": "SELECT ...",
    }
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"DASHBOARD_PLAN": plan})
    monkeypatch.setattr(snowflake_client, "run_query", lambda sql, max_rows=None: raw)
    monkeypatch.setattr(snowflake_client, "touch_saved_dashboard_run",
                        lambda rid: (1, "2026-01-01T00:00:00"))

    res = dashboards.run_dashboard("rid")
    panel = res["panels"][0]

    # Columns and rows are byte-identical to run_query output — no reshaping.
    assert panel["columns"] == raw["columns"]
    assert panel["rows"] == raw["rows"]
    assert panel["row_count"] == 3
    # Specifically: a year is NOT stringified (the old shaping would have made
    # it "2023"); it stays the int Snowflake returned.
    assert panel["rows"][0][0] == 2023 and isinstance(panel["rows"][0][0], int)
    # Row order is preserved exactly (no server-side re-sort).
    assert [r[0] for r in panel["rows"]] == [2023, 2024, 2025]


def test_run_dashboard_preserves_sql_row_order_for_quarter_labels(monkeypatch):
    # The SQL is responsible for chronological order (ORDER BY companion date);
    # run_dashboard must not reorder or reshape the labels.
    plan = {"title": "T", "panels": [{
        "title": "Expenses by Quarter", "chart_type": "line",
        "x": "QUARTER_LABEL", "y": "EXP", "color": None,
        "orientation": None, "display": "both", "sql": "SELECT ...",
    }]}
    raw = {
        "columns": ["QUARTER_LABEL", "EXP"],
        "rows": [["Q3 2022", 10], ["Q4 2022", 12], ["Q1 2023", 9]],
        "row_count": 3, "sql": "SELECT ...",
    }
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"DASHBOARD_PLAN": plan})
    monkeypatch.setattr(snowflake_client, "run_query", lambda sql, max_rows=None: raw)
    monkeypatch.setattr(snowflake_client, "touch_saved_dashboard_run",
                        lambda rid: (1, "2026-01-01T00:00:00"))

    res = dashboards.run_dashboard("rid")
    assert [r[0] for r in res["panels"][0]["rows"]] == ["Q3 2022", "Q4 2022", "Q1 2023"]

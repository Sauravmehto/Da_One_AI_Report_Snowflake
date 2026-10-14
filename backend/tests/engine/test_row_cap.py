"""The warehouse's row cap stops being invisible.

Every query is capped at snowflake_client.MAX_ROWS and the rest are dropped
without a word. Chart SQL is ordered oldest-first, so what a cap removes is the
RECENT end of the series — the part a reader opened the dashboard for — and a
capped panel is indistinguishable from a genuinely short one.

Two halves, both tested here: a run now says when a panel hit the cap, and the
planner is told the cap exists so it stops writing panels that need more rows
than it can ever get.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards, file_dashboards, llm
from app.engine.connectors.duckdb import client as duckdb_client
from app.engine.connectors.snowflake import client as snowflake_client

PLAN = {
    "title": "T",
    "panels": [{
        "title": "Weekly trend", "sql": "SELECT WEEK, AMT FROM t ORDER BY WEEK",
        "chart_type": "line", "x": "WEEK", "y": "AMT", "color": None,
        "size": None, "orientation": None, "display": "chart",
    }],
}


def _run(monkeypatch, row_count: int, caps: list | None = None):
    rows = [[f"w{i}", i] for i in range(row_count)]
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"DASHBOARD_PLAN": PLAN})
    monkeypatch.setattr(snowflake_client, "touch_saved_dashboard_run",
                        lambda rid: (1, "2026-01-01T00:00:00"))

    def _query(sql, max_rows=snowflake_client.MAX_ROWS):
        if caps is not None:
            caps.append(max_rows)
        return {"columns": ["WEEK", "AMT"], "rows": rows,
                "row_count": len(rows), "sql": sql}

    monkeypatch.setattr(snowflake_client, "run_query", _query)
    return dashboards.run_dashboard("rid", None)["panels"][0]


def test_a_panel_that_fills_the_cap_is_flagged(monkeypatch):
    """Exactly the cap back means the warehouse stopped counting, not that the
    data happened to end on a round number."""
    panel = _run(monkeypatch, snowflake_client.DASHBOARD_MAX_ROWS)
    assert panel["truncated"] is True


def test_a_panel_under_the_cap_is_not_flagged(monkeypatch):
    panel = _run(monkeypatch, snowflake_client.DASHBOARD_MAX_ROWS - 1)
    assert panel["truncated"] is False


def test_a_dashboard_panel_asks_for_the_dashboard_cap(monkeypatch):
    """Not the chat cap: a panel of 101 weeks lost its latest week."""
    caps: list = []
    _run(monkeypatch, 3, caps)
    assert caps == [snowflake_client.DASHBOARD_MAX_ROWS]
    assert snowflake_client.DASHBOARD_MAX_ROWS > snowflake_client.MAX_ROWS


def test_a_panel_past_the_chat_cap_is_complete(monkeypatch):
    panel = _run(monkeypatch, snowflake_client.MAX_ROWS + 1)
    assert panel["truncated"] is False
    assert len(panel["rows"]) == snowflake_client.MAX_ROWS + 1


def test_an_empty_panel_is_not_flagged(monkeypatch):
    assert _run(monkeypatch, 0)["truncated"] is False


def test_the_flag_never_changes_the_data(monkeypatch):
    """The cap is reported beside the rows, never by trimming or padding them."""
    panel = _run(monkeypatch, snowflake_client.DASHBOARD_MAX_ROWS)
    assert len(panel["rows"]) == snowflake_client.DASHBOARD_MAX_ROWS
    assert panel["rows"][0] == ["w0", 0]
    assert panel["columns"] == ["WEEK", "AMT"]


def test_a_failed_panel_reports_no_truncation(monkeypatch):
    """It returned nothing at all; saying "truncated" would send a reader
    looking for rows that were never fetched."""
    monkeypatch.setattr(snowflake_client, "read_saved_dashboard",
                        lambda rid: {"DASHBOARD_PLAN": PLAN})
    monkeypatch.setattr(snowflake_client, "touch_saved_dashboard_run",
                        lambda rid: (1, "2026-01-01T00:00:00"))

    def _boom(sql, max_rows=None):
        raise RuntimeError("warehouse said no")

    monkeypatch.setattr(snowflake_client, "run_query", _boom)
    panel = dashboards.run_dashboard("rid", None)["panels"][0]
    assert panel["error"]
    assert panel["truncated"] is False


# --- the planner is told, so it stops needing the cap ------------------------

def test_each_planner_is_told_the_cap_of_the_engine_THAT_RUNS_IT():
    """Not the same number twice.

    Warehouse dashboards run on Snowflake (100 rows); file dashboards run on
    DuckDB, whose cap is 50x larger and environment-tunable. Quoting the
    warehouse's 100 at the file planner would shrink every spreadsheet
    dashboard to a fiftieth of what it is allowed to show.
    """
    pairs = ((llm._DASHBOARD_RULES, snowflake_client.MAX_ROWS),
             (llm._FILE_DASHBOARD_RULES, duckdb_client.MAX_ROWS))
    for rules, cap in pairs:
        assert f"AT MOST {cap} ROWS" in rules
        # The reason matters as much as the number: without it the model trims
        # the wrong end of the series.
        assert "most RECENT" in rules
    assert snowflake_client.MAX_ROWS != duckdb_client.MAX_ROWS


def test_the_file_planner_is_not_handed_the_warehouse_cap():
    """The mistake this guards against, stated directly."""
    assert f"AT MOST {snowflake_client.MAX_ROWS} ROWS" not in llm._FILE_DASHBOARD_RULES


def test_a_file_panel_that_fills_its_cap_is_flagged(monkeypatch):
    """Same promise as the warehouse path, on the connector's own cap."""
    rows = [[f"w{i}", i] for i in range(duckdb_client.MAX_ROWS)]

    class _Connector:
        MAX_ROWS = duckdb_client.MAX_ROWS

        def run_query(self, sql):
            return {"columns": ["WEEK", "AMT"], "rows": rows, "row_count": len(rows)}

    monkeypatch.setattr(file_dashboards, "read_record",
                        lambda rid: {"dashboard_plan": PLAN, "source_key": "EXCEL:x"})
    monkeypatch.setattr(file_dashboards, "_write_record", lambda rec: None)
    panel = file_dashboards.run_dashboard("rid", _Connector())["panels"][0]
    assert panel["truncated"] is True
    assert len(panel["rows"]) == duckdb_client.MAX_ROWS


def test_a_short_file_panel_is_not_flagged(monkeypatch):
    class _Connector:
        MAX_ROWS = duckdb_client.MAX_ROWS

        def run_query(self, sql):
            return {"columns": ["WEEK", "AMT"], "rows": [["w0", 0]], "row_count": 1}

    monkeypatch.setattr(file_dashboards, "read_record",
                        lambda rid: {"dashboard_plan": PLAN, "source_key": "EXCEL:x"})
    monkeypatch.setattr(file_dashboards, "_write_record", lambda rec: None)
    assert file_dashboards.run_dashboard("rid", _Connector())["panels"][0]["truncated"] is False


def test_a_connector_with_no_cap_never_claims_truncation(monkeypatch):
    """Silence beats a guess: an unknown cap must not be reported as reached."""
    class _Connector:
        def run_query(self, sql):
            return {"columns": ["WEEK", "AMT"], "rows": [["w0", 0]], "row_count": 1}

    monkeypatch.setattr(file_dashboards, "read_record",
                        lambda rid: {"dashboard_plan": PLAN, "source_key": "EXCEL:x"})
    monkeypatch.setattr(file_dashboards, "_write_record", lambda rec: None)
    assert file_dashboards.run_dashboard("rid", _Connector())["panels"][0]["truncated"] is False


def test_the_rule_says_what_to_do_instead_of_just_forbidding():
    rules = llm._DASHBOARD_RULES
    assert "Aggregate" in rules
    assert "LIMIT deliberately" in rules

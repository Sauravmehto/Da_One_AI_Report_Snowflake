"""Regression guard for the connectors/ abstraction layer added to make new
vendors pluggable without touching existing code. Hermetic — Snowflake access
is stubbed via monkeypatch, same pattern as test_run_dashboard_passthrough.py.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine.connectors.snowflake import client as snowflake_client
from app.engine.connectors import registry
from app.engine.connectors.base import DBConnector
import app.engine.connectors.snowflake as sf_connector_pkg


def test_snowflake_connector_registers_itself_on_import():
    conn = registry.get("snowflake")
    assert conn.dialect == "snowflake"
    assert isinstance(conn, DBConnector)  # satisfies the Protocol structurally


def test_unregistered_connector_raises_with_available_names():
    try:
        registry.get("sqlserver_not_registered_yet")
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "snowflake" in str(exc)


def test_registering_the_same_name_twice_raises():
    try:
        registry.register("snowflake", sf_connector_pkg.connector)
        assert False, "expected ValueError on duplicate registration"
    except ValueError:
        pass


def test_run_query_delegates_to_snowflake_client_unchanged(monkeypatch):
    raw = {"columns": ["X"], "rows": [[1]], "row_count": 1, "sql": "SELECT 1 AS X"}
    monkeypatch.setattr(snowflake_client, "run_query", lambda sql: raw)
    conn = registry.get("snowflake")
    assert conn.run_query("SELECT 1 AS X") is raw


def test_dry_run_query_delegates_to_snowflake_client_unchanged(monkeypatch):
    monkeypatch.setattr(snowflake_client, "dry_run_query", lambda sql: ["X"])
    conn = registry.get("snowflake")
    assert conn.dry_run_query("SELECT 1 AS X") == ["X"]


def test_session_identity_delegates_to_snowflake_client_unchanged(monkeypatch):
    monkeypatch.setattr(snowflake_client, "session_identity", lambda: ("BOB", "ANALYST_ROLE"))
    conn = registry.get("snowflake")
    assert conn.session_identity() == ("BOB", "ANALYST_ROLE")

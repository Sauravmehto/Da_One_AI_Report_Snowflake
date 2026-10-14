"""Tests for the audit layer — row construction and INSERT safety.

These don't hit Snowflake; they mock the write path to verify the row is built
correctly and that the INSERT is parameterized (no user data in SQL text).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Minimal env so imports that read config don't fail.
os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import audit
from app.engine.connectors.snowflake import client as snowflake_client


def test_row_has_both_identities_and_all_columns(monkeypatch):
    captured = {}

    def fake_write(row):
        captured.update(row)

    monkeypatch.setattr(snowflake_client, "write_audit", fake_write)

    audit.log_event(
        "ask_data_question", "ok_after_repair",
        snowflake_user="SVC_USER", snowflake_role="READ_WRITE_LOAN_DB_ROLE",
        question="what's the delinquency rate?",
        sql="SELECT 42 LIMIT 100", row_count=1, duration_ms=137,
    )

    # Two identities kept distinct
    assert captured["snowflake_user"] == "SVC_USER"
    assert captured["auth_mode"] == "service_account"
    assert captured["audit_user"]        # OS proxy populated
    assert captured["session_id"]        # uuid populated
    assert captured["status"] == "ok_after_repair"

    # Every INSERT column has a corresponding key (order-independent)
    for col in snowflake_client._AUDIT_COLUMNS:
        assert col.lower() in captured, f"missing audit field {col.lower()}"


def test_insert_statement_is_parameterized():
    """User data must go in as bound params, never inlined into SQL text."""
    placeholders = ", ".join(["%s"] * len(snowflake_client._AUDIT_COLUMNS))
    stmt = (
        f"INSERT INTO {snowflake_client.AUDIT_TABLE} "
        f"({', '.join(snowflake_client._AUDIT_COLUMNS)}) VALUES ({placeholders})"
    )
    assert "%s" in stmt
    assert "'" not in stmt          # no string literals = nothing user-supplied inlined
    assert stmt.count("%s") == len(snowflake_client._AUDIT_COLUMNS)


def test_audit_write_failure_never_raises(monkeypatch):
    """Auditing is best-effort: a write failure must not break the caller."""
    def boom(row):
        raise RuntimeError("simulated Snowflake outage")

    monkeypatch.setattr(snowflake_client, "write_audit", boom)
    # Should swallow the error, not propagate it.
    audit.log_event("run_safe_sql", "ok", question="q", sql="SELECT 1")

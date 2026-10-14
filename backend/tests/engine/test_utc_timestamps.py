"""Dashboard and Excel-source times are written in UTC.

They were written with CURRENT_TIMESTAMP() -- the Snowflake SESSION's wall
clock -- into TIMESTAMP_NTZ columns, which keep no zone. The same moment was
stored as a different time depending on the session that saved it, and the app
could only guess. SYSDATE() is UTC regardless of session.

The Excel-report tables are deliberately out of scope: their column types are
defined outside this repo, and writing a UTC wall clock into a zone-aware column
would move the time rather than fix it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine.connectors.snowflake import client

APP = Path(__file__).resolve().parents[2] / "app"


def _code_only(path: Path) -> str:
    """The file without comments, so a comment explaining the change does not
    count as a use of the thing it warns about."""
    return "\n".join(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("relative", [
    "engine/connectors/snowflake/client.py",
    "engine/excel_sources.py",
])
def test_no_session_local_time_is_written(relative):
    assert "CURRENT_TIMESTAMP()" not in _code_only(APP / relative)


def _split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return parts


def _columns_and_expressions(sql: str) -> tuple[list[str], list[str]]:
    columns = [c.strip() for c in sql[sql.index("(") + 1: sql.index(")")].split(",")]
    expressions = _split_top_level(sql.split("SELECT", 1)[1])
    return columns, expressions


ROW = {
    "report_id": "r1", "version_id": "r1_v2", "version": 2, "report_title": "T",
    "description": None, "tags": [], "dashboard_plan": {"panels": []},
    "created_by": "U", "edit_note": "note", "database": "ECOMMERCE_DB",
}


@pytest.mark.parametrize("has_group_columns", [True, False], ids=["migrated", "legacy"])
def test_the_version_insert_sets_created_at_in_utc_and_still_lines_up(monkeypatch, has_group_columns):
    """The risk in adding a column to an INSERT is binding every later value to
    the wrong column. Both table shapes are checked: columns match expressions,
    placeholders match bound values, and CREATED_AT sits against SYSDATE()."""
    monkeypatch.setattr(client, "_has_group_columns", lambda: has_group_columns)
    statement, values = client._dashboard_insert_statement(ROW)

    columns, expressions = _columns_and_expressions(statement)
    assert len(columns) == len(expressions)
    assert statement.count("%s") == len(values)
    assert expressions[columns.index("CREATED_AT")] == "SYSDATE()"
    # The values after CREATED_AT still land where they did.
    assert expressions[columns.index("EDIT_NOTE")] == "%s"
    assert expressions[columns.index("RUN_COUNT")] == "0"


def test_a_run_stamps_last_run_at_in_utc():
    source = _code_only(APP / "engine/connectors/snowflake/client.py")
    assert re.search(r"LAST_RUN_AT = SYSDATE\(\)", source)

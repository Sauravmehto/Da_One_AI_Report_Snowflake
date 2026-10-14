"""Tests for the SQL safety layer — the read-only guarantee.

Includes the two regressions found in the original regex validator:
 - SELECT ... INTO (a write on SQL Server) must now be BLOCKED
 - a table legitimately named MERGE must NOT be false-positive blocked
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.engine.sql_safety import validate_sql, enforce_row_limit, UnsafeSQLError

ALLOWED = [
    "SELECT * FROM loans",
    "SELECT loan_id, balance FROM loans WHERE status = 'ACTIVE'",
    "WITH active AS (SELECT * FROM loans WHERE status='ACTIVE') SELECT count(*) FROM active",
    "SELECT sum(balance) FROM loans GROUP BY servicer HAVING sum(balance) > 1000",
    "SELECT * FROM MERGE",           # regression: table named MERGE is legit
    "SELECT * FROM orders WHERE region = 'US'",
    # UNION / UNION ALL of clean SELECTs — the KPI-panel pattern (one metric row
    # per branch). A 3-way UNION ALL must validate.
    "SELECT 'A' AS METRIC, 1 AS VALUE UNION ALL SELECT 'B', 2 UNION ALL SELECT 'C', 3",
    "SELECT a FROM t1 UNION SELECT b FROM t2",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "WITH w AS (SELECT 1 AS n) SELECT n FROM w UNION ALL SELECT 2",
    # Year/quarter SQL patterns the dashboard rules now generate (must be allowed):
    "SELECT TO_VARCHAR(FISCAL_YEAR) AS FISCAL_YEAR, SUM(REVENUE) AS REV FROM pcbf GROUP BY FISCAL_YEAR ORDER BY FISCAL_YEAR",
    "SELECT TO_CHAR(MARK_DATE, 'YYYY') AS YR, AVG(FAIR_VALUE) AS FV FROM pcm GROUP BY TO_CHAR(MARK_DATE, 'YYYY') ORDER BY YR",
    "SELECT QUARTER_LABEL, SUM(TOTAL_FUND_EXPENSES) AS EXP FROM pcfe GROUP BY QUARTER_LABEL, QUARTER_END_DATE ORDER BY QUARTER_END_DATE",
]

REJECTED = [
    "DELETE FROM loans",
    "UPDATE loans SET balance = 0",
    "INSERT INTO loans VALUES (1)",
    "DROP TABLE loans",
    "TRUNCATE TABLE loans",
    "ALTER TABLE loans ADD COLUMN x INT",
    "CREATE TABLE evil (id INT)",
    "MERGE INTO loans USING staging ON loans.id=staging.id WHEN MATCHED THEN DELETE",
    "GRANT SELECT ON loans TO ROLE public",
    "USE DATABASE production",
    "SELECT 1; DROP TABLE loans",                    # stacked
    "SELECT 1 /* x */; DROP TABLE loans",            # comment-smuggled stacked
    "SELECT * INTO backup FROM loans",               # regression: T-SQL write
    # UNION branches must each be a clean read-only SELECT:
    "SELECT 1 AS n UNION ALL WITH d AS (DELETE FROM loans RETURNING id) SELECT id FROM d",  # forbidden node in a branch
    "SELECT 1 AS x UNION ALL SELECT 2 AS x INTO backup",   # SELECT ... INTO write in a branch
    "SELECT 1 UNION ALL (SELECT 2 INTERSECT SELECT 3)",    # branch is not a plain SELECT
    "SELECT 1 INTERSECT SELECT 2",                          # INTERSECT is not an allowed top-level op
    "",
    "   ",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert validate_sql(sql)  # returns cleaned SQL, no raise


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql):
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql)


def test_missing_limit_injected():
    out = enforce_row_limit("SELECT * FROM loans", 100)
    assert "LIMIT" in out.upper() and "100" in out


def test_excessive_limit_clamped():
    out = enforce_row_limit("SELECT * FROM loans LIMIT 999999", 100)
    assert "100" in out and "999999" not in out


def test_reasonable_limit_preserved():
    out = enforce_row_limit("SELECT * FROM loans LIMIT 50", 100)
    assert "50" in out


def test_a_parse_error_carries_no_terminal_colour_codes():
    """sqlglot underlines the failing token with ANSI escapes, which reached the
    chat as '[4m' and '[0m' around the part a reader needed."""
    from app.engine.sql_safety import UnsafeSQLError, validate_sql

    with pytest.raises(UnsafeSQLError) as err:
        validate_sql("SELECT * FROM T WHERE (''All'' = 'All' OR X = ''All'')")

    message = str(err.value)
    assert "Could not parse SQL" in message
    assert "\x1b" not in message

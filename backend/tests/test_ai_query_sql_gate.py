"""The legacy /chat path must use the AST safety gate, not the old regex one.

Both gates existed for a while and this path imported the wrong one. The regex
gate had two proven failure modes, and the first is the dangerous one:

  - it ALLOWED `SELECT ... INTO t FROM ...`, which is harmless on Snowflake but
    a table-creating WRITE on SQL Server;
  - it BLOCKED legitimate queries whose text merely contained a keyword, e.g. a
    table named MERGE or the word "copy" inside a string literal.

Asserting on the module's imported symbol rather than re-testing sqlglot: the
regression to prevent is this path silently reverting to the weaker gate.
"""

import pytest

from app.engine.sql_safety import UnsafeSQLError, validate_sql
from app.services import ai_query


def test_ai_query_uses_the_ast_gate():
    assert ai_query.validate_sql is validate_sql
    assert ai_query.UnsafeSQLError is UnsafeSQLError


def test_select_into_is_rejected():
    """A write on SQL Server. The old gate let this through."""
    with pytest.raises(UnsafeSQLError):
        ai_query.validate_sql("SELECT * INTO staging FROM PRIVATE_CREDIT")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM MERGE",
        "SELECT SECTOR FROM PRIVATE_CREDIT WHERE NOTES LIKE '%copy%'",
    ],
)
def test_keyword_lookalikes_are_allowed(sql):
    """Identifiers and literals that merely contain a keyword are legitimate;
    blocking them made the assistant look broken on valid questions."""
    assert ai_query.validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE T",
        "WITH x AS (DELETE FROM T RETURNING *) SELECT * FROM x",
        "DROP TABLE PRIVATE_CREDIT",
    ],
)
def test_writes_and_stacked_statements_still_blocked(sql):
    with pytest.raises(UnsafeSQLError):
        ai_query.validate_sql(sql)


def test_the_old_regex_module_is_gone():
    """It was superseded, not kept as a fallback — leaving it importable is how
    a future edit quietly reintroduces the weaker gate."""
    with pytest.raises(ImportError):
        __import__("app.services.sql_safety")

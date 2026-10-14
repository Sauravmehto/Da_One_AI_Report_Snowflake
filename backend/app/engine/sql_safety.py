"""Safety gate for SQL that reaches the database.

Claude writes the SQL; this module decides whether it is allowed to run.

Why AST parsing instead of keyword matching: the previous regex approach
allow-listed by first word and denied a fixed keyword list. That has two
failure modes proven in testing:
  1. It ALLOWED `SELECT ... INTO newtable FROM ...` — harmless on Snowflake,
     but a table-creating WRITE on SQL Server (SSMS), which is on the roadmap.
     `INTO` was never in the keyword denylist.
  2. It FALSE-POSITIVE blocked legitimate queries, e.g. a table literally named
     MERGE, because the keyword appeared as an identifier.

sqlglot parses the query into a syntax tree and we inspect the actual structure:
allow-list by parsed statement type, reject any write/DDL node anywhere in the
tree (including nested in CTEs), and block SELECT-INTO explicitly. This is
dialect-aware, so when SSMS is added we validate T-SQL with `dialect="tsql"`.

Public interface (unchanged, so snowflake_client.py is untouched):
    validate_sql(sql, dialect="snowflake") -> str      # cleaned SQL, or raises
    enforce_row_limit(sql, limit, dialect=...) -> str  # inject/clamp LIMIT
    UnsafeSQLError
"""

import re

import sqlglot
from sqlglot import exp

_DIALECT = "snowflake"

# Terminal colour codes. sqlglot underlines the failing token in its parse
# errors with them, and they reached the chat as "[4m" and "[0m" around
# the text a reader needed to see.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Only these parsed top-level statement types may execute. WITH ... SELECT
# parses as a Select carrying a `with` clause, so "Select" covers CTEs too.
# "Union" covers UNION / UNION ALL (a read-only set operation over SELECTs) —
# required by e.g. KPI panels that stack one metric row per branch. Its branches
# are validated individually below. INTERSECT/EXCEPT parse as distinct top types
# ("Intersect"/"Except"), not "Union", so they remain disallowed.
_ALLOWED_STATEMENTS = {"Select", "Union"}

# Node types that must never appear anywhere in the tree — even nested inside
# an otherwise-valid SELECT (e.g. a data-modifying CTE). Allow-list on the
# statement type, deny-list as defense-in-depth on the whole tree.
_FORBIDDEN_NODES = {
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter",
    "TruncateTable", "Command", "Use", "Grant", "Set",
}


class UnsafeSQLError(ValueError):
    """Raised when SQL is not a single, read-only SELECT statement."""


def _union_branch_selects(node) -> list:
    """Return the operand expressions of a UNION/UNION ALL tree.

    A multi-way union nests left-associatively —
    Union(this=Union(this=Select, expression=Select), expression=Select) — so we
    recurse into nested Unions. A parenthesized branch parses as a Subquery/Paren
    wrapping its inner query, so we unwrap those. Anything that is NOT a plain
    SELECT after unwrapping (e.g. a nested INTERSECT/EXCEPT) is returned as-is so
    the caller can reject it.
    """
    branches = []
    for child in (node.this, node.expression):
        if child is None:
            continue
        while type(child).__name__ in ("Subquery", "Paren") and child.this is not None:
            child = child.this
        if type(child).__name__ == "Union":
            branches.extend(_union_branch_selects(child))
        else:
            branches.append(child)
    return branches


def validate_sql(sql: str, dialect: str = _DIALECT) -> str:
    """Return the cleaned SQL if it is a single read-only SELECT, else raise."""
    cleaned = (sql or "").strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeSQLError("Empty SQL statement.")

    try:
        statements = [s for s in sqlglot.parse(cleaned, dialect=dialect) if s is not None]
    except Exception as exc:  # sqlglot ParseError and subclasses
        raise UnsafeSQLError(f"Could not parse SQL: {_ANSI_RE.sub('', str(exc))}") from exc

    if not statements:
        raise UnsafeSQLError("No executable statement found.")
    if len(statements) > 1:
        raise UnsafeSQLError("Only a single SQL statement is allowed.")

    stmt = statements[0]

    top = type(stmt).__name__
    if top not in _ALLOWED_STATEMENTS:
        raise UnsafeSQLError(
            f"Only read-only SELECT queries are allowed. Got a {top} statement."
        )

    # Defense-in-depth: reject any write/DDL node anywhere in the WHOLE tree.
    # stmt.walk() descends into every UNION branch and nested subquery, so this
    # single walk covers all branches, not just the top-level node.
    for node in stmt.walk():
        node_type = type(node).__name__
        if node_type in _FORBIDDEN_NODES:
            raise UnsafeSQLError(
                f"Query contains a forbidden operation ({node_type}). "
                "This connector is strictly read-only."
            )

    # Determine the SELECT(s) that must each be a plain, read-only SELECT. For a
    # UNION, every branch must itself be a plain SELECT (not another set op).
    if top == "Union":
        branch_selects = _union_branch_selects(stmt)
        for leaf in branch_selects:
            if type(leaf).__name__ != "Select":
                raise UnsafeSQLError(
                    f"Every branch of a UNION must be a plain read-only SELECT; "
                    f"got a {type(leaf).__name__}."
                )
    else:
        branch_selects = [stmt]

    # SELECT ... INTO writes a table (T-SQL). sqlglot models the target as an
    # `into` arg on the Select. Block it on the statement and on every UNION
    # branch — this is the gap the old keyword validator missed.
    for sel in branch_selects:
        if sel.args.get("into") is not None:
            raise UnsafeSQLError("SELECT ... INTO is not permitted; it writes data.")

    return cleaned


def enforce_row_limit(sql: str, limit: int, dialect: str = _DIALECT) -> str:
    """Inject a LIMIT when absent, or clamp one that exceeds the cap.

    Uses the parse tree rather than a regex so we can both add and clamp. Falls
    back to the original SQL if anything about the limit can't be reasoned about
    statically (the connection-layer row cap is the backstop either way).
    """
    limit = int(limit)
    try:
        stmt = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return sql  # connection-layer cap still applies

    existing = stmt.args.get("limit")
    if existing is None:
        return stmt.limit(limit).sql(dialect=dialect)

    try:
        requested = int(existing.expression.name)
        if requested > limit:
            return stmt.limit(limit).sql(dialect=dialect)
        return sql
    except (AttributeError, ValueError):
        # Non-literal limit (param/expression) — clamp to be safe.
        return stmt.limit(limit).sql(dialect=dialect)

"""Vendor-agnostic data-source interface.

Every connector (Snowflake today; SQL Server, Oracle, etc. later) implements
this Protocol so core modules (dashboards.py, sql_safety.py, server.py, ...)
never import a vendor-specific module directly — they ask
connectors.registry.get(name) for one instead.

Adding a new vendor should NEVER require editing this file. Implement
DBConnector in your own self-contained connectors/<vendor>/ package and
register it with connectors.registry on import — see connectors/snowflake/
for the reference implementation, which adapts the existing
snowflake_client.py rather than duplicating it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DBConnector(Protocol):
    # The sqlglot dialect name this connector's SQL is validated against:
    # sql_safety.validate_sql(sql, dialect=connector.dialect).
    dialect: str

    def run_query(self, sql: str) -> dict[str, Any]:
        """Validate, cap, and execute a read-only query.

        Returns {columns, rows, row_count, sql} — the same shape every panel
        renderer and the webapp already consume today.
        """
        ...

    def dry_run_query(self, sql: str) -> list[str]:
        """Compile a query with zero rows fetched, to prove it resolves
        (columns/joins are valid) without paying for real execution.

        Returns the result column names.
        """
        ...

    def session_identity(self) -> tuple[str | None, str | None]:
        """Return (user, role) for the live session, for the audit trail."""
        ...

"""DuckDB connector — uploaded spreadsheets, queried as SQL.

client.py holds the implementation; this file adapts it to the DBConnector
interface and registers it, exactly as connectors/snowflake/ does. Nothing in
connectors/base.py, connectors/registry.py or the Snowflake package changed to
add this one, which was the point of that structure existing.
"""

from __future__ import annotations

from typing import Any

from app.engine.connectors import registry
from app.engine.connectors.duckdb import client


class DuckDBConnector:
    dialect = "duckdb"

    def run_query(self, sql: str) -> dict[str, Any]:
        return client.run_query(sql)

    def dry_run_query(self, sql: str) -> list[str]:
        return client.dry_run_query(sql)

    def session_identity(self) -> tuple[str | None, str | None]:
        return client.session_identity()

    # Bound per request, like the Snowflake connector — except what is bound is
    # a path to an uploaded source rather than a warehouse session:
    #   with connector.use_connection(path, identity=(user, role)):
    #       ...engine calls read that spreadsheet...
    use_connection = staticmethod(client.use_connection)


connector = DuckDBConnector()
registry.register("duckdb", connector)

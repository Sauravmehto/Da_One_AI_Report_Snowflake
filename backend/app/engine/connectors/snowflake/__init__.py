"""Snowflake connector.

client.py holds the actual implementation (connection lifecycle, query
execution, audit and dashboard persistence) — this file just adapts it to
the DBConnector interface and registers it.

A future vendor (connectors/sqlserver/, etc.) follows the same shape but owns
its OWN client/auth code inside its own folder — it never touches this
package or core/.
"""

from __future__ import annotations

from typing import Any

from app.engine.connectors import registry
from app.engine.connectors.snowflake import client


class SnowflakeConnector:
    dialect = "snowflake"

    def run_query(self, sql: str) -> dict[str, Any]:
        return client.run_query(sql)

    def dry_run_query(self, sql: str) -> list[str]:
        return client.dry_run_query(sql)

    def session_identity(self) -> tuple[str | None, str | None]:
        return client.session_identity()

    # Re-exported so callers bind a per-request connection through the
    # connector rather than reaching into the vendor module directly:
    #   with connector.use_connection(conn, identity=(user, role)):
    #       ...engine calls run as that user...
    use_connection = staticmethod(client.use_connection)


connector = SnowflakeConnector()
registry.register("snowflake", connector)

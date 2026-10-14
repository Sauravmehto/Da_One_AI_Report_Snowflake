"""Explore service: merges live Snowflake schema with curated TOML metadata.

The contract here is the important part:
  - Table list and column types come from Snowflake's information_schema
    (always current, always accurate — this is the source of truth for what exists).
  - Descriptions, column meanings, and notes come from the metadata configs
    (human knowledge Snowflake doesn't store).
  - Where metadata is missing, raw names/types are returned unchanged.

Preview safety: this service never executes user-supplied SQL. Preview queries
are built from path identifiers that are first validated against a strict
whitelist pattern and then double-quoted, so injection is not possible. (The
old app's keyword-based validate_sql guarded *generated* SQL from the LLM —
that belongs with the upcoming query/chat feature, not here.)
"""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from app.metadata.loader import MetadataService, metadata_service
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")
_PREVIEW_DEFAULT_LIMIT = 50
_PREVIEW_MAX_LIMIT = 200


class InvalidIdentifierError(ValueError):
    """Raised when a database/schema/table name fails whitelist validation."""


class TableNotFoundError(Exception):
    """Raised when a requested table does not exist or isn't accessible."""


def _validate_identifier(value: str, kind: str) -> str:
    if not value or not _IDENTIFIER_RE.match(value):
        raise InvalidIdentifierError(f"Invalid {kind} name: {value!r}")
    return value


def _quote(identifier: str) -> str:
    # identifier is already whitelist-validated; double internal quotes defensively.
    return '"' + identifier.replace('"', '""') + '"'


def _jsonify(value: Any) -> Any:
    """Make a Snowflake cell value safe for JSON serialization."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # Preserve precision; the UI formats display.
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


class ExploreService:
    def __init__(
        self,
        snowflake: SnowflakeService,
        metadata: MetadataService | None = None,
    ) -> None:
        self._sf = snowflake
        self._meta = metadata or metadata_service

    async def list_tables(
        self, session: SessionData, database: str
    ) -> list[dict[str, Any]]:
        db = _validate_identifier(database, "database")

        sql = (
            "SELECT table_schema, table_name, table_type, row_count "
            f"FROM {_quote(db)}.information_schema.tables "
            "WHERE table_schema <> 'INFORMATION_SCHEMA' "
            "ORDER BY table_schema, table_name"
        )
        rows = await self._sf.execute_query_dict(session, sql)

        tables: list[dict[str, Any]] = []
        for row in rows:
            schema = str(row["TABLE_SCHEMA"])
            name = str(row["TABLE_NAME"])
            meta = self._meta.get_table_meta(db, schema, name)
            row_count = row.get("ROW_COUNT")
            tables.append(
                {
                    "schema": schema,
                    "name": name,
                    "full_name": f"{db}.{schema}.{name}",
                    "table_type": str(row.get("TABLE_TYPE") or ""),
                    "row_count": int(row_count) if row_count is not None else None,
                    "description": str(meta.get("description", "")) if meta else "",
                    "is_documented": meta is not None,
                }
            )
        return tables

    async def schema_map(
        self, session: SessionData, database: str
    ) -> dict[str, dict[str, str]]:
        """Every column in the database as {FULL_TABLE: {COLUMN: DATA_TYPE}}.

        One query rather than one per table: the semantic layer editor
        validates a whole document at once, and a per-table round trip would
        make publishing a wide database take seconds.

        Uppercased throughout because it is compared against names typed by a
        human into the editor.
        """
        db = _validate_identifier(database, "database")

        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            f"FROM {_quote(db)}.information_schema.columns "
            "WHERE table_schema <> 'INFORMATION_SCHEMA'"
        )
        rows = await self._sf.execute_query_dict(session, sql)

        schema: dict[str, dict[str, str]] = {}
        for row in rows:
            full_name = (
                f"{db}.{row['TABLE_SCHEMA']}.{row['TABLE_NAME']}"
            ).upper()
            schema.setdefault(full_name, {})[str(row["COLUMN_NAME"]).upper()] = str(
                row["DATA_TYPE"]
            )
        return schema

    async def get_table(
        self, session: SessionData, database: str, schema: str, table: str
    ) -> dict[str, Any]:
        db = _validate_identifier(database, "database")
        sc = _validate_identifier(schema, "schema")
        tb = _validate_identifier(table, "table")

        sql = (
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            f"FROM {_quote(db)}.information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position"
        )
        rows = await self._sf.execute_query_dict(session, sql, (sc, tb))
        if not rows:
            raise TableNotFoundError(f"{db}.{sc}.{tb}")

        col_meta = self._meta.get_column_meta(db, sc, tb)
        table_meta = self._meta.get_table_meta(db, sc, tb)

        columns: list[dict[str, Any]] = []
        for row in rows:
            cname = str(row["COLUMN_NAME"])
            columns.append(
                {
                    "name": cname,
                    "data_type": str(row["DATA_TYPE"]),
                    "nullable": str(row.get("IS_NULLABLE", "")).upper() == "YES",
                    "description": col_meta.get(cname.upper(), ""),
                }
            )

        return {
            "database": db,
            "schema": sc,
            "name": tb,
            "full_name": f"{db}.{sc}.{tb}",
            "description": str(table_meta.get("description", "")) if table_meta else "",
            "notes": str(table_meta.get("notes", "")) if table_meta else "",
            "is_documented": table_meta is not None,
            "columns": columns,
        }

    async def preview(
        self,
        session: SessionData,
        database: str,
        schema: str,
        table: str,
        limit: int = _PREVIEW_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        db = _validate_identifier(database, "database")
        sc = _validate_identifier(schema, "schema")
        tb = _validate_identifier(table, "table")
        safe_limit = max(1, min(int(limit), _PREVIEW_MAX_LIMIT))

        fqtn = f"{_quote(db)}.{_quote(sc)}.{_quote(tb)}"
        sql = f"SELECT * FROM {fqtn} LIMIT {safe_limit}"

        try:
            columns, rows = await self._sf.execute_query_table(session, sql)
        except Exception as exc:  # noqa: BLE001 - identifiers are pre-validated
            raise TableNotFoundError(f"{db}.{sc}.{tb}") from exc

        return {
            "columns": columns,
            "rows": [[_jsonify(cell) for cell in row] for row in rows],
            "row_limit": safe_limit,
        }

    async def build_schema_context(
        self, session: SessionData, database: str
    ) -> str:
        """Render the live schema (every table + columns) as a text block.

        This is the authoritative, always-current half of the context handed to
        the SQL-generating model. Ported from the old `build_schema_context`,
        scoped to a single database.
        """
        db = _validate_identifier(database, "database")
        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            f"FROM {_quote(db)}.information_schema.columns "
            "WHERE table_schema <> 'INFORMATION_SCHEMA' "
            "ORDER BY table_schema, table_name, ordinal_position"
        )
        rows = await self._sf.execute_query_dict(session, sql)

        grouped: dict[tuple[str, str], list[str]] = {}
        order: list[tuple[str, str]] = []
        for row in rows:
            key = (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(f"{row['COLUMN_NAME']} ({row['DATA_TYPE']})")

        lines = [
            f"Table {db}.{schema}.{table}: " + ", ".join(grouped[(schema, table)])
            for (schema, table) in order
        ]
        return "\n".join(lines)

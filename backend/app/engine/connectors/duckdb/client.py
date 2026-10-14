"""DuckDB connection lifecycle for uploaded spreadsheets.

Mirrors connectors/snowflake/client.py in shape — same `use_connection`
context manager, same `run_query` result contract — so dashboards.py, the agent
tools and the panel renderers cannot tell which vendor answered them.

WHY A LOCAL CACHE
-----------------
A .duckdb file lives in shared storage (local disk today, S3 when configured),
because production runs four uvicorn workers and anything held on one worker's
filesystem is invisible to the other three. But DuckDB queries a PATH, not a
byte stream, so each worker pulls the file down once and keeps it under a cache
directory. Opened READ ONLY, which is what makes concurrent access across
processes safe — several readers are fine, a writer is not.

The cache is keyed by source id and validated against the stored size, so a
re-materialized source (next month's file) replaces a stale copy rather than
answering from last month's numbers.

READ-ONLY, LIKE EVERYTHING ELSE
-------------------------------
Queries go through sql_safety.validate_sql with dialect="duckdb". Ingestion
writes these files, and it does so through its own code before the connector
ever opens them — no SQL a model wrote has ever reached this database.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

DIALECT = "duckdb"

#: Same cap the Snowflake connector applies, for the same reason: a model
#: writing SELECT * against a 200k-row sheet should not stream all of it.
MAX_ROWS = int(os.environ.get("MCP_MAX_ROWS", "5000"))

#: Where pulled-down database files live on this worker.
CACHE_DIR = Path(
    os.environ.get(
        "EXCEL_DUCKDB_CACHE_DIR",
        str(Path(__file__).resolve().parents[4] / "excel_storage" / "duckdb_cache"),
    )
)

# ContextVars, not thread-locals, for the same reason the Snowflake client uses
# them: the agent runs its tools through asyncio.to_thread, which copies the
# calling context into a pooled worker thread but shares no thread-local state.
# A thread-local binding made in the request handler is simply invisible by the
# time a tool executes, and every query fails with "nothing bound".
_bound_database: ContextVar[Path | None] = ContextVar("duckdb_database", default=None)
_bound_identity: ContextVar[tuple | None] = ContextVar("duckdb_identity", default=None)
#: A whole collection rather than one file: the databases to attach and the
#: views that name their tables. Attachments are connection state, so this is
#: rebuilt per connection rather than stored anywhere.
_bound_collection: ContextVar[tuple | None] = ContextVar(
    "duckdb_collection", default=None
)
#: The collection's document database, for the search tool. Searched directly,
#: because its full-text index is a schema inside that file.
_bound_documents: ContextVar[Path | None] = ContextVar(
    "duckdb_documents", default=None
)

_cache_lock = threading.Lock()


class NoDatabaseBound(RuntimeError):
    """Raised when a query is attempted with no source bound to this request."""


@contextmanager
def use_connection(database_path: str | Path, identity: tuple | None = None):
    """Bind one uploaded source for the duration of this request.

    `identity` is the signed-in Snowflake user, carried only so the audit trail
    records who asked. A DuckDB file has no role of its own — which is exactly
    why access to an uploaded source is decided before we get here, by whoever
    may see that source at all.
    """
    database_token = _bound_database.set(Path(database_path))
    identity_token = _bound_identity.set(identity)
    try:
        yield
    finally:
        _bound_database.reset(database_token)
        _bound_identity.reset(identity_token)


@contextmanager
def use_collection(
    sources: list[tuple[str, str]],
    views: list[tuple[str, str, str]],
    documents: Path | str | None = None,
    identity: tuple | None = None,
):
    """Bind every source in a collection for the duration of this request.

    What makes a question that spans a folder's spreadsheets AND its documents
    answerable in one query, rather than one source at a time.
    """
    collection_token = _bound_collection.set((sources, views))
    documents_token = _bound_documents.set(Path(documents) if documents else None)
    identity_token = _bound_identity.set(identity)
    try:
        yield
    finally:
        _bound_collection.reset(collection_token)
        _bound_documents.reset(documents_token)
        _bound_identity.reset(identity_token)


def bound_documents() -> Path | None:
    """The document database bound to this request, if there is one."""
    return _bound_documents.get()


def bound_database() -> Path:
    path = _bound_database.get()
    if path is None:
        raise NoDatabaseBound(
            "No uploaded spreadsheet is bound to this request. "
            "Wrap the call in duckdb client.use_connection(path)."
        )
    return path


def ensure_cached(source_id: str, fetch, expected_size: int | None = None) -> Path:
    """A local path for this source's database, downloading it if needed.

    `fetch` is a zero-argument callable returning the file's bytes, so this
    module never learns whether the bytes came from disk or a bucket.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{source_id}.duckdb"

    with _cache_lock:
        if path.is_file():
            if expected_size is None or path.stat().st_size == expected_size:
                return path
            # Size disagrees: the source was re-materialized. Answering from the
            # stale copy would silently serve last month's numbers.
            log.info("Cached database for %s is stale; refreshing.", source_id)
        data = fetch()
        # Written via a temp name so a crashed download cannot leave a
        # half-written file that later looks like a valid cache hit.
        temporary = path.with_suffix(".partial")
        temporary.write_bytes(data)
        temporary.replace(path)
        return path


def get_connection() -> duckdb.DuckDBPyConnection:
    """A read-only connection to the bound source.

    Opened per call rather than pooled: DuckDB connections are cheap, and a
    long-lived handle would pin a file the cache may need to replace.

    A collection binding is assembled here rather than read from a file, since
    ATTACH does not survive a reconnection. The connection itself is in-memory
    so the views can be created; every real source is attached READ ONLY.
    """
    collection = _bound_collection.get()
    if collection is not None:
        from app.engine.collection_query import connect

        return connect(*collection)
    return duckdb.connect(str(bound_database()), read_only=True)


def _jsonify(value: Any) -> Any:
    """Make one cell JSON-safe, matching the Snowflake connector's output."""
    import datetime as _dt
    from decimal import Decimal

    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _execute_select(safe_sql: str) -> dict[str, Any]:
    conn = get_connection()
    try:
        cursor = conn.execute(safe_sql)
        columns = [description[0] for description in cursor.description or []]
        rows = [[_jsonify(cell) for cell in row] for row in cursor.fetchall()]
    finally:
        conn.close()

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "sql": safe_sql,
    }


def run_query(sql: str) -> dict[str, Any]:
    """Validate, cap, and execute a read-only query."""
    from app.engine.sql_safety import enforce_row_limit, validate_sql

    safe_sql = enforce_row_limit(
        validate_sql(sql, dialect=DIALECT), MAX_ROWS, dialect=DIALECT
    )
    return _execute_select(safe_sql)


def dry_run_query(sql: str) -> list[str]:
    """Prove a query resolves without paying for its rows.

    DuckDB has no PREPARE-and-describe that covers every statement shape, so
    this runs the query with a zero-row limit. Against a local file that costs
    effectively nothing, and it catches the same class of error — a missing
    column, a bad join — that a compile step would.
    """
    from app.engine.sql_safety import enforce_row_limit, validate_sql

    safe_sql = enforce_row_limit(validate_sql(sql, dialect=DIALECT), 0, dialect=DIALECT)
    conn = get_connection()
    try:
        cursor = conn.execute(safe_sql)
        return [description[0] for description in cursor.description or []]
    finally:
        conn.close()


def session_identity() -> tuple[str | None, str | None]:
    """Who is asking, for the audit trail.

    The identity of the signed-in Snowflake user, passed in at bind time — an
    uploaded file has no identity of its own, and recording "duckdb" here would
    make the audit trail useless for the exact question it exists to answer.
    """
    identity = _bound_identity.get()
    if isinstance(identity, tuple) and len(identity) == 2:
        return identity
    return (None, None)

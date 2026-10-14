"""Bind DuckDB for uploaded spreadsheet or folder sources.

Shared by the agent chat endpoint and the dashboards API so file-dashboard
runs attach the same sources the conversation would.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.engine import collection_query
from app.engine import excel_sources as engine_sources
from app.engine.connectors.duckdb import client as duckdb_client
from app.engine.file_dashboards import COLLECTION_PREFIX, EXCEL_PREFIX


@contextmanager
def bind_upload_source(
    source_key: str,
    identity: tuple[str | None, str | None],
    *,
    collection: Any | None = None,
    excel_source: Any | None = None,
) -> Iterator[None]:
    """Bind DuckDB for EXCEL:<id> or FOLDER:<id> for the duration of a request."""
    upper = (source_key or "").upper()
    if upper.startswith(COLLECTION_PREFIX):
        if collection is None:
            collection_id = source_key.split(":", 1)[1]
            collection = engine_sources.get_collection(collection_id)
        if collection is None:
            raise ValueError(f"Folder '{source_key}' is not available.")
        plan = collection_query.plan_for(collection)
        sources, views = plan.as_binding()
        with duckdb_client.use_collection(
            sources, views, documents=plan.documents, identity=identity
        ):
            yield
    elif upper.startswith(EXCEL_PREFIX):
        if excel_source is None:
            dataset_id = source_key.split(":", 1)[1]
            excel_source = engine_sources.get_dataset(dataset_id)
        if excel_source is None:
            raise ValueError(f"Spreadsheet '{source_key}' is not available.")
        database_path = engine_sources.local_database(excel_source)
        with duckdb_client.use_connection(database_path, identity=identity):
            yield
    else:
        raise ValueError(f"Not an upload source: {source_key!r}")

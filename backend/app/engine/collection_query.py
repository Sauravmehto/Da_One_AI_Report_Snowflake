"""Querying a whole collection: its tables and its documents through one handle.

WHY THIS EXISTS
---------------
A folder holds a spreadsheet dataset and a set of PDFs, and the question worth
asking spans both — what is owed against the building whose rent is falling.
Each lives in its own DuckDB file for good reasons: a dataset's database is
keyed by shape and shared with any collection built on the same template, while
documents belong to the folder.

DuckDB ATTACHes other database files, and a view over an attached table costs
nothing and reads like a local one. So one connection can see everything.

A PLAN, NOT A FILE
------------------
Attachments are connection state, not file state: a database holding views over
attached files is useless the moment it is reopened, because the attachments
are gone and the views dangle. So this produces a PLAN — which files to attach
and which views to create — and the connection is assembled from it each time.

The plan's own database is `:memory:`. Every real source is attached READ ONLY,
which is what makes concurrent access across four workers safe, and the views
that name them live only for the life of the connection.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from app.engine import collection_documents, document_store

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_]")


@dataclass
class View:
    """One table of one source, under the name a query should use."""

    name: str
    alias: str
    table: str


@dataclass
class CollectionPlan:
    """Everything in a collection, and how to see it through one connection."""

    sources: list[tuple[str, Path]] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    #: The collection's document database, searched directly rather than
    #: through the plan: its full-text index lives inside that file.
    documents: Path | None = None
    #: Tables whose name collided across datasets and were suffixed.
    renamed: dict[str, str] = field(default_factory=dict)

    @property
    def table_names(self) -> list[str]:
        return [v.name for v in self.views if v.alias != "docs"]

    @property
    def document_tables(self) -> list[str]:
        return [v.name for v in self.views if v.alias == "docs"]

    @property
    def has_documents(self) -> bool:
        return self.documents is not None

    @property
    def is_empty(self) -> bool:
        return not self.views

    def as_binding(self) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
        """The plan as plain data, for the connector to rebuild from."""
        return (
            [(alias, str(path)) for alias, path in self.sources],
            [(v.name, v.alias, v.table) for v in self.views],
        )


def _tables_in(conn: duckdb.DuckDBPyConnection, alias: str) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = ?", [alias]
        ).fetchall()
    ]


def plan(
    dataset_databases: list[tuple[str, Path]],
    documents_database: Path | None,
) -> CollectionPlan:
    """Work out what a collection's sources hold and how to name it.

    Names that collide across datasets are suffixed rather than shadowed: two
    datasets in one folder can genuinely both have a `deal_pipeline`, and
    answering from whichever attached first would be wrong without being
    visible.
    """
    result = CollectionPlan(documents=documents_database)
    probe = duckdb.connect(":memory:")
    try:
        for index, (dataset_id, source) in enumerate(dataset_databases):
            if not source or not Path(source).exists():
                continue
            alias = f"src{index}"
            try:
                probe.execute(f"ATTACH '{source}' AS {alias} (READ_ONLY)")
            except duckdb.Error as exc:
                log.warning("Could not attach dataset %s: %s", dataset_id, exc)
                continue
            result.sources.append((alias, Path(source)))
            taken = {v.name for v in result.views}
            for table in _tables_in(probe, alias):
                name = table
                if name in taken:
                    name = f"{table}_{_SAFE.sub('', dataset_id)[:6]}"
                    result.renamed[name] = table
                result.views.append(View(name=name, alias=alias, table=table))
                taken.add(name)

        if documents_database and Path(documents_database).exists():
            try:
                probe.execute(f"ATTACH '{documents_database}' AS docs (READ_ONLY)")
                result.sources.append(("docs", Path(documents_database)))
                present = set(_tables_in(probe, "docs"))
                for table in document_store.GRAPH_TABLES:
                    if table in present:
                        result.views.append(View(name=table, alias="docs", table=table))
            except duckdb.Error as exc:
                log.warning("Could not attach documents: %s", exc)
                result.documents = None
    finally:
        probe.close()
    return result


def plan_for(collection) -> CollectionPlan:
    """The plan for a collection, pulling every source to this worker first."""
    from app.engine import excel_sources

    databases: list[tuple[str, Path]] = []
    for dataset in collection.datasets:
        try:
            databases.append(
                (dataset.dataset_id, excel_sources.local_database(dataset))
            )
        except Exception as exc:
            log.warning("Could not open dataset %s: %s", dataset.dataset_id, exc)
    documents = collection_documents.local_database(collection.collection_id)
    return plan(databases, documents)


def connect(
    sources: list[tuple[str, str]], views: list[tuple[str, str, str]]
) -> duckdb.DuckDBPyConnection:
    """A connection seeing every source, with the views a query expects.

    In-memory so the views can be created; every real file is attached read
    only, so nothing here can write to a source.
    """
    conn = duckdb.connect(":memory:")
    try:
        for alias, path in sources:
            conn.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")
        for name, alias, table in views:
            conn.execute(f'CREATE VIEW "{name}" AS SELECT * FROM {alias}."{table}"')
    except duckdb.Error:
        conn.close()
        raise
    return conn


def search_documents(
    documents_database: Path | str,
    query: str,
    limit: int = 8,
    source_file: str | None = None,
):
    """Search a collection's documents.

    Against the documents database itself, not the merged connection: DuckDB's
    full-text index is a schema inside the file that holds it, and reaching it
    through an attachment would mean qualifying every call.
    """
    conn = duckdb.connect(str(documents_database), read_only=True)
    try:
        return document_store.search(
            conn, query, limit=limit, source_file=source_file
        )
    finally:
        conn.close()

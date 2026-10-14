"""Documents belonging to a collection: ingest, list, remove, describe.

The document half of what a folder holds. Spreadsheets become datasets keyed by
shape; documents become one graph per collection, because a PDF has no shape to
key on and its value is in being joined to everything else in the folder.

The database is written to shared storage exactly like a dataset's, for the
same reason: four workers, and a file written by one is invisible to the rest.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import duckdb

from app.engine import document_extract, document_parser, document_store
from app.engine import storage as storage_mod

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = document_parser.SUPPORTED_SUFFIXES


@dataclass
class DocumentSummary:
    """One document in a collection, as the library shows it."""

    source_file: str
    file_format: str = ""
    doc_type: str = ""
    entity: str = ""
    #: "stated" | "title" | "model" | "filename" — a subject read off a file
    #: name is a guess, and the UI should be able to say so.
    entity_source: str = ""
    page_count: int = 0
    chunk_count: int = 0
    fact_count: int = 0
    is_scanned: bool = False
    uploaded_by: str = ""
    uploaded_at: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class DocumentBatch:
    documents: list[DocumentSummary] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": [asdict(d) for d in self.documents],
            "failed": [{"filename": f, "detail": d} for f, d in self.failed],
            "warnings": self.warnings,
        }


def is_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_SUFFIXES


# ── the collection's document database ────────────────────────────────────────


def _local_copy(collection_id: str, store: Any) -> Path:
    """This collection's document database, fetched if it exists."""
    from app.engine import excel_sources

    path = Path(tempfile.gettempdir()) / f"docs_{collection_id}.build.duckdb"
    path.unlink(missing_ok=True)
    try:
        path.write_bytes(store.get(excel_sources.documents_key(collection_id)))
    except Exception:
        pass  # first documents for this collection
    return path


def _publish(collection_id: str, path: Path, store: Any) -> int:
    from app.engine import excel_sources
    from app.engine.connectors.duckdb import client as duckdb_client

    data = path.read_bytes()
    store.put(excel_sources.documents_key(collection_id), data)
    path.unlink(missing_ok=True)
    # This worker's cached copy is now stale.
    try:
        (duckdb_client.CACHE_DIR / f"docs_{collection_id}.duckdb").unlink(
            missing_ok=True
        )
    except OSError:
        pass
    return len(data)


def local_database(collection_id: str, expected_size: int | None = None) -> Path | None:
    """This worker's cached copy of a collection's document database."""
    from app.engine import excel_sources
    from app.engine.connectors.duckdb import client as duckdb_client

    store = storage_mod.get_storage()
    try:
        return duckdb_client.ensure_cached(
            f"docs_{collection_id}",
            lambda: store.get(excel_sources.documents_key(collection_id)),
            expected_size=expected_size or None,
        )
    except Exception:
        return None


# ── ingestion ─────────────────────────────────────────────────────────────────


def ingest_documents(
    collection_id: str,
    files: list[tuple[str, bytes]],
    owner: str = "",
    on_progress: Callable[[int, str], None] | None = None,
    use_model: bool = True,
) -> DocumentBatch:
    """Read documents into a collection's graph.

    Each file is parsed, extracted and stored on its own, so one unreadable PDF
    costs that file and not the batch.
    """

    def report(percent: int, stage: str) -> None:
        if on_progress is not None:
            on_progress(percent, stage)

    batch = DocumentBatch()
    if not files:
        return batch

    # Imported here, not at module scope: excel_sources reaches back into this
    # module to ingest a folder's documents, and importing both ways at load
    # time is a cycle.
    from app.engine import excel_sources

    store = storage_mod.get_storage()
    database = _local_copy(collection_id, store)
    conn = duckdb.connect(str(database))
    try:
        document_store.ensure_schema(conn)
        with tempfile.TemporaryDirectory() as tmp:
            for index, (filename, data) in enumerate(files):
                name = Path(filename).name
                report(
                    int(5 + 80 * index / len(files)),
                    f"Reading {name} ({index + 1} of {len(files)})",
                )
                try:
                    path = Path(tmp) / name
                    path.write_bytes(data)
                    summary = _ingest_one(conn, path, name, owner, use_model)
                    batch.documents.append(summary)
                    batch.warnings.extend(summary.warnings)
                    store.put(
                        excel_sources.document_key(collection_id, name), data
                    )
                except Exception as exc:
                    log.exception("Could not read %s", name)
                    batch.failed.append((name, str(exc)[:200]))

        report(90, "Indexing")
        if not document_store.build_index(conn):
            batch.warnings.append(
                "Documents are stored and queryable, but the full-text index "
                "could not be built, so search will be less accurate."
            )
        conn.commit()
    finally:
        conn.close()

    _publish(collection_id, database, store)
    report(100, "Ready")
    return batch


def _ingest_one(
    conn: duckdb.DuckDBPyConnection,
    path: Path,
    name: str,
    owner: str,
    use_model: bool,
) -> DocumentSummary:
    parsed = document_parser.parse_document(path)
    extraction = document_extract.extract(parsed, name)
    if use_model:
        extraction = document_extract.review(parsed, extraction)

    outcome = document_store.write_document(
        conn,
        parsed,
        doc_type=extraction.doc_type,
        facts=extraction.facts,
        edges=extraction.edges,
    )
    # The subject gets an entity even when nothing was extracted from it, so a
    # document with no summary block still joins to the rest of the folder.
    entity_id = document_store.resolve_entity(conn, extraction.entity)
    for alias in extraction.aliases:
        document_store.add_alias(conn, entity_id, alias)

    return DocumentSummary(
        source_file=name,
        file_format=parsed.file_format,
        doc_type=extraction.doc_type,
        entity=extraction.entity,
        entity_source=extraction.entity_source,
        page_count=parsed.page_count,
        chunk_count=outcome.chunk_count,
        fact_count=outcome.fact_count,
        is_scanned=parsed.is_scanned,
        uploaded_by=owner,
        uploaded_at=datetime.now(UTC).isoformat(),
        warnings=outcome.warnings,
    )


def remove_document(collection_id: str, filename: str) -> bool:
    """Take one document back out of a collection."""
    store = storage_mod.get_storage()
    database = _local_copy(collection_id, store)
    if not database.exists():
        return False

    conn = duckdb.connect(str(database))
    try:
        document_store.ensure_schema(conn)
        exists = conn.execute(
            "SELECT 1 FROM documents WHERE source_file = ?", [filename]
        ).fetchone()
        if not exists:
            return False
        document_store.remove_document(conn, filename)
        document_store.build_index(conn)
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()

    _publish(collection_id, database, store)

    # A folder with nothing left in it should leave the library rather than sit
    # there answering every question with nothing.
    if remaining == 0:
        from app.engine import excel_sources

        collection = excel_sources.get_collection(collection_id)
        if collection is not None and not collection.datasets:
            excel_sources.deactivate_collection(collection_id)
    return True


# ── describing what is there ──────────────────────────────────────────────────


def list_documents(collection_id: str) -> list[DocumentSummary]:
    """Every document in a collection, read back from its database."""
    path = local_database(collection_id)
    if path is None or not Path(path).exists():
        return []
    conn = duckdb.connect(str(path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT d.source_file, d.file_format, d.doc_type, d.page_count, "
            "d.is_scanned, COUNT(DISTINCT c.chunk_id), COUNT(DISTINCT f.fact_id) "
            "FROM documents d "
            "LEFT JOIN chunks c ON c.document_id = d.document_id "
            "LEFT JOIN facts f ON f.source_file = d.source_file "
            "GROUP BY 1, 2, 3, 4, 5 ORDER BY 1"
        ).fetchall()
    except duckdb.Error:
        return []
    finally:
        conn.close()
    return [
        DocumentSummary(
            source_file=r[0], file_format=r[1] or "", doc_type=r[2] or "",
            page_count=r[3] or 0, is_scanned=bool(r[4]),
            chunk_count=r[5] or 0, fact_count=r[6] or 0,
        )
        for r in rows
    ]


def schema_context(collection_id: str, documents: list[DocumentSummary]) -> str:
    """What the model needs to know to use a collection's documents."""
    if not documents:
        return ""

    lines = ["\n## Documents in this folder"]
    lines.append(
        f"{len(documents)} document(s) are loaded. They are NOT SQL tables of "
        "their own: use the search_documents tool to find wording, and query "
        "the tables below for figures already extracted from them."
    )
    for doc in documents:
        bits = [f"- {doc.source_file}"]
        if doc.doc_type:
            bits.append(f"({doc.doc_type.replace('_', ' ')})")
        bits.append(f"{doc.page_count} page(s)")
        if doc.fact_count:
            bits.append(f"{doc.fact_count} extracted figures")
        if doc.is_scanned:
            bits.append("— SCANNED, no text could be read from it")
        lines.append(" ".join(bits))

    lines.append(
        "\n### Tables holding what was read out of the documents\n"
        "  facts(entity_id, metric, value_text, value_num, unit, source_file, "
        "source_page, source_label, confidence)\n"
        "      One figure or term per row, e.g. metric='loan_amount'. Join to "
        "entities for the name.\n"
        "  entities(entity_id, kind, canonical_name)\n"
        "      The properties and parties named across the folder.\n"
        "  entity_aliases(entity_id, alias_normalized, alias_raw)\n"
        "      Every name each entity is written under. alias_raw is the name "
        "as it appears in a document; alias_normalized is a lowercased form "
        "with legal suffixes and generic words removed.\n"
        "      TO JOIN A SPREADSHEET COLUMN TO A DOCUMENT, match that column "
        "against alias_raw:\n"
        "        JOIN entity_aliases a ON lower(trim(a.alias_raw)) = "
        "lower(trim(t.property_column))\n"
        "      Do not try to reproduce alias_normalized in SQL — it is built "
        "by the loader and you cannot recreate it reliably.\n"
        "  entity_edges(src_entity_id, relation, dst_entity_id, source_file, "
        "source_page)\n"
        "      Who lends to, leases from or manages whom.\n"
        "  chunks(chunk_id, source_file, source_page, text)\n"
        "      The document text. Prefer search_documents over scanning this.\n"
        "  documents(source_file, file_format, doc_type, page_count, is_scanned)"
    )
    lines.append(
        "\nEvery fact carries source_file and source_page: cite them. A figure "
        "that is not in facts and not in a spreadsheet table has not been "
        "extracted — search the text for it rather than inferring it."
    )
    lines.append(
        "\nCARE WHEN JOINING facts TO A SPREADSHEET TABLE. facts holds one row "
        "per figure, so joining it to a detail table multiplies that table's "
        "rows by the number of facts and inflates every SUM over it. Either "
        "filter to a single metric in the join, or aggregate the two sides "
        "separately and join the results."
    )
    return "\n".join(lines)

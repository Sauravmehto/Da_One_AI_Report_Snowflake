"""Documents as a queryable graph, in the same DuckDB file as the tables.

WHY A GRAPH AND NOT JUST SEARCH
-------------------------------
A folder of property records is not a pile of prose. The same property appears
in its rent roll, its mortgage agreement and its property sheet under slightly
different names, and the questions worth asking span them: what is the debt
against the building whose rent is falling. Retrieval alone cannot answer that,
because no single passage contains it.

So documents land as three things:

  entities / entity_aliases  who or what is being talked about, with the
                             normalised names that resolve one property across
                             every file that mentions it
  facts                      a number or a term, attached to an entity, with
                             the file and page it came from
  chunks                     the text itself, searchable, for the questions
                             whose answer is language rather than a figure

Facts answer precisely and cite exactly. Chunks answer the rest. The aliases
are what let both join back to the spreadsheet side.

EVERYTHING CITES ITS SOURCE
---------------------------
Every fact and every chunk carries `source_file` and `source_page`. A number on
screen leads back to the page it was read from, which is the same guarantee the
spreadsheet tables give with `source_sheet` and `source_row`.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

#: Tables the document side owns. Named apart from the spreadsheet tables, which
#: are named after their own content, so the two can share one database.
GRAPH_TABLES = ("documents", "entities", "entity_aliases", "facts", "chunks", "entity_edges")

#: `entity_aliases` keeps both forms of every name. `alias_normalized` is how
#: WE match one property across files; `alias_raw` is the name as written, and
#: exists because a model writing SQL cannot reproduce the normaliser — joining
#: a spreadsheet's property column to a document needs the original spelling.
#:
#: No SQL comments below: ensure_schema splits on ';', and a semicolon inside a
#: comment silently truncates the statement it belongs to.

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id   VARCHAR PRIMARY KEY,
    source_file   VARCHAR NOT NULL,
    file_format   VARCHAR,
    doc_type      VARCHAR,
    page_count    INTEGER,
    is_scanned    BOOLEAN DEFAULT FALSE,
    scanned_pages VARCHAR,
    ingested_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id      VARCHAR PRIMARY KEY,
    kind           VARCHAR,
    canonical_name VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id        VARCHAR NOT NULL,
    alias_normalized VARCHAR NOT NULL,
    alias_raw        VARCHAR,
    source           VARCHAR
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id      VARCHAR PRIMARY KEY,
    entity_id    VARCHAR,
    metric       VARCHAR NOT NULL,
    value_text   VARCHAR,
    value_num    DOUBLE,
    unit         VARCHAR,
    period_start DATE,
    period_end   DATE,
    source_file  VARCHAR NOT NULL,
    source_page  INTEGER,
    source_label VARCHAR,
    extractor    VARCHAR,
    confidence   DOUBLE
);

CREATE TABLE IF NOT EXISTS entity_edges (
    edge_id       VARCHAR PRIMARY KEY,
    src_entity_id VARCHAR NOT NULL,
    relation      VARCHAR NOT NULL,
    dst_entity_id VARCHAR NOT NULL,
    source_file   VARCHAR NOT NULL,
    source_page   INTEGER,
    confidence    DOUBLE
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    VARCHAR PRIMARY KEY,
    document_id VARCHAR NOT NULL,
    source_file VARCHAR NOT NULL,
    source_page INTEGER,
    ordinal     INTEGER,
    text        VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_metric ON facts(metric);
CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity_id);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON entity_aliases(alias_normalized);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(source_file);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the document tables if they are not already there."""
    for statement in SCHEMA.strip().split(";"):
        if statement.strip():
            conn.execute(statement)


def has_documents(conn: duckdb.DuckDBPyConnection) -> bool:
    """Whether this database holds any documents at all."""
    try:
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0
    except duckdb.Error:
        return False


# ── entity resolution ─────────────────────────────────────────────────────────

#: Words that say what kind of thing something is rather than which one, and so
#: must not decide whether two names refer to the same entity. "Cherry Creek
#: Apartments" and "Cherry Creek Apts LLC" are one property.
_NOISE_WORDS = {
    "the", "a", "an", "llc", "lp", "llp", "inc", "corp", "corporation",
    "company", "co", "ltd", "limited", "partners", "partnership", "trust",
    "holdings", "properties", "property", "apartments", "apts", "apt",
    "residences", "residence", "building", "plaza", "center", "centre",
}


def normalize_entity_name(name: str) -> str:
    """A name reduced to what identifies it.

    Case, punctuation, legal suffixes and generic property words all vary
    between the rent roll and the mortgage agreement for one building, so none
    of them can be part of the key that decides those are the same thing.
    """
    cleaned = re.sub(r"[^\w\s]", " ", (name or "").lower())
    words = [w for w in cleaned.split() if w and w not in _NOISE_WORDS]
    return " ".join(words) or (name or "").strip().lower()


def resolve_entity(
    conn: duckdb.DuckDBPyConnection, name: str, kind: str = "unknown"
) -> str:
    """The entity this name refers to, creating it the first time.

    Matching on the normalised alias is what makes one property resolve to a
    single entity across every file that names it — the point of the exercise.
    """
    normalized = normalize_entity_name(name)
    if not normalized:
        normalized = "unnamed"
    found = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias_normalized = ? LIMIT 1",
        [normalized],
    ).fetchone()
    if found:
        return found[0]

    entity_id = new_id("ent")
    conn.execute(
        "INSERT INTO entities (entity_id, kind, canonical_name) VALUES (?, ?, ?)",
        [entity_id, kind, (name or "").strip() or "Unnamed"],
    )
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias_normalized, alias_raw, "
        "source) VALUES (?, ?, ?, 'rule')",
        [entity_id, normalized, (name or "").strip()],
    )
    return entity_id


def add_alias(conn: duckdb.DuckDBPyConnection, entity_id: str, alias: str) -> None:
    """Record another name for an entity, if it is new."""
    normalized = normalize_entity_name(alias)
    if not normalized:
        return
    exists = conn.execute(
        "SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias_normalized = ?",
        [entity_id, normalized],
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias_normalized, "
            "alias_raw, source) VALUES (?, ?, ?, 'alias')",
            [entity_id, normalized, (alias or "").strip()],
        )


# ── chunking ──────────────────────────────────────────────────────────────────

#: Target size of a searchable passage. Small enough that a hit points at
#: something specific, large enough to carry the sentence around it.
CHUNK_CHARS = 1200
#: Carried from the end of one chunk into the next, so a clause split across the
#: boundary is still findable whole from one side of it.
CHUNK_OVERLAP = 150


def split_into_chunks(text: str) -> list[str]:
    """Break a page into overlapping passages, preferring paragraph breaks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            # Prefer to break where the text itself does.
            window = text[start:end]
            for marker in ("\n\n", "\n", ". "):
                cut = window.rfind(marker)
                if cut > CHUNK_CHARS // 2:
                    end = start + cut + len(marker)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


# ── writing ───────────────────────────────────────────────────────────────────


@dataclass
class Fact:
    """One extracted figure or term, and where it was read from."""

    entity_name: str
    metric: str
    value_text: str | None = None
    value_num: float | None = None
    unit: str = ""
    source_page: int | None = None
    source_label: str = ""
    extractor: str = ""
    confidence: float = 0.0


@dataclass
class Edge:
    """A relationship between two entities, as one document asserts it."""

    src_name: str
    relation: str
    dst_name: str
    source_page: int | None = None
    confidence: float = 0.0


@dataclass
class DocumentOutcome:
    document_id: str
    source_file: str
    page_count: int
    chunk_count: int = 0
    fact_count: int = 0
    entity_count: int = 0
    edge_count: int = 0
    is_scanned: bool = False
    scanned_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def remove_document(conn: duckdb.DuckDBPyConnection, source_file: str) -> None:
    """Clear everything an earlier load of this file wrote.

    Re-uploading a corrected document must replace it rather than double it,
    the same way re-uploading a workbook replaces its rows.
    """
    conn.execute(
        "DELETE FROM chunks WHERE document_id IN "
        "(SELECT document_id FROM documents WHERE source_file = ?)",
        [source_file],
    )
    for table in ("facts", "entity_edges"):
        conn.execute(f"DELETE FROM {table} WHERE source_file = ?", [source_file])
    conn.execute("DELETE FROM documents WHERE source_file = ?", [source_file])


def write_document(
    conn: duckdb.DuckDBPyConnection,
    parsed: Any,
    doc_type: str = "",
    facts: list[Fact] | None = None,
    edges: list[Edge] | None = None,
) -> DocumentOutcome:
    """Store one parsed document: its pages, its passages, and what was read
    out of it."""
    ensure_schema(conn)
    remove_document(conn, parsed.filename)

    document_id = new_id("doc")
    scanned = parsed.scanned_pages
    conn.execute(
        "INSERT INTO documents (document_id, source_file, file_format, doc_type, "
        "page_count, is_scanned, scanned_pages) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            document_id,
            parsed.filename,
            parsed.file_format,
            doc_type,
            parsed.page_count,
            parsed.is_scanned,
            ",".join(str(p) for p in scanned),
        ],
    )

    outcome = DocumentOutcome(
        document_id=document_id,
        source_file=parsed.filename,
        page_count=parsed.page_count,
        is_scanned=parsed.is_scanned,
        scanned_pages=scanned,
    )

    ordinal = 0
    for page in parsed.pages:
        for piece in split_into_chunks(page.text):
            conn.execute(
                "INSERT INTO chunks (chunk_id, document_id, source_file, "
                "source_page, ordinal, text) VALUES (?, ?, ?, ?, ?, ?)",
                [new_id("chunk"), document_id, parsed.filename, page.page, ordinal, piece],
            )
            ordinal += 1
    outcome.chunk_count = ordinal

    entities: set[str] = set()
    for fact in facts or []:
        entity_id = resolve_entity(conn, fact.entity_name)
        entities.add(entity_id)
        conn.execute(
            "INSERT INTO facts (fact_id, entity_id, metric, value_text, value_num, "
            "unit, source_file, source_page, source_label, extractor, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                new_id("fact"), entity_id, fact.metric, fact.value_text,
                fact.value_num, fact.unit, parsed.filename, fact.source_page,
                fact.source_label, fact.extractor, fact.confidence,
            ],
        )
        outcome.fact_count += 1

    for edge in edges or []:
        src = resolve_entity(conn, edge.src_name)
        dst = resolve_entity(conn, edge.dst_name)
        entities.update({src, dst})
        conn.execute(
            "INSERT INTO entity_edges (edge_id, src_entity_id, relation, "
            "dst_entity_id, source_file, source_page, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                new_id("edge"), src, edge.relation, dst, parsed.filename,
                edge.source_page, edge.confidence,
            ],
        )
        outcome.edge_count += 1

    outcome.entity_count = len(entities)
    if parsed.is_scanned:
        outcome.warnings.append(
            f"{parsed.filename} has no text layer on any page — it is a scan. "
            "Nothing could be read from it without OCR."
        )
    elif scanned:
        outcome.warnings.append(
            f"{parsed.filename}: pages {', '.join(str(p) for p in scanned[:8])} "
            "have no text layer and were not indexed."
        )
    conn.commit()
    return outcome


# ── searching ─────────────────────────────────────────────────────────────────


@dataclass
class Passage:
    """One search hit, with enough to cite it."""

    source_file: str
    source_page: int | None
    text: str
    score: float


def _build_index(conn: duckdb.DuckDBPyConnection) -> bool:
    """Build the full-text index over chunks.

    Rebuilt on write rather than kept live: DuckDB's FTS index is a snapshot
    over the table as it stood, so a document added after the last build is
    invisible to search until this runs again.
    """
    try:
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
        conn.execute(
            "PRAGMA create_fts_index('chunks', 'chunk_id', 'text', overwrite=1)"
        )
        return True
    except duckdb.Error as exc:
        # Worth saying out loud: the extension downloads on first use, so a
        # container with no egress silently loses search rather than failing.
        log.warning("Could not build the document index: %s", exc)
        return False


def build_index(conn: duckdb.DuckDBPyConnection) -> bool:
    return _build_index(conn)


def search(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    limit: int = 8,
    source_file: str | None = None,
) -> list[Passage]:
    """Passages matching a query, best first.

    Ranked by BM25 where the index is available, and by a plain substring match
    where it is not — a degraded answer beats no answer, and the caller cannot
    tell the difference except in quality.
    """
    query = (query or "").strip()
    if not query:
        return []

    where = "WHERE score IS NOT NULL"
    params: list[Any] = [query]
    if source_file:
        where += " AND source_file = ?"
        params.append(source_file)

    try:
        conn.execute("LOAD fts")
        rows = conn.execute(
            "SELECT source_file, source_page, text, score FROM ("
            "  SELECT *, fts_main_chunks.match_bm25(chunk_id, ?) AS score"
            "  FROM chunks"
            f") {where} ORDER BY score DESC LIMIT {int(limit)}",
            params,
        ).fetchall()
        return [Passage(r[0], r[1], r[2], float(r[3])) for r in rows]
    except duckdb.Error:
        return _fallback_search(conn, query, limit, source_file)


def _fallback_search(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    limit: int,
    source_file: str | None,
) -> list[Passage]:
    """Rank by how many of the query's words a passage contains."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if not words:
        return []
    scoring = " + ".join(
        ["CASE WHEN lower(text) LIKE '%' || ? || '%' THEN 1 ELSE 0 END"] * len(words)
    )
    where = ""
    params: list[Any] = list(words)
    if source_file:
        where = "WHERE source_file = ?"
        params.append(source_file)
    try:
        # A subquery rather than QUALIFY, which needs a window function; this
        # score is a plain expression.
        rows = conn.execute(
            "SELECT source_file, source_page, text, score FROM ("
            f"  SELECT source_file, source_page, text, ({scoring}) AS score"
            f"  FROM chunks {where}"
            f") WHERE score > 0 ORDER BY score DESC LIMIT {int(limit)}",
            params,
        ).fetchall()
    except duckdb.Error:
        log.exception("Document search failed")
        return []
    return [Passage(r[0], r[1], r[2], float(r[3])) for r in rows]


def document_summary(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """What documents this database holds, for the model's context."""
    try:
        rows = conn.execute(
            "SELECT d.source_file, d.file_format, d.doc_type, d.page_count, "
            "d.is_scanned, COUNT(c.chunk_id) "
            "FROM documents d LEFT JOIN chunks c ON c.document_id = d.document_id "
            "GROUP BY 1, 2, 3, 4, 5 ORDER BY 1"
        ).fetchall()
    except duckdb.Error:
        return []
    return [
        {
            "source_file": r[0], "file_format": r[1], "doc_type": r[2],
            "page_count": r[3], "is_scanned": bool(r[4]), "chunks": r[5],
        }
        for r in rows
    ]

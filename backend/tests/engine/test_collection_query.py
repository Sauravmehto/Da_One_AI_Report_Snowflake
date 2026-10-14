"""A folder's sources seen through one connection.

The point of a collection is that a question can span it: rent from a workbook
and debt from a mortgage in the same query. These cover the plan that makes
that possible and the ways it can quietly go wrong.
"""

from __future__ import annotations

import duckdb
import pytest

from app.engine import collection_query, document_store


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "dataset.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE unit_rent_roll (property VARCHAR, unit VARCHAR, rent DOUBLE)"
    )
    conn.executemany(
        "INSERT INTO unit_rent_roll VALUES (?, ?, ?)",
        [
            ("Cherry Creek Senior Residences", "101", 2400.0),
            ("Cherry Creek Senior Residences", "102", 2250.0),
            ("Willow Bend", "201", 3100.0),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def documents(tmp_path):
    path = tmp_path / "documents.duckdb"
    conn = duckdb.connect(str(path))
    document_store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO documents (document_id, source_file, file_format, page_count) "
        "VALUES ('doc_1', 'mortgage.pdf', 'pdf', 1)"
    )
    entity = document_store.resolve_entity(conn, "Cherry Creek Apts LLC")
    document_store.add_alias(conn, entity, "Cherry Creek Senior Residences")
    conn.execute(
        "INSERT INTO facts (fact_id, entity_id, metric, value_num, unit, "
        "source_file, source_page) VALUES "
        "('f1', ?, 'loan_amount', 12400000, 'usd', 'mortgage.pdf', 1)",
        [entity],
    )
    conn.execute(
        "INSERT INTO chunks (chunk_id, document_id, source_file, source_page, "
        "ordinal, text) VALUES ('c1', 'doc_1', 'mortgage.pdf', 1, 0, "
        "'The Lender may terminate upon ninety days written notice.')"
    )
    conn.commit()
    conn.close()
    return path


# -- the plan -----------------------------------------------------------------


def test_a_plan_sees_both_the_tables_and_the_documents(dataset, documents):
    plan = collection_query.plan([("d1", dataset)], documents)
    assert "unit_rent_roll" in plan.table_names
    assert "facts" in plan.document_tables
    assert plan.has_documents


def test_a_folder_with_no_documents_still_plans(dataset):
    plan = collection_query.plan([("d1", dataset)], None)
    assert plan.table_names == ["unit_rent_roll"]
    assert not plan.has_documents


def test_a_missing_source_is_skipped_rather_than_fatal(tmp_path, dataset):
    plan = collection_query.plan(
        [("gone", tmp_path / "nope.duckdb"), ("d1", dataset)], None
    )
    assert plan.table_names == ["unit_rent_roll"]


def test_colliding_table_names_are_kept_apart(tmp_path, dataset):
    """Two datasets in one folder can genuinely both have a `deal_pipeline`."""
    second = tmp_path / "second.duckdb"
    conn = duckdb.connect(str(second))
    conn.execute("CREATE TABLE unit_rent_roll (property VARCHAR, rent DOUBLE)")
    conn.commit()
    conn.close()

    plan = collection_query.plan([("aaa111", dataset), ("bbb222", second)], None)
    assert len(plan.table_names) == 2
    assert len(set(plan.table_names)) == 2, "a shadowed table answers wrongly"
    assert plan.renamed


# -- querying through it ------------------------------------------------------


def test_one_query_spans_a_workbook_and_a_document(dataset, documents):
    """The whole reason a folder is the unit of asking.

    The join is on alias_raw, which is the join the model is told to write:
    the normalised key is built by the loader and cannot be reproduced in SQL.
    """
    plan = collection_query.plan([("d1", dataset)], documents)
    conn = collection_query.connect(*plan.as_binding())
    try:
        rows = conn.execute(
            """
            WITH rent AS (
              SELECT property, SUM(rent) AS monthly
              FROM unit_rent_roll GROUP BY 1
            ), debt AS (
              SELECT lower(trim(a.alias_raw)) AS name,
                     MAX(f.value_num)  AS loan,
                     MAX(f.source_file) AS cited_in,
                     MAX(f.source_page) AS cited_page
              FROM facts f
              JOIN entity_aliases a USING (entity_id)
              WHERE f.metric = 'loan_amount'
              GROUP BY 1
            )
            SELECT r.property, r.monthly, d.loan, d.cited_in, d.cited_page
            FROM rent r
            JOIN debt d ON d.name = lower(trim(r.property))
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows, "the workbook and the document could not be joined"
    property_name, monthly, loan, cited_in, cited_page = rows[0]
    assert property_name == "Cherry Creek Senior Residences"
    assert monthly == 4650.0          # from the workbook
    assert loan == 12_400_000.0       # from the PDF
    assert (cited_in, cited_page) == ("mortgage.pdf", 1)


def test_a_document_name_joins_to_a_spreadsheet_column(documents):
    """alias_raw exists so a join can be written without our normaliser."""
    conn = duckdb.connect(str(documents), read_only=True)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT alias_raw FROM entity_aliases"
        ).fetchall()}
    finally:
        conn.close()
    assert "Cherry Creek Senior Residences" in names
    assert "Cherry Creek Apts LLC" in names


def test_a_binding_survives_being_reconnected(dataset, documents):
    """Attachments are connection state, so the plan must rebuild them.

    A database file holding views over attached sources is useless the moment
    it is reopened — the attachments are gone and every view dangles.
    """
    plan = collection_query.plan([("d1", dataset)], documents)
    binding = plan.as_binding()
    for _ in range(3):
        conn = collection_query.connect(*binding)
        try:
            assert conn.execute("SELECT COUNT(*) FROM unit_rent_roll").fetchone()[0] == 3
        finally:
            conn.close()


def test_sources_are_attached_read_only(dataset):
    plan = collection_query.plan([("d1", dataset)], None)
    conn = collection_query.connect(*plan.as_binding())
    try:
        with pytest.raises(duckdb.Error):
            conn.execute("DELETE FROM unit_rent_roll")
    finally:
        conn.close()


def test_document_search_runs_against_the_document_database(documents):
    hits = collection_query.search_documents(documents, "terminate notice")
    assert hits
    assert hits[0].source_file == "mortgage.pdf"
    assert hits[0].source_page == 1

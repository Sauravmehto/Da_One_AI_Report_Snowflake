"""Semantic layer authoring: round-trip, validation, draft/publish.

The rules under test are the ones whose failure is silent in production. A
date role on a TEXT column does not raise anywhere — the planner simply binds
a date range to 'Q1 2025' and returns no rows. A two-part table key never
matches the loader index, so the table's documentation is quietly ignored.
Both have to be caught at publish time or not at all.
"""

import tomllib

import pytest

from app.semantic.document import MetricDoc, SemanticDocument, TableDoc
from app.semantic.repository import LocalDirectoryRepository
from app.semantic.service import (
    DocumentNotFoundError,
    SemanticLayerService,
    ValidationFailedError,
)
from app.semantic.validation import has_errors, validate_document

TABLE = "PRIVATECREDIT.PUBLIC.PC_MARKS"

SCHEMA = {
    TABLE: {
        "SECURITY_ID": "TEXT",
        "MARK_DATE": "DATE",
        "QUARTER_LABEL": "TEXT",
        "FAIR_VALUE": "NUMBER",
    }
}


def _doc(**overrides) -> SemanticDocument:
    base = dict(
        database="PRIVATECREDIT",
        display_name="Private Credit Fund",
        tables={
            TABLE: TableDoc(
                description="Quarterly marks.",
                key_columns={"SECURITY_ID": "Deal id", "FAIR_VALUE": "Fair value"},
                date_roles={"MARK_DATE": "snapshot_date"},
            )
        },
    )
    base.update(overrides)
    return SemanticDocument(**base)


# ── Round trip ────────────────────────────────────────────────────────────


def test_document_survives_a_toml_round_trip():
    doc = _doc(
        join_rules="- PC_MARKS joins PRIVATE_CREDIT on SECURITY_ID.",
        metrics={"Delinquency Rate": MetricDoc(definition="Late / total", sql="SELECT 1")},
    )

    reparsed = SemanticDocument.from_toml_dict(tomllib.loads(doc.to_toml().decode()))

    assert reparsed.database == "PRIVATECREDIT"
    assert reparsed.join_rules == doc.join_rules
    assert reparsed.tables[TABLE].date_roles == {"MARK_DATE": "snapshot_date"}
    assert reparsed.metrics["Delinquency Rate"].sql == "SELECT 1"


def test_multiline_prose_stays_multiline():
    """Join rules are one rule per line. Serialised as a single \\n-escaped
    string they are unreadable and undiffable — and reviewing a change to the
    join rules is exactly when readability matters most."""
    doc = _doc(join_rules="- A joins B on ID.\n- C never joins D.\n")

    text = doc.to_toml().decode()

    assert 'join_rules = """' in text
    assert "\\n" not in text.split("join_rules")[1].split('"""')[1]


def test_unmodelled_sections_survive_serialisation():
    """Covers the document layer only — see the service test for the round trip
    through the API, which is where this guarantee actually used to break."""
    raw = {"database": "PRIVATECREDIT", "briefing": {"enabled": True, "cron": "0 8 * * *"}}

    doc = SemanticDocument.from_toml_dict(raw)
    reparsed = tomllib.loads(doc.to_toml().decode())

    assert reparsed["briefing"] == {"enabled": True, "cron": "0 8 * * *"}


def test_empty_date_roles_are_omitted_entirely():
    """An empty table reads as 'declared but blank'; absent reads as 'no date
    dimension', which is what the planner must see."""
    doc = _doc(tables={TABLE: TableDoc(description="No dates here.")})

    assert "date_roles" not in tomllib.loads(doc.to_toml().decode())["tables"][TABLE]


# ── Validation ────────────────────────────────────────────────────────────


def test_date_role_on_a_text_column_is_an_error():
    doc = _doc(tables={TABLE: TableDoc(date_roles={"QUARTER_LABEL": "snapshot_date"})})

    issues = validate_document(doc, schema=SCHEMA)

    assert has_errors(issues)
    assert any("not a date type" in i.message for i in issues)


def test_domain_specific_date_roles_are_allowed():
    """Roles are open vocabulary. The planner is shown the roles a table
    declares and asks for one by name — it carries no fixed list — so real
    estate needs `acquisition_date` and PE needs `warrant_expiry_date`."""
    doc = _doc(tables={TABLE: TableDoc(date_roles={"MARK_DATE": "warrant_expiry_date"})})

    issues = validate_document(doc, schema=SCHEMA)

    assert not has_errors(issues)
    assert any(i.severity == "warning" for i in issues)


def test_malformed_role_is_an_error():
    """Shape is enforced even though vocabulary is not: roles match exactly,
    so 'Snapshot Date' could never resolve against anything."""
    doc = _doc(tables={TABLE: TableDoc(date_roles={"MARK_DATE": "Snapshot Date"})})

    assert has_errors(validate_document(doc, schema=SCHEMA))


def test_published_toml_keeps_the_shape_both_loaders_expect(tmp_path):
    """The real contract for a published file.

    Byte equality is the wrong test — formatting legitimately changes when a
    document is written by the editor rather than by hand. What must not change
    is what the loaders PRODUCE from it: the exact text handed to the model,
    how date roles resolve, the metric definitions. So round-trip every shipped
    config and compare the loaders' outputs, not the bytes.
    """
    import pathlib
    import tomllib

    from app.engine.metadata_loader import MetadataStore
    from app.metadata.loader import MetadataService

    src = pathlib.Path(__file__).resolve().parents[1] / "config" / "metadata"

    for path in sorted(src.glob("*.toml")):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        (tmp_path / path.name).write_bytes(
            SemanticDocument.from_toml_dict(raw).to_toml()
        )

    before_store, before_svc = MetadataStore(src), MetadataService(src)
    after_store, after_svc = MetadataStore(tmp_path), MetadataService(tmp_path)

    databases = before_store.documented_databases()
    assert databases, "no configs found — the test would pass vacuously"

    for db in databases:
        # The exact text the model is given.
        assert before_store.build_context(db) == after_store.build_context(db), db
        assert before_store.dashboard_heuristics(db) == \
            after_store.dashboard_heuristics(db), db
        assert before_svc.get_metrics(db) == after_svc.get_metrics(db), db
        assert before_svc.get_join_rules(db) == after_svc.get_join_rules(db), db

        filename = f"{db.lower().lstrip('_')}.toml"
        original = tomllib.loads((src / filename).read_text(encoding="utf-8"))

        for table in original.get("tables") or {}:
            # Date-role resolution: the surface whose failures are silent.
            assert before_store.date_columns(db, table) == \
                after_store.date_columns(db, table), table
            for role in set(before_store.date_columns(db, table).values()) | {None}:
                assert before_store.resolve_date_column(db, table, role) == \
                    after_store.resolve_date_column(db, table, role), (table, role)

            _, schema, name = table.split(".")
            assert before_svc.get_column_meta(db, schema, name) == \
                after_svc.get_column_meta(db, schema, name), table

        # Sections the editor does not model — [briefing] and its
        # [[briefing.kpi_cards]] array-of-tables — must survive untouched.
        written = tomllib.loads((tmp_path / filename).read_text(encoding="utf-8"))
        for key, value in original.items():
            if key == "database":
                continue  # normalised to upper case on purpose
            assert written.get(key) == value, f"{db}: section '{key}' changed"


def test_shipped_configs_validate_clean():
    """The hand-curated configs are the reference for what valid looks like.
    If a rule flags them, the rule is wrong — that is how the closed role
    enum was caught."""
    import pathlib
    import tomllib

    config_dir = pathlib.Path(__file__).resolve().parents[1] / "config" / "metadata"
    for path in sorted(config_dir.glob("*.toml")):
        doc = SemanticDocument.from_toml_dict(tomllib.loads(path.read_text(encoding="utf-8")))
        errors = [i for i in validate_document(doc) if i.severity == "error"]
        assert not errors, f"{path.name}: {[i.message for i in errors]}"


def test_two_part_table_key_is_an_error():
    doc = _doc(tables={"PUBLIC.PC_MARKS": TableDoc(description="x")})

    issues = validate_document(doc, schema=SCHEMA)

    assert has_errors(issues)
    assert any("three-part name" in i.message for i in issues)


def test_duplicate_date_role_is_an_error():
    """Two columns with one role make resolve_date_column() permanently
    ambiguous, so the planner stops and asks on every single request."""
    doc = _doc(tables={
        TABLE: TableDoc(date_roles={
            "MARK_DATE": "snapshot_date",
            "QUARTER_LABEL": "snapshot_date",
        })
    })

    issues = validate_document(doc, schema=SCHEMA)

    assert any("already used by" in i.message for i in issues)


def test_unknown_column_and_missing_table_are_errors():
    doc = _doc(tables={
        TABLE: TableDoc(key_columns={"NO_SUCH_COL": "?"}),
        "PRIVATECREDIT.PUBLIC.GONE": TableDoc(description="dropped table"),
    })

    issues = validate_document(doc, schema=SCHEMA)
    messages = " ".join(i.message for i in issues)

    assert "does not exist on" in messages
    assert "does not exist in the database" in messages


def test_missing_descriptions_warn_but_do_not_block():
    """Publishing must be possible before every column is documented, or the
    layer never ships at all."""
    doc = _doc(tables={TABLE: TableDoc(key_columns={"SECURITY_ID": "Deal id"})})

    issues = validate_document(doc, schema=SCHEMA)

    assert not has_errors(issues)
    assert any(i.severity == "warning" for i in issues)


def test_structural_validation_runs_without_a_schema():
    doc = _doc(tables={"BAD.KEY": TableDoc(description="x")})

    assert has_errors(validate_document(doc))


# ── Draft / publish ───────────────────────────────────────────────────────


@pytest.fixture
def service(tmp_path):
    return SemanticLayerService(LocalDirectoryRepository(tmp_path))


def test_draft_is_invisible_to_the_loaders(service, tmp_path):
    """The whole point of draft/publish: an unfinished layer must not be
    picked up by the loaders, which glob *.toml in the config directory."""
    service.save_draft("PRIVATECREDIT", _doc())

    assert list(tmp_path.glob("*.toml")) == []
    assert service.get_state("PRIVATECREDIT").published is None
    assert service.get_state("PRIVATECREDIT").draft is not None


def test_publish_promotes_the_draft_and_clears_it(service, tmp_path):
    service.save_draft("PRIVATECREDIT", _doc())

    service.publish("PRIVATECREDIT", schema=SCHEMA)
    state = service.get_state("PRIVATECREDIT")

    assert state.published is not None
    assert state.draft is None
    assert (tmp_path / "privatecredit.toml").is_file()


def test_publish_refuses_a_document_with_errors(service):
    service.save_draft("PRIVATECREDIT", _doc(
        tables={TABLE: TableDoc(date_roles={"QUARTER_LABEL": "snapshot_date"})}
    ))

    with pytest.raises(ValidationFailedError) as exc:
        service.publish("PRIVATECREDIT", schema=SCHEMA)

    assert has_errors(exc.value.issues)
    # Nothing was written — the live layer is untouched by a failed publish.
    assert service.get_state("PRIVATECREDIT").published is None


def test_editing_through_the_api_does_not_delete_unmodelled_sections(service):
    """The bug this exists for, reproduced exactly.

    `extra` is `exclude=True`, so a document that has been to the browser and
    back arrives with it empty. Before the fix, saving that stripped `[briefing]`
    out of the published file — silently, with no diff and no warning. The
    earlier document-layer test passed throughout, because it never crossed the
    API boundary where the loss happened.
    """
    original = _doc(extra={"briefing": {"enabled": True, "title": "Morning Briefing"}})
    service.save_draft("PRIVATECREDIT", original)
    service.publish("PRIVATECREDIT", schema=SCHEMA)
    assert "briefing" in service.get_state("PRIVATECREDIT").published.extra

    # Exactly what the API hands the browser, and what the browser hands back.
    from_browser = SemanticDocument(**service.get_state("PRIVATECREDIT").published.model_dump())
    assert not from_browser.extra, "precondition: the API really does strip extra"

    from_browser.display_name = "Edited In The UI"
    service.save_draft("PRIVATECREDIT", from_browser)
    service.publish("PRIVATECREDIT", schema=SCHEMA)

    published = service.get_state("PRIVATECREDIT").published
    assert published.display_name == "Edited In The UI"
    assert published.extra["briefing"] == {"enabled": True, "title": "Morning Briefing"}


def test_publish_without_a_draft_is_an_error(service):
    with pytest.raises(DocumentNotFoundError):
        service.publish("PRIVATECREDIT", schema=SCHEMA)


def test_start_draft_seeds_from_the_published_document(service):
    service.save_draft("PRIVATECREDIT", _doc(description="Original."))
    service.publish("PRIVATECREDIT", schema=SCHEMA)

    draft = service.start_draft("PRIVATECREDIT")

    assert draft.description == "Original."


def test_publish_invalidates_the_loader_caches(service, monkeypatch):
    """A published layer that needs a backend restart to take effect is the
    failure this guards — both loaders cache their configs for the process."""
    called = []
    monkeypatch.setattr("app.semantic.service.metadata_store",
                        type("S", (), {"invalidate": lambda self: called.append("engine")})())
    monkeypatch.setattr("app.semantic.service.metadata_service",
                        type("S", (), {"invalidate": lambda self: called.append("app")})())

    service.save_draft("PRIVATECREDIT", _doc())
    service.publish("PRIVATECREDIT", schema=SCHEMA)

    assert sorted(called) == ["app", "engine"]


def test_repository_rejects_a_traversing_database_name(tmp_path):
    repo = LocalDirectoryRepository(tmp_path)

    repo.write_draft("../../etc/passwd", b"database = 'X'\n")

    # Sanitised to a flat name inside the store, never written outside it.
    assert not (tmp_path.parent / "etc").exists()
    assert list((tmp_path / "drafts").glob("*.toml"))


def test_version_marker_changes_when_a_document_is_published(service, tmp_path):
    repo = LocalDirectoryRepository(tmp_path)
    before = repo.version_marker()

    service.save_draft("PRIVATECREDIT", _doc())
    service.publish("PRIVATECREDIT", schema=SCHEMA)

    assert repo.version_marker() != before

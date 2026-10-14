"""Loaders notice a publish made by ANOTHER process.

Production runs four uvicorn workers. Publishing invalidates the caches of the
worker that served the request and no others, so a freshly published database
came back documented from one worker in four — which looked like the semantic
layer never appearing at all.

Every test here therefore reloads WITHOUT calling invalidate(): that call is
exactly what the other workers never get.
"""

from __future__ import annotations

import pytest

from app.engine.metadata_loader import MetadataStore
from app.metadata.loader import MetadataService


def write_config(directory, database: str, description: str = "first") -> None:
    (directory / f"{database.lower()}.toml").write_text(
        f'database = "{database}"\n'
        f'display_name = "{database} DB"\n'
        f'description = "{description}"\n',
        encoding="utf-8",
    )


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "metadata"
    d.mkdir()
    return d


# --- the engine's store (what the model is told) ----------------------------


def test_store_sees_a_database_published_by_another_process(config_dir):
    store = MetadataStore(config_dir)
    assert store.documented_databases() == [], "started with a config it should not have"

    write_config(config_dir, "LOAN")

    assert store.documented_databases() == ["LOAN"], (
        "a publish by another worker was invisible until this one restarted"
    )


def test_store_drops_a_database_whose_file_was_removed(config_dir):
    write_config(config_dir, "LOAN")
    store = MetadataStore(config_dir)
    assert store.documented_databases() == ["LOAN"]

    (config_dir / "loan.toml").unlink()

    assert store.documented_databases() == [], "a reload merged over the old view"


def test_store_picks_up_an_edit_to_an_existing_file(config_dir):
    """A re-publish of a database that already existed — the file name is
    unchanged, so only its timestamp and size say anything happened."""
    write_config(config_dir, "LOAN", description="first")
    store = MetadataStore(config_dir)
    assert "first" in store.build_context("LOAN")

    write_config(config_dir, "LOAN", description="a materially longer description")

    context = store.build_context("LOAN")
    assert "a materially longer description" in context
    assert "first" not in context


def test_store_does_not_reread_when_nothing_changed(config_dir, monkeypatch):
    """The fingerprint is checked on every read, so it must not turn every
    request into a full re-parse of every config."""
    write_config(config_dir, "LOAN")
    store = MetadataStore(config_dir)
    store.documented_databases()

    opened: list[str] = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    for _ in range(5):
        store.documented_databases()

    assert opened == [], "re-parsed the configs when the directory had not changed"


# --- the API's service (what the UI is told) --------------------------------


def test_service_reports_documented_after_another_process_publishes(config_dir):
    service = MetadataService(config_dir)
    assert service.is_documented("LOAN") is False

    write_config(config_dir, "LOAN")

    assert service.is_documented("LOAN") is True, (
        "the UI would still offer no chat for a published database"
    )


def test_service_drops_a_removed_database(config_dir):
    write_config(config_dir, "LOAN")
    service = MetadataService(config_dir)
    assert service.is_documented("LOAN") is True

    (config_dir / "loan.toml").unlink()

    assert service.is_documented("LOAN") is False


def test_both_loaders_agree_after_a_publish(config_dir):
    """They read the same directory; the UI and the model must not disagree
    about whether a database has meaning attached."""
    store = MetadataStore(config_dir)
    service = MetadataService(config_dir)
    store.documented_databases()
    service.is_documented("LOAN")

    write_config(config_dir, "LOAN")

    assert ("LOAN" in store.documented_databases()) is service.is_documented("LOAN")

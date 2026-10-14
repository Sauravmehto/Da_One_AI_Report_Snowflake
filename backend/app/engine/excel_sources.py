"""Uploaded spreadsheets as first-class data sources.

Ties the pieces together: store the files, read their structure, work out what
their fields mean, load the rows into DuckDB, and remember the definitions so
the next file of the same shape costs nothing.

WHAT A DATASET IS
-----------------
A dataset is a collection somebody uploaded together. Drop in a folder of
twelve monthly rent rolls and you get ONE dataset holding twelve periods, so
"how did rent move this year" is a GROUP BY rather than twelve files that
cannot be compared. Drop in a mixed folder and you get one dataset per shape
found in it.

Within a dataset, files of one shape stack into one set of tables — the same
rule the loader applies to the twenty property tabs inside a workbook, carried
past the file boundary.

Shape does NOT decide identity. Two folders built on one template — a second
client, last year's deals — must stay separate, so each upload starts a new
dataset unless it is explicitly told to join an existing one.

WHERE THINGS LIVE, AND WHY
--------------------------
Files (the workbooks, the .duckdb) go to engine.storage — the same local-or-S3
backend the report builder uses, so switching to a bucket is a config change
rather than a rewrite. They must not live on one worker's filesystem:
production runs four workers and a file written by one is invisible to the
other three.

Definitions (what a column MEANS) go to Snowflake as rows, for the same reason
saved dashboards do — config/metadata is baked into the image with no volume,
so anything written there disappears on the next redeploy.

THE FINGERPRINT IS AN INDEX, NOT A CONTRACT
-------------------------------------------
A saved definition is looked up by shape fingerprint. When it hits, the mapper
is skipped entirely and the previous answers apply — what makes the tenth rent
roll instant and the hundredth free. When it misses, nothing breaks: the mapper
runs as it did the first time. So a template that drifts costs one re-mapping,
not a wrong answer.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable

from app.engine import excel_mapping, excel_materialize, excel_profiler
from app.engine import storage as storage_mod
from app.engine.connectors.duckdb import client as duckdb_client
from app.engine.llm import LLMError

log = logging.getLogger(__name__)

#: Snowflake tables holding dataset metadata and the definitions learned from
#: uploaded workbooks. Fully qualified, like every other table this app writes:
#: engine connections carry no current database, so an unqualified name fails
#: with "This session does not have a current database".
DATASETS_TABLE = os.environ.get(
    "MCP_EXCEL_DATASETS_TABLE", "LOAN.PUBLIC.MCP_EXCEL_DATASETS"
)
DEFINITIONS_TABLE = os.environ.get(
    "MCP_EXCEL_DEFINITIONS_TABLE", "LOAN.PUBLIC.MCP_EXCEL_DEFINITIONS"
)
#: The named folders the library shows. A collection is what somebody uploaded;
#: the datasets under it are how it had to be stored. Keeping the name here
#: rather than deriving it from the files is what makes it renameable, and gives
#: other kinds of source somewhere to attach.
COLLECTIONS_TABLE = os.environ.get(
    "MCP_EXCEL_COLLECTIONS_TABLE", "LOAN.PUBLIC.MCP_EXCEL_COLLECTIONS"
)

_tables_ready = False


# ── identity and storage keys ─────────────────────────────────────────────────


def new_collection_id() -> str:
    """A fresh collection identity, one per upload."""
    return uuid.uuid4().hex[:16]


def new_dataset_id() -> str:
    """A fresh dataset identity.

    A dataset is a collection somebody chose to upload together, not a shape.
    Two folders built on one template — a second client, last year's deals —
    have the same fingerprint and must still be separate sets of data.

    The fingerprint keys the learned DEFINITION instead, which is where reuse
    is wanted: a shape seen before costs no model call whichever dataset the
    file lands in.
    """
    return uuid.uuid4().hex[:16]


def workbook_key(dataset_id: str, filename: str) -> str:
    return f"excel_sources/{dataset_id}/files/{filename}"


def database_key(dataset_id: str) -> str:
    return f"excel_sources/{dataset_id}/data.duckdb"


def documents_key(collection_id: str) -> str:
    """Where a collection's documents live.

    On the COLLECTION, not on a dataset: a PDF has no shape to be keyed by, and
    a deal folder's documents belong to the folder rather than to whichever
    spreadsheet happened to arrive with them.
    """
    return f"collections/{collection_id}/documents.duckdb"


def document_key(collection_id: str, filename: str) -> str:
    return f"collections/{collection_id}/files/{filename}"


# ── result types ──────────────────────────────────────────────────────────────


@dataclass
class SourceTable:
    name: str
    row_count: int
    columns: list[str]
    sheets: list[str]
    grain: str = ""
    purpose: str = ""
    #: Columns that are all-NULL because the workbook's formulas carry no saved
    #: results. Kept so the model can be told the figures are UNREADABLE rather
    #: than zero, and compute them from the source tables instead.
    uncalculated_columns: list[str] = field(default_factory=list)
    #: Columns whose source values existed but would not convert to the
    #: declared type, so they loaded as NULL. Same symptom from the user's
    #: side — a column of blanks with no reason given.
    unreadable_columns: list[str] = field(default_factory=list)
    #: Columns of this table's companion `<name>_derived`, holding the figures
    #: the spreadsheet computed for itself. Named so the model can select them;
    #: a table it is only told exists is a table it cannot use.
    derived_columns: list[str] = field(default_factory=list)


@dataclass
class DatasetFile:
    """One workbook that has been loaded into a dataset."""

    filename: str
    as_of: str | None = None
    #: Where the period came from — "sheet", "filename", or "" when unknown.
    #: Surfaced because a period read off a file name is weaker evidence, and a
    #: user correcting one should be able to see which files were guessed at.
    as_of_source: str = ""
    row_count: int = 0
    uploaded_by: str = ""
    uploaded_at: str = ""
    #: The folder this file was picked from. Recorded per file rather than on
    #: the dataset so it needs no column of its own: a folder that fans out into
    #: several datasets can still be shown as one group in the library.
    folder: str = ""


@dataclass
class Dataset:
    """Every file of one shape, queryable as a single set of tables."""

    dataset_id: str
    name: str
    fingerprint: str
    #: The collection this belongs to. Empty on datasets loaded before
    #: collections existed; those are grouped by their files' folder instead.
    collection_id: str = ""
    tables: list[SourceTable] = field(default_factory=list)
    files: list[DatasetFile] = field(default_factory=list)
    sheet_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    database_size: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.row_count for t in self.tables)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def folder(self) -> str:
        """The folder this dataset came from, when every file agrees on one."""
        folders = {f.folder for f in self.files if f.folder}
        return folders.pop() if len(folders) == 1 else ""

    @property
    def period_range(self) -> tuple[str | None, str | None]:
        """Earliest and latest period across this dataset's files."""
        periods = sorted(f.as_of for f in self.files if f.as_of)
        return (periods[0], periods[-1]) if periods else (None, None)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_rows"] = self.total_rows
        data["file_count"] = self.file_count
        data["folder"] = self.folder
        earliest, latest = self.period_range
        data["period_start"] = earliest
        data["period_end"] = latest
        return data


@dataclass
class FileOutcome:
    """What happened to one file of an upload."""

    filename: str
    status: str  # "loaded" | "skipped" | "failed"
    dataset_id: str = ""
    dataset_name: str = ""
    as_of: str | None = None
    as_of_source: str = ""
    row_count: int = 0
    detail: str = ""


@dataclass
class BatchResult:
    datasets: list[Dataset] = field(default_factory=list)
    files: list[FileOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: The documents that arrived in the same upload. A deal folder holds a
    #: rent roll and its mortgage, and splitting the two into separate uploads
    #: is exactly what this is meant to stop.
    documents: list[Any] = field(default_factory=list)
    #: The folder everything landed in, so the UI can offer to ask across it.
    collection_id: str = ""

    @property
    def undated(self) -> list[FileOutcome]:
        """Files that loaded but carry no period, so cannot be trended yet."""
        return [f for f in self.files if f.status == "loaded" and not f.as_of]

    def as_dict(self) -> dict[str, Any]:
        return {
            "datasets": [d.as_dict() for d in self.datasets],
            "files": [asdict(f) for f in self.files],
            "warnings": self.warnings,
            "undated": [asdict(f) for f in self.undated],
            "documents": [asdict(d) for d in self.documents],
            "collection_id": self.collection_id,
        }


# ── Snowflake persistence ─────────────────────────────────────────────────────


def _ensure_tables() -> bool:
    """Create the metadata tables if they are missing.

    Degrades to session-only behaviour rather than failing the upload if the
    signed-in role cannot create them — but the caller is told, so a
    misconfigured deployment reports itself instead of silently discarding
    every upload.
    """
    global _tables_ready
    if _tables_ready:
        return True

    from app.engine.connectors.snowflake import client as snowflake_client

    statements = [
        f"""CREATE TABLE IF NOT EXISTS {DATASETS_TABLE} (
            DATASET_ID STRING, DATASET_NAME STRING, FINGERPRINT STRING,
            SHEET_COUNT NUMBER, TABLES VARIANT, FILES VARIANT,
            DATABASE_SIZE NUMBER, CREATED_AT TIMESTAMP_NTZ,
            UPDATED_AT TIMESTAMP_NTZ, IS_ACTIVE BOOLEAN,
            COLLECTION_ID STRING
        )""",
        f"""CREATE TABLE IF NOT EXISTS {DEFINITIONS_TABLE} (
            FINGERPRINT STRING, MAPPING VARIANT, CONCEPTS VARIANT,
            UPDATED_BY STRING, UPDATED_AT TIMESTAMP_NTZ
        )""",
        f"""CREATE TABLE IF NOT EXISTS {COLLECTIONS_TABLE} (
            COLLECTION_ID STRING, NAME STRING, CREATED_AT TIMESTAMP_NTZ,
            UPDATED_AT TIMESTAMP_NTZ, IS_ACTIVE BOOLEAN
        )""",
        # Added after the datasets table shipped, so an existing deployment
        # needs it bolted on rather than created.
        f"ALTER TABLE {DATASETS_TABLE} ADD COLUMN IF NOT EXISTS "
        "COLLECTION_ID STRING",
    ]
    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        finally:
            cursor.close()
        _tables_ready = True
        return True
    except Exception as exc:
        log.warning("Could not create Excel dataset tables: %s", exc)
        return False


class PersistenceUnavailable(RuntimeError):
    """The metadata tables cannot be written.

    Raised rather than swallowed: an upload that cannot be recorded looks
    successful, redirects the user, and then does not appear anywhere — which
    is a far worse experience than being told the deployment is misconfigured.
    """


def _save_definition(fingerprint: str, mapping: dict[str, Any], owner: str) -> None:
    """Remember what this shape's fields mean, replacing any earlier answer."""
    if not _ensure_tables():
        return
    from app.engine.connectors.snowflake import client as snowflake_client

    concepts = sorted(
        {
            str(f.get("concept"))
            for region in mapping.get("regions") or []
            for f in region.get("fields") or []
            if f.get("concept")
        }
    )
    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"DELETE FROM {DEFINITIONS_TABLE} WHERE FINGERPRINT = %s",
                (fingerprint,),
            )
            cursor.execute(
                f"INSERT INTO {DEFINITIONS_TABLE} "
                "(FINGERPRINT, MAPPING, CONCEPTS, UPDATED_BY, UPDATED_AT) "
                # SYSDATE(): UTC. CURRENT_TIMESTAMP() is the session's wall
                # clock, and these columns are TIMESTAMP_NTZ -- no zone kept.
                "SELECT %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, SYSDATE()",
                (fingerprint, json.dumps(mapping), json.dumps(concepts), owner),
            )
        finally:
            cursor.close()
    except Exception:
        log.exception("Could not save definitions for %s", fingerprint[:12])


def load_definition(fingerprint: str) -> dict[str, Any] | None:
    """The mapping previously agreed for this shape, if any."""
    if not _ensure_tables():
        return None
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT MAPPING FROM {DEFINITIONS_TABLE} WHERE FINGERPRINT = %s "
                "ORDER BY UPDATED_AT DESC LIMIT 1",
                (fingerprint,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row and row[0]:
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        log.exception("Could not load definitions for %s", fingerprint[:12])
    return None


def known_concepts() -> list[str]:
    """Every concept the firm's dictionary already holds.

    Passed into the mapper so a new workbook attaches to existing meaning
    rather than coining synonyms. Measured on the sample rent roll, this is the
    difference between 50 new concepts and none.
    """
    if not _ensure_tables():
        return []
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT CONCEPTS FROM {DEFINITIONS_TABLE}")
            rows = cursor.fetchall()
        finally:
            cursor.close()
        found: set[str] = set()
        for row in rows:
            raw = row[0]
            if not raw:
                continue
            values = json.loads(raw) if isinstance(raw, str) else raw
            found.update(str(v) for v in values or [])
        return sorted(found)
    except Exception:
        log.exception("Could not read the concept dictionary")
        return []


def _save_dataset(dataset: Dataset) -> None:
    """Write a dataset's current state, replacing any earlier row for it."""
    if not _ensure_tables():
        raise PersistenceUnavailable(
            "The spreadsheet metadata tables could not be created or written. "
            "The signed-in Snowflake role needs CREATE/INSERT on "
            f"{DATASETS_TABLE} and {DEFINITIONS_TABLE}."
        )
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"DELETE FROM {DATASETS_TABLE} WHERE DATASET_ID = %s",
                (dataset.dataset_id,),
            )
            cursor.execute(
                f"INSERT INTO {DATASETS_TABLE} "
                "(DATASET_ID, DATASET_NAME, FINGERPRINT, SHEET_COUNT, TABLES, "
                "FILES, DATABASE_SIZE, CREATED_AT, UPDATED_AT, IS_ACTIVE, "
                "COLLECTION_ID) "
                "SELECT %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, "
                "%s, SYSDATE(), TRUE, %s",
                (
                    dataset.dataset_id, dataset.name, dataset.fingerprint,
                    dataset.sheet_count,
                    json.dumps([asdict(t) for t in dataset.tables]),
                    json.dumps([asdict(f) for f in dataset.files]),
                    dataset.database_size,
                    dataset.created_at or datetime.now(UTC).isoformat(),
                    dataset.collection_id,
                ),
            )
        finally:
            cursor.close()
    except PersistenceUnavailable:
        raise
    except Exception as exc:
        log.exception("Could not save dataset %s", dataset.dataset_id)
        raise PersistenceUnavailable(f"Could not record the upload: {exc}") from exc


def list_datasets() -> list[Dataset]:
    """Every dataset, most recently updated first.

    Shared, not per-user. These are the firm's documents: five analysts each
    keeping a private copy of the same rent roll would mean five answers to one
    question, and the shape dictionary would learn nothing across them.
    """
    if not _ensure_tables():
        return []
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT DATASET_ID, DATASET_NAME, FINGERPRINT, SHEET_COUNT, "
                "TABLES, FILES, DATABASE_SIZE, CREATED_AT, UPDATED_AT, "
                "COLLECTION_ID "
                f"FROM {DATASETS_TABLE} WHERE IS_ACTIVE = TRUE "
                "ORDER BY UPDATED_AT DESC"
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
    except Exception:
        log.exception("Could not list datasets")
        return []

    def _json(raw: Any) -> list:
        if not raw:
            return []
        return json.loads(raw) if isinstance(raw, str) else raw

    datasets: list[Dataset] = []
    for row in rows:
        datasets.append(
            Dataset(
                dataset_id=row[0], name=row[1], fingerprint=row[2],
                sheet_count=int(row[3] or 0),
                tables=[SourceTable(**t) for t in _json(row[4])],
                files=[DatasetFile(**f) for f in _json(row[5])],
                database_size=int(row[6] or 0),
                created_at=row[7].isoformat() if row[7] else "",
                updated_at=row[8].isoformat() if row[8] else "",
                collection_id=row[9] or "",
            )
        )
    return datasets


def get_dataset(dataset_id: str) -> Dataset | None:
    for dataset in list_datasets():
        if dataset.dataset_id == dataset_id:
            return dataset
    return None


def delete_dataset(dataset_id: str) -> bool:
    """Retire a dataset and every file in it.

    Deactivated rather than dropped, matching saved dashboards — a deletion
    made in error stays recoverable by whoever administers the warehouse. The
    stored files are left in place; only this worker's cached database is
    cleared, which is safe because a deactivated dataset cannot be queried.
    """
    if not _ensure_tables():
        return False
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE {DATASETS_TABLE} SET IS_ACTIVE = FALSE "
                "WHERE DATASET_ID = %s AND IS_ACTIVE = TRUE",
                (dataset_id,),
            )
            removed = (cursor.rowcount or 0) > 0
        finally:
            cursor.close()
    except Exception:
        log.exception("Could not delete dataset %s", dataset_id)
        return False

    if removed:
        cached = duckdb_client.CACHE_DIR / f"{dataset_id}.duckdb"
        try:
            cached.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not clear cached database for %s", dataset_id)
    return removed


@dataclass
class FileRemoval:
    """The outcome of taking one file back out of a dataset."""

    removed: bool
    #: The dataset as it now stands, or None when that file was the last one and
    #: the dataset went with it.
    dataset: Dataset | None = None
    dataset_deleted: bool = False


def remove_file(dataset_id: str, filename: str) -> FileRemoval:
    """Take one workbook's rows back out of a dataset.

    Every row carries the file it came from, so this is a delete rather than a
    rebuild — the other files are untouched and nothing is re-read or re-mapped.

    Removing the last file removes the dataset: a dataset with no files answers
    every question with nothing, which is worse than not being in the list.
    """
    dataset = get_dataset(dataset_id)
    if dataset is None:
        return FileRemoval(removed=False)
    if not any(f.filename == filename for f in dataset.files):
        return FileRemoval(removed=False)

    remaining = [f for f in dataset.files if f.filename != filename]
    if not remaining:
        delete_dataset(dataset_id)
        return FileRemoval(removed=True, dataset_deleted=True)

    import duckdb

    store = storage_mod.get_storage()
    working = Path(tempfile.gettempdir()) / f"{dataset_id}.edit.duckdb"
    working.unlink(missing_ok=True)
    try:
        working.write_bytes(store.get(database_key(dataset_id)))
        conn = duckdb.connect(str(working))
        try:
            for (table,) in conn.execute("SHOW TABLES").fetchall():
                columns = {
                    row[1]
                    for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                if "source_file" in columns:
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE source_file = ?', [filename]
                    )
            conn.commit()
        finally:
            conn.close()

        tables = _apply_actual_row_counts(working, list(dataset.tables))
        database_bytes = working.read_bytes()
        store.put(database_key(dataset_id), database_bytes)
    except Exception as exc:
        log.exception("Could not remove %s from %s", filename, dataset_id)
        raise PersistenceUnavailable(f"Could not remove that file: {exc}") from exc
    finally:
        working.unlink(missing_ok=True)

    dataset.files = remaining
    dataset.tables = tables
    dataset.database_size = len(database_bytes)
    dataset.updated_at = datetime.now(UTC).isoformat()
    _save_dataset(dataset)

    try:
        (duckdb_client.CACHE_DIR / f"{dataset_id}.duckdb").unlink(missing_ok=True)
    except OSError:
        pass
    return FileRemoval(removed=True, dataset=dataset)


# ── collections ───────────────────────────────────────────────────────────────

#: Prefixes for the identity of a collection that has never been named. Datasets
#: loaded before collections existed have no collection of their own, so they are
#: grouped on the fly — by the folder they came from, or alone. Renaming one
#: turns it into a real collection; until then there is nothing to store.
_FOLDER_ID = "folder:"
_DATASET_ID = "dataset:"


@dataclass
class Collection:
    """What somebody uploaded, holding whatever it turned into.

    One folder of same-shaped rent rolls is a collection of one dataset; a mixed
    folder is a collection of several. The name belongs to the user, which is
    why it is stored rather than derived.
    """

    collection_id: str
    name: str
    datasets: list[Dataset] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_named(self) -> bool:
        """False while this is still grouped on the fly rather than stored."""
        return not self.collection_id.startswith((_FOLDER_ID, _DATASET_ID))

    @property
    def file_count(self) -> int:
        return sum(d.file_count for d in self.datasets)

    @property
    def total_rows(self) -> int:
        return sum(d.total_rows for d in self.datasets)

    @property
    def period_range(self) -> tuple[str | None, str | None]:
        periods = sorted(
            p for d in self.datasets for p in d.period_range if p
        )
        return (periods[0], periods[-1]) if periods else (None, None)


def _collection_names() -> dict[str, tuple[str, str, str]]:
    """Stored collection names, by id."""
    if not _ensure_tables():
        return {}
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COLLECTION_ID, NAME, CREATED_AT, UPDATED_AT "
                f"FROM {COLLECTIONS_TABLE} WHERE IS_ACTIVE = TRUE"
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
    except Exception:
        log.exception("Could not read collections")
        return {}
    return {
        row[0]: (
            row[1],
            row[2].isoformat() if row[2] else "",
            row[3].isoformat() if row[3] else "",
        )
        for row in rows
    }


def list_collections() -> list[Collection]:
    """The library, as the user thinks of it: folders holding sources.

    A dataset with no collection of its own is grouped by the folder its files
    came from, and stands alone when there is not one. That keeps everything
    loaded before collections existed looking exactly as it did.
    """
    stored = _collection_names()
    groups: dict[str, Collection] = {}
    order: list[str] = []

    for dataset in list_datasets():
        if dataset.collection_id:
            identity = dataset.collection_id
            fallback = dataset.name
        elif dataset.folder:
            identity = f"{_FOLDER_ID}{dataset.folder}"
            fallback = dataset.folder
        else:
            identity = f"{_DATASET_ID}{dataset.dataset_id}"
            fallback = dataset.name

        collection = groups.get(identity)
        if collection is None:
            name, created, updated = stored.get(identity, (fallback, "", ""))
            collection = Collection(
                collection_id=identity,
                name=name,
                created_at=created or dataset.created_at,
                updated_at=updated or dataset.updated_at,
            )
            groups[identity] = collection
            order.append(identity)
        collection.datasets.append(dataset)
        # list_datasets is newest first, so the first one seen is the newest.
        collection.updated_at = max(collection.updated_at, dataset.updated_at)

    # A folder of documents alone has no dataset to be found through, and
    # enumerating only datasets made it disappear from the library entirely.
    # Its database is opened to check, which is affordable only because a
    # collection with no datasets at all is the rare case.
    from app.engine import collection_documents

    for identity, (name, created, updated) in stored.items():
        if identity in groups:
            continue
        if not collection_documents.list_documents(identity):
            # Nothing left in it: the datasets were deleted and the documents
            # removed. Keeping the row would offer a folder that answers
            # every question with nothing.
            continue
        groups[identity] = Collection(
            collection_id=identity, name=name,
            created_at=created, updated_at=updated,
        )
        order.append(identity)

    return [groups[identity] for identity in order]


def deactivate_collection(collection_id: str) -> bool:
    """Retire a collection that holds nothing any more."""
    if not _ensure_tables():
        return False
    from app.engine.connectors.snowflake import client as snowflake_client

    try:
        conn = snowflake_client.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE {COLLECTIONS_TABLE} SET IS_ACTIVE = FALSE "
                "WHERE COLLECTION_ID = %s AND IS_ACTIVE = TRUE",
                (collection_id,),
            )
            return (cursor.rowcount or 0) > 0
        finally:
            cursor.close()
    except Exception:
        log.exception("Could not retire collection %s", collection_id)
        return False


def delete_collection(collection_id: str) -> bool:
    """Retire a whole folder: every dataset in it, and its documents.

    Deleting a folder one dataset at a time is the only thing available today
    when the folder fanned out into several, which is exactly the case where
    the datasets are least worth picking through individually.
    """
    collection = get_collection(collection_id)
    if collection is None:
        return False

    for dataset in collection.datasets:
        delete_dataset(dataset.dataset_id)

    from app.engine.connectors.duckdb import client as duckdb_client

    try:
        (duckdb_client.CACHE_DIR / f"docs_{collection_id}.duckdb").unlink(
            missing_ok=True
        )
    except OSError:
        pass

    # Stored files are left in place, matching delete_dataset: a deletion made
    # in error stays recoverable by whoever administers the warehouse.
    deactivate_collection(collection_id)
    return True


def rename_collection(collection_id: str, name: str) -> Collection | None:
    """Give a collection the name the user wants to see.

    Renaming one that was only ever grouped on the fly promotes it: a real
    collection is written and its datasets are pointed at it, so the name
    survives a file being added or removed later.
    """
    name = name.strip()[:120]
    if not name:
        return None

    collections = {c.collection_id: c for c in list_collections()}
    collection = collections.get(collection_id)
    if collection is None:
        return None

    identity = collection_id
    if not collection.is_named:
        identity = new_collection_id()
        for dataset in collection.datasets:
            dataset.collection_id = identity
            _save_dataset(dataset)

    try:
        _write_collection_name(identity, name)
    except Exception as exc:
        log.exception("Could not rename collection %s", collection_id)
        raise PersistenceUnavailable(f"Could not rename that folder: {exc}") from exc

    collection.collection_id = identity
    collection.name = name
    return collection


# ── ingestion ─────────────────────────────────────────────────────────────────


@dataclass
class _Prepared:
    """One file, read and understood, ready to load."""

    filename: str
    data: bytes
    path: Path
    profile: Any
    grids: list
    fingerprint: str
    as_of: date | None
    as_of_source: str
    #: The folder this file was picked from, when it came from one. Names the
    #: dataset, so two collections on the same template are told apart by where
    #: they came from rather than by whichever file happened to sort first.
    folder: str = ""


def _folder_of(relative_path: str) -> str:
    """The folder a picked file sits under, from its browser-relative path."""
    parts = Path(relative_path).parts
    return parts[0] if len(parts) > 1 else ""


def _read_period(profile, filename: str) -> tuple[date | None, str]:
    """The period a file covers, and where that came from.

    The sheet wins over the file name: a name is what somebody typed when
    saving, the sheet is what the report asserts about itself.
    """
    stated = [s.as_of for s in profile.sheets if s.as_of]
    if stated:
        return max(stated), "sheet"
    from_name = excel_profiler.as_of_from_filename(filename)
    if from_name:
        return from_name, "filename"
    return None, ""


def ingest_files(
    files: list[tuple[str, bytes]],
    owner: str = "",
    on_progress: Callable[[int, str], None] | None = None,
    add_to: str | None = None,
    add_to_collection: str | None = None,
) -> BatchResult:
    """Load a folder of workbooks into datasets, one per shape in the upload.

    Files are read and fingerprinted FIRST, then loaded shape by shape, so
    every file of one dataset is written against the same mapping and the
    mapper runs once per shape rather than once per file. Twelve monthly rent
    rolls therefore cost one model call, not twelve.

    `add_to_collection` names a FOLDER to add to rather than starting a new
    one. Files matching a shape already in that folder join it; files of a new
    shape become a new dataset inside it; documents join its graph. That is
    what "add files to this folder" means, and it is the only form the UI
    offers — the things inside a folder are files grouped by shape, not folders
    of their own.

    `add_to` is the older, narrower form naming a single dataset.

    Documents in the same selection go to the collection alongside the
    datasets. A deal folder holds a rent roll and its mortgage, and the whole
    point is that they end up in one place.
    """
    from app.engine import collection_documents

    def report(percent: int, stage: str) -> None:
        if on_progress is not None:
            on_progress(percent, stage)

    result = BatchResult()
    if not files:
        return result

    # Split by kind first: a PDF has no shape to fingerprint, and a workbook is
    # not text to index.
    documents = [f for f in files if collection_documents.is_document(f[0])]
    files = [f for f in files if not collection_documents.is_document(f[0])]

    store = storage_mod.get_storage()

    with tempfile.TemporaryDirectory() as tmp:
        # ── pass 1: read and fingerprint everything ───────────────────────
        prepared: list[_Prepared] = []
        for index, (filename, data) in enumerate(files):
            report(
                int(5 + 25 * index / len(files)),
                f"Reading {filename} ({index + 1} of {len(files)})",
            )
            path = Path(tmp) / Path(filename).name
            try:
                path.write_bytes(data)
                grids = excel_profiler.read_grids(path)
                profile = excel_profiler.profile_workbook(path, grids=grids)
                as_of, as_of_source = _read_period(profile, filename)
                prepared.append(
                    _Prepared(
                        filename=Path(filename).name, data=data, path=path,
                        profile=profile, grids=grids,
                        fingerprint=profile.fingerprint,
                        as_of=as_of, as_of_source=as_of_source,
                        folder=_folder_of(filename),
                    )
                )
            except Exception as exc:
                log.warning("Could not read %s: %s", filename, exc)
                result.files.append(
                    FileOutcome(
                        filename=Path(filename).name, status="failed",
                        detail=str(exc)[:200],
                    )
                )

        if not prepared and not documents:
            result.warnings.append("None of the uploaded files could be read.")
            return result
        if not prepared:
            # Documents only: there is no shape to key a dataset on, so the
            # collection is created here rather than by _load_shape.
            collection_id = (
                target.collection_id
                if (target := (get_dataset(add_to) if add_to else None))
                and target.collection_id
                else new_collection_id()
            )
            result.collection_id = collection_id
            _ingest_collection_documents(
                collection_id, documents, owner, result, report
            )
            if not get_collection(collection_id):
                _name_documents_only_collection(collection_id, documents)
            report(100, "Ready")
            return result

        # ── group by shape ────────────────────────────────────────────────
        by_shape: dict[str, list[_Prepared]] = {}
        for item in prepared:
            by_shape.setdefault(item.fingerprint, []).append(item)

        shapes = len(by_shape)
        report(
            30,
            f"Read {len(prepared)} files, {shapes} distinct "
            f"shape{'s' if shapes != 1 else ''}",
        )

        # ── pass 2: one dataset per shape ─────────────────────────────────
        folder = get_collection(add_to_collection) if add_to_collection else None
        if add_to_collection and folder is None:
            result.warnings.append(
                "The folder these files were meant to join no longer exists, "
                "so they were loaded as a new one."
            )
        target = get_dataset(add_to) if add_to else None
        if add_to and target is None:
            result.warnings.append(
                "The dataset these files were meant to join no longer exists, "
                "so they were loaded as a new one."
            )
        # Every shape already in the target folder, so a file matching one joins
        # it instead of starting a second dataset holding the same thing.
        in_folder = {d.fingerprint: d for d in (folder.datasets if folder else [])}

        taken = {d.name for d in list_datasets()}
        # One collection per upload, unless told to join an existing one.
        collection_id = (
            (folder.collection_id if folder else None)
            or (target.collection_id if target and target.collection_id else None)
            or new_collection_id()
        )

        for shape_index, (fingerprint, items) in enumerate(by_shape.items()):
            base = 30 + int(60 * shape_index / shapes)
            existing = in_folder.get(fingerprint)
            if existing is None and target and target.fingerprint == fingerprint:
                existing = target
            dataset = _load_shape(
                fingerprint, items, owner, store, result,
                lambda pct, stage, _b=base: report(_b + pct, stage),
                existing=existing, taken=taken, use_folder=shapes == 1,
                collection_id=collection_id,
            )
            if dataset is not None:
                result.datasets.append(dataset)
                taken.add(dataset.name)

        if folder is None and target is None and result.datasets:
            _name_new_collection(collection_id, prepared, result.datasets)

        result.collection_id = collection_id
        if documents:
            _ingest_collection_documents(
                collection_id, documents, owner, result, report
            )

    report(100, "Ready")
    return result


def _name_new_collection(
    collection_id: str, items: list[_Prepared], datasets: list[Dataset]
) -> None:
    """Give a freshly created collection the name of what was uploaded.

    The folder is what the user picked, so it wins. Files chosen individually
    have no folder: one dataset can borrow its own name, and several cannot, so
    those fall back to the day they arrived — which is at least true, and the
    point of renaming is that the user fixes it.

    Made unique against the folders already in the library. Uploading the same
    folder twice is a thing people do, deliberately or by accident, and two
    rows reading "DA EXPRESS" with identical counts leaves no way to tell which
    is which or which to delete.
    """
    folders = {item.folder for item in items if item.folder}
    if len(folders) == 1:
        name = folders.pop().replace("_", " ").strip()[:120]
    elif len(datasets) == 1:
        name = datasets[0].name
    else:
        name = f"Upload {datetime.now(UTC).strftime('%d %b %Y')}"
    try:
        # Excluding this one: its datasets are already saved, so it is in the
        # listing under a fallback name and would otherwise bump itself.
        taken = {
            c.name for c in list_collections() if c.collection_id != collection_id
        }
        _write_collection_name(collection_id, _unique(name, taken))
    except Exception:
        # A folder with no stored name still groups and still renames; it just
        # shows its datasets' own naming until then. Not worth failing an
        # upload that otherwise succeeded.
        log.warning("Could not name collection %s", collection_id)


def _write_collection_name(collection_id: str, name: str) -> None:
    if not _ensure_tables():
        return
    from app.engine.connectors.snowflake import client as snowflake_client

    conn = snowflake_client.get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"DELETE FROM {COLLECTIONS_TABLE} WHERE COLLECTION_ID = %s",
            (collection_id,),
        )
        cursor.execute(
            f"INSERT INTO {COLLECTIONS_TABLE} "
            "(COLLECTION_ID, NAME, CREATED_AT, UPDATED_AT, IS_ACTIVE) "
            "SELECT %s, %s, SYSDATE(), SYSDATE(), TRUE",
            (collection_id, name),
        )
    finally:
        cursor.close()


def get_collection(collection_id: str):
    """One collection by id, or None."""
    for candidate in list_collections():
        if candidate.collection_id == collection_id:
            return candidate
    return None


def _ingest_collection_documents(
    collection_id: str,
    documents: list[tuple[str, bytes]],
    owner: str,
    result: BatchResult,
    report: Callable[[int, str], None],
) -> None:
    """Read the upload's documents into the same folder as its spreadsheets."""
    from app.engine import collection_documents

    report(92, f"Reading {len(documents)} document(s)")
    try:
        batch = collection_documents.ingest_documents(
            collection_id, documents, owner=owner,
            on_progress=lambda pct, stage: report(min(92 + pct // 12, 99), stage),
        )
    except Exception as exc:
        log.exception("Could not read documents for %s", collection_id)
        result.warnings.append(f"Could not read the documents: {exc}")
        return

    result.documents.extend(batch.documents)
    result.warnings.extend(batch.warnings)
    for filename, detail in batch.failed:
        result.files.append(
            FileOutcome(filename=filename, status="failed", detail=detail)
        )


def _name_documents_only_collection(
    collection_id: str, documents: list[tuple[str, bytes]]
) -> None:
    """Name a folder that arrived with no spreadsheets in it."""
    folders = {_folder_of(name) for name, _ in documents}
    folders.discard("")
    if len(folders) == 1:
        name = folders.pop().replace("_", " ").strip()[:120]
    elif len(documents) == 1:
        name = Path(documents[0][0]).stem.replace("_", " ")[:120]
    else:
        name = f"Upload {datetime.now(UTC).strftime('%d %b %Y')}"
    try:
        taken = {
            c.name for c in list_collections() if c.collection_id != collection_id
        }
        _write_collection_name(collection_id, _unique(name, taken))
    except Exception:
        log.warning("Could not name collection %s", collection_id)


def _load_shape(
    fingerprint: str,
    items: list[_Prepared],
    owner: str,
    store: Any,
    result: BatchResult,
    report: Callable[[int, str], None],
    existing: Dataset | None = None,
    taken: set[str] | None = None,
    use_folder: bool = True,
    collection_id: str = "",
) -> Dataset | None:
    """Load every file of one shape into a dataset, new unless one is given."""
    dataset_id = existing.dataset_id if existing else new_dataset_id()

    # A shape seen before needs no model at all.
    mapping = load_definition(fingerprint)
    if mapping is None:
        report(2, f"Working out the layout of {items[0].filename}")
        try:
            packet = excel_profiler.evidence_packet(items[0].profile)
            proposal = excel_mapping.propose_mapping(packet, known_concepts())
            mapping = proposal.as_dict()
            _save_definition(fingerprint, mapping, owner)
        except LLMError as exc:
            log.warning("Mapping unavailable for %s: %s", items[0].filename, exc)
            result.warnings.append(
                f"Could not work out what the fields mean in "
                f"{items[0].filename}: {exc}"
            )
            mapping = None
    else:
        report(2, "Recognised this layout — reusing saved definitions")

    # Load the dataset's database, appending to whatever is already there.
    database_path = Path(tempfile.gettempdir()) / f"{dataset_id}.build.duckdb"
    database_path.unlink(missing_ok=True)
    already_loaded = bool(existing)
    if already_loaded:
        try:
            database_path.write_bytes(store.get(database_key(dataset_id)))
        except Exception:
            log.warning("Dataset %s has no stored database; rebuilding", dataset_id)
            already_loaded = False

    # Merged across files, not reassigned per file: every file writes into the
    # same tables but reports only its own contribution.
    tables: dict[str, Any] = {t.name: t for t in (existing.tables if existing else [])}
    #: Table names this batch wrote, used to prune anything else.
    produced: set[str] = set()
    loaded_files: list[DatasetFile] = []
    ordered = sorted(items, key=lambda i: (i.as_of or date.min, i.filename))

    for index, item in enumerate(ordered):
        report(
            5 + int(50 * index / len(ordered)),
            f"Loading {item.filename} ({index + 1} of {len(ordered)})",
        )
        try:
            outcome = excel_materialize.materialize(
                item.path, item.profile, mapping, database_path,
                grids=item.grids,
                batch=excel_materialize.Batch(
                    source_file=item.filename,
                    as_of=item.as_of,
                    append=already_loaded or index > 0,
                ),
            )
            _merge_tables(tables, outcome.tables)
            produced.update(t.name for t in outcome.tables)
            rows = sum(t.row_count for t in outcome.tables)
            store.put(workbook_key(dataset_id, item.filename), item.data)
            loaded_files.append(
                DatasetFile(
                    filename=item.filename,
                    as_of=item.as_of.isoformat() if item.as_of else None,
                    as_of_source=item.as_of_source,
                    row_count=rows,
                    uploaded_by=owner,
                    uploaded_at=datetime.now(UTC).isoformat(),
                    folder=item.folder,
                )
            )
            result.files.append(
                FileOutcome(
                    filename=item.filename, status="loaded",
                    dataset_id=dataset_id,
                    as_of=item.as_of.isoformat() if item.as_of else None,
                    as_of_source=item.as_of_source, row_count=rows,
                )
            )
        except Exception as exc:
            log.exception("Could not load %s", item.filename)
            result.files.append(
                FileOutcome(
                    filename=item.filename, status="failed", detail=str(exc)[:200]
                )
            )

    if not loaded_files:
        return None

    _prune_stale_tables(database_path, produced)

    # materialize reports the rows IT wrote, which is one file's worth. The
    # dataset's real size is what is in the database after every file has been
    # appended, so read it back — and drop anything a rename or a failed load
    # left in the metadata but not in the database.
    table_list = _apply_actual_row_counts(database_path, list(tables.values()))

    report(60, "Saving")
    database_bytes = database_path.read_bytes()
    store.put(database_key(dataset_id), database_bytes)
    database_path.unlink(missing_ok=True)

    # Merge this batch's files with any already recorded, newest wins per name.
    merged: dict[str, DatasetFile] = {
        f.filename: f for f in (existing.files if existing else [])
    }
    for file in loaded_files:
        merged[file.filename] = file

    name = _resolve_name(existing, table_list, ordered, taken or set(), use_folder)
    dataset = Dataset(
        dataset_id=dataset_id,
        name=name,
        fingerprint=fingerprint,
        collection_id=(
            existing.collection_id if existing and existing.collection_id
            else collection_id
        ),
        tables=[
            SourceTable(
                name=t.name, row_count=t.row_count, columns=t.columns,
                sheets=t.sheets, grain=t.grain, purpose=t.purpose,
                uncalculated_columns=t.uncalculated_columns,
                unreadable_columns=t.unreadable_columns,
                derived_columns=list(getattr(t, "derived_columns", []) or []),
            )
            for t in table_list
        ],
        files=sorted(merged.values(), key=lambda f: f.as_of or ""),
        sheet_count=len(ordered[0].profile.sheets),
        created_at=existing.created_at if existing else datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        database_size=len(database_bytes),
    )
    _save_dataset(dataset)

    # A dataset needs to be discardable when the ingestion of a NEW file into
    # it changed the tables — the cached copy on this worker is now stale.
    try:
        (duckdb_client.CACHE_DIR / f"{dataset_id}.duckdb").unlink(missing_ok=True)
    except OSError:
        pass
    return dataset


def _merge_tables(into: dict[str, Any], produced: list[Any]) -> None:
    """Fold one file's tables into the dataset's.

    Where files disagree:

    * columns are unioned — a later file may carry a trailing column, and it is
      queryable either way;
    * `unreadable` is unioned — a column that failed to parse in any file holds
      nulls for that file's rows, so a total over it is wrong;
    * `uncalculated` is intersected — a formula result one file saved is missing
      only from the other file, not from the dataset.
    """
    for table in produced:
        prior = into.get(table.name)
        if prior is None:
            into[table.name] = table
            continue
        for column in table.columns:
            if column not in prior.columns:
                prior.columns.append(column)
        prior.unreadable_columns = sorted(
            set(prior.unreadable_columns) | set(table.unreadable_columns)
        )
        prior.uncalculated_columns = sorted(
            set(prior.uncalculated_columns) & set(table.uncalculated_columns)
        )
        prior.grain = prior.grain or table.grain
        prior.purpose = prior.purpose or table.purpose
        merged_derived = list(getattr(prior, "derived_columns", []) or [])
        for column in getattr(table, "derived_columns", []) or []:
            if column not in merged_derived:
                merged_derived.append(column)
        if merged_derived:
            prior.derived_columns = merged_derived


def _prune_stale_tables(database_path: Path, produced: set[str]) -> None:
    """Drop tables left in a dataset's database by an earlier naming scheme.

    A dataset is one shape, so every file writes the same tables. Anything else
    holds a partial copy of the same rows and can still be queried, so it has to
    go rather than sit alongside.

    `<name>_derived` companions are kept: they are written without ever being
    reported as tables in their own right.
    """
    if not produced:
        return
    import duckdb

    keep = produced | {f"{name}_derived" for name in produced}
    conn = duckdb.connect(str(database_path))
    try:
        existing = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        for name in sorted(existing - keep):
            log.info("Dropping stale table %s from %s", name, database_path.name)
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.commit()
    except Exception:
        # Not fatal: the dataset is still correct, just carrying dead weight.
        log.warning("Could not prune stale tables from %s", database_path.name)
    finally:
        conn.close()


def _apply_actual_row_counts(database_path: Path, tables: list[Any]) -> list[Any]:
    """Replace per-file row counts with the dataset's real totals.

    Also records which sheets the whole dataset spans, so the library and the
    model's context describe what is actually queryable rather than what the
    last upload happened to contain.

    Returns only the tables that really exist in the database. A table carried
    over from an earlier upload that this batch no longer produces would
    otherwise keep its stale count and be offered to the model as queryable.
    """
    import duckdb

    live: list[Any] = []
    conn = duckdb.connect(str(database_path), read_only=True)
    try:
        for table in tables:
            try:
                table.row_count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table.name}"'
                ).fetchone()[0]
                table.sheets = sorted(
                    row[0]
                    for row in conn.execute(
                        f'SELECT DISTINCT source_sheet FROM "{table.name}"'
                    ).fetchall()
                    if row[0]
                )
                live.append(table)
            except Exception:  # not in this database
                continue
    finally:
        conn.close()
    return live


def _resolve_name(
    existing: Dataset | None,
    tables: list[Any],
    items: list[_Prepared],
    taken: set[str],
    use_folder: bool = True,
) -> str:
    """A dataset's display name.

    Adding files never renames: the name belongs to the collection, and the
    person adding March to a set called after its folder did not ask for it to
    become something else.
    """
    if existing and existing.name:
        return existing.name
    return _unique(_name_for(tables, items, use_folder), taken)


def _titled(table_name: str) -> str:
    return table_name.replace("_", " ").strip().title()


def _name_for(
    tables: list[Any], items: list[_Prepared], use_folder: bool = True
) -> str:
    """A readable name for a dataset.

    The folder is the best name when the whole upload became one dataset: it is
    what the user chose, and it tells two collections of one template apart
    where their shared table name would not.

    It is the worst name when a folder fans out into several datasets, since
    every one of them would claim it. Those are named from their own content
    instead.
    """
    folders = {item.folder for item in items if item.folder}
    if use_folder and len(folders) == 1:
        return folders.pop().replace("_", " ").strip()[:80]
    if tables:
        biggest = max(tables, key=lambda t: t.row_count)
        anonymous = ("records_", "entity_", "c_", excel_materialize.SHAPE_NAME_PREFIX)
        if biggest.name and not biggest.name.startswith(anonymous):
            return _titled(biggest.name)
    return Path(items[0].filename).stem.replace("_", " ")[:80]


def _unique(name: str, taken: set[str]) -> str:
    """Keep two datasets of one shape distinguishable in the library."""
    if name not in taken:
        return name
    suffix = 2
    while f"{name} ({suffix})" in taken:
        suffix += 1
    return f"{name} ({suffix})"


def local_database(dataset: Dataset) -> Path:
    """This worker's cached copy of a dataset's database."""
    store = storage_mod.get_storage()
    return duckdb_client.ensure_cached(
        dataset.dataset_id,
        lambda: store.get(database_key(dataset.dataset_id)),
        expected_size=dataset.database_size or None,
    )


def schema_context(dataset: Dataset) -> str:
    """What the model needs to know to write SQL against this dataset.

    The same job metadata_loader.build_context does for a Snowflake database,
    from the definitions agreed for this shape — so the agent asks about a
    spreadsheet exactly as it asks about a warehouse.
    """
    earliest, latest = dataset.period_range
    lines = [f"# {dataset.name} (uploaded spreadsheets)"]
    lines.append(
        f"{dataset.file_count} file(s), {len(dataset.tables)} tables, "
        f"{dataset.total_rows} rows."
        + (f" Periods {earliest} to {latest}." if earliest else "")
    )
    # Stated first and bluntly, because the general SQL guidance says to write
    # DATABASE.SCHEMA.TABLE. That is right for the warehouse and wrong here — a
    # qualified name does not parse, and the model will otherwise retry it
    # until the loop's iteration ceiling stops it.
    lines.append(
        "\n## Writing SQL against this source\n"
        "This is a DuckDB database holding uploaded spreadsheets. Use BARE "
        "table names exactly as listed below. Do NOT qualify them with a "
        "database or schema: there is no DATABASE.SCHEMA.TABLE form here and a "
        "qualified name is a syntax error. Date literals are ISO ('2026-01-31')."
    )
    lines.append(
        "\nEvery table carries these provenance columns:\n"
        "  as_of        the period the file covers — GROUP BY this to trend\n"
        "  source_file  which workbook the row came from\n"
        "  source_sheet which sheet within it\n"
        "  source_row   which row on that sheet\n"
        "Use them to answer 'where did this come from', and to compare periods."
    )
    if dataset.file_count > 1:
        names = ", ".join(f.filename for f in dataset.files[:12])
        more = "" if dataset.file_count <= 12 else f", and {dataset.file_count - 12} more"
        lines.append(
            f"\nThis dataset holds {dataset.file_count} files of the same shape, "
            f"ALL STACKED INTO THE TABLES BELOW: {names}{more}.\n"
            "Every one of those files is already included in a plain query over "
            "these tables. Do NOT scope an answer to one file, and do NOT say a "
            "figure covers only part of the set, unless the question asked for "
            "that — then filter on source_file. A question about change over "
            "time is a GROUP BY as_of, not a comparison of separate tables."
        )
        if earliest and earliest == latest:
            lines.append(
                f"These files all cover the same period ({earliest}), so they "
                "differ by subject rather than by date — a total 'across all of "
                "them' is a single un-filtered aggregate, and a breakdown is a "
                "GROUP BY on the column that distinguishes them (or source_file)."
            )
    undated = [f.filename for f in dataset.files if not f.as_of]
    if undated:
        lines.append(
            f"\n{len(undated)} file(s) have no period recorded "
            f"({', '.join(undated[:3])}). Their rows have as_of NULL and are "
            "excluded from any trend — say so if a time question depends on them."
        )
    for table in dataset.tables:
        lines.append(f"\n### {table.name}")
        if table.purpose:
            lines.append(table.purpose)
        if table.grain:
            lines.append(f"Grain: {table.grain}")
        lines.append(f"Rows: {table.row_count}. Sheets: {', '.join(table.sheets[:6])}")
        if table.columns:
            lines.append("Columns: " + ", ".join(table.columns))
        if table.derived_columns:
            lines.append(
                f"Companion table {table.name}_derived holds the figures the "
                "spreadsheet computed for itself: "
                + ", ".join(table.derived_columns)
                + ". Query it to cross-check a total you calculated, never to "
                "add to the rows it was calculated from."
            )
        if table.unreadable_columns:
            lines.append(
                "COULD NOT BE READ — "
                + ", ".join(table.unreadable_columns)
                + ". The spreadsheet holds values here, but they did not match "
                "the expected type and loaded as NULL. Treat them as unknown, "
                "never as zero or absent, and say so if a question depends on them."
            )
        if table.uncalculated_columns:
            lines.append(
                "UNAVAILABLE in this file — "
                + ", ".join(table.uncalculated_columns)
                + ". These are spreadsheet formulas whose results were never "
                "saved, so they are NULL here and are NOT zero. Never report "
                "them as 0, empty, or 'no data'. Derive the figure from the "
                "underlying detail tables instead, say that you did, and tell "
                "the user the workbook needs opening and saving in Excel."
            )
    lines.append(
        "\nDerived figures the spreadsheets computed themselves (subtotals, "
        "NOI, ratios) are in companion `*_derived` tables. Never add them to "
        "the rows they were computed from."
    )
    return "\n".join(lines)

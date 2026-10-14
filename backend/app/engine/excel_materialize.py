"""Turn a profiled workbook into queryable tables in DuckDB.

This is the step that makes an uploaded spreadsheet behave like a database. The
profiler said where the data is; the mapper said what it means; this writes the
rows so that every number a user later sees is computed by a query engine rather
than read out of a cell by a model. That is the whole reason the pipeline exists
in this order — Phil's requirement was numbers that are "exact, sourced, and
traced", and a model summing 196 rent rows satisfies none of the three.

TWELVE FILES, ONE TABLE
-----------------------
Sheets that share a shape are stacked into a SINGLE table with `source_sheet`
and `source_row` columns, not one table per sheet. A 20-tab rent roll becomes
one 1,895-row table, so "rent by property" is a GROUP BY rather than a 20-way
UNION the model has to write correctly every time. The same applies across
uploads later: next month's file appends, and a trend becomes a date filter.

`source_sheet` and `source_row` are not bookkeeping. They are how a number on
screen leads back to the cell it came from.

DERIVED ROWS ARE NOT DATA
-------------------------
Subtotals, totals and computed ratios are excluded from the row set. Loading
"Total operating expenses" alongside the seven expense lines it sums would
double every total anyone asks for. The profiler already identifies them —
by formula, or by a "Total" label — and the mapper marks the concept derived.
They are kept in a separate `_derived` table so a question about NOI can still
find the number the spreadsheet itself reported.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from app.engine.excel_profiler import (
    Grid,
    Region,
    WorkbookProfile,
    normalise_label,
    read_grids,
)

log = logging.getLogger(__name__)

#: DuckDB types we map onto. Deliberately few: an uploaded spreadsheet does not
#: benefit from precision beyond this, and every extra type is another way for a
#: cast to fail silently.
_SQL_TYPE = {
    "currency": "DOUBLE",
    "percent": "DOUBLE",
    "number": "DOUBLE",
    "date": "DATE",
    "bool": "BOOLEAN",
    "text": "VARCHAR",
}

_IDENT = re.compile(r"[^a-z0-9_]+")
_LEADING_DIGIT = re.compile(r"^\d")

#: Strip these before casting a text-encoded number: '$113,990' -> 113990,
#: '(1,234)' -> -1234, '94%' -> 94.
_CURRENCY_CHARS = str.maketrans("", "", "$£€¥,  ")

#: A unit written after the number — the "x" in "2.9x", or "sqft", "bps",
#: "mo". Carries no numeric meaning and is extremely common in rent rolls.
_TRAILING_UNIT = re.compile(r"[A-Za-z\s./]+$")


def to_identifier(name: str, fallback: str = "col") -> str:
    """A safe, stable SQL identifier for a sheet or column name.

    Quoting the original would preserve it exactly but force every generated
    query to quote too, and a model writing `SELECT "Rent/Mo"` gets it wrong
    often enough to matter. A predictable snake_case name is worth more here
    than fidelity to the spreadsheet's punctuation.
    """
    cleaned = _IDENT.sub("_", normalise_label(name).lower()).strip("_")
    if not cleaned:
        return fallback
    if _LEADING_DIGIT.match(cleaned):
        cleaned = f"c_{cleaned}"
    return cleaned[:63]


def coerce(value: Any, sql_type: str) -> Any:
    """Convert one cell to the column's declared type, or None.

    Returns None rather than raising on anything unconvertible. A single
    malformed cell in a 2,000-row rent roll must not fail the whole upload, and
    a NULL is both visible in a query and honest about what happened.
    """
    if value is None or value == "":
        return None

    if sql_type == "VARCHAR":
        return str(value)

    if sql_type == "BOOLEAN":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("yes", "y", "true", "1"):
            return True
        if text in ("no", "n", "false", "0"):
            return False
        return None

    if sql_type == "DATE":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%y", "%d-%b-%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    # DOUBLE
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    text = text.translate(_CURRENCY_CHARS)
    try:
        number = float(text)
    except ValueError:
        # A trailing unit is extremely common in these workbooks and carries no
        # numeric meaning: "2.9x" for a coverage multiple, "1,200 sqft",
        # "250 bps". Stripped only as a fallback, and only when what remains is
        # a number — so "N/A" and "TBD" still fail, as they should.
        stripped = _TRAILING_UNIT.sub("", text)
        try:
            number = float(stripped)
        except ValueError:
            return None
    if negative:
        number = -number
    # A percent written as text is a percentage POINT value ('94%' -> 94.0).
    # Left as-is rather than divided: the mapper records the unit, and silently
    # rescaling here would make the stored number disagree with the source cell.
    return number


#: Marker for a NULL in the staging CSV. Chosen because no spreadsheet cell we
#: have seen contains it, and an empty field cannot distinguish "no value" from
#: "empty string" in a VARCHAR column.
_NULL = "\\N"


def _csv_value(value: Any) -> str:
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _bulk_insert(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    spec: dict[str, str],
    rows: list[tuple],
) -> None:
    """Load rows through DuckDB's CSV reader rather than executemany.

    executemany converts every value through the Python API one at a time, and
    on this platform that path spends most of its time in the import machinery —
    profiling a 1,750-row load showed 62,000 module lookups and 440,000 stat
    calls, for eighteen seconds of work that should be milliseconds. Staging to
    a CSV and letting the C++ reader parse it does the same load in under a
    second.

    The staging file is written beside the database and removed straight after,
    so nothing survives the call.
    """
    if not rows:
        return

    columns = ", ".join(f"'{name}': '{sql_type}'" for name, sql_type in spec.items())
    with tempfile.TemporaryDirectory() as staging:
        path = Path(staging) / "load.csv"
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for row in rows:
                writer.writerow([_csv_value(v) for v in row])
        location = str(path).replace("\\", "/")
        conn.execute(
            f'INSERT INTO "{table}" SELECT * FROM read_csv('
            f"'{location}', header=false, nullstr='{_NULL}', "
            f"columns={{{columns}}})"
        )


@dataclass
class Batch:
    """One file's contribution to a dataset.

    A dataset is keyed by workbook SHAPE, so twelve monthly rent rolls all land
    in the same tables rather than twelve isolated copies — which is what turns
    a folder of files into something you can trend. Each row records the file it
    came from and the period that file covers.
    """

    source_file: str
    as_of: date | None = None
    #: Add to what is already there rather than replacing it. False for the
    #: first file of a dataset, True for every file after.
    append: bool = False

    @property
    def replaces_own_rows(self) -> bool:
        """Whether an earlier load of this same file must be cleared first.

        Someone will re-upload a corrected March file. Without this the March
        rows simply double, and every total for that month is silently wrong.
        """
        return self.append


def _prepare_table(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    batch: "Batch | None",
) -> None:
    """Create the table, or make room for this file's rows within it."""
    if batch is None or not batch.append:
        conn.execute(f'CREATE OR REPLACE TABLE "{table}" ({", ".join(columns)})')
        return

    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(columns)})')
    conn.execute(
        f'DELETE FROM "{table}" WHERE source_file = ?', [batch.source_file]
    )


@dataclass
class MaterializedTable:
    name: str
    row_count: int
    columns: list[str]
    sheets: list[str]
    grain: str = ""
    purpose: str = ""
    derived_row_count: int = 0
    #: Columns of the companion `<name>_derived` table, holding the figures the
    #: spreadsheet computed for itself. Named so they can be selected: a model
    #: cannot query a column it has not been told about.
    derived_columns: list[str] = field(default_factory=list)
    #: Columns loaded as all-NULL because the workbook's formulas carry no
    #: saved results. Carried through so the model is told the numbers are
    #: unavailable rather than zero — the difference between "no exposure" and
    #: "exposure could not be read".
    uncalculated_columns: list[str] = field(default_factory=list)
    #: Columns where the source HAD values but nearly none survived the cast —
    #: the declared type is wrong for what is written in the cells. Same failure
    #: mode as above from the user's side: a column full of NULLs and no reason
    #: given, so it is surfaced the same way.
    unreadable_columns: list[str] = field(default_factory=list)


@dataclass
class MaterializeResult:
    database_path: str
    tables: list[MaterializedTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.row_count for t in self.tables)


# ── planning ──────────────────────────────────────────────────────────────────


@dataclass
class _ColumnPlan:
    """One output column: where it comes from and what it becomes."""

    source: str
    identifier: str
    sql_type: str
    col_index: int | None = None  # records regions
    label: str | None = None  # entity regions
    derived: bool = False
    #: The source column is formulas with no saved results, so this loads as
    #: all-NULL. Tracked here rather than inferred later, because "empty" and
    #: "unreadable" are indistinguishable once the rows are written.
    uncalculated: bool = False


def _field_lookup(mapping: dict[str, Any] | None) -> dict[tuple[str, str], dict]:
    """Index the mapper's fields by (table, source) for quick lookup."""
    index: dict[tuple[str, str], dict] = {}
    for region in (mapping or {}).get("regions") or []:
        table = str(region.get("suggested_table") or "")
        for entry in region.get("fields") or []:
            source = str(entry.get("source") or "")
            if source:
                index[(table, source)] = entry
    return index


def _region_meta(mapping: dict[str, Any] | None) -> list[dict]:
    """The mapper's region descriptions, as a LIST.

    Not keyed by suggested_table: shapes are mapped in parallel and cannot see
    each other's choices, so two of them routinely land on the same name — this
    workbook produces two `property_summary` and two `tenant_rent_roll`. A dict
    would silently drop one of each pair and leave that region unnamed.
    """
    return [
        region
        for region in ((mapping or {}).get("regions") or [])
        if str(region.get("suggested_table") or "")
    ]


def _shape_signature(regions: list[Region]) -> str:
    """The profiler's own shape key for a sheet.

    Grouping must not depend on the mapper. Sheets that share a structure share
    a table whether or not a model has said anything about them, which is what
    turns 20 property tabs into one queryable rent roll rather than 20.
    """
    return "|".join(f"{r.kind}:{r.last_col - r.first_col + 1}c" for r in regions)


#: Marks a table named from its own columns because nothing described it. Kept
#: recognisable so a dataset never takes its display name from a digest.
SHAPE_NAME_PREFIX = "shape_"


def _region_signature(region: Region) -> str:
    """A region's column names, which are its identity across files.

    Sheet names and row ranges differ between files of one shape; the headers do
    not. They are also what the fingerprint is built from, so keying the table on
    them keeps the loader consistent with the grouping.
    """
    if region.columns:
        names = [c.header.strip().lower() for c in region.columns if c.header]
    else:
        names = [f.label.strip().lower() for f in region.fields if f.label]
    return "|".join(names)


def _meta_headers(meta: dict) -> set[str]:
    """The columns the mapper recorded for a region."""
    return {
        str(entry.get("source") or "").strip().lower()
        for entry in (meta.get("fields") or [])
        if str(entry.get("source") or "").strip()
    }


def _region_headers(region: Region) -> set[str]:
    return {part for part in _region_signature(region).split("|") if part}


#: How much of a mapper's column list must appear in a region for the two to be
#: the same thing. Not 1.0: a mapping may omit a column, and a later file of a
#: shape may carry a trailing one the first lacked.
_HEADER_MATCH = 0.6


def _shape_fallback_name(region: Region, signature: str) -> str:
    """A table name derived from the region's own columns.

    Readable where it can be — the leading header names — and made unique by a
    digest of all of them, so two different shapes never collide and one shape
    never diverges between files.
    """
    if region.columns:
        leading = [c.header for c in region.columns if c.header][:3]
    else:
        leading = [f.label for f in region.fields if f.label][:3]
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:6]
    stem = to_identifier("_".join(leading), fallback="") if leading else ""
    raw = f"{SHAPE_NAME_PREFIX}{stem}_{digest}" if stem else f"{SHAPE_NAME_PREFIX}{digest}"
    return to_identifier(raw, fallback="table")


def _name_for_group(
    region: Region,
    sheets: list[str],
    regions_meta: list[dict],
    used: set[int],
) -> tuple[str, int | None]:
    """The table name for a group of same-shaped regions.

    Tried in order of how well each test survives a change of file, because
    every file after the first in a dataset is matched against a mapping made
    from an earlier one:

    1. The mapper described a region with these column names. The only test
       that still holds for the second file of a dataset.
    2. The mapper described a region at this row range on one of these sheets.
    3. The same sheets, and whichever unclaimed region is closest in width.
       Guarding on field count stops a 5-column summary adopting a 15-column
       detail's name just because both live on the same sheet.
    4. Nothing matched: name it from the shape, never from the sheet.
    """
    rows = f"{region.first_row}-{region.last_row}"
    mine = set(sheets)
    signature = _region_signature(region)

    headers = _region_headers(region)
    if headers:
        candidates: list[tuple[int, float, int]] = []
        for index, meta in enumerate(regions_meta):
            if index in used:
                continue
            recorded = _meta_headers(meta)
            if not recorded:
                continue
            overlap = len(recorded & headers) / len(recorded)
            if overlap >= _HEADER_MATCH:
                # Sheet overlap breaks ties, so two regions of ONE workbook that
                # share a column list still get their own names.
                covers = 1 if mine & set(meta.get("sheets") or []) else 0
                candidates.append((covers, overlap, index))
        if candidates:
            _, _, index = max(candidates, key=lambda c: (c[0], c[1], -c[2]))
            return str(regions_meta[index]["suggested_table"]), index

    for index, meta in enumerate(regions_meta):
        if index in used:
            continue
        if str(meta.get("region_rows") or "") == rows and mine & set(
            meta.get("sheets") or []
        ):
            return str(meta["suggested_table"]), index

    width = len(region.columns) or len(region.fields)
    best: tuple[int, int] | None = None
    for index, meta in enumerate(regions_meta):
        if index in used or not (mine & set(meta.get("sheets") or [])):
            continue
        distance = abs(len(meta.get("fields") or []) - width)
        if best is None or distance < best[0]:
            best = (distance, index)
    if best is not None:
        return str(regions_meta[best[1]]["suggested_table"]), best[1]

    return _shape_fallback_name(region, signature), None


def _plan_records(
    region: Region, table: str, fields: dict[tuple[str, str], dict]
) -> list[_ColumnPlan]:
    plans: list[_ColumnPlan] = []
    seen: set[str] = set()
    for column in region.columns:
        header = column.header
        if not header:
            continue
        entry = fields.get((table, header)) or {}
        # The mapper's type wins when it has one: it knows "Unit" is an
        # identifier despite looking numeric, which the profiler cannot.
        declared = str(entry.get("data_type") or "")
        if declared in _SQL_TYPE:
            sql_type = _SQL_TYPE[declared]
        elif column.needs_cast == "date":
            sql_type = "DATE"
        elif column.needs_cast == "number":
            sql_type = "DOUBLE"
        else:
            sql_type = _SQL_TYPE.get(column.dominant_unit, "VARCHAR")

        identifier = to_identifier(
            str(entry.get("concept") or "") or header, fallback=f"col_{column.col}"
        )
        base = identifier
        suffix = 2
        while identifier in seen:
            identifier = f"{base}_{suffix}"
            suffix += 1
        seen.add(identifier)

        plans.append(
            _ColumnPlan(
                source=header,
                identifier=identifier,
                sql_type=sql_type,
                col_index=column.col,
                derived=str(entry.get("role") or "") == "derived",
                uncalculated=column.is_uncalculated,
            )
        )
    return plans


# ── writing ───────────────────────────────────────────────────────────────────


def _write_records(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    plans: list[_ColumnPlan],
    instances: list[tuple[str, Grid, Region]],
    batch: "Batch | None" = None,
) -> MaterializedTable:
    """Stack every sheet sharing this shape into one table."""
    kept = [p for p in plans if not p.derived]
    # Provenance first, so every row can say where it came from and when. The
    # order matters: the CSV loader writes positionally.
    spec = {
        "as_of": "DATE",
        "source_file": "VARCHAR",
        "source_sheet": "VARCHAR",
        "source_row": "INTEGER",
    }
    for plan in kept:
        spec[plan.identifier] = plan.sql_type
    columns = [f'"{name}" {sql_type}' for name, sql_type in spec.items()]
    _prepare_table(conn, table, columns, batch)

    # A value the source had but the cast could not read. Counted per column
    # because the failure is otherwise invisible: the column types cleanly, the
    # rows load, and every figure is NULL. That is how "2.9x" in an
    # income-to-rent column became "no data available for this property".
    present: dict[str, int] = {p.identifier: 0 for p in kept}
    lost: dict[str, int] = {p.identifier: 0 for p in kept}

    rows: list[tuple] = []
    for sheet, grid, region in instances:
        first = region.data_first_row
        last = region.data_last_row
        if first is None or last is None:
            continue
        # Set, not the list it is stored as: this test runs once per row, and a
        # computed sheet can put most of its rows in total_rows.
        skip = set(region.total_rows)
        for row_number in range(first, last + 1):
            if row_number in skip or grid.is_blank_row(row_number):
                continue
            values: list[Any] = [
                batch.as_of if batch else None,
                batch.source_file if batch else "",
                sheet,
                row_number,
            ]
            for plan in kept:
                cell = grid.at(row_number, plan.col_index or 0)
                raw = cell.value if cell else None
                converted = coerce(raw, plan.sql_type)
                if raw is not None and raw != "":
                    present[plan.identifier] += 1
                    if converted is None:
                        lost[plan.identifier] += 1
                values.append(converted)
            rows.append(tuple(values))

    _bulk_insert(conn, table, spec, rows)

    uncalculated = [p.identifier for p in kept if p.uncalculated]
    # Reported when MOST of a column was unreadable, not merely some: a rent
    # roll legitimately holds "N/A" for a vacant unit, and flagging that would
    # be noise. A column where nearly nothing parsed is a different thing —
    # the declared type is wrong for the data.
    unreadable = [
        name
        for name in present
        if present[name] >= 3 and lost[name] / present[name] >= 0.8
    ]

    return MaterializedTable(
        name=table,
        row_count=len(rows),
        columns=[p.identifier for p in kept],
        sheets=sorted({sheet for sheet, _, _ in instances}),
        uncalculated_columns=uncalculated,
        unreadable_columns=unreadable,
    )


def _write_entities(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    instances: list[tuple[str, Grid, Region]],
    fields: dict[tuple[str, str], dict],
    batch: "Batch | None" = None,
) -> tuple[MaterializedTable, int]:
    """Pivot form-shaped sheets into one row per sheet.

    An operating statement lays its line items down a column, so 20 property
    tabs are 20 records of the same table once transposed — which is what makes
    "NOI by property" a normal query instead of twenty lookups.

    Derived line items (EGI, Total opex, NOI) go to a separate `<table>_derived`
    so they are available to answer with, but can never be summed alongside the
    inputs they were computed from.
    """
    inputs: dict[str, str] = {}
    derived: dict[str, str] = {}
    for _, _, region in instances:
        for entry in region.fields:
            identifier = to_identifier(
                str((fields.get((table, entry.label)) or {}).get("concept") or "")
                or entry.label
            )
            target = derived if entry.is_derived else inputs
            target.setdefault(identifier, _SQL_TYPE.get(entry.unit, "DOUBLE"))

    def build(name: str, spec: dict[str, str], want_derived: bool) -> int:
        full = {
            "as_of": "DATE",
            "source_file": "VARCHAR",
            "source_sheet": "VARCHAR",
            **spec,
        }
        columns = [f'"{c}" {t}' for c, t in full.items()]
        _prepare_table(conn, name, columns, batch)
        if not spec:
            return 0
        rows: list[tuple] = []
        for sheet, _, region in instances:
            values: dict[str, Any] = {}
            for entry in region.fields:
                if entry.is_derived != want_derived:
                    continue
                identifier = to_identifier(
                    str((fields.get((table, entry.label)) or {}).get("concept") or "")
                    or entry.label
                )
                if identifier in spec:
                    values[identifier] = coerce(entry.value, spec[identifier])
            rows.append(
                tuple(
                    [
                        batch.as_of if batch else None,
                        batch.source_file if batch else "",
                        sheet,
                    ]
                    + [values.get(c) for c in spec]
                )
            )
        _bulk_insert(conn, name, full, rows)
        return len(rows)

    row_count = build(table, inputs, want_derived=False)
    derived_count = build(f"{table}_derived", derived, want_derived=True)

    if not inputs and derived:
        # A summary block: all computed figures, with no inputs underneath them
        # to protect from double-counting. Splitting it would leave an empty
        # table under the region's name and the figures in an unreported
        # companion, so the computed table is this region's table.
        return (
            MaterializedTable(
                name=f"{table}_derived",
                row_count=derived_count,
                columns=list(derived),
                sheets=sorted({sheet for sheet, _, _ in instances}),
            ),
            derived_count,
        )

    return (
        MaterializedTable(
            name=table,
            row_count=row_count,
            columns=list(inputs),
            sheets=sorted({sheet for sheet, _, _ in instances}),
            derived_row_count=derived_count,
            derived_columns=list(derived),
        ),
        derived_count,
    )


# ── entry point ───────────────────────────────────────────────────────────────


def materialize(
    workbook_path: str | Path,
    profile: WorkbookProfile,
    mapping: dict[str, Any] | None,
    database_path: str | Path,
    grids: list[Grid] | None = None,
    batch: Batch | None = None,
) -> MaterializeResult:
    """Write every data region of a workbook into a DuckDB file.

    `mapping` is optional. Without it the tables still load, named and typed
    from the profiler alone — a workbook is more useful as awkwardly-named SQL
    than as a spreadsheet nobody can query.

    `grids` avoids re-reading a workbook the caller has already parsed. Reading
    is by far the slowest step, so an ingest that profiles and then loads should
    pay for it once.

    `batch` names the file being loaded and the period it covers. Passing one
    with `append=True` adds this file to a dataset that already holds others of
    the same shape — twelve monthly rent rolls in one set of tables — replacing
    any earlier rows from the same file rather than doubling them.
    """
    workbook_path = Path(workbook_path)
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists() and not (batch and batch.append):
        # A fresh load starts clean; an append must keep what is there.
        database_path.unlink()

    source_grids = grids if grids is not None else read_grids(workbook_path)
    grids = {g.name: g for g in source_grids}
    fields = _field_lookup(mapping)
    regions_meta = _region_meta(mapping)

    # Group by (shape, position within the sheet). Sheets sharing a signature
    # hold the same regions in the same order, so region 2 of every multifamily
    # tab is the same table — 10 sheets, one 1,700-row rent roll.
    buckets: dict[tuple[str, int], list[tuple[str, Grid, Region]]] = {}

    for sheet_profile in profile.sheets:
        grid = grids.get(sheet_profile.name)
        if grid is None:
            continue
        signature = _shape_signature(sheet_profile.regions)
        for position, region in enumerate(sheet_profile.regions):
            if region.kind not in ("records", "entity"):
                continue
            if region.kind == "records" and not region.columns:
                continue
            if region.kind == "entity" and not region.fields:
                continue
            buckets.setdefault((signature, position), []).append(
                (sheet_profile.name, grid, region)
            )

    # Name each bucket once its full membership is known.
    groups: dict[str, list[tuple[str, Grid, Region]]] = {}
    kinds: dict[str, str] = {}
    meta_for: dict[str, dict] = {}
    used: set[int] = set()

    # Widest buckets claim names first: a detail region is more distinctive than
    # a five-column summary, so letting it match before the summary keeps the
    # summary from taking the rent roll's name.
    ordered = sorted(
        buckets.values(),
        key=lambda i: len(i[0][2].columns) or len(i[0][2].fields),
        reverse=True,
    )

    for instances in ordered:
        sheets = [sheet for sheet, _, _ in instances]
        exemplar = instances[0][2]
        raw_name, matched = _name_for_group(exemplar, sheets, regions_meta, used)
        if matched is not None:
            used.add(matched)
            meta_for_name = regions_meta[matched]
        else:
            meta_for_name = {}

        name = to_identifier(raw_name, fallback="table")
        base = name
        suffix = 2
        while name in groups:
            name = f"{base}_{suffix}"
            suffix += 1
        kinds[name] = exemplar.kind
        groups[name] = instances
        meta_for[name] = meta_for_name

    result = MaterializeResult(database_path=str(database_path))
    conn = duckdb.connect(str(database_path))
    try:
        for name, instances in groups.items():
            try:
                if kinds[name] == "records":
                    # Column layout comes from the widest instance: sheets of the
                    # same shape can differ by a trailing column.
                    widest = max(instances, key=lambda i: len(i[2].columns))
                    plans = _plan_records(widest[2], name, fields)
                    if not plans:
                        continue
                    table = _write_records(conn, name, plans, instances, batch)
                else:
                    table, _ = _write_entities(conn, name, instances, fields, batch)

                meta = meta_for.get(name) or {}
                table.grain = str(meta.get("grain") or "")
                table.purpose = str(meta.get("purpose") or "")
                result.tables.append(table)
            except Exception as exc:  # one bad region must not lose the rest
                log.exception("Could not materialize region %s", name)
                result.warnings.append(f"Skipped '{name}': {exc}")
        conn.commit()
    finally:
        conn.close()

    if not result.tables:
        result.warnings.append(
            "No data regions could be loaded from this workbook."
        )
    return result

"""Saved Excel-report core — the openpyxl analogue of dashboards.py.

Mirrors dashboards.py's plan / save / run shape so both features behave the same
way for the model and the user:

  scan  -> classify an uploaded .xlsx (excel_template_scanner) and cache it
  save  -> persist the *exact* scanned+approved template to MCP_EXCEL_TEMPLATES,
           copying the workbook into local storage so runs can rebuild from it
  plan  -> bind an approved template to a parameterized read-only query plus a
           Tier-1 formula-regeneration plan; cache it
  save  -> persist the cached plan to MCP_EXCEL_REPORTS
  run   -> substitute parameter values, execute the query through the SAME
           read-only safety path as every other query (snowflake_client.run_query
           -> sql_safety.validate_sql + row cap), drop the rows into the template's
           data sheet, re-extend Tier-1 formulas to the new row count, write the
           output .xlsx, and bump RUN_COUNT / LAST_RUN_AT.

Boundaries this module respects (nothing existing is modified):
  * Reads/writes go through snowflake_client's PUBLIC helpers only
    (get_connection / session_identity / run_query). The persistence writes here
    are hardcoded, parameterized statements against fixed (config) table names
    with every user value BOUND — the same intentional-write pattern as
    snowflake_client.write_audit / write_saved_dashboard.
  * The report query is NEVER executed except via snowflake_client.run_query, so
    it is validate_sql-gated (SELECT/WITH/UNION only) exactly like user SQL —
    a malicious parameter value cannot turn it into DDL/DML.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.engine.connectors.snowflake import client as snowflake_client
from app.engine import sql_params
from app.engine import excel_template_scanner as scanner
from app.engine.sql_safety import UnsafeSQLError

# How a saved report's stored config says which runner it needs. A report saved
# before multi-block existed has no `kind` at all, so the absence of this marker
# is itself the answer: the original one-sheet/one-query path.
MULTIBLOCK_KIND = "multiblock"

# Fixed (config-overridable) table names — these tables already exist; this code
# never creates or alters them. Never user input, so safe to interpolate.
EXCEL_TEMPLATES_TABLE = os.environ.get(
    "MCP_EXCEL_TEMPLATES_TABLE", "LOAN.PUBLIC.MCP_EXCEL_TEMPLATES"
)
EXCEL_REPORTS_TABLE = os.environ.get(
    "MCP_EXCEL_REPORTS_TABLE", "LOAN.PUBLIC.MCP_EXCEL_REPORTS"
)

# Local backend storage for approved template workbooks and rendered outputs.
EXCEL_STORAGE_DIR = Path(
    os.environ.get("MCP_EXCEL_STORAGE_DIR", str(Path(__file__).resolve().parents[2] / "excel_storage"))
)

# Drop-folder on THIS machine for workbooks to be scanned. It exists because a
# file uploaded in a chat lives in the model's sandbox, not on this filesystem —
# and handing the bytes through the model as base64 is impractical for a real
# template (a 37KB workbook is ~50k characters the model would have to emit).
# Putting the file here instead costs nothing and has no size limit.
EXCEL_INBOX_DIR = Path(
    os.environ.get("MCP_EXCEL_INBOX_DIR", str(Path(__file__).resolve().parents[2] / "excel_inbox"))
)

# Dedicated row cap for Excel-report generation — much higher than
# snowflake_client.MAX_ROWS (100), which exists to protect Claude's context on
# chat/dashboard queries. An Excel report is a full data extract, so it has no
# context-window constraint.
#
# What actually bounds it is THIS engine, not Snowflake and not the warehouse.
# Measured on the US_RealEstate template, two blocks, openpyxl in normal (not
# write-only) mode:
#
#      20,000 rows total -> 12.2s, 1.9 MB file
#      50,000 rows total -> 34.6s, 4.7 MB file
#     100,000 rows total -> 62.9s, 9.4 MB file
#
# Dead linear at ~0.65ms/row. Profiling splits it ~40% openpyxl's XML writer,
# ~20% loading the template, ~20% the conservation check (which re-loads two
# more workbooks), ~17% reflow. Excel's own ceiling is 1,048,576 rows per sheet
# regardless.
#
# Peak memory is the figure NOT nailed down here: tracemalloc suggests ~13KB per
# row (so ~1.3GB at 100k rows), but that tracks allocations rather than resident
# size and two attempts to sample the working set read back nothing usable.
# Treat it as the open variable and size a container conservatively.
#
# 25,000 because the old 10,000 was too low for a real report: the US_RealEstate
# weekly extract matches 11,354 sales rows, so every run was quietly dropping
# 1,354 of them. 25,000 gives that better than 2x headroom and a worst case —
# every block full — of about 35 seconds. Raising it further is cheap on time
# (100k rows is roughly a minute); memory is the thing to watch, and to budget
# per CONCURRENT run rather than per deployment. Whatever it is set to, hitting
# it is never silent — see excel_multiblock.run.
EXCEL_MAX_ROWS = int(os.environ.get("MCP_EXCEL_MAX_ROWS", "50000"))

# How many past runs a report card offers. Every run is KEPT — this is only how
# many the page lists, because a monthly report accumulates faster than anyone
# wants to scroll. Older files stay downloadable by name, and the card says how
# many it is not showing rather than implying five is all there is.
REPORT_RUNS_SHOWN = int(os.environ.get("MCP_EXCEL_RUNS_SHOWN", "5"))

# Parameter placeholder rendering ({{name}} substitution) is shared with
# parameterized dashboards — see sql_params. render_sql below is a thin wrapper
# that keeps this module's ExcelReportError contract.
# The end row of a numeric A1-style range, so it can be re-extended.
_RANGE_END_RE = re.compile(r"(\$?[A-Za-z]{1,3}\$?)(\d+)(:)(\$?[A-Za-z]{1,3}\$?)(\d+)")


class ExcelReportError(Exception):
    """An Excel-report operation failed in a way worth reporting to the caller."""


# --- In-memory caches (mirror dashboards._PLAN_CACHE; process-scoped) ---------

_SCAN_CACHE: dict[str, dict[str, Any]] = {}       # scan_id  -> {scan, source_path}
_REPORT_PLAN_CACHE: dict[str, dict[str, Any]] = {}  # plan_id -> {plan, template_id, ...}


# ---------------------------------------------------------------------------
# Pure helpers (no Snowflake, no filesystem) — independently unit-testable.
# ---------------------------------------------------------------------------

def render_sql(sql_template: str, parameters: list[dict[str, Any]],
               values: dict[str, Any] | None) -> str:
    """Substitute {{name}} placeholders in a parameterized query (shared with
    dashboards via sql_params). Thin wrapper that re-raises the shared
    ParameterError as ExcelReportError to preserve this module's error contract."""
    try:
        return sql_params.render_sql(sql_template, parameters, values)
    except sql_params.ParameterError as exc:
        raise ExcelReportError(str(exc)) from exc


def regenerate_formula(formula: str, new_last_row: int) -> str:
    """Re-point every numeric range end in a Tier-1 formula at new_last_row.

    =SUM(Data!B2:B100) with 40 new rows (header row 1) -> =SUM(Data!B2:B41).
    Whole-column ranges (A:A) carry no end row and are left untouched."""
    def _fix(m: re.Match) -> str:
        start_col, start_row, colon, end_col, _end_row = m.groups()
        return f"{start_col}{start_row}{colon}{end_col}{new_last_row}"

    return _RANGE_END_RE.sub(_fix, formula)


def build_regeneration_plan(formula_classification: dict[str, Any]) -> list[dict[str, Any]]:
    """The Tier-1 formula cells a run must re-extend, as a compact list."""
    plan: list[dict[str, Any]] = []
    for fc in (formula_classification or {}).get("tier_1", []):
        plan.append({
            "sheet": fc["sheet"],
            "cell": fc["cell"],
            "formula": fc["formula"],
            "ranges": fc.get("ranges", []),
        })
    return plan


# --- name matching + row-mirror regeneration (Tier 1b, STEP 1 & 2) -----------

# A range with an optional sheet qualifier and numeric row ends, so its end row
# can be re-extended (whole-column ranges carry no digits and won't match).
_QUALIFIED_RANGE_RE = re.compile(
    r"(?P<sheet>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
    r"(?P<c1>\$?[A-Za-z]{1,3}\$?)(?P<r1>\d+):(?P<c2>\$?[A-Za-z]{1,3}\$?)(?P<r2>\d+)"
)


def _run_report_query(rendered_sql: str) -> dict[str, Any]:
    """Execute the report query with the SAME read-only safety validation as
    run_query (validate_sql: SELECT/WITH/UNION only), but capped at the higher
    EXCEL_MAX_ROWS instead of the 100-row chat/dashboard cap. Mirrors
    snowflake_client.run_query exactly, only the row limit differs."""
    from app.engine.sql_safety import validate_sql, enforce_row_limit
    safe_sql = enforce_row_limit(validate_sql(rendered_sql), EXCEL_MAX_ROWS)
    return snowflake_client._execute_select(safe_sql)


def _count_total_rows(rendered_sql: str) -> int | None:
    """Pre-cap COUNT(*) of the report query, so a truncation notice can say
    'N of M'. Wrapped as a derived table and run through the normal safe path."""
    inner = rendered_sql.strip().rstrip(";").strip()
    q = f"SELECT COUNT(*) AS N FROM (\n{inner}\n) AS _mcp_count"
    try:
        res = snowflake_client.run_query(q)
        return int(res["rows"][0][0])
    except Exception:  # noqa: BLE001 - a failed count must not break generation
        return None


def _write_truncation_notice(ws, header_row: int, header_by_col: dict[str, str],
                             shown: int, total: int) -> dict[str, Any]:
    """Write a visible 'showing first N of M rows' banner into the workbook, ABOVE
    the header row so it is never mistaken for (or counted as) a data row and no
    formula's row references shift. Returns {cell, text}."""
    from openpyxl.cell.cell import MergedCell
    from openpyxl.utils import column_index_from_string

    text = (f"⚠ Showing first {shown:,} of {total:,} rows — result set truncated, "
            f"narrow your query to see all rows.")
    if header_row > 1:
        r, c0 = header_row - 1, 1          # the title/blank band above the header
    else:                                   # no room above: sit right of the data
        last = max((column_index_from_string(col) for col in header_by_col), default=1)
        r, c0 = 1, last + 2

    target = None
    for c in range(c0, c0 + 8):             # skip any merged (read-only) members
        cell = ws.cell(row=r, column=c)
        if not isinstance(cell, MergedCell):
            target = cell
            break
    if target is None:
        target = ws.cell(row=r, column=c0)

    existing = target.value
    target.value = f"{text}  |  {existing}" if existing and str(existing).strip() else text
    return {"cell": target.coordinate, "text": text}


def _safe_set(ws, row: int, col: int, value: Any) -> bool:
    """Set a cell's value, skipping merged cells (whose value is read-only in
    openpyxl). Returns True if written, False if skipped."""
    from openpyxl.cell.cell import MergedCell
    cell = ws.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        return False
    cell.value = value
    return True


def normalize_name(name: Any) -> str:
    """Case/space/punctuation-insensitive key for column-name matching.
    'Line Total' and 'LINE_TOTAL' both -> 'LINETOTAL'."""
    return re.sub(r"[^A-Za-z0-9]", "", str(name)).upper()


def resolve_value_sourced_columns(header_by_col: dict[str, str],
                                  query_columns: list[str]) -> dict[str, str]:
    """STEP 2 — generic, name-based. Return {template_column_letter: query_column}
    for every template header that matches a query result column by normalized
    name. Those columns are populated as plain VALUES from the query and their
    local formulas are NOT regenerated, even if they would otherwise be Tier 1b."""
    q_by_norm: dict[str, str] = {}
    for q in query_columns:
        q_by_norm.setdefault(normalize_name(q), q)
    matched: dict[str, str] = {}
    for col, label in header_by_col.items():
        q = q_by_norm.get(normalize_name(label))
        if q is not None:
            matched[col] = q
    return matched


def regenerate_tier1_formula(formula: str, data_last_row: int,
                             data_sheet: str, on_data_sheet: bool) -> str:
    """Re-extend the END row of ranges that track the DATA sheet's rows to
    data_last_row. A range qualified with the data sheet's name is extended; an
    unqualified same-sheet range is extended only when the formula itself lives on
    the data sheet. Ranges over other blocks (e.g. a fixed sector table on a
    dashboard sheet) are left untouched."""
    ds = data_sheet.upper()

    def _fix(m: re.Match) -> str:
        sheet = m.group("sheet")
        if sheet is not None:
            qualifies = sheet.rstrip("!").strip("'").upper() == ds
        else:
            qualifies = on_data_sheet
        if not qualifies:
            return m.group(0)
        return f"{sheet or ''}{m.group('c1')}{m.group('r1')}:{m.group('c2')}{data_last_row}"

    return _QUALIFIED_RANGE_RE.sub(_fix, formula)


def regenerate_block_formulas(template_formula: str, count: int) -> list[str]:
    """The Tier-1b block re-expressed for `count` rows: the template formula
    fill-shifted down by 0, 1, 2, … rows."""
    return [scanner.shift_formula_rows(template_formula, k) for k in range(count)]


def fill_data_sheet_by_name(ws, header_by_col: dict[str, str], header_row: int,
                            query_columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """Write query rows into the data sheet by matching each query column to a
    template header column by normalized name (order-independent). Clears the old
    data region of the matched columns first. Returns {new_last_row, filled_cols}."""
    from openpyxl.utils import column_index_from_string

    matched = resolve_value_sourced_columns(header_by_col, query_columns)  # col -> qname
    q_index = {name: i for i, name in enumerate(query_columns)}
    start_row = header_row + 1
    # clear existing values under the header in the matched columns
    for col in matched:
        ci = column_index_from_string(col)
        for r in range(start_row, ws.max_row + 1):
            _safe_set(ws, r, ci, None)
    # write fresh values
    for i, row in enumerate(rows):
        for col, qname in matched.items():
            ci = column_index_from_string(col)
            _safe_set(ws, start_row + i, ci, row[q_index[qname]])
    new_last_row = start_row + len(rows) - 1 if rows else header_row
    return {"new_last_row": new_last_row, "filled_cols": sorted(matched)}


def fill_workbook(wb, data_sheet: str, columns: list[str], rows: list[list[Any]],
                  regeneration: list[dict[str, Any]], header_row: int = 1) -> dict[str, Any]:
    """Write query rows into the data sheet under its header and re-extend Tier-1
    formulas. Mutates wb in place; returns {new_last_row, regenerated:[...]}.

    The template's header row is preserved; query columns are written left-to-right
    beneath it (query column order is assumed to match the template's data columns)."""
    if data_sheet not in wb.sheetnames:
        raise ExcelReportError(
            f"template has no sheet named '{data_sheet}' (sheets: {', '.join(wb.sheetnames)})"
        )
    ws = wb[data_sheet]
    start_row = header_row + 1
    width = max(len(columns), ws.max_column)

    # Clear any existing data rows below the header before writing fresh rows.
    for r in range(start_row, ws.max_row + 1):
        for c in range(1, width + 1):
            ws.cell(row=r, column=c).value = None

    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            ws.cell(row=start_row + i, column=j + 1).value = val

    new_last_row = start_row + len(rows) - 1 if rows else header_row

    regenerated: list[dict[str, Any]] = []
    for entry in regeneration or []:
        target = wb[entry["sheet"]] if entry["sheet"] in wb.sheetnames else None
        if target is None:
            continue
        new_formula = regenerate_formula(entry["formula"], new_last_row)
        target[entry["cell"]] = new_formula
        regenerated.append({
            "sheet": entry["sheet"], "cell": entry["cell"],
            "from": entry["formula"], "to": new_formula,
        })

    return {"new_last_row": new_last_row, "regenerated": regenerated}


def force_recalculation_on_open(wb) -> None:
    """Mark the workbook so Excel recalculates everything when it is opened.

    The generated report ships FORMULAS, not computed values — openpyxl writes
    formulas but never evaluates them, so every formula cell leaves here with no
    cached result. Excel normally recalculates in that situation, but "normally"
    is not a guarantee, and a report showing blank or stale totals to an investor
    is worse than one that fails outright.

    A consequence worth knowing: because the cells carry no cached value, any
    tool that PREVIEWS a workbook rather than computing it — Outlook's attachment
    preview, SharePoint, macOS Quick Look, or openpyxl with data_only=True —
    shows those cells empty until the file is opened in a spreadsheet
    application. That is inherent to shipping formulas, not a defect here.
    """
    wb.calculation.fullCalcOnLoad = True


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _template_path(template_id: str) -> Path:
    return EXCEL_STORAGE_DIR / "templates" / f"{template_id}.xlsx"


# ---------------------------------------------------------------------------
# Snowflake persistence (mirrors snowflake_client.write_audit: fixed statement,
# config table name, all user data BOUND). These tables already exist.
# ---------------------------------------------------------------------------

def _insert_template(row: dict[str, Any]) -> None:
    stmt = (
        f"INSERT INTO {EXCEL_TEMPLATES_TABLE} "
        "(TEMPLATE_ID, TEMPLATE_NAME, FINGERPRINT, TAB_ROLES, FORMULA_CLASSIFICATION, "
        "APPROVED_BY, APPROVED_AT, CREATED_AT, IS_ACTIVE) "
        "SELECT %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, "
        "CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), TRUE"
    )
    values = (
        row["template_id"], row["template_name"], row["fingerprint"],
        json.dumps(row["tab_roles"]), json.dumps(row["formula_classification"]),
        row.get("approved_by"),
    )
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(stmt, values)
    finally:
        cur.close()


def _find_template_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    sql = (
        "SELECT TEMPLATE_ID, TEMPLATE_NAME, APPROVED_BY, APPROVED_AT "
        f"FROM {EXCEL_TEMPLATES_TABLE} WHERE FINGERPRINT = %s AND IS_ACTIVE = TRUE "
        "ORDER BY CREATED_AT DESC LIMIT 1"
    )
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, (fingerprint,))
        row = cur.fetchone()
        cols = [c[0] for c in cur.description] if row else []
    finally:
        cur.close()
    if not row:
        return None
    return {col: snowflake_client._jsonify(val) for col, val in zip(cols, row)}


def _read_template(template_id: str) -> dict[str, Any] | None:
    sql = (
        "SELECT TEMPLATE_ID, TEMPLATE_NAME, FINGERPRINT, TAB_ROLES, "
        "FORMULA_CLASSIFICATION FROM " + EXCEL_TEMPLATES_TABLE +
        " WHERE TEMPLATE_ID = %s AND IS_ACTIVE = TRUE"
    )
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, (template_id,))
        row = cur.fetchone()
        cols = [c[0] for c in cur.description] if row else []
    finally:
        cur.close()
    if not row:
        return None
    rec = {col: snowflake_client._jsonify(val) for col, val in zip(cols, row)}
    for variant in ("TAB_ROLES", "FORMULA_CLASSIFICATION"):
        v = rec.get(variant)
        rec[variant] = json.loads(v) if isinstance(v, str) else v
    return rec


def _insert_report(row: dict[str, Any]) -> None:
    stmt = (
        f"INSERT INTO {EXCEL_REPORTS_TABLE} "
        "(REPORT_ID, TEMPLATE_ID, REPORT_TITLE, DESCRIPTION, PARAMETERIZED_QUERY, "
        "CREATED_BY, CREATED_AT, RUN_COUNT, IS_ACTIVE) "
        "SELECT %s, %s, %s, %s, PARSE_JSON(%s), %s, CURRENT_TIMESTAMP(), 0, TRUE"
    )
    values = (
        row["report_id"], row["template_id"], row["report_title"],
        row.get("description"), json.dumps(row["parameterized_query"]),
        row.get("created_by"),
    )
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(stmt, values)
    finally:
        cur.close()


def _read_report(report_id: str) -> dict[str, Any] | None:
    sql = (
        "SELECT REPORT_ID, TEMPLATE_ID, REPORT_TITLE, DESCRIPTION, "
        "PARAMETERIZED_QUERY, RUN_COUNT, LAST_RUN_AT FROM " + EXCEL_REPORTS_TABLE +
        " WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE"
    )
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, (report_id,))
        row = cur.fetchone()
        cols = [c[0] for c in cur.description] if row else []
    finally:
        cur.close()
    if not row:
        return None
    rec = {col: snowflake_client._jsonify(val) for col, val in zip(cols, row)}
    pq = rec.get("PARAMETERIZED_QUERY")
    rec["PARAMETERIZED_QUERY"] = json.loads(pq) if isinstance(pq, str) else pq
    return rec


def _touch_report_run(report_id: str) -> tuple[int, str | None]:
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE {EXCEL_REPORTS_TABLE} "
            "SET RUN_COUNT = RUN_COUNT + 1, LAST_RUN_AT = CURRENT_TIMESTAMP() "
            "WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE",
            (report_id,),
        )
        cur.execute(
            f"SELECT RUN_COUNT, LAST_RUN_AT FROM {EXCEL_REPORTS_TABLE} WHERE REPORT_ID = %s",
            (report_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    if not row:
        return (0, None)
    return (int(row[0]), snowflake_client._jsonify(row[1]))


# ---------------------------------------------------------------------------
# Orchestration (what the MCP tool wrappers call).
# ---------------------------------------------------------------------------

def list_inbox() -> dict[str, Any]:
    """Workbooks sitting in the local inbox folder, newest first. Creates the
    folder if it doesn't exist yet, so there is always somewhere to point the user."""
    EXCEL_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    files = [p for p in EXCEL_INBOX_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in (".xlsx", ".xlsm")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "inbox_path": str(EXCEL_INBOX_DIR),
        "count": len(files),
        "files": [
            {
                "file_name": p.name,
                "path": str(p),
                "size_kb": round(p.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
            }
            for p in files
        ],
    }


def _resolve_input_path(file_path: str) -> str:
    """Accept a real path as-is, or a BARE filename that lives in the inbox folder
    (so `file_path="Template.xlsx"` just works). Anything else is returned unchanged
    and fails the caller's existence check with a helpful message."""
    if os.path.isfile(file_path):
        return file_path
    if os.path.basename(file_path) == file_path:      # bare name, no directory part
        candidate = EXCEL_INBOX_DIR / file_path
        if candidate.is_file():
            return str(candidate)
    return file_path


def scan_template(file_path: str | None = None,
                  file_content_base64: str | None = None,
                  file_name: str | None = None) -> dict[str, Any]:
    """Classify an .xlsx and cache it under a scan_id. Best-effort fingerprint
    match against already-approved templates. Saves nothing.

    Exactly one input mode:
      * file_content_base64 — the workbook's bytes, base64-encoded. This is the
        path for a file uploaded in chat (which lives in Claude's sandbox, not on
        the MCP server's filesystem). file_name names it in the response.
      * file_path — a path on the MCP SERVER's filesystem (local/scripted use).

    Both modes run the SAME classification (scanner.scan_workbook)."""
    if bool(file_path) == bool(file_content_base64):
        raise ExcelReportError(
            "provide exactly one of file_path or file_content_base64 "
            "(received both or neither)."
        )

    if file_path:
        file_path = _resolve_input_path(file_path)
        if not os.path.isfile(file_path):
            raise ExcelReportError(
                f"file not found on this server: {file_path}. If the user uploaded this "
                f"workbook in chat, it lives in the model's sandbox and this server cannot "
                f"read it — ask them to save it into the inbox folder ({EXCEL_INBOX_DIR}) "
                f"and scan it by name (see list_excel_inbox)."
            )
        if not file_path.lower().endswith((".xlsx", ".xlsm")):
            raise ExcelReportError("expected an .xlsx/.xlsm workbook")
        scan = scanner.scan_file(file_path)
        source_path = file_path
    else:
        try:
            data = base64.b64decode(file_content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ExcelReportError(f"file_content_base64 is not valid base64: {exc}") from exc
        raw_name = file_name or "uploaded_template"
        template_name = os.path.splitext(os.path.basename(raw_name))[0] or "uploaded_template"
        try:
            scan = scanner.scan_bytes(data, template_name)
        except Exception as exc:  # noqa: BLE001 - bad/corrupt upload -> clear error
            raise ExcelReportError(f"could not read the uploaded workbook: {exc}") from exc
        # persist the bytes so save_template can copy them into storage later
        uploads = EXCEL_STORAGE_DIR / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        source_path = str(uploads / f"{uuid.uuid4().hex}.xlsx")
        with open(source_path, "wb") as fh:
            fh.write(data)

    existing: dict[str, Any] | None = None
    lookup_note: str | None = None
    try:
        existing = _find_template_by_fingerprint(scan["fingerprint"])
    except Exception as exc:  # noqa: BLE001 - a lookup miss must not block scanning
        lookup_note = f"fingerprint lookup skipped: {exc}"

    scan_id = uuid.uuid4().hex
    _SCAN_CACHE[scan_id] = {"scan": scan, "source_path": source_path}
    # AND to storage, because the cache above is one worker's memory. Under
    # `--workers 4` the upload and the save that follows it are served by
    # different processes at random, so a builder session that depends on the
    # dict works roughly one time in four. Both halves the save needs — the
    # classification and the bytes — go somewhere every worker can read.
    _persist_scan(scan_id, scan, Path(source_path).read_bytes())

    return {
        "scan_id": scan_id,
        "template_name": scan["template_name"],
        "fingerprint": scan["fingerprint"],
        "tab_roles": scan["tab_roles"],
        "formula_classification": scan["formula_classification"],
        "summary": scan["summary"],
        "sheets": scan["sheets"],
        "already_approved": existing is not None,
        "existing_template": existing,
        **({"note": lookup_note} if lookup_note else {}),
    }


def _scan_meta_key(scan_id: str) -> str:
    """Sits beside the upload, so the lifecycle rule that expires abandoned
    uploads sweeps the metadata with them rather than leaving orphans."""
    return f"uploads/{scan_id}.json"


def _persist_scan(scan_id: str, scan: dict[str, Any], data: bytes) -> None:
    """Put a scan somewhere every worker can see it. Best effort: the in-process
    cache still serves the single-worker case, so a storage failure degrades to
    the old behaviour rather than breaking the upload outright."""
    from app.engine import storage as storage_mod

    try:
        store = storage_mod.get_storage()
        store.put(storage_mod.upload_key(scan_id), data)
        store.put(_scan_meta_key(scan_id), json.dumps(scan).encode("utf-8"))
    except Exception:  # noqa: BLE001 - storage is the improvement, not the path
        pass


def _scan_entry(scan_id: str) -> dict[str, Any] | None:
    """The classification and bytes behind a scan_id, from whichever worker
    holds them — this process's cache, or shared storage."""
    entry = _SCAN_CACHE.get(scan_id)
    if entry is not None:
        return {"scan": entry["scan"],
                "data": Path(entry["source_path"]).read_bytes()}

    from app.engine import storage as storage_mod

    store = storage_mod.get_storage()
    meta_key = _scan_meta_key(scan_id)
    if not store.exists(meta_key):
        return None
    return {"scan": json.loads(store.get(meta_key).decode("utf-8")),
            "data": store.get(storage_mod.upload_key(scan_id))}


def save_template(scan_id: str) -> dict[str, Any]:
    """Persist a cached scan to MCP_EXCEL_TEMPLATES and copy the workbook into
    local storage so later runs can rebuild from it. Raises if scan_id expired."""
    entry = _scan_entry(scan_id)
    if entry is None:
        raise ExcelReportError(
            f"No scan found for scan_id '{scan_id}' (it may have expired). "
            "Upload the workbook again, then save that scan."
        )
    scan = entry["scan"]
    template_id = uuid.uuid4().hex
    approved_by, _role = snowflake_client.session_identity()

    data = entry["data"]
    dest = _template_path(template_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    # AND into configured storage, which is where the multi-block runner looks.
    # With the local backend these are the same file written twice; with a bucket
    # they are not, and a template that exists only on a container filesystem is
    # one that vanishes on the next redeploy.
    from app.engine import storage as storage_mod
    storage_mod.get_storage().put(storage_mod.template_key(template_id), data)

    _insert_template({
        "template_id": template_id,
        "template_name": scan["template_name"],
        "fingerprint": scan["fingerprint"],
        "tab_roles": scan["tab_roles"],
        "formula_classification": scan["formula_classification"],
        "approved_by": approved_by,
    })

    return {
        "template_id": template_id,
        "template_name": scan["template_name"],
        "fingerprint": scan["fingerprint"],
        "stored_path": str(dest),
        "saved": True,
    }


def _default_data_sheet(tab_roles: dict[str, str]) -> str | None:
    for name, role in (tab_roles or {}).items():
        if role == "data":
            return name
    return None


def plan_report(template_id: str, report_title: str, sql_template: str,
                parameters: list[dict[str, Any]] | None = None,
                data_sheet: str = "", description: str = "") -> dict[str, Any]:
    """Bind an approved template to a parameterized read-only query + a Tier-1
    regeneration plan. Validates the query by rendering it with defaults and
    dry-running it against Snowflake (columns/joins must resolve). Caches the
    plan under a plan_id. Saves nothing."""
    tmpl = _read_template(template_id)
    if tmpl is None:
        raise ExcelReportError(
            f"No active template with template_id '{template_id}'. "
            "Scan and save the template first."
        )

    tab_roles = tmpl.get("TAB_ROLES") or {}
    fclass = tmpl.get("FORMULA_CLASSIFICATION") or {}

    chosen_sheet = data_sheet or _default_data_sheet(tab_roles)
    if not chosen_sheet:
        raise ExcelReportError(
            "Could not infer the data sheet (no sheet classified 'data'). "
            "Pass data_sheet explicitly."
        )
    if chosen_sheet not in tab_roles:
        raise ExcelReportError(
            f"data_sheet '{chosen_sheet}' is not a sheet in this template "
            f"(sheets: {', '.join(tab_roles)})."
        )

    parameters = parameters or []
    # Validate the query shape: render with defaults/type-samples, then dry-run.
    rendered = render_sql(sql_template, parameters, sql_params.sample_values(parameters))
    try:
        snowflake_client.dry_run_query(rendered)
    except UnsafeSQLError as exc:
        raise ExcelReportError(f"query rejected by the read-only safety layer: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ExcelReportError(f"query failed to compile against Snowflake: {exc}") from exc

    # Real header row of the data sheet (e.g. row 2 under a title banner), read
    # from the stored template workbook — the source of truth for its layout.
    header_row = _data_sheet_header_row(template_id, chosen_sheet)

    tier1 = [
        {"sheet": e["sheet"], "cell": e["cell"], "formula": e["formula"],
         "on_data_sheet": e["sheet"] == chosen_sheet}
        for e in fclass.get("tier_1", [])
    ]
    tier1b = [
        {"sheet": b["sheet"], "column": b["column"], "start_row": b["start_row"],
         "end_row": b["end_row"], "rows": b["rows"],
         "template_formula": b["template_formula"],
         "on_data_sheet": b["sheet"] == chosen_sheet}
        for b in fclass.get("tier_1b", [])
    ]
    parameterized_query = {
        "sql_template": sql_template,
        "parameters": parameters,
        "data_sheet": chosen_sheet,
        "header_row": header_row,
        "tier1": tier1,
        "tier1b": tier1b,
    }
    plan_id = uuid.uuid4().hex
    _REPORT_PLAN_CACHE[plan_id] = {
        "plan": parameterized_query,
        "template_id": template_id,
        "report_title": report_title,
        "description": description,
    }

    return {
        "plan_id": plan_id,
        "template_id": template_id,
        "report_title": report_title,
        "data_sheet": chosen_sheet,
        "header_row": header_row,
        "parameters": parameters,
        "tier_1_regenerated": tier1,
        "tier_1b_blocks": tier1b,
        "tier_2_needs_review": fclass.get("tier_2", []),
        "validated": True,
    }


def _data_sheet_header_row(template_id: str, sheet_name: str) -> int:
    """Read the data sheet's header-row index from the stored template workbook."""
    path = _template_path(template_id)
    if not path.is_file():
        return 1
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        if sheet_name not in wb.sheetnames:
            return 1
        return scanner.header_row_index(wb[sheet_name])
    finally:
        wb.close()


def save_report(plan_id: str) -> dict[str, Any]:
    """Persist a cached report plan to MCP_EXCEL_REPORTS. Raises if plan_id expired."""
    entry = _REPORT_PLAN_CACHE.get(plan_id)
    if entry is None:
        raise ExcelReportError(
            f"No cached report plan for plan_id '{plan_id}' (it may have expired). "
            "Call plan_excel_report again, then save that plan."
        )
    report_id = uuid.uuid4().hex
    created_by, _role = snowflake_client.session_identity()
    _insert_report({
        "report_id": report_id,
        "template_id": entry["template_id"],
        "report_title": entry["report_title"],
        "description": entry.get("description") or None,
        "parameterized_query": entry["plan"],
        "created_by": created_by,
    })
    return {"report_id": report_id, "report_title": entry["report_title"], "saved": True}


# ---------------------------------------------------------------------------
# Multi-block reports — several queries across several sheets.
#
# The single-sheet path above binds ONE sheet to ONE query, which no real
# template fits. Saving that richer binding is what makes a multi-block report a
# REPORT rather than a one-off download: without it the builder could produce a
# file but nothing was persisted, so the Reports page had nothing to list and
# there was no way to re-run the same binding for next month.
# ---------------------------------------------------------------------------

def save_multiblock_report(template_id: str, report_title: str,
                           config: dict[str, Any],
                           description: str = "") -> dict[str, Any]:
    """Persist a multi-block binding to MCP_EXCEL_REPORTS.

    The whole binding — every block's SQL, the declared parameters, and the role
    and area corrections the user made — goes into the same PARAMETERIZED_QUERY
    variant the single-sheet path uses, tagged with `kind`. Storing the overrides
    matters as much as the SQL: block detection is heuristic, and a re-run that
    re-detected from scratch could bind a different set of areas than the one the
    user checked and approved.
    """
    block_sql = {k: v for k, v in (config.get("block_sql") or {}).items() if str(v).strip()}
    if not block_sql:
        raise ExcelReportError(
            "A multi-block report needs at least one area bound to a query."
        )
    report_id = uuid.uuid4().hex
    created_by, _role = snowflake_client.session_identity()
    _insert_report({
        "report_id": report_id,
        "template_id": template_id,
        "report_title": report_title or "Report",
        "description": description or None,
        "parameterized_query": {
            "kind": MULTIBLOCK_KIND,
            "block_sql": block_sql,
            "parameters": config.get("parameters") or [],
            "role_overrides": config.get("role_overrides") or {},
            "block_overrides": config.get("block_overrides") or {},
            "allow_unmatched_columns": bool(config.get("allow_unmatched_columns")),
            "allow_truncated_rows": bool(config.get("allow_truncated_rows")),
        },
        "created_by": created_by,
    })
    return {"report_id": report_id, "report_title": report_title or "Report",
            "template_id": template_id, "saved": True}


def publish_multiblock(scan_id: str, report_title: str, config: dict[str, Any],
                       description: str = "") -> dict[str, Any]:
    """Approve the scanned workbook AND save its binding as a runnable report.

    One call because it is one decision. Approving a template on its own leaves
    a workbook nobody can run, which is exactly the state the builder used to
    finish in — the file downloaded, the template stored, and the Reports page
    still empty.
    """
    template = save_template(scan_id)
    report = save_multiblock_report(
        template["template_id"], report_title, config, description
    )
    return {**template, **report}


def _run_multiblock_report(report_id: str, rec: dict[str, Any],
                           config: dict[str, Any],
                           parameter_values: dict[str, Any] | None) -> dict[str, Any]:
    """Re-run a saved multi-block binding for a new period."""
    from app.engine import excel_multiblock
    from app.engine import storage as storage_mod

    template_id = rec.get("TEMPLATE_ID")
    store = storage_mod.get_storage()
    key = storage_mod.template_key(str(template_id))
    if not store.exists(key):
        raise ExcelReportError(
            f"The stored workbook for template '{template_id}' is missing, so this "
            f"report cannot be regenerated. Re-upload the template."
        )

    try:
        result = excel_multiblock.run(
            store.get(key),
            config.get("block_sql") or {},
            excel_multiblock.snowflake_executor(),
            parameters=config.get("parameters") or [],
            parameter_values=parameter_values,
            allow_unmatched_columns=bool(config.get("allow_unmatched_columns")),
            allow_truncated_rows=bool(config.get("allow_truncated_rows")),
            role_overrides=config.get("role_overrides") or {},
            block_overrides=config.get("block_overrides") or {},
        )
    except excel_multiblock.MultiBlockError as exc:
        # Re-raised in this module's currency so the route surfaces it as a 400
        # like every other binding problem, rather than as a blank 500.
        raise ExcelReportError(str(exc)) from exc

    title = str(rec.get("REPORT_TITLE") or report_id)
    out_key = storage_mod.report_key(report_id, _now_stamp(), title)
    store.put(out_key, result.pop("workbook"))

    run_count, last_run_at = _touch_report_run(report_id)
    row_counts = result.get("row_counts") or {}
    return {
        "report_id": report_id,
        "report_title": title,
        "kind": MULTIBLOCK_KIND,
        "storage_key": out_key,
        "output_filename": out_key.rsplit("/", 1)[-1],
        # The Reports page shows one number. Across several blocks the honest
        # single figure is everything written, with the per-block split beside it.
        "row_count": sum(row_counts.values()),
        "row_counts": row_counts,
        "blocks_filled": result.get("blocks_filled", []),
        "blocks_regenerated": result.get("blocks_regenerated", []),
        "cells_moved": result.get("cells_moved", 0),
        "column_warnings": result.get("column_warnings", []),
        "lost_content": result.get("lost_content", []),
        "run_count": run_count,
        "last_run_at": last_run_at,
    }


def run_report(report_id: str, parameter_values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Substitute parameter values, run the read-only query, refill the template's
    data sheet by column name, re-extend Tier-1 ranges and Tier-1b row-mirror
    blocks to the new row count, write the output .xlsx, and bump the run count.
    Columns whose values come from the query (STEP 2) are populated as plain values
    and their local formulas are NOT regenerated. Zero LLM calls."""
    rec = _read_report(report_id)
    if rec is None:
        raise ExcelReportError(f"No active saved report with report_id '{report_id}'.")

    template_id = rec["TEMPLATE_ID"]
    pq = rec["PARAMETERIZED_QUERY"] or {}

    # Which runner this report needs is a property of what was saved, so the
    # branch lives here rather than in the route — every caller (HTTP, MCP tool,
    # a future scheduler) gets it without having to know the distinction exists.
    if pq.get("kind") == MULTIBLOCK_KIND:
        return _run_multiblock_report(report_id, rec, pq, parameter_values)

    rendered_sql = render_sql(pq.get("sql_template", ""), pq.get("parameters", []), parameter_values)
    # Read-only safety validation identical to run_query (SELECT/WITH/UNION only),
    # but capped at the higher, report-specific EXCEL_MAX_ROWS.
    result = _run_report_query(rendered_sql)

    template_path = _template_path(template_id)
    if not template_path.is_file():
        raise ExcelReportError(
            f"stored template workbook missing for template_id '{template_id}' "
            f"(expected {template_path}). Re-save the template."
        )

    import openpyxl
    from openpyxl.utils import column_index_from_string

    wb = openpyxl.load_workbook(template_path, data_only=False)
    try:
        data_sheet = pq["data_sheet"]
        header_row = int(pq.get("header_row", 1))
        if data_sheet not in wb.sheetnames:
            raise ExcelReportError(f"template has no data sheet '{data_sheet}'.")
        ws = wb[data_sheet]
        header_by_col = scanner.header_labels(ws, header_row)

        # STEP 2 — columns the query supplies by matching name become plain values.
        value_sourced = resolve_value_sourced_columns(header_by_col, result["columns"])
        fill = fill_data_sheet_by_name(ws, header_by_col, header_row,
                                       result["columns"], result["rows"])
        data_last_row = fill["new_last_row"]
        n = len(result["rows"])

        # If we hit the report row cap, confirm real truncation via a pre-cap
        # COUNT(*) and write a visible notice ABOVE the data (not a data row).
        capped = False
        total_rows = result["row_count"]
        truncation_notice = None
        if result["row_count"] >= EXCEL_MAX_ROWS:
            counted = _count_total_rows(rendered_sql)
            if counted is not None and counted > result["row_count"]:
                capped = True
                total_rows = counted
                truncation_notice = _write_truncation_notice(
                    ws, header_row, header_by_col, result["row_count"], counted)

        # Tier 1 — extend data-sheet-tracking ranges to the new last row.
        tier1_regen: list[dict[str, Any]] = []
        for e in pq.get("tier1", []):
            col_letter = re.match(r"[A-Za-z]+", e["cell"]).group(0)
            if e.get("on_data_sheet") and col_letter in value_sourced:
                continue  # value-sourced column — leave to the query values
            new_f = regenerate_tier1_formula(
                e["formula"], data_last_row, data_sheet, e.get("on_data_sheet", False))
            if new_f != e["formula"]:
                tgt = wb[e["sheet"]]
                cell_obj = tgt[e["cell"]]
                if _safe_set(tgt, cell_obj.row, cell_obj.column, new_f):
                    tier1_regen.append({"sheet": e["sheet"], "cell": e["cell"],
                                        "from": e["formula"], "to": new_f})

        # Tier 1b — extend/shrink each row-mirror block to n rows.
        tier1b_regen: list[dict[str, Any]] = []
        value_sourced_blocks: list[dict[str, Any]] = []
        for b in pq.get("tier1b", []):
            col = b["column"]
            if b.get("on_data_sheet") and col in value_sourced:
                value_sourced_blocks.append(
                    {"sheet": b["sheet"], "column": col, "query_column": value_sourced[col]})
                continue  # populated as plain values from the query instead
            target = wb[b["sheet"]]
            ci = column_index_from_string(col)
            start = int(b["start_row"])
            new_end = start + n - 1
            old_end = int(b.get("end_row", start - 1))
            for r in range(start, max(old_end, new_end) + 1):
                _safe_set(target, r, ci, None)
            for k, formula in enumerate(regenerate_block_formulas(b["template_formula"], n)):
                _safe_set(target, start + k, ci, formula)
            tier1b_regen.append({"sheet": b["sheet"], "column": col, "rows": n,
                                 "start_row": start, "end_row": new_end})

        force_recalculation_on_open(wb)

        out_dir = EXCEL_STORAGE_DIR / "reports" / report_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{report_id}_{_now_stamp()}.xlsx"
        wb.save(out_path)
    finally:
        wb.close()

    run_count, last_run_at = _touch_report_run(report_id)
    return {
        "report_id": report_id,
        "report_title": rec.get("REPORT_TITLE"),
        "output_path": str(out_path),
        "output_filename": out_path.name,
        "row_count": result["row_count"],
        "row_cap": EXCEL_MAX_ROWS,
        "total_rows": total_rows,
        "capped": capped,
        "truncation_notice": truncation_notice,
        "data_last_row": data_last_row,
        "value_sourced_columns": value_sourced,
        "value_sourced_blocks": value_sourced_blocks,
        "filled_columns": fill["filled_cols"],
        "tier1_regenerated": tier1_regen,
        "tier1b_regenerated": tier1b_regen,
        "run_count": run_count,
        "last_run_at": last_run_at,
    }


# ---------------------------------------------------------------------------
# Listing / metadata (for the webapp Excel Reports tab). Read-only, no run.
# ---------------------------------------------------------------------------

def _template_name(template_id: str | None) -> str | None:
    """The template's display name (even if the template was later soft-deleted)."""
    if not template_id:
        return None
    sql = f"SELECT TEMPLATE_NAME FROM {EXCEL_TEMPLATES_TABLE} WHERE TEMPLATE_ID = %s"
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, (template_id,))
        row = cur.fetchone()
    finally:
        cur.close()
    return row[0] if row else None


def list_reports(limit: int = 50) -> list[dict[str, Any]]:
    """Active saved Excel reports, newest first — metadata only (no run). Joins the
    templates table so each report carries the name of the template it uses."""
    n = max(1, min(int(limit), 500))
    sql = (
        "SELECT r.REPORT_ID, r.REPORT_TITLE, r.TEMPLATE_ID, t.TEMPLATE_NAME, "
        "r.CREATED_BY, r.CREATED_AT, r.RUN_COUNT, r.LAST_RUN_AT "
        f"FROM {EXCEL_REPORTS_TABLE} r "
        f"LEFT JOIN {EXCEL_TEMPLATES_TABLE} t ON r.TEMPLATE_ID = t.TEMPLATE_ID "
        f"WHERE r.IS_ACTIVE = TRUE ORDER BY r.CREATED_AT DESC LIMIT {n}"
    )
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    out = []
    for r in rows:
        rec = {col: snowflake_client._jsonify(val) for col, val in zip(cols, r)}
        out.append({
            "report_id": rec.get("REPORT_ID"),
            "report_title": rec.get("REPORT_TITLE"),
            "template_id": rec.get("TEMPLATE_ID"),
            "template_name": rec.get("TEMPLATE_NAME"),
            "created_by": rec.get("CREATED_BY"),
            "created_at": rec.get("CREATED_AT"),
            "run_count": rec.get("RUN_COUNT"),
            "last_run_at": rec.get("LAST_RUN_AT"),
        })
    return out


def get_report(report_id: str) -> dict[str, Any] | None:
    """One report's metadata + declared parameters, WITHOUT running it. Includes a
    pointer to the most recent generated file (if any). Returns None if not found."""
    rec = _read_report(report_id)
    if rec is None:
        return None
    pq = rec.get("PARAMETERIZED_QUERY") or {}
    kind = pq.get("kind") or "single_sheet"
    # One listing, trimmed for the page. Older runs stay downloadable by name —
    # the cap is what the card shows, not what exists.
    all_runs = list_report_outputs(report_id)
    runs = all_runs[:REPORT_RUNS_SHOWN]
    return {
        "report_id": rec.get("REPORT_ID"),
        "report_title": rec.get("REPORT_TITLE"),
        "description": rec.get("DESCRIPTION"),
        "template_id": rec.get("TEMPLATE_ID"),
        "template_name": _template_name(rec.get("TEMPLATE_ID")),
        "kind": kind,
        # What a multi-block report fills, in the sheet's own terms — the Reports
        # page has no other way to say what pressing Generate is about to do.
        "areas": sorted(pq.get("block_sql") or {}),
        "data_sheet": pq.get("data_sheet"),
        "parameters": pq.get("parameters", []),
        "run_count": rec.get("RUN_COUNT"),
        "last_run_at": rec.get("LAST_RUN_AT"),
        # The most recent files, newest first. Costs nothing extra: finding the
        # latest already listed the whole prefix.
        "runs": runs,
        "runs_total": len(all_runs),
        "latest_output": runs[0] if runs else None,
    }


def delete_report(report_id: str) -> dict[str, Any]:
    """Retire a saved Excel report so it stops appearing in the Reports list.

    A soft delete: IS_ACTIVE = FALSE, which every read path here already filters
    on. Generated workbooks are deliberately left in storage — they are what
    somebody was sent last month, and a tidy-up of the list should not
    invalidate a file already circulating. The template stays too, since other
    reports may be bound to it.
    """
    stmt = (
        f"UPDATE {EXCEL_REPORTS_TABLE} SET IS_ACTIVE = FALSE "
        "WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE"
    )
    rec = _read_report(report_id)
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(stmt, (report_id,))
        retired = int(cur.rowcount or 0)
    finally:
        cur.close()
    if not retired:
        raise ExcelReportError(
            f"No active saved report with report_id '{report_id}'; nothing deleted."
        )
    return {
        "report_id": report_id,
        "report_title": (rec or {}).get("REPORT_TITLE"),
        "deleted": True,
    }


def _stamped_at(name: str) -> str | None:
    """The UTC stamp a generated filename ends with, as an ISO timestamp."""
    m = re.search(r"_(\d{8}T\d{6}Z)\.xlsx$", name)
    if not m:
        return None
    return (datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=timezone.utc).isoformat())


def list_report_outputs(report_id: str,
                        limit: int | None = None) -> list[dict[str, Any]]:
    """Generated .xlsx files for a report, newest first.

    `limit` trims the list for DISPLAY only. Lookups must not pass it — see
    find_report_output, which has to search everything or a link to an older
    run would break the moment newer ones pushed it past the cut.

    Configured storage first — that is where multi-block runs write, and under a
    bucket there is no local directory to glob. The local scan stays as the
    fallback so reports generated before storage existed are still downloadable.

    Ordered by the UTC stamp in the filename rather than by mtime: object
    storage has no mtime to sort by, and the stamp is the only ordering that
    means the same thing on both backends.

    Each run is kept rather than overwritten because the data underneath moves.
    Re-running last quarter's report today gives different numbers for the same
    as_of_date — so the file that was sent is the only record of what was sent.
    """
    from app.engine import storage as storage_mod

    try:
        store = storage_mod.get_storage()
        keys = [k for k in store.list_prefix(storage_mod.reports_prefix(report_id))
                if k.lower().endswith(".xlsx")]
    except Exception:  # noqa: BLE001 - an unreachable bucket falls back to disk
        keys = []
    if keys:
        out = [{"storage_key": k, "filename": k.rsplit("/", 1)[-1],
                "generated_at": _stamped_at(k.rsplit("/", 1)[-1])}
               for k in keys]
        out.sort(key=lambda r: (r["generated_at"] or "", r["filename"]), reverse=True)
        return out[:limit] if limit else out

    d = EXCEL_STORAGE_DIR / "reports" / report_id
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = [{"path": str(p), "filename": p.name,
            "generated_at": _stamped_at(p.name) or datetime.fromtimestamp(
                p.stat().st_mtime, timezone.utc).isoformat()}
           for p in files]
    return out[:limit] if limit else out


def count_report_outputs(report_id: str) -> int:
    """How many runs exist in total, so a trimmed list can say what it is
    hiding. A list of five that silently stands for twenty-three is the same
    class of problem as a truncated query that reports no truncation."""
    return len(list_report_outputs(report_id))


def latest_report_output(report_id: str) -> dict[str, Any] | None:
    """The most recently generated .xlsx for a report, or None."""
    runs = list_report_outputs(report_id)
    return runs[0] if runs else None


def find_report_output(report_id: str, filename: str) -> dict[str, Any] | None:
    """One named run of a report.

    Resolved by MEMBERSHIP in the report's own listing rather than by joining
    the name onto a path. The filename arrives from the client, and a download
    endpoint that builds a path out of it is one traversal away from serving
    anything on the disk or in the bucket.
    """
    for run in list_report_outputs(report_id):
        if run["filename"] == filename:
            return run
    return None

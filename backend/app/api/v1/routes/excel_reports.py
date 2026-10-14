"""Excel template reports: upload, approve, plan, save, run, download.

Thin REST over the ported engine, in the same shape as dashboards.py — the
engine is synchronous, so every call goes through asyncio.to_thread with the
signed-in user's connection bound inside that thread.

The flow this exposes, which mirrors the dashboards one:

    scan     upload an .xlsx; the scanner classifies sheet roles and formula
             tiers and caches the result under a scan_id
    approve  persist that exact scan as a template
    plan     bind a template to parameterised read-only SQL
    save     persist the plan as a report
    run      substitute the period, query, refill the data sheet, re-extend the
             formulas, write the .xlsx
    download return the generated workbook

Upload is a real multipart POST rather than a chat attachment. The MCP
deployment used a drop-folder because a file attached in chat lives in the
model's sandbox and passing a workbook through the model as base64 is
impractical — a constraint that simply does not exist over HTTP.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi import Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_session, require_admin
from app.core.config import settings
from app.engine import excel_reports as engine_excel
from app.engine import excel_multiblock as engine_multiblock
from app.engine import storage as engine_storage
from app.engine.connectors.snowflake import client as engine_client
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

router = APIRouter(prefix="/excel", tags=["excel-reports"])

# Workbooks are small; this only stops an accidental upload of something huge.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


async def _run_engine(request: Request, session: SessionData, fn, *args, **kwargs):
    """Run a blocking engine call off the event loop, as the signed-in user."""
    service = SnowflakeService(
        settings, request.app.state.session_store, request.app.state.token_encryptor
    )
    connection = await service.get_connection(session)
    identity = (session.snowflake_user, session.role)

    def call():
        with engine_client.use_connection(connection, identity=identity):
            return fn(*args, **kwargs)

    return await asyncio.to_thread(call)


def _engine_error(exc: Exception) -> HTTPException:
    """Engine failures are user-facing input problems far more often than bugs —
    a template with no data sheet, SQL that fails validation, an expired plan.
    Surfacing the message beats a blank 500."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    )


# ── Templates ─────────────────────────────────────────────────────────────


@router.post("/templates/scan")
async def scan_template(
    request: Request,
    file: UploadFile = File(...),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Upload an .xlsx and classify it. Persists nothing until approved."""
    name = file.filename or "workbook.xlsx"
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .xlsx or .xlsm workbooks can be scanned.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Workbook exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
        )

    import base64

    try:
        result = await _run_engine(
            request,
            session,
            engine_excel.scan_template,
            None,
            base64.b64encode(data).decode("ascii"),
            name,
        )
        # Also keep the upload in configured storage, keyed by scan id. The
        # engine writes it to local disk for the legacy single-sheet flow; the
        # multi-block flow reads it back from here, which is what lets a bucket
        # replace the container filesystem — and what lets a second uvicorn
        # worker find a file the first one received.
        try:
            engine_storage.get_storage().put(
                engine_storage.upload_key(result["scan_id"]), data
            )
        except Exception:  # noqa: BLE001 - storage is a bonus here, not the path
            pass
        return result
    except engine_excel.ExcelReportError as exc:
        raise _engine_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - a corrupt workbook is user input
        raise _engine_error(exc) from exc


@router.post("/templates/{scan_id}/approve")
async def approve_template(
    scan_id: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Persist a reviewed scan as a template the reports can bind to."""
    try:
        return await _run_engine(
            request, session, engine_excel.save_template, scan_id
        )
    except engine_excel.ExcelReportError as exc:
        raise _engine_error(exc) from exc


# ── Reports ───────────────────────────────────────────────────────────────


class PlanRequest(BaseModel):
    template_id: str
    report_title: str
    sql_template: str
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    data_sheet: str = ""
    description: str = ""


@router.post("/reports/plan")
async def plan_report(
    body: PlanRequest,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Bind a template to parameterised SQL. Saves nothing — returns a plan."""
    try:
        return await _run_engine(
            request,
            session,
            engine_excel.plan_report,
            body.template_id,
            body.report_title,
            body.sql_template,
            body.parameters,
            body.data_sheet,
            body.description,
        )
    except engine_excel.ExcelReportError as exc:
        raise _engine_error(exc) from exc


@router.post("/reports/{plan_id}/save")
async def save_report(
    plan_id: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Persist a plan as a saved report, runnable for any period."""
    try:
        return await _run_engine(
            request, session, engine_excel.save_report, plan_id
        )
    except engine_excel.ExcelReportError as exc:
        raise _engine_error(exc) from exc


@router.get("/reports")
async def list_reports(
    request: Request,
    limit: int = 50,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Saved Excel reports, newest first. Metadata only — nothing runs."""
    items = await _run_engine(request, session, engine_excel.list_reports, limit)
    return {"count": len(items), "reports": items}


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """One report's metadata and declared parameters, without running it."""
    report = await _run_engine(request, session, engine_excel.get_report, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Report not found.")
    return report


@router.delete("/reports/{report_id}")
async def delete_report(
    report_id: str,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """Retire a saved report. Administrators only.

    Anyone signed in can generate a report; only an admin can remove one, for
    the same reason as dashboards — a saved report is shared, so deleting it
    takes it away from everybody. Generated workbooks stay in storage: one of
    them may already be attached to an email somebody sent.
    """
    try:
        return await _run_engine(
            request, session, engine_excel.delete_report, report_id
        )
    except engine_excel.ExcelReportError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc


@router.post("/reports/{report_id}/run")
async def run_report(
    report_id: str,
    request: Request,
    parameter_values: dict[str, Any] = Body(default_factory=dict),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Generate the workbook for a period. The query runs read-only, through
    the same validate_sql gate as every other query in the product."""
    try:
        return await _run_engine(
            request, session, engine_excel.run_report, report_id, parameter_values
        )
    except engine_excel.ExcelReportError as exc:
        raise _engine_error(exc) from exc


# ── Multi-block reports ───────────────────────────────────────────────────
#
# The routes above bind ONE sheet to ONE query. Real templates need several:
# CreditFund_RiskDashboard takes three queries across two sheets, and its other
# two sheets must receive no data at all because the workbook computes them.
# These sit alongside rather than replacing them, so nothing already saved
# changes behaviour.


def _scan_source(scan_id: str) -> bytes:
    """The uploaded workbook behind a cached scan.

    Configuration has to happen BEFORE approval — being asked to commit a
    template before seeing whether the app read it correctly is backwards — so
    the block scan reads the cached upload rather than a stored template.
    """
    # Storage first: it is the only copy every worker can see. The in-process
    # cache is the fallback, and the reason Report Builder cannot be enabled in
    # production until the cache itself is shared.
    store = engine_storage.get_storage()
    key = engine_storage.upload_key(scan_id)
    if store.exists(key):
        return store.get(key)

    entry = engine_excel._SCAN_CACHE.get(scan_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cached scan '{scan_id}' — it may have expired. Upload again.",
        )
    return Path(entry["source_path"]).read_bytes()


@router.post("/scans/{scan_id}/blocks")
async def scan_blocks_for_upload(
    scan_id: str,
    overrides: dict[str, Any] = Body(default_factory=dict),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """What a report over this UPLOAD would need, before anything is saved.

    `role_overrides` corrects the detection: {"Sheet1": "source"} forces a sheet
    to take data even if its formulas made it look computed."""
    try:
        return await asyncio.to_thread(
            engine_multiblock.plan, _scan_source(scan_id),
            overrides.get("roles") or {}, overrides.get("areas") or {},
        )
    except engine_multiblock.MultiBlockError as exc:
        raise _engine_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable workbook is input
        raise _engine_error(exc) from exc


@router.post("/templates/{template_id}/blocks")
async def scan_template_blocks(
    template_id: str,
    request: Request,
    role_overrides: dict[str, str] = Body(default_factory=dict),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """What a report over this template would need: which blocks take a query,
    which sheets the workbook computes itself, and anything unclear. Read-only."""
    store = engine_storage.get_storage()
    key = engine_storage.template_key(template_id)
    if not store.exists(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No stored workbook for template '{template_id}'.",
        )
    try:
        # Deliberately NOT through _run_engine: reading a stored workbook needs
        # no database, and wrapping it in a Snowflake connection made template
        # scanning fail with "authentication token has expired" — a warehouse
        # round-trip and a failure mode bought for nothing.
        return await asyncio.to_thread(
            engine_multiblock.plan, store.get(key), role_overrides
        )
    except engine_multiblock.MultiBlockError as exc:
        raise _engine_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - an unreadable workbook is input
        raise _engine_error(exc) from exc


class MultiBlockRunRequest(BaseModel):
    # Exactly one of these. A scan_id runs against the upload so the result can
    # be seen before committing; a template_id runs a template already approved.
    template_id: str = ""
    scan_id: str = ""
    report_title: str = "Report"
    # {"LOAN_DATA!3": "SELECT …"} — one statement per block that takes data.
    block_sql: dict[str, str]
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    # Off by default: a template column the query does not supply is left blank,
    # and a silently blank column in a report is worse than a refusal.
    allow_unmatched_columns: bool = False
    # Off for the same reason: a report cut short by the row cap computes
    # its totals over the rows that fit and reads as if they were
    # everything. Opting in is a decision, not a default.
    allow_truncated_rows: bool = False
    # Corrections to the detected sheet roles: {"Sheet1": "source"}.
    role_overrides: dict[str, str] = Field(default_factory=dict)
    block_overrides: dict[str, bool] = Field(default_factory=dict)


# NOT "/reports/multiblock/run": `/reports/{report_id}/run` is declared above
# it, and FastAPI matches in definition order — so that path would bind
# report_id="multiblock" and quietly run the single-block route instead.
@router.post("/multiblock/run")
async def run_multiblock(
    body: MultiBlockRunRequest,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Generate a workbook from several queries. Each statement runs read-only
    through the same validate_sql gate as every other query in the product."""
    if bool(body.template_id) == bool(body.scan_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of template_id or scan_id.",
        )

    store = engine_storage.get_storage()
    if body.scan_id:
        template_bytes = _scan_source(body.scan_id)
        report_owner = body.scan_id
    else:
        template_key = engine_storage.template_key(body.template_id)
        if not store.exists(template_key):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No stored workbook for template '{body.template_id}'.",
            )
        template_bytes = store.get(template_key)
        report_owner = body.template_id

    def call():
        return engine_multiblock.run(
            template_bytes,
            body.block_sql,
            engine_multiblock.snowflake_executor(),
            parameters=body.parameters,
            parameter_values=body.parameter_values,
            allow_unmatched_columns=body.allow_unmatched_columns,
            allow_truncated_rows=body.allow_truncated_rows,
            role_overrides=body.role_overrides,
            block_overrides=body.block_overrides,
        )

    try:
        result = await _run_engine(request, session, call)
    except engine_multiblock.MultiBlockError as exc:
        raise _engine_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - a bad query is user input
        raise _engine_error(exc) from exc

    # The engine hands back bytes; storing them is this layer's job, which is
    # what lets the same code write to a bucket once one exists.
    stamp = engine_excel._now_stamp()
    key = engine_storage.report_key(report_owner, stamp, body.report_title)
    store.put(key, result.pop("workbook"))

    return {**result, "report_title": body.report_title, "storage_key": key,
            "output_filename": key.rsplit("/", 1)[-1],
            "template_id": body.template_id, "scan_id": body.scan_id,
            "download_id": report_owner}


class MultiBlockSaveRequest(BaseModel):
    """The binding as configured in the builder, saved so it can be re-run."""

    report_title: str = "Report"
    description: str = ""
    block_sql: dict[str, str]
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    allow_unmatched_columns: bool = False
    # Off for the same reason: a report cut short by the row cap computes
    # its totals over the rows that fit and reads as if they were
    # everything. Opting in is a decision, not a default.
    allow_truncated_rows: bool = False
    role_overrides: dict[str, str] = Field(default_factory=dict)
    block_overrides: dict[str, bool] = Field(default_factory=dict)


@router.post("/scans/{scan_id}/publish")
async def publish_multiblock(
    scan_id: str,
    body: MultiBlockSaveRequest,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Approve the workbook as a template AND save its binding as a report.

    The builder used to call `/templates/{scan_id}/approve` alone, which stored a
    workbook and nothing else: no block SQL, no parameters, no row in the reports
    table. The file downloaded, and the Reports page stayed empty. One call now
    persists both halves, so a saved report is a thing you can come back to and
    re-run for another period — which is the entire point of the feature.
    """
    try:
        return await _run_engine(
            request,
            session,
            engine_excel.publish_multiblock,
            scan_id,
            body.report_title,
            body.model_dump(exclude={"report_title", "description"}),
            body.description,
        )
    except engine_excel.ExcelReportError as exc:
        raise _engine_error(exc) from exc


@router.get("/templates/{template_id}/download")
async def download_generated(
    template_id: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> FileResponse:
    """The most recent multi-block output for a template."""
    store = engine_storage.get_storage()
    keys = store.list_prefix(engine_storage.reports_prefix(template_id))
    if not keys:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Nothing generated yet — run the report first.")
    # Filenames carry a UTC stamp, so lexical order is chronological — and it
    # works against object storage, which has no mtime to sort by.
    key = sorted(keys)[-1]
    # Streamed through the backend rather than handed out as a presigned URL:
    # a presigned link bypasses the session check, and these are investor
    # reports. Workbooks are small enough that proxying costs nothing.
    return Response(
        content=store.get(key),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{key.rsplit("/", 1)[-1]}"'},
    )


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: str,
    request: Request,
    run: str = "",
    session: SessionData = Depends(get_current_session),
) -> FileResponse:
    """A generated workbook for a report — the newest, or the named `run`.

    `run` is a filename from the report's own run list, resolved by looking it
    up in that list rather than by joining it onto a path. It never becomes
    path structure, so it cannot address anything outside this report.
    """
    if run:
        latest = await _run_engine(
            request, session, engine_excel.find_report_output, report_id, run
        )
        if not latest:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"This report has no run called {run!r}.",
            )
    else:
        latest = await _run_engine(
            request, session, engine_excel.latest_report_output, report_id
        )
    if not latest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No generated output yet — run the report first.",
        )

    # Configured storage, where every multi-block run writes — and where a bucket
    # deployment keeps everything. Streamed through the backend rather than
    # presigned: a presigned link bypasses the session check.
    key = latest.get("storage_key")
    if key:
        return Response(
            content=engine_storage.get_storage().get(str(key)),
            media_type="application/vnd.openxmlformats-officedocument."
                       "spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f'attachment; filename="{latest.get("filename") or "report.xlsx"}"'},
        )

    # A local file from the single-sheet path, from before storage existed.
    if not latest.get("path"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No generated output yet — run the report first.",
        )
    path = Path(str(latest["path"]))
    # The path comes from our own storage record, but resolve and re-check it
    # anyway: serving a file by a path read out of a database is exactly where a
    # traversal would land.
    storage_root = engine_excel.EXCEL_STORAGE_DIR.resolve()
    resolved = path.resolve()
    if not resolved.is_file() or storage_root not in resolved.parents:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Generated file is missing.")

    return FileResponse(
        resolved,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=str(latest.get("filename") or resolved.name),
    )

"""Saved dashboards: list, run live, and version history.

Snowflake warehouse dashboards use the Snowflake-bound engine path. File/folder
dashboards (upload sources) bind DuckDB via source_binding and use
file_dashboards.py — same REST shapes, different backend.
"""

import asyncio
import base64
import binascii
import functools
import hashlib
import json
import logging
import secrets
import threading
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import get_current_session, require_admin
from app.conversation import Conversation
from app.core.config import settings
from app.engine import dashboard_export
from app.engine import dashboards as engine_dashboards
from app.engine import file_dashboards as engine_file_dashboards
from app.engine import source_binding
from app.engine.connectors.duckdb import client as duckdb_client
from app.engine.connectors.snowflake import client as engine_client
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


async def _bound_connection(request: Request, session: SessionData):
    """This session's Snowflake connection, for binding into engine calls."""
    service = SnowflakeService(
        settings, request.app.state.session_store, request.app.state.token_encryptor
    )
    return await service.get_connection(session)


async def _run_snowflake_engine(request: Request, session: SessionData, fn, *args):
    """Run a blocking Snowflake dashboard call off the event loop."""
    connection = await _bound_connection(request, session)
    identity = (session.snowflake_user, session.role)

    def call():
        with engine_client.use_connection(connection, identity=identity):
            return fn(*args)

    return await asyncio.to_thread(call)


async def _run_ai_call(request: Request | None, session: SessionData, fn, *args):
    """Run a model call with the signed-in user's connection bound, so its audit
    row is written as them.

    Best-effort binding: these calls never needed Snowflake, and a warehouse
    that cannot be reached must cost the audit row, not the summary.
    """
    try:
        connection = await _bound_connection(request, session)
    except Exception:  # noqa: BLE001 - the call itself does not need the connection
        log.warning("no Snowflake connection to audit %s with", getattr(fn, "__name__", fn),
                    exc_info=True)
        return await asyncio.to_thread(fn, *args)
    identity = (session.snowflake_user, session.role)

    def call():
        with engine_client.use_connection(connection, identity=identity):
            return fn(*args)

    return await asyncio.to_thread(call)


async def _run_file_dashboard(
    request: Request,
    session: SessionData,
    report_id: str,
    fn: Callable[..., Any],
    *args,
):
    """Run a file-dashboard call with DuckDB bound to the saved source.

    Resolving FOLDER:/EXCEL: metadata (list_collections, get_dataset) goes
    through Snowflake tables — the same as the agent chat — so Snowflake must
    be bound before DuckDB is attached for panel SQL.
    """
    rec = engine_file_dashboards.read_record(report_id)
    if rec is None:
        raise engine_dashboards.DashboardError(
            f"No active saved dashboard with report_id '{report_id}'."
        )
    connection = await _bound_connection(request, session)
    identity = (session.snowflake_user, session.role)
    source_key = rec.get("source_key") or ""

    def call():
        with engine_client.use_connection(connection, identity=identity):
            with source_binding.bind_upload_source(source_key, identity):
                return fn(report_id, duckdb_client, *args)

    try:
        return await asyncio.to_thread(call)
    except ValueError as exc:
        raise engine_dashboards.DashboardError(str(exc)) from exc


def _is_file_dashboard(report_id: str) -> bool:
    return engine_file_dashboards.exists(report_id)


@router.get("")
async def list_dashboards(
    request: Request,
    limit: int = 200,
    source_key: str | None = None,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Saved dashboards, newest first. Metadata only — no panel SQL runs."""
    snowflake_items = await _run_snowflake_engine(
        request, session, engine_dashboards.list_dashboards, limit
    )
    for item in snowflake_items:
        item.setdefault("source_type", "snowflake")

    file_items = engine_file_dashboards.list_dashboards(
        source_key=source_key,
        limit=limit,
    )
    merged = snowflake_items + file_items
    for item in merged:
        item["data_group"] = _data_group(item)
    merged.sort(
        key=lambda r: r.get("last_run_at") or r.get("updated_at") or "",
        reverse=True,
    )
    merged = merged[: max(1, min(int(limit), 500))]
    return {"count": len(merged), "dashboards": merged}


#: Dashboards built from an upload rather than a warehouse. One bucket, not one
#: per file: a folder of spreadsheets is a source, not a database.
FILE_DATA_GROUP = "Dashboards created from files"

#: A dashboard whose database could not be determined — hand-written SQL, or a
#: database since removed from the semantic layer. Named rather than hidden, so
#: nothing drops out of the list silently.
UNGROUPED = "Ungrouped"


def _data_group(item: dict[str, Any]) -> str:
    """Which folder a dashboard belongs in.

    The database today. The spreadsheet this came from calls it a DataGroup and
    puts databases, folders, dashboards and reports under one, so this returns a
    NAME rather than a database reference — when data groups become real, the
    name is what moves, and nothing here has to change shape.
    """
    # A deliberate filing decision outranks everything: somebody moved this.
    override = (item.get("data_group_override") or "").strip()
    if override:
        return override

    if item.get("source_type") == "file":
        return FILE_DATA_GROUP

    database = (item.get("database") or "").strip()
    if database:
        return database.upper()

    # Nothing stored: either the migration has not run, or this row predates
    # the column. The planner puts the database in quotes at the end of the
    # title, so read it from there rather than showing a folder of everything.
    from_title = engine_dashboards.infer_database_from_title(
        item.get("report_title")
    )
    return from_title.upper() if from_title else UNGROUPED


@router.get("/{report_id}")
async def get_dashboard(
    report_id: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Live data for one dashboard, every parameter at its saved default."""
    try:
        if _is_file_dashboard(report_id):
            return await _run_file_dashboard(
                request,
                session,
                report_id,
                engine_file_dashboards.run_dashboard,
                None,
            )
        return await _run_snowflake_engine(
            request, session, engine_dashboards.run_dashboard, report_id, None
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{report_id}/run")
async def run_dashboard(
    report_id: str,
    request: Request,
    payload: dict = Body(default={}),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Run with explicit parameter values from the UI's controls."""
    values = (payload or {}).get("parameter_values") or {}
    try:
        if _is_file_dashboard(report_id):
            return await _run_file_dashboard(
                request,
                session,
                report_id,
                engine_file_dashboards.run_dashboard,
                values,
            )
        return await _run_snowflake_engine(
            request, session, engine_dashboards.run_dashboard, report_id, values
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{report_id}/config")
async def get_dashboard_config(
    report_id: str,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """The stored configuration behind a dashboard — every panel's SQL included."""
    try:
        if _is_file_dashboard(report_id):
            return engine_file_dashboards.get_dashboard_config(report_id)
        return await _run_snowflake_engine(
            request, session, engine_dashboards.get_dashboard_config, report_id
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.put("/{report_id}")
async def update_dashboard(
    report_id: str,
    request: Request,
    payload: dict = Body(...),
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """Replace a dashboard's configuration. Administrators only.

    `assistant_conversation_id`, when the Edit panel's assistant was used,
    names its thread: a successful save adds a line there saying which version
    the draft became, so the thread shows what was saved and what was only
    drafted.
    """
    if _is_file_dashboard(report_id):
        try:
            result = await _run_file_dashboard(
                request,
                session,
                report_id,
                engine_file_dashboards.update_plan,
                payload.get("plan") or {},
                payload.get("edit_note") or "",
            )
        except engine_dashboards.DashboardError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
    else:
        try:
            result = await _run_snowflake_engine(
                request,
                session,
                engine_dashboards.update_plan,
                report_id,
                payload.get("plan") or {},
                payload.get("edit_note") or "",
            )
        except engine_dashboards.DashboardError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    conversation_id = payload.get("assistant_conversation_id")
    if isinstance(conversation_id, str) and conversation_id and result.get("version"):
        await _note_assistant_save(request, session, report_id, conversation_id,
                                   int(result["version"]))
    return result


async def _note_assistant_save(request: Request, session: SessionData, report_id: str,
                               conversation_id: str, version: int) -> None:
    """Record a save in the Edit-panel assistant's thread. Best-effort: the
    dashboard is already saved, and a missing note must not turn that into an
    error."""
    try:
        store = request.app.state.conversation_store
        conversation = await store.get(conversation_id)
        if (conversation is None or conversation.owner != session.snowflake_user
                or conversation.kind != "dashboard_edit"
                or conversation.dashboard_id != report_id):
            return
        conversation.append(
            "assistant", [{"type": "text", "text": f"Saved as version {version}."}]
        )
        await store.save(conversation, settings.CONVERSATION_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        log.warning("could not note the save in assistant thread %s", conversation_id,
                    exc_info=True)


#: What the Edit panel can record in its assistant thread without a model call.
#: Fixed wording, chosen by key, so the endpoint cannot be used to write
#: arbitrary text into a thread the model later reads as its own words.
_ASSIST_NOTES = {
    "undo": "Undid the last change to the draft.",
    "not_applied": (
        "That change was not applied: the draft was edited while it was being "
        "prepared. The draft is as it was before this request."
    ),
}


class AssistNoteRequest(BaseModel):
    conversation_id: str
    event: str


@router.post("/{report_id}/assist/note")
async def assist_note(
    report_id: str,
    body: AssistNoteRequest,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """Record something that happened to the draft outside the assistant —
    an Undo, or an answer the panel could not apply — so the thread does not
    go on describing a change the draft no longer has."""
    text = _ASSIST_NOTES.get(body.event)
    if text is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Unknown note.")
    store = request.app.state.conversation_store
    conversation = await store.get(body.conversation_id)
    if (conversation is None or conversation.owner != session.snowflake_user
            or conversation.kind != "dashboard_edit"
            or conversation.dashboard_id != report_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Conversation not found")
    conversation.append("assistant", [{"type": "text", "text": text}])
    await store.save(conversation, settings.CONVERSATION_TTL_SECONDS)
    return {"conversation_id": conversation.conversation_id, "message": text}


class AssistRequest(BaseModel):
    message: str
    #: The draft on screen: {title, panels, parameters}.
    plan: dict[str, Any]
    #: Omitted on the first message; the response carries the id to send next.
    conversation_id: str | None = None


def _assist_history(conversation: Conversation) -> list[dict[str, Any]]:
    """The thread's earlier turns as plain text."""
    history = []
    for message in conversation.messages:
        text = "\n\n".join(
            str(block.get("text") or "") for block in message.content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if text:
            history.append({"role": message.role, "text": text})
    return history


def _assist_snowflake(report_id: str, draft: dict[str, Any], message: str,
                      history: list[dict[str, Any]]) -> dict[str, Any]:
    from app.engine import dashboard_assist
    from app.engine.metadata_loader import metadata_store

    rec = engine_client.read_saved_dashboard(report_id)
    if rec is None:
        raise engine_dashboards.DashboardError(
            f"No active saved dashboard with report_id '{report_id}'."
        )
    # The draft names its tables; the saved plan covers a draft whose panels
    # were all replaced.
    database = (engine_dashboards._infer_database(draft)
                or engine_dashboards._infer_database(rec.get("DASHBOARD_PLAN") or {}))
    return dashboard_assist.assist(
        draft, message, history,
        business_context=metadata_store.build_context(database) if database else "",
        heuristics=metadata_store.dashboard_heuristics(database) if database else "",
        source="snowflake",
        dry_run=engine_client.dry_run_query,
        database=database,
        saved=rec.get("DASHBOARD_PLAN") or {},
        saved_version=int(rec.get("VERSION") or 1),
    )


def _assist_file(report_id: str, connector: Any, draft: dict[str, Any], message: str,
                 history: list[dict[str, Any]]) -> dict[str, Any]:
    from app.agent.tools import ToolContext
    from app.engine import dashboard_assist
    from app.engine import excel_sources as engine_sources

    rec = engine_file_dashboards.read_record(report_id) or {}
    source_key = rec.get("source_key") or ""
    kind, _, source_id = source_key.partition(":")
    # The same source description the chat builds for this folder or workbook.
    if kind.upper() == "FOLDER":
        ctx = ToolContext(database=source_key,
                          collection=engine_sources.get_collection(source_id))
    else:
        ctx = ToolContext(database=source_key,
                          excel_source=engine_sources.get_dataset(source_id))
    return dashboard_assist.assist(
        draft, message, history,
        business_context=ctx.semantic_context or "",
        source="file",
        dry_run=connector.dry_run_query,
        saved=rec.get("dashboard_plan") or {},
        saved_version=int(rec.get("version") or 1),
    )


async def _stop_on_disconnect(request: Request, signal: threading.Event) -> None:
    """Set `signal` once the browser has gone — the person pressed Stop, or closed
    the page — so the model call it guards stops instead of running on."""
    while not signal.is_set():
        if await request.is_disconnected():
            signal.set()
            return
        await asyncio.sleep(0.5)


def _stoppable(signal: threading.Event, fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn`, with every model call inside it stoppable by `signal`. Set inside the
    worker thread itself, so it does not depend on context being copied there."""
    from app.engine import llm as engine_llm

    @functools.wraps(fn)
    def run(*args: Any) -> Any:
        with engine_llm.cancellable(signal):
            return fn(*args)

    return run


@router.post("/{report_id}/assist")
async def assist_edit(
    report_id: str,
    body: AssistRequest,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """The Edit panel's assistant: one message about the draft on screen.

    Saves nothing to the dashboard. A change comes back as the updated draft,
    which the panel shows and the person saves as usual. The exchange is kept in
    a thread of its own for this person and dashboard, so reopening the panel
    shows what earlier messages changed.
    """
    from app.engine import dashboard_assist
    from app.engine import llm as engine_llm

    if not settings.ai_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="AI is not configured (missing ANTHROPIC_API_KEY).")
    message = body.message.strip()[: dashboard_assist.MAX_MESSAGE_CHARS]
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Message is empty.")
    draft = dashboard_assist.clean_draft(body.plan)
    if not draft["panels"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The draft has no panels.")

    store = request.app.state.conversation_store
    if body.conversation_id:
        conversation = await store.get(body.conversation_id)
        if (conversation is None or conversation.owner != session.snowflake_user
                or conversation.kind != "dashboard_edit"
                or conversation.dashboard_id != report_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Conversation not found")
    else:
        conversation = Conversation(
            conversation_id=secrets.token_urlsafe(16),
            owner=session.snowflake_user,
            dashboard_id=report_id,
            kind="dashboard_edit",
            title=message[:80],
        )

    history = _assist_history(conversation)
    stop = threading.Event()
    watcher = asyncio.create_task(_stop_on_disconnect(request, stop))
    try:
        if _is_file_dashboard(report_id):
            result = await _run_file_dashboard(
                request, session, report_id, _stoppable(stop, _assist_file),
                draft, message, history
            )
        else:
            result = await _run_snowflake_engine(
                request, session, _stoppable(stop, _assist_snowflake),
                report_id, draft, message, history
            )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc
    except engine_llm.LLMCancelled as exc:
        # Nobody is waiting for this answer, and the thread keeps no record of a
        # request that was stopped. 499 is the conventional "client closed".
        raise HTTPException(status_code=499, detail=str(exc)) from exc
    except engine_llm.LLMError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=str(exc)) from exc
    finally:
        stop.set()  # also ends the watcher's loop
        watcher.cancel()

    text = dashboard_assist.turn_text(result)
    conversation.append("user", [{"type": "text", "text": message}])
    conversation.append("assistant", [{"type": "text", "text": text}])
    await store.save(conversation, settings.CONVERSATION_TTL_SECONDS)

    return {**result, "message": text, "conversation_id": conversation.conversation_id}


@router.put("/{report_id}/layout")
async def update_dashboard_layout(
    report_id: str,
    request: Request,
    payload: dict = Body(...),
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """Save which tab each panel sits on and how much of the grid it occupies.

    Separate from PUT /{report_id} because it cannot change SQL: that endpoint
    re-validates and dry-runs every panel, which a drag has no reason to pay
    for. Administrators only, matching the rest of editing — one layout is what
    everybody sees.
    """
    layout = (payload or {}).get("layout") or []
    if not isinstance(layout, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="layout must be a list of {title, section, span, row_span}",
        )
    title = (payload or {}).get("title")
    try:
        if _is_file_dashboard(report_id):
            # No connector needed: update_layout touches no SQL.
            return await asyncio.to_thread(
                engine_file_dashboards.update_layout, report_id, layout, title
            )
        return await _run_snowflake_engine(
            request, session, engine_dashboards.update_layout, report_id, layout, title
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


#: A picture of a chart as the page draws it. Capped because this arrives in a
#: request body: a dashboard of charts should not be able to push a hundred
#: megabytes through. The count covers the largest dashboard the planner builds.
_MAX_PANEL_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_PANEL_IMAGES = 60

#: A table as the page writes it. Bounded on every side for the same reason.
_MAX_PANEL_TABLES = 60
_MAX_TABLE_COLUMNS_IN = 60
_MAX_TABLE_CELL_CHARS = 400


def _decode_panel_images(raw: Any) -> dict[str, bytes]:
    """Decode {panel title: PNG data URL} from the request body.

    Anything unusable is dropped rather than refused: a picture that will not
    decode costs that panel its image and nothing more, and failing the whole
    export over one bad canvas would be a poor trade.
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, bytes] = {}
    for title, value in list(raw.items())[:_MAX_PANEL_IMAGES]:
        if not isinstance(title, str) or not isinstance(value, str):
            continue
        _, _, encoded = value.partition("base64,")
        if not encoded:
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            log.warning("panel image for %r was not valid base64", title)
            continue
        if not data or len(data) > _MAX_PANEL_IMAGE_BYTES:
            log.warning("panel image for %r rejected on size (%d bytes)",
                        title, len(data))
            continue
        # Only PNG: it is what the client canvas produces, and accepting
        # arbitrary bytes here would hand a decoder untrusted input.
        if not data.startswith(bytes.fromhex("89504e470d0a1a0a")):
            log.warning("panel image for %r is not a PNG", title)
            continue
        out[title] = data
    return out


def _decode_panel_tables(raw: Any) -> dict[str, dict[str, Any]]:
    """Decode {panel title: {headers, numeric, rows}} from the request body.

    Every cell is text the page already formatted. A table that does not have
    the expected shape is dropped, and the server formats that panel's rows
    itself.
    """
    from app.engine.dashboard_export import _MAX_PDF_TABLE_ROWS

    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for title, table in list(raw.items())[:_MAX_PANEL_TABLES]:
        if not isinstance(title, str) or not isinstance(table, dict):
            continue
        headers = table.get("headers")
        numeric = table.get("numeric")
        rows = table.get("rows")
        if (not isinstance(headers, list) or not headers
                or not all(isinstance(h, str) for h in headers)
                or not isinstance(rows, list)):
            continue
        headers = [h[:_MAX_TABLE_CELL_CHARS] for h in headers[:_MAX_TABLE_COLUMNS_IN]]
        width = len(headers)
        flags = [bool(f) for f in numeric[:width]] if isinstance(numeric, list) else []
        flags += [False] * (width - len(flags))
        cells: list[list[str]] = []
        for row in rows[:_MAX_PDF_TABLE_ROWS]:
            if not isinstance(row, list):
                break
            cells.append([
                (str(cell) if cell is not None else "")[:_MAX_TABLE_CELL_CHARS]
                for cell in row[:width]
            ] + [""] * max(0, width - len(row)))
        else:
            out[title] = {"headers": headers, "numeric": flags, "rows": cells}
    return out


@router.api_route("/{report_id}/export", methods=["GET", "POST"])
async def export_dashboard(
    report_id: str,
    request: Request,
    format: str = "excel",
    session: SessionData = Depends(get_current_session),
) -> Response:
    """Download the live dashboard run as Excel or PDF.

    Re-runs every panel with saved defaults so the file matches what the
    detail page would show after Refresh — not a stale cached snapshot.

    POST additionally accepts {"narrative": "..."} so a PDF can carry the
    summary the page is already showing. Sent by the client rather than
    regenerated here because the page has just paid for that model call, and
    doing it again would double the cost and the wait for the same paragraph.
    GET stays available so the URL is still a plain download link.
    """
    fmt = (format or "excel").strip().lower()
    if fmt not in ("excel", "xlsx", "pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format must be excel or pdf",
        )
    try:
        if _is_file_dashboard(report_id):
            result = await _run_file_dashboard(
                request,
                session,
                report_id,
                engine_file_dashboards.run_dashboard,
                None,
            )
        else:
            result = await _run_snowflake_engine(
                request, session, engine_dashboards.run_dashboard, report_id, None
            )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    panels = result.get("panels") or []
    title = str(result.get("title") or report_id)

    narrative: str | None = None
    panel_images: dict[str, bytes] = {}
    panel_tables: dict[str, dict[str, Any]] = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an empty or malformed body is not fatal
            body = None
        if isinstance(body, dict):
            supplied = body.get("narrative")
            if isinstance(supplied, str) and supplied.strip():
                narrative = supplied.strip()
            panel_images = _decode_panel_images(body.get("panel_images"))
            panel_tables = _decode_panel_tables(body.get("panel_tables"))

    if fmt == "pdf":
        payload = await asyncio.to_thread(
            dashboard_export.build_dashboard_pdf,
            title,
            panels,
            report_id=report_id,
            narrative=narrative,
            panel_images=panel_images or None,
            panel_tables=panel_tables or None,
        )
        filename = dashboard_export.export_filename(title, report_id, "pdf")
        return Response(
            content=payload,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    payload = await asyncio.to_thread(
        dashboard_export.build_dashboard_workbook,
        title,
        panels,
        report_id=report_id,
    )
    filename = dashboard_export.export_filename(title, report_id, "xlsx")
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{report_id}/narrative")
async def dashboard_narrative(
    report_id: str,
    request: Request,
    payload: dict = Body(default={}),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """AI executive narrative from a compact snapshot of the live run.

    Prefers the client-supplied KPI/panel snapshot (already on screen) so we
    do not re-query Snowflake. Falls back to a fresh run when the body is empty.

    Cached in shared storage under a fingerprint of the numbers it describes,
    the same way panel descriptions are. The UI asks for this on every load and
    every refresh, so without it a dashboard someone keeps open is re-narrated
    all day for figures that have not moved. Keying on the figures rather than
    the report means a run whose numbers DID move still gets a fresh sentence.

    The fingerprint covers _narrative_payload's output, not the request body:
    the key must move when the ANSWER would, and not otherwise. See that
    function for why the two have to be built together.
    """
    from app.engine import llm as engine_llm
    from app.engine import storage as storage_mod

    body = payload or {}
    title = str(body.get("title") or "").strip()
    kpi_facts = body.get("kpis") if isinstance(body.get("kpis"), list) else None
    snapshots = (
        body.get("panels") if isinstance(body.get("panels"), list) else None
    )
    default_currency = body.get("default_currency") if isinstance(
        body.get("default_currency"), str
    ) else None

    if not title or kpi_facts is None:
        try:
            if _is_file_dashboard(report_id):
                result = await _run_file_dashboard(
                    request,
                    session,
                    report_id,
                    engine_file_dashboards.run_dashboard,
                    None,
                )
            else:
                result = await _run_snowflake_engine(
                    request,
                    session,
                    engine_dashboards.run_dashboard,
                    report_id,
                    None,
                )
        except engine_dashboards.DashboardError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        title = str(result.get("title") or report_id)
        kpi_facts, snapshots = _snapshot_for_narrative(result.get("panels") or [])
        default_currency = next(
            (p.get("default_currency") for p in result.get("panels") or []
             if isinstance(p, dict) and p.get("default_currency")),
            None,
        )

    title, kpis, panels = _narrative_payload(title, kpi_facts, snapshots, default_currency)

    # The instructions and model are part of the key: a wording fix must reach
    # dashboards already viewed, not wait for their numbers to change.
    fingerprint = _digest([title, kpis, panels, engine_llm.MODEL_SUMMARY,
                           engine_llm.SUMMARY_SYSTEM])[:32]
    key = storage_mod.narrative_key(report_id, fingerprint)

    cached = await asyncio.to_thread(storage_mod.get_json, key)
    # Per-section digests, so a miss says WHICH part moved. Without them the
    # only evidence of a cache that is not working is a bill.
    log.info(
        "narrative %s: %s fingerprint=%s title=%s kpis=%s panels=%s",
        report_id,
        "hit" if cached else "miss",
        fingerprint[:8],
        _digest(title)[:8],
        _digest(kpis)[:8],
        _digest(panels)[:8],
    )

    if isinstance(cached, dict) and cached.get("narrative"):
        return {
            "narrative": cached["narrative"],
            "report_id": report_id,
            "cached": True,
        }

    try:
        text = await _run_ai_call(
            request, session, engine_llm.summarize_dashboard, title, kpis, panels
        )
    except engine_llm.LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    # Best-effort, like the panel-description cache: a storage backend that is
    # down should make this feature slower, not broken.
    try:
        await asyncio.to_thread(
            storage_mod.put_json, key, {"narrative": text}
        )
    except Exception:  # noqa: BLE001
        log.warning("could not cache narrative for %s", report_id, exc_info=True)

    return {"narrative": text, "report_id": report_id, "cached": False}


def _digest(value: Any) -> str:
    """A stable hash of anything JSON-shaped."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


#: What a panel snapshot contributes to the narrative.
#:
#: chart_type is deliberately absent. summarize_dashboard's prompt forbids
#: mentioning chart types, so it cannot change a word of the output — but while
#: it was in the payload it changed the cache KEY, and switching one unrelated
#: panel from a bar to a line re-narrated the whole dashboard.
#: Charts a summary is written from. Each is a handful of figures now rather
#: than rows, so a dashboard of the largest size the planner builds fits whole.
_NARRATIVE_MAX_PANELS = 25


def _overall(value: str) -> str:
    return f"{value} (overall)" if value else value


def _narrative_payload(
    title: str,
    kpi_facts: list[dict[str, Any]] | None,
    snapshots: list[dict[str, Any]] | None,
    default_currency: str | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Exactly what the model is shown — and therefore exactly what the cache
    key is taken over.

    One function for both so the two cannot drift, because they drift in
    opposite and equally bad directions: a field in the key but not the prompt
    busts the cache for a change that could not affect the answer, and a field
    in the prompt but not the key serves a narrative written for different
    numbers.

    It also normalises, because the payload arrives from two places. The
    dashboard page builds it from what is already on screen; the fallback path
    builds it from a fresh run. Both are trimmed to the same shape here rather
    than trusted to agree.
    """
    from app.engine.narrative_figures import panel_figures, show_kpi

    # Every value arrives written out, in its unit and scale, exactly as its
    # tile shows it. Handed 235930000.12, the model chose the scale itself and
    # wrote "AED 235.9 billion" under a tile reading AED 235.9M.
    kpis = [
        {"metric": fact.get("metric"),
         # "(overall)": the tile and a chart of the same measure share a name,
         # and the summary paired the chart's change with the tile's value.
         "value": _overall(show_kpi(fact.get("value"), fact.get("format"), default_currency))}
        for fact in (kpi_facts or [])[:12]
        if isinstance(fact, dict)
    ]

    # Each chart becomes its figures -- first, latest, change, high and low, or
    # top and bottom -- rather than its first five rows, which for a series
    # ordered oldest-first are its five OLDEST points. chart_type stays out: the
    # prompt forbids mentioning it, and it would only move the cache key.
    panels = []
    for snap in (snapshots or [])[:_NARRATIVE_MAX_PANELS]:
        if not isinstance(snap, dict):
            continue
        # A page still running the previous release sends five sample rows
        # instead of all of them; describe what arrived rather than fail.
        rows = snap.get("rows") if isinstance(snap.get("rows"), list) else snap.get("sample_rows")
        figures = panel_figures({
            **snap,
            "rows": rows or [],
            "default_currency": snap.get("default_currency") or default_currency,
        })
        panels.append({
            "title": snap.get("title"),
            "row_count": snap.get("row_count"),
            # Normalised: the page sends this flag and the fallback run computes
            # it, and missing/None/False must not be three cache keys for one state.
            "truncated": bool(snap.get("truncated")),
            "figures": figures,
        })
    return title, kpis, panels


def _snapshot_for_narrative(
    panels: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compact KPI facts + per-panel previews for the narrative model."""
    kpis: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    for panel in panels:
        display = (panel.get("display") or "").lower()
        chart = (panel.get("chart_type") or "").lower()
        columns = [str(c) for c in (panel.get("columns") or [])]
        rows = panel.get("rows") or []
        if chart == "kpi" or display == "kpi":
            mi = next((i for i, c in enumerate(columns) if c.upper() == "METRIC"), 0)
            vi = next((i for i, c in enumerate(columns) if c.upper() == "VALUE"), 1)
            fi = next((i for i, c in enumerate(columns) if c.upper() == "FORMAT"), -1)
            for row in rows:
                if not row:
                    continue
                kpis.append({
                    "metric": row[mi] if mi < len(row) else None,
                    "value": row[vi] if vi < len(row) else None,
                    "format": row[fi] if fi >= 0 and fi < len(row) else None,
                })
            continue
        snaps.append({
            "title": panel.get("title"),
            "chart_type": panel.get("chart_type") or panel.get("display"),
            "x": panel.get("x"),
            "y": panel.get("y"),
            "color": panel.get("color"),
            "columns": columns,
            "rows": rows,
            "column_formats": panel.get("column_formats"),
            "default_currency": panel.get("default_currency"),
            "row_count": panel.get("row_count") or len(rows),
            # Without this the model reads a capped series as a finished one
            # and writes the cap's last row up as the current position.
            "truncated": bool(panel.get("truncated")),
        })
    return kpis, snaps


@router.delete("/{report_id}")
async def delete_dashboard(
    report_id: str,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> dict[str, Any]:
    """Retire a saved dashboard. Administrators only."""
    if _is_file_dashboard(report_id):
        rec = engine_file_dashboards.read_record(report_id)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found"
            )
        rec["is_active"] = False
        from app.engine import storage as storage_mod

        storage_mod.put_json(
            storage_mod.file_dashboard_key(report_id),
            rec,
        )
        return {"report_id": report_id, "deactivated": True, "source_type": "file"}
    try:
        return await _run_snowflake_engine(
            request, session, engine_dashboards.delete_dashboard, report_id
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{report_id}/versions")
async def list_versions(
    report_id: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    if _is_file_dashboard(report_id):
        try:
            return engine_file_dashboards.list_versions(report_id)
        except engine_dashboards.DashboardError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
    try:
        return await _run_snowflake_engine(
            request, session, engine_dashboards.list_versions, report_id
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{report_id}/revert")
async def revert_dashboard(
    report_id: str,
    request: Request,
    payload: dict = Body(default={}),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    version = (payload or {}).get("version")
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="version is required"
        )
    if _is_file_dashboard(report_id):
        try:
            return engine_file_dashboards.revert(report_id, int(version))
        except engine_dashboards.DashboardError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
    try:
        return await _run_snowflake_engine(
            request, session, engine_dashboards.revert, report_id, int(version)
        )
    except engine_dashboards.DashboardError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{report_id}/group")
async def set_dashboard_group(
    report_id: str,
    request: Request,
    payload: dict = Body(default={}),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Move a dashboard into a folder, or send it back to its database.

    An empty or absent `group` clears the override, so the dashboard files
    itself again from its database — which is what "reset" means here, and why
    the column is empty for almost every row.

    This is NOT an edit: it writes no new version. Where a dashboard is filed
    says nothing about what it shows, and a reordering of folders should not
    appear in a dashboard's history as a change to the dashboard.
    """
    raw = payload.get("group") if isinstance(payload, dict) else None
    group = str(raw).strip() if raw is not None else ""
    if len(group) > 120:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Folder name must be 120 characters or fewer.",
        )

    if _is_file_dashboard(report_id):
        # File-backed dashboards live in shared storage, not the table this
        # writes to. Say so rather than accepting a move that does nothing.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Dashboards created from files cannot be moved between folders.",
        )

    await _run_snowflake_engine(
        request, session, engine_dashboards.set_group, report_id, group or None
    )
    return {"report_id": report_id, "group": group or None}


@router.post("/panel-description")
async def panel_description(
    request: Request = None,
    payload: dict = Body(default={}),
    session: SessionData = Depends(get_current_session),
) -> dict[str, Any]:
    """Plain-English explanation of one chart, for its info button.

    The panel descriptor comes from the CLIENT because it is already on screen,
    the same reasoning as the narrative route above — so opening the info
    button costs no warehouse query at all, and this works for a chat chart or
    a file-backed dashboard without knowing which it is.

    Only NAMES are sent to the model: the title, the chart type and the column
    names. No rows, ever. That keeps this feature outside the row-sharing we
    describe to clients, and a description that cannot see the data cannot
    state a figure it has misread.

    Cached in shared storage under a fingerprint of the descriptor, so the
    first person to ask pays for it and everyone afterwards — on any worker —
    does not. Editing the panel changes the fingerprint, which retires the old
    explanation rather than leaving a stale one attached.
    """
    from app.engine import llm as engine_llm
    from app.engine import storage as storage_mod

    body = payload or {}
    title = str(body.get("title") or "").strip()[:200]
    chart_type = str(body.get("chart_type") or "").strip().lower()[:40]
    if not title or not chart_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="title and chart_type are required",
        )

    x = str(body.get("x")).strip()[:100] if body.get("x") else None
    raw_y = body.get("y")
    if isinstance(raw_y, str):
        raw_y = [raw_y]
    y = [str(v).strip()[:100] for v in (raw_y or []) if str(v).strip()][:12]
    raw_columns = body.get("columns")
    columns = [
        str(c).strip()[:100] for c in (raw_columns or []) if str(c).strip()
    ][:25]
    dashboard_title = str(body.get("dashboard_title") or "").strip()[:200] or None

    fingerprint = hashlib.sha256(
        json.dumps([title, chart_type, x, y, columns], sort_keys=True).encode()
    ).hexdigest()[:32]
    key = storage_mod.panel_description_key(fingerprint)

    cached = await asyncio.to_thread(storage_mod.get_json, key)
    if isinstance(cached, dict) and cached.get("description"):
        return {"description": cached["description"], "cached": True}

    try:
        text = await _run_ai_call(
            request,
            session,
            engine_llm.describe_panel,
            title,
            chart_type,
            columns,
            x,
            y,
            dashboard_title,
        )
    except engine_llm.LLMError as exc:
        # The key is missing or rejected: say so rather than returning an empty
        # popover the reader would take for "this chart has no explanation".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if text:
        await asyncio.to_thread(storage_mod.put_json, key, {"description": text})
    return {"description": text, "cached": False}

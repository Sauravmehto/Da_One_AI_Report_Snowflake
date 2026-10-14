"""Saved dashboards for uploaded files and folders (DuckDB sources).

Parallel to dashboards.py (Snowflake warehouse databases). Same plan → save →
run lifecycle and the same panel JSON shape the frontend already renders, but:

  - Context comes from ctx.semantic_context (spreadsheet schema or document facts)
  - SQL is validated and executed through the DuckDB connector
  - Plans and saved dashboards persist in shared storage (JSON), not Snowflake

Snowflake dashboard code is never imported here except for shared error types.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.engine import llm, sql_params
from app.engine.dashboard_sections import (
    consolidate_sections,
    consolidation_note,
    settle_new_sections,
    settlement_note,
)
from app.engine.dashboards import (
    WEBAPP_BASE_URL,
    DashboardError,
    _PANEL_CONFIG_FIELDS,
    _apply_layout,
    _diff_plans,
    _normalise_layout,
    _panel_config,
    _parameter_redirect,
    _resolve_param_values,
    _save_warnings,
)
from app.engine.sql_safety import UnsafeSQLError

# Prefixes for upload sources — must match agent.py / excel-source-api.ts.
EXCEL_PREFIX = "EXCEL:"
COLLECTION_PREFIX = "FOLDER:"


def is_upload_source(source_key: str) -> bool:
    upper = (source_key or "").upper()
    return upper.startswith(EXCEL_PREFIX) or upper.startswith(COLLECTION_PREFIX)


@runtime_checkable
class QueryConnector(Protocol):
    def dry_run_query(self, sql: str) -> list[str]: ...
    def run_query(self, sql: str) -> dict[str, Any]: ...
    def session_identity(self) -> tuple[str | None, str | None]: ...


# --- Plan cache (worker-safe, same pattern as dashboards.py) ------------------

_PLAN_CACHE: dict[str, dict[str, Any]] = {}


def _cache_plan(plan: dict[str, Any], source_key: str) -> str:
    from app.engine import storage as storage_mod

    plan_id = uuid.uuid4().hex
    entry = {"plan": plan, "source_key": source_key, "kind": "file"}
    _PLAN_CACHE[plan_id] = entry
    storage_mod.put_json(storage_mod.file_plan_key(plan_id), entry)
    return plan_id


def get_cached_plan(plan_id: str) -> dict[str, Any] | None:
    from app.engine import storage as storage_mod

    return _PLAN_CACHE.get(plan_id) or storage_mod.get_json(
        storage_mod.file_plan_key(plan_id)
    )


# --- Edit-proposal cache (preview -> confirm, same pattern as dashboards.py) -

_EDIT_CACHE: dict[str, dict[str, Any]] = {}


def _cache_edit(report_id: str, entry: dict[str, Any]) -> None:
    from app.engine import storage as storage_mod

    _EDIT_CACHE[report_id] = entry
    storage_mod.put_json(storage_mod.file_edit_key(report_id), entry)


def _get_cached_edit(report_id: str) -> dict[str, Any] | None:
    from app.engine import storage as storage_mod

    return _EDIT_CACHE.get(report_id) or storage_mod.get_json(
        storage_mod.file_edit_key(report_id)
    )


def _drop_cached_edit(report_id: str) -> None:
    from app.engine import storage as storage_mod

    _EDIT_CACHE.pop(report_id, None)
    storage_mod.drop(storage_mod.file_edit_key(report_id))


# --- Persistence --------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _write_record(record: dict[str, Any]) -> None:
    from app.engine import storage as storage_mod

    report_id = record["report_id"]
    storage_mod.put_json(storage_mod.file_dashboard_key(report_id), record)


def read_record(report_id: str) -> dict[str, Any] | None:
    from app.engine import storage as storage_mod

    rec = storage_mod.get_json(storage_mod.file_dashboard_key(report_id))
    if rec is None or not rec.get("is_active", True):
        return None
    return rec


def exists(report_id: str) -> bool:
    return read_record(report_id) is not None


def resolve_report_ids(query: str) -> list[dict[str, Any]]:
    """Resolve an id-or-title to zero or more file-dashboard matches."""
    q = (query or "").strip()
    if not q:
        return []
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list_dashboards(limit=500):
        rid = item.get("report_id") or ""
        title = (item.get("report_title") or "").strip()
        matched = rid == q or rid.lower() == q.lower() or title.lower() == q.lower()
        if matched and rid not in seen:
            seen.add(rid)
            matches.append(item)
    return matches


def _resolve_one(report_id_or_title: str) -> dict[str, Any]:
    matches = resolve_report_ids(report_id_or_title)
    if not matches:
        return {
            "status": "not_found",
            "message": (
                f"No current file-source dashboard matches '{report_id_or_title}'. "
                "Check the exact title (case-insensitive) or pass the report_id — "
                "see list_saved_dashboards."
            ),
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "matches": matches,
            "message": (
                f"'{report_id_or_title}' matches {len(matches)} file-source dashboards. "
                "Re-run with the exact report_id of the one you mean — I won't guess."
            ),
        }
    return {"status": "ok", "report_id": matches[0]["report_id"]}


def _archive_version(rec: dict[str, Any]) -> None:
    from app.engine import storage as storage_mod

    version = int(rec.get("version") or 1)
    storage_mod.put_json(
        storage_mod.file_version_key(rec["report_id"], version),
        {
            "version": version,
            "dashboard_plan": rec.get("dashboard_plan") or {},
            "edit_note": rec.get("last_edit_note"),
            "archived_at": _now_iso(),
        },
    )


# --- Validation ---------------------------------------------------------------


def _validate_panels(
    connector: QueryConnector,
    panels: list[dict[str, Any]],
    parameters: list[dict[str, Any]] | None = None,
    columns: dict[int, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """`columns`, when given, is filled with the names each panel's query
    returns, keyed by the panel's position in `panels`."""
    samples = sql_params.sample_values(parameters) if parameters else {}
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for i, panel in enumerate(panels, start=1):
        title = panel.get("title") or f"panel {i}"
        sql = panel.get("sql")
        if not sql:
            skipped.append({"title": title, "reason": "panel has no SQL"})
            continue
        try:
            probe_sql = sql
            if sql_params.has_placeholders(sql):
                probe_sql = sql_params.render_sql(sql, parameters or [], samples)
            returned = connector.dry_run_query(probe_sql)
            if columns is not None and isinstance(returned, (list, tuple)):
                columns[i - 1] = [str(name) for name in returned]
        except sql_params.ParameterError as exc:
            skipped.append({"title": title, "reason": f"parameter error: {exc}"})
            continue
        except UnsafeSQLError as exc:
            skipped.append({"title": title, "reason": f"SQL rejected by safety layer: {exc}"})
            continue
        except Exception as exc:  # noqa: BLE001
            skipped.append({"title": title, "reason": f"SQL failed to compile: {exc}"})
            continue
        kept.append(_normalise_layout(panel))
    return kept, skipped


# --- Plan orchestration -------------------------------------------------------


def create_plan(
    description: str,
    source_key: str,
    business_context: str,
    connector: QueryConnector,
) -> dict[str, Any]:
    """Generate, validate, dry-run, and cache a dashboard plan for an upload source."""
    if not is_upload_source(source_key):
        raise DashboardError(
            f"Source '{source_key}' is not an uploaded file or folder."
        )
    if not (business_context or "").strip():
        raise DashboardError(
            "No schema context is available for this source — cannot plan a dashboard."
        )

    plan = llm.generate_file_dashboard_plan(description, business_context)

    if plan.needs_clarification:
        return {
            "status": "needs_clarification",
            "database": source_key,
            "source_key": source_key,
            "source_type": "file",
            "questions": [plan.needs_clarification],
            "message": "The dashboard could not be planned without clarification:",
        }

    if not plan.panels:
        raise DashboardError("The planner returned no panels; try a more specific description.")

    parameters = plan.parameters or []
    kept_panels, skipped_panels = _validate_panels(connector, plan.panels, parameters)

    if not kept_panels:
        reasons = "; ".join(f"'{s['title']}': {s['reason']}" for s in skipped_panels)
        raise DashboardError(f"No panels could be validated. {reasons}")

    # Tabs are held to the layout rules here rather than trusted to the prompt.
    planned_panels = kept_panels
    kept_panels = consolidate_sections(kept_panels)
    layout_note = consolidation_note(planned_panels, kept_panels)
    plan_obj = {"title": plan.title, "panels": kept_panels, "parameters": parameters}
    plan_id = _cache_plan(plan_obj, source_key)
    return {
        "plan_id": plan_id,
        "plan": plan_obj,
        "database": source_key,
        "source_key": source_key,
        "source_type": "file",
        "panel_count": len(kept_panels),
        "parameters": parameters,
        "skipped_panels": skipped_panels,
        **({"layout_note": layout_note} if layout_note else {}),
    }


def save_plan(
    plan_id: str,
    report_title: str,
    description: str = "",
    *,
    created_by: str | None = None,
) -> dict[str, Any]:
    entry = get_cached_plan(plan_id)
    if entry is None:
        raise DashboardError(
            f"No cached plan for plan_id '{plan_id}' (it may have expired). "
            "Call plan_dashboard again to regenerate a plan, then save that."
        )

    report_id = uuid.uuid4().hex
    now = _now_iso()
    record = {
        "report_id": report_id,
        "version": 1,
        "report_title": report_title,
        "description": description or "",
        "source_key": entry["source_key"],
        "source_type": "file",
        "dashboard_plan": entry["plan"],
        "created_by": created_by,
        "created_at": now,
        "last_run_at": None,
        "run_count": 0,
        "is_active": True,
    }
    _write_record(record)
    return {
        "report_id": report_id,
        "report_title": report_title,
        "version": 1,
        "saved": True,
        "source_type": "file",
        "source_key": entry["source_key"],
        "webapp_url": f"{WEBAPP_BASE_URL}/dashboards/{report_id}",
    }


def list_dashboards(
    *,
    source_key: str | None = None,
    owner: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from app.engine import storage as storage_mod

    cap = max(1, min(int(limit), 500))
    keys = storage_mod.get_storage().list_prefix("file_dashboards/")
    items: list[dict[str, Any]] = []
    for key in keys:
        if not key.endswith(".json"):
            continue
        rec = storage_mod.get_json(key)
        if not rec or not rec.get("is_active", True):
            continue
        if source_key and rec.get("source_key") != source_key:
            continue
        if owner and rec.get("created_by") != owner:
            continue
        items.append(
            {
                "report_id": rec.get("report_id"),
                "report_title": rec.get("report_title"),
                "version": rec.get("version", 1),
                "created_by": rec.get("created_by"),
                "created_at": rec.get("created_at"),
                "last_run_at": rec.get("last_run_at"),
                "run_count": rec.get("run_count", 0),
                "source_type": "file",
                "source_key": rec.get("source_key"),
            }
        )
    items.sort(
        key=lambda r: r.get("last_run_at") or r.get("created_at") or "",
        reverse=True,
    )
    return items[:cap]


def run_dashboard(
    report_id: str,
    connector: QueryConnector,
    parameter_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    plan = rec.get("dashboard_plan") or {}
    parameters = plan.get("parameters") or []
    # The connector caps every result and drops the rest silently, so a capped
    # panel and a genuinely short one look identical. The file engine's cap is
    # its own (MCP_MAX_ROWS), not the warehouse's. Read off the connector so a
    # connector without one simply never claims truncation.
    cap = getattr(connector, "MAX_ROWS", None)
    panels_out: list[dict[str, Any]] = []
    for panel in plan.get("panels", []):
        config = _panel_config(panel)
        sql = panel.get("sql")
        try:
            run_sql = sql
            if sql and parameters and sql_params.has_placeholders(sql):
                run_sql = sql_params.render_sql(sql, parameters, parameter_values)
            result = connector.run_query(run_sql)
            panels_out.append(
                {
                    **config,
                    "columns": result["columns"],
                    "rows": result["rows"],
                    "row_count": result["row_count"],
                    "truncated": bool(cap) and result["row_count"] >= cap,
                }
            )
        except Exception as exc:  # noqa: BLE001
            panels_out.append(
                {**config, "columns": [], "rows": [], "row_count": 0,
                 "truncated": False, "error": str(exc)}
            )

    rec["run_count"] = int(rec.get("run_count") or 0) + 1
    rec["last_run_at"] = _now_iso()
    _write_record(rec)

    return {
        "report_id": report_id,
        "title": plan.get("title"),
        "run_count": rec["run_count"],
        "last_run_at": rec["last_run_at"],
        "parameters": parameters,
        "parameter_values": _resolve_param_values(parameters, parameter_values),
        "panels": panels_out,
        "source_type": "file",
        "source_key": rec.get("source_key"),
    }


def get_dashboard_config(report_id: str) -> dict[str, Any]:
    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    plan = rec.get("dashboard_plan") or {}
    return {
        "report_id": report_id,
        "title": plan.get("title"),
        "description": rec.get("description") or "",
        "database": rec.get("source_key"),
        "source_key": rec.get("source_key"),
        "source_type": "file",
        "parameters": plan.get("parameters") or [],
        "panels": [
            {**_panel_config(p), "sql": p.get("sql")}
            for p in plan.get("panels") or []
        ],
    }


def update_plan(
    report_id: str,
    connector: QueryConnector,
    plan: dict[str, Any],
    edit_note: str = "",
) -> dict[str, Any]:
    """Replace a file-source dashboard plan directly, as a new version.

    Same gates as dashboards.update_plan, but SQL is dry-run against DuckDB.
    """
    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    current_plan = rec.get("dashboard_plan") or {}
    current_version = int(rec.get("version") or 1)

    incoming = plan.get("panels")
    if not incoming:
        raise DashboardError(
            "A dashboard needs at least one panel; the edit was not saved."
        )
    panels = [
        {**{k: p.get(k) for k in _PANEL_CONFIG_FIELDS}, "sql": p.get("sql")}
        for p in incoming
    ]
    parameters = plan.get("parameters")
    if parameters is None:
        parameters = current_plan.get("parameters") or []

    returned: dict[int, list[str]] = {}
    kept, skipped = _validate_panels(connector, panels, parameters, returned)
    if skipped:
        reasons = "; ".join(f"'{s['title']}': {s['reason']}" for s in skipped)
        raise DashboardError(
            f"{len(skipped)} panel(s) failed validation, so nothing was saved "
            f"(no panel is dropped on an edit). {reasons}"
        )
    warnings = _save_warnings(kept, returned, current_plan)

    title = plan.get("title") or current_plan.get("title") or rec.get("report_title")
    new_plan = {"title": title, "panels": kept, "parameters": parameters}

    _archive_version(rec)
    new_version = current_version + 1
    rec["version"] = new_version
    rec["dashboard_plan"] = new_plan
    rec["report_title"] = title
    if plan.get("description") is not None:
        rec["description"] = plan.get("description") or ""
    rec["last_edit_note"] = edit_note or "edited directly"
    _write_record(rec)

    return {
        "report_id": report_id,
        "report_title": title,
        "version": new_version,
        "version_id": f"{report_id}_v{new_version}",
        "diff": _diff_plans(current_plan, new_plan),
        "saved": True,
        "warnings": warnings,
        "source_type": "file",
        "source_key": rec.get("source_key"),
    }


def update_layout(
    report_id: str,
    layout: list[dict[str, Any]],
    title: str | None = None,
) -> dict[str, Any]:
    """Save section/span/row_span for a file dashboard's panels, as a new version.

    The file-source twin of dashboards.update_layout, and it takes no connector
    for the same reason: nothing here touches SQL, so there is nothing to
    dry-run against DuckDB.
    """
    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    current_plan = rec.get("dashboard_plan") or {}
    panels = [dict(p) for p in (current_plan.get("panels") or [])]
    if not panels:
        raise DashboardError("This dashboard has no panels to lay out.")

    panels, changed = _apply_layout(panels, layout or [])
    renamed = bool(title and title.strip() and title.strip() != current_plan.get("title"))
    if not changed and not renamed:
        return {"report_id": report_id, "saved": False, "reason": "no layout change"}

    new_title = title.strip() if renamed else (
        current_plan.get("title") or rec.get("report_title")
    )
    new_plan = {
        "title": new_title,
        "panels": panels,
        "parameters": current_plan.get("parameters") or [],
    }

    _archive_version(rec)
    new_version = int(rec.get("version") or 1) + 1
    rec["version"] = new_version
    rec["dashboard_plan"] = new_plan
    rec["report_title"] = new_title
    rec["last_edit_note"] = "layout changed"
    _write_record(rec)

    return {
        "report_id": report_id,
        "report_title": new_title,
        "version": new_version,
        "panels_changed": changed,
        "saved": True,
        "source_type": "file",
        "source_key": rec.get("source_key"),
    }


def preview_edit(
    report_id_or_title: str,
    changes_description: str,
    connector: QueryConnector,
    business_context: str,
) -> dict[str, Any]:
    """Preview an edit to a file-source dashboard. Does not persist."""
    resolved = _resolve_one(report_id_or_title)
    if resolved["status"] != "ok":
        return resolved
    report_id = resolved["report_id"]

    rec = read_record(report_id)
    if rec is None:
        return {
            "status": "not_found",
            "message": f"No current version found for report_id '{report_id}'.",
        }

    current_plan = rec.get("dashboard_plan") or {}
    current_version = int(rec.get("version") or 1)
    current_params = current_plan.get("parameters") or []
    source_key = rec.get("source_key") or ""

    redirect = _parameter_redirect(changes_description, current_params)
    if redirect is not None:
        return {
            **redirect,
            "report_id": report_id,
            "report_title": rec.get("report_title"),
            "source_type": "file",
            "source_key": source_key,
        }

    if not (business_context or "").strip():
        raise DashboardError(
            "No schema context is available for this source — cannot edit the dashboard."
        )

    edited = llm.edit_file_dashboard_plan(
        current_plan, changes_description, business_context
    )
    if edited.needs_clarification:
        return {
            "status": "needs_clarification",
            "report_id": report_id,
            "report_title": rec.get("report_title"),
            "source_type": "file",
            "source_key": source_key,
            "questions": [edited.needs_clarification],
            "message": "The edit could not be completed without clarification:",
        }
    if not edited.panels:
        raise DashboardError("The edit produced no panels; rephrase the change.")

    new_params = edited.parameters or []
    kept, skipped = _validate_panels(connector, edited.panels, new_params)
    if skipped:
        reasons = "; ".join(f"'{s['title']}': {s['reason']}" for s in skipped)
        raise DashboardError(
            f"The edited plan has {len(skipped)} panel(s) that failed validation, "
            f"so the edit was not staged (no panel is dropped on an edit). {reasons}"
        )

    # Only tabs this edit invents are checked; existing ones are out of scope.
    proposed_panels = kept
    kept = settle_new_sections(current_plan.get("panels") or [], kept)
    layout_note = settlement_note(proposed_panels, kept)
    new_plan = {"title": edited.title, "panels": kept, "parameters": new_params}
    diff = _diff_plans(current_plan, new_plan)

    _cache_edit(
        report_id,
        {
            "plan": new_plan,
            "changes_description": changes_description,
            "base_version": current_version,
            "report_title": rec.get("report_title"),
            "description": rec.get("description") or "",
            "diff": diff,
        },
    )
    return {
        "status": "preview",
        "report_id": report_id,
        "report_title": rec.get("report_title"),
        "database": source_key,
        "source_key": source_key,
        "source_type": "file",
        "current_version": current_version,
        "new_version": current_version + 1,
        "edit_note": changes_description,
        "diff": diff,
        "parameters": new_params,
        "proposed_plan": new_plan,
        **({"layout_note": layout_note} if layout_note else {}),
        "message": (
            "Review this diff against the requested change. Confirm the ONLY "
            "differences are the ones you asked for — flag any panel or field "
            "that changed outside the request — then call edit_dashboard again "
            "with confirm=true to commit this as a new version."
        ),
    }


def commit_edit(report_id_or_title: str, changes_description: str) -> dict[str, Any]:
    """Commit a previewed edit as a new version of a file-source dashboard."""
    resolved = _resolve_one(report_id_or_title)
    if resolved["status"] != "ok":
        return resolved
    report_id = resolved["report_id"]

    cached = _get_cached_edit(report_id)
    if cached is None or cached["changes_description"] != changes_description:
        raise DashboardError(
            "No previewed edit found for this dashboard and change. Call edit_dashboard "
            "WITHOUT confirm first to preview the diff, then confirm with the same "
            "report and changes_description."
        )

    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    _archive_version(rec)
    new_version = int(cached["base_version"]) + 1
    rec["version"] = new_version
    rec["dashboard_plan"] = cached["plan"]
    rec["last_edit_note"] = changes_description
    _write_record(rec)
    _drop_cached_edit(report_id)

    source_key = rec.get("source_key") or ""
    return {
        "status": "committed",
        "report_id": report_id,
        "report_title": cached["report_title"],
        "version": new_version,
        "version_id": f"{report_id}_v{new_version}",
        "edit_note": changes_description,
        "diff": cached["diff"],
        "source_type": "file",
        "source_key": source_key,
        "webapp_url": f"{WEBAPP_BASE_URL}/dashboards/{report_id}",
    }


def list_versions(report_id: str) -> dict[str, Any]:
    """Every version of a file-source dashboard, oldest first (metadata only)."""
    from app.engine import storage as storage_mod

    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No versions found for report_id '{report_id}'.")

    prefix = f"file_dashboard_versions/{report_id}/"
    archived: list[dict[str, Any]] = []
    for key in storage_mod.get_storage().list_prefix(prefix):
        if not key.endswith(".json"):
            continue
        snap = storage_mod.get_json(key)
        if not snap:
            continue
        archived.append(
            {
                "version": snap.get("version"),
                "version_id": f"{report_id}_v{snap.get('version')}",
                "edit_note": snap.get("edit_note"),
                "created_at": snap.get("archived_at"),
                "is_current": False,
            }
        )
    archived.sort(key=lambda r: int(r.get("version") or 0))

    current_version = int(rec.get("version") or 1)
    versions = archived + [
        {
            "version": current_version,
            "version_id": f"{report_id}_v{current_version}",
            "edit_note": rec.get("last_edit_note"),
            "created_at": rec.get("created_at"),
            "is_current": True,
        }
    ]
    return {"report_id": report_id, "count": len(versions), "versions": versions}


def revert(report_id: str, version: int) -> dict[str, Any]:
    """Restore a file-source dashboard to a prior archived version."""
    from app.engine import storage as storage_mod

    rec = read_record(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    snap = storage_mod.get_json(storage_mod.file_version_key(report_id, int(version)))
    if snap is None:
        raise DashboardError(
            f"Version {version} not found for report_id '{report_id}'."
        )

    _archive_version(rec)
    new_version = int(rec.get("version") or 1) + 1
    rec["version"] = new_version
    rec["dashboard_plan"] = snap.get("dashboard_plan") or {}
    rec["last_edit_note"] = f"Reverted to version {version}"
    _write_record(rec)

    return {
        "reverted": True,
        "report_id": report_id,
        "reverted_to_version": version,
        "version": new_version,
        "source_type": "file",
    }

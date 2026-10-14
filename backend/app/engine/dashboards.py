"""Saved-dashboard core, shared by the MCP server tools and (later) the FastAPI
webapp so both drive identical behavior.

Two responsibilities:
  1. Planning: turn a description into a validated, dry-run-checked plan and
     cache it in-memory keyed by a generated plan_id (so save_dashboard persists
     the *exact* iteration the user approved, without the model retyping it).
  2. Persistence orchestration: save a cached plan; list/run saved dashboards.

run_dashboard returns panel rows EXACTLY as Snowflake returns them — there is no
server-side row shaping. Any year/quarter formatting or ordering a chart needs is
produced in the panel SQL itself (see the year/quarter rules in llm.py's
_DASHBOARD_RULES), so the chat and webapp paths render byte-identical data.

The plan cache is a module-level dict BACKED BY shared storage. The dict alone
was enough under local stdio, where one process serves the whole session, but
the webapp runs `uvicorn --workers 4`: planning and saving are separate requests
and land on whichever worker is free, so a dict-only cache reported "no cached
plan ... it may have expired" for a plan made seconds earlier.
"""

from __future__ import annotations

import contextvars
import logging
import math
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from app.engine import llm
from app.engine.connectors.snowflake import client as snowflake_client
from app.engine import sql_params
from app.engine.dashboard_sections import (
    consolidate_sections,
    consolidation_note,
    settle_new_sections,
    settlement_note,
)
from app.engine.metadata_loader import metadata_store
from app.engine.sql_safety import UnsafeSQLError

log = logging.getLogger(__name__)

# Base URL of the companion webapp — the ONLY surface that can refresh a saved
# dashboard's data live (a sandboxed Artifact cannot reach this local MCP
# server). Overridable per deployment; defaults to the Vite dev server.
WEBAPP_BASE_URL = os.environ.get("WEBAPP_BASE_URL", "http://localhost:5173").rstrip("/")


class DashboardError(Exception):
    """A dashboard operation failed in a way worth reporting to the caller
    (validation failure, expired plan_id, unknown report_id)."""


# --- Plan cache -------------------------------------------------------------
#
# Planning and saving are two calls, and the save must persist the EXACT plan
# that was shown — not a regenerated one, which could differ. That proposal
# lived only in this dict, which is one process's memory, so behind
# `--workers 4` the save usually landed on a worker that had never planned
# anything and reported "No cached plan ... it may have expired" for something
# made seconds earlier. A backend restart mid-conversation did the same.
#
# The dict stays as the fast path; storage is what makes it survive the jump.

_PLAN_CACHE: dict[str, dict[str, Any]] = {}


def cache_plan(plan: dict[str, Any], database: str) -> str:
    """Store a plan and return a fresh plan_id. Each call mints a new id, so
    iterating on a plan never overwrites a previous one."""
    from app.engine import storage as storage_mod

    plan_id = uuid.uuid4().hex
    entry = {"plan": plan, "database": database}
    _PLAN_CACHE[plan_id] = entry
    storage_mod.put_json(storage_mod.plan_key(plan_id), entry)
    return plan_id


def get_cached_plan(plan_id: str) -> dict[str, Any] | None:
    from app.engine import storage as storage_mod

    return _PLAN_CACHE.get(plan_id) or storage_mod.get_json(
        storage_mod.plan_key(plan_id)
    )


# --- Edit-proposal cache ----------------------------------------------------
# edit_dashboard is a two-step (preview -> confirm) flow like plan/save: the
# preview validates the narrow-edited plan and stashes it here keyed by
# report_id, so the confirm step commits the EXACT plan the user reviewed
# rather than re-generating (which could differ). Keyed by report_id because a
# conversation edits one report at a time. Shared across workers for the same
# reason as the plan cache above.
_EDIT_CACHE: dict[str, dict[str, Any]] = {}


def _cache_edit(report_id: str, entry: dict[str, Any]) -> None:
    from app.engine import storage as storage_mod

    _EDIT_CACHE[report_id] = entry
    storage_mod.put_json(storage_mod.edit_key(report_id), entry)


def _get_cached_edit(report_id: str) -> dict[str, Any] | None:
    from app.engine import storage as storage_mod

    return _EDIT_CACHE.get(report_id) or storage_mod.get_json(
        storage_mod.edit_key(report_id)
    )


def _drop_cached_edit(report_id: str) -> None:
    from app.engine import storage as storage_mod

    _EDIT_CACHE.pop(report_id, None)
    storage_mod.drop(storage_mod.edit_key(report_id))


# --- Plan orchestration -----------------------------------------------------

# `size` carries the bubble-radius column for bubble panels; it must survive into
# the saved/returned config or the webapp can't size the bubbles. `y` already
# doubles as a LIST of value columns for stacked_bar panels (JSON, passed as-is).
# `section`, `span` and `row_span` are the layout fields: which tab a panel sits
# on, and how many of the grid's 4 columns / height steps it occupies.
# `zero_baseline` and `palette` are presentation-only, and both are ABSENT by
# default so every dashboard saved before they existed keeps rendering exactly
# as it did: absent zero_baseline means "anchor the value axis at zero", absent
# palette means the default series colours plus the semantic status rules.
#
# `format` holds a panel's label and axis settings (axis titles, value unit,
# value labels, legend). Absent means every setting follows the
# chart standard in the frontend's chart-standard.ts, so it too leaves existing
# dashboards untouched.
_PANEL_CONFIG_FIELDS = ("title", "chart_type", "x", "y", "color", "orientation",
                        "display", "size", "section", "span", "row_span",
                        "column_formats", "zero_baseline", "palette", "format")

#: Series palettes a panel may name. The colours themselves live with the
#: renderers (frontend and PDF) — this list exists so an unknown name is
#: dropped here rather than reaching a renderer that has no colours for it.
PALETTE_NAMES = frozenset({"ocean", "ember", "meadow", "slate"})

#: How a table column's numbers should read, declared per column rather than
#: guessed from its name.
#:
#: The renderer has always inferred the unit from the column name — BALANCE and
#: PCT and DSCR each match a pattern. That works until a column is called TOTAL,
#: AMT or OPENING_BAL, which read as plain numbers and lose their currency.
#: KPI tiles already solved this with an authored FORMAT column, and formatKpi
#: carries the reason: "Debt Count" and "Debt Outstanding" share a word and are
#: a count and a currency respectively. A name cannot be trusted to carry a unit.
#:
#: Shaped as {column_name: {"format": ..., "decimals": ..., "label": ...}} so a
#: panel can also rename a column for display without changing its SQL alias.
#: Undeclared columns fall back to the name classifier, so every dashboard saved
#: before this keeps rendering exactly as it did.
COLUMN_FORMATS = ("currency", "percent", "number", "ratio", "text", "date")

#: The panel grid is 4 columns wide, so a span of 1-4 covers quarter through full
#: width. Height is stepped rather than free, and step 2 — not 1 — is the normal
#: height: a chart is given an explicit pixel height, so without a step BELOW the
#: default there is no way to make a tall panel shorter.
GRID_COLUMNS = 4
MAX_ROW_SPAN = 4


def _clamp_int(value: Any, low: int, high: int) -> int | None:
    """An int inside [low, high], or None when absent or unusable.

    None is meaningful: it means "no stored layout", and the renderer then
    derives a width from the panel's own shape. A planner that emits a span of
    0, 9 or "wide" must not be able to produce an unrenderable panel, so an
    out-of-range number is pulled to the nearest edge and junk becomes None.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, n))


#: How a panel may be presented. Anything else the model invents is dropped
#: rather than passed through — the renderer only understands these four, and a
#: stored value nothing honours looks configured while behaving as though it is
#: not.
DISPLAY_MODES = ("chart", "table", "both", "kpi")


def _normalise_display(panel: dict[str, Any]) -> None:
    """Reconcile `display` with `chart_type`, in place.

    The two fields can contradict each other, and the renderer used to settle it
    the destructive way: `display == "table"` returned a table BEFORE it looked
    at `chart_type`, so a panel that had gone to the trouble of choosing a funnel
    — with x, y and a chart-shaped query behind it — was drawn as a grid of
    numbers, and nothing anywhere said why. It presented as "the planner never
    uses the good chart types", which sent us looking at the prompt instead.

    Choosing a chart type is the deliberate act; `display` is the incidental one.
    So a panel that names a drawable chart keeps it, and where display said
    "table" it becomes "both" — the chart is drawn and the rows stay under it.
    Nothing is discarded either way. The reverse contradiction, a display of
    "chart" with nothing to draw, can only be a table.
    """
    display = panel.get("display")
    display = display.strip().lower() if isinstance(display, str) else None
    if display not in DISPLAY_MODES:
        display = None

    chart_type = panel.get("chart_type")
    chart_type = chart_type.strip().lower() if isinstance(chart_type, str) else None
    has_chart = bool(chart_type) and chart_type not in ("table", "kpi")

    if display == "kpi" or chart_type == "kpi":
        display = "kpi"
    elif display == "table" and has_chart:
        # Contradiction: keep the chart, keep the rows.
        display = "both"
    elif display in ("chart", "both") and not has_chart:
        # Nothing to draw, whatever the field claims.
        display = "table"
        if panel.get("x") and panel.get("y"):
            # The other contradiction, and the one that cost a dashboard: a
            # panel that picked a group-by column, a measure and chart-shaped
            # SQL, then named no chart. Rendering it as a grid of numbers is
            # the only honest thing left to do, but it is a planner defect
            # rather than a choice, and it used to happen in silence — a whole
            # dashboard came back as tables with nothing anywhere saying why.
            log.warning(
                "panel %r asks for a chart but names no chart_type; "
                "falling back to a table",
                panel.get("title"),
            )
    elif display is None:
        display = "chart" if has_chart else "table"

    panel["display"] = display


def _normalise_layout(panel: dict[str, Any]) -> dict[str, Any]:
    """Coerce a panel's layout fields in place, and return the panel.

    Applied wherever panels enter the system — a new plan, a direct edit, a
    layout patch — because all three arrive from outside (the model or a
    client) and the grid has to be able to trust these three values.
    """
    section = panel.get("section")
    panel["section"] = section.strip() or None if isinstance(section, str) else None
    span = _clamp_int(panel.get("span"), 1, GRID_COLUMNS)
    # Charts at quarter width are illegible. Half is the floor for anything
    # that is not a KPI (KPIs leave the grid and ignore span).
    is_kpi = panel.get("display") == "kpi" or panel.get("chart_type") == "kpi"
    if span == 1 and not is_kpi:
        span = 2
    panel["span"] = span
    panel["row_span"] = _clamp_int(panel.get("row_span"), 1, MAX_ROW_SPAN)
    panel["column_formats"] = _normalise_column_formats(panel.get("column_formats"))
    _normalise_display(panel)
    _normalise_style(panel)
    panel["format"] = _normalise_format(panel.get("format"))
    return panel


def _normalise_style(panel: dict[str, Any]) -> None:
    """Coerce the presentation-only fields, in place.

    Both drop to None when unusable rather than being passed through. A stored
    value no renderer honours is worse than an absent one: it looks configured
    while behaving as though it is not — the same reasoning as column formats.

    None is also the DEFAULT for both, and the defaults are the pre-existing
    behaviour, so a panel that never mentions them renders unchanged.
    """
    zero = panel.get("zero_baseline")
    if isinstance(zero, bool):
        # Only False is worth storing; True is what absent already means.
        panel["zero_baseline"] = False if zero is False else None
    elif isinstance(zero, str):
        panel["zero_baseline"] = False if zero.strip().lower() in {"false", "0", "no"} else None
    else:
        panel["zero_baseline"] = None

    palette = panel.get("palette")
    if isinstance(palette, str) and palette.strip().lower() in PALETTE_NAMES:
        panel["palette"] = palette.strip().lower()
    else:
        panel["palette"] = None


#: The settings a panel's `format` may carry, and the values each accepts.
#: Titles are free text; every other setting is one of a fixed set, because the
#: renderer switches on these words and an unknown one would do nothing.
FORMAT_CHOICES: dict[str, frozenset[str]] = {
    "unit": frozenset({"none", "K", "M", "B"}),
    "data_labels": frozenset({"none", "max", "min", "extremes", "all"}),
    "legend": frozenset({"bottom", "right", "none"}),
    "label_position": frozenset({"auto", "inside_end", "outside_end"}),
    "markers": frozenset({"auto", "show", "hide"}),
    # Funnel: percentage of the top stage, of the stage above, or none.
    "funnel_percent": frozenset({"first", "previous", "none"}),
}
FORMAT_TITLES = ("x_title", "y_title")
FORMAT_TITLE_MAX = 80
#: Settings one series can override, keyed by its column name under "series".
FORMAT_SERIES_KEYS = ("data_labels", "label_position", "markers")
FORMAT_SERIES_MAX = 25
FORMAT_SERIES_NAME_MAX = 128
#: Manual axis bounds, as numbers. A min at or above its max drops both.
FORMAT_BOUNDS = (("x_min", "x_max"), ("y_min", "y_max"))
#: A reference line across an axis: at the mean, the median, or a typed value.
FORMAT_REFERENCES = ("x_reference", "y_reference")
FORMAT_REFERENCE_KINDS = frozenset({"mean", "median", "value"})


def _finite_number(value: Any) -> int | float | None:
    """A real number, or None. True and False are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _normalise_format(raw: Any) -> dict[str, Any] | None:
    """Coerce a panel's label settings, keeping only what a renderer honours.

    None when nothing usable is set, which means "follow the chart standard".
    Each setting is independent: an unknown value drops that setting alone,
    so one bad word from a client cannot erase the others.

    An empty title is kept, and is different from an absent one. Absent means
    the standard's automatic title; "" means the author turned the title off.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in FORMAT_TITLES:
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value.strip()[:FORMAT_TITLE_MAX]
    for key, allowed in FORMAT_CHOICES.items():
        value = raw.get(key)
        if isinstance(value, str) and value.strip() in allowed:
            out[key] = value.strip()
    if isinstance(raw.get("stack_totals"), bool):
        out["stack_totals"] = raw["stack_totals"]
    if isinstance(raw.get("trend_line"), bool):
        out["trend_line"] = raw["trend_line"]

    for low_key, high_key in FORMAT_BOUNDS:
        low = _finite_number(raw.get(low_key))
        high = _finite_number(raw.get(high_key))
        if low is not None and high is not None and low >= high:
            continue
        if low is not None:
            out[low_key] = low
        if high is not None:
            out[high_key] = high

    for key in FORMAT_REFERENCES:
        ref = raw.get(key)
        if not isinstance(ref, dict) or ref.get("kind") not in FORMAT_REFERENCE_KINDS:
            continue
        if ref["kind"] == "value":
            value = _finite_number(ref.get("value"))
            if value is not None:
                out[key] = {"kind": "value", "value": value}
        else:
            out[key] = {"kind": ref["kind"]}

    # Per-series overrides, keyed by the series' column. A series with nothing
    # usable left is dropped rather than stored empty.
    series = raw.get("series")
    if isinstance(series, dict):
        kept: dict[str, dict[str, str]] = {}
        for name, settings in list(series.items())[:FORMAT_SERIES_MAX]:
            if not isinstance(name, str) or not isinstance(settings, dict):
                continue
            column = name.strip()
            if not column or len(column) > FORMAT_SERIES_NAME_MAX:
                continue
            entry = {}
            for key in FORMAT_SERIES_KEYS:
                value = settings.get(key)
                if isinstance(value, str) and value.strip() in FORMAT_CHOICES[key]:
                    entry[key] = value.strip()
            if entry:
                kept[column] = entry
        if kept:
            out["series"] = kept
    return out or None


def _normalise_column_formats(raw: Any) -> dict[str, dict[str, Any]] | None:
    """Coerce authored column formats, dropping anything unusable.

    None when nothing usable is declared, which is what tells the renderer to
    fall back to classifying by column name. An unknown format word is dropped
    rather than passed through: the renderer would ignore it anyway, and a
    stored value nothing honours is worse than an absent one — it looks
    configured while behaving as though it is not.
    """
    if not isinstance(raw, dict):
        return None

    out: dict[str, dict[str, Any]] = {}
    for column, spec in raw.items():
        if not isinstance(column, str) or not column.strip():
            continue
        if isinstance(spec, str):          # {"BALANCE": "currency"} shorthand
            spec = {"format": spec}
        if not isinstance(spec, dict):
            continue

        entry: dict[str, Any] = {}
        fmt = str(spec.get("format") or "").strip().lower()
        if fmt in COLUMN_FORMATS:
            entry["format"] = fmt
        decimals = _clamp_int(spec.get("decimals"), 0, 6)
        if decimals is not None:
            entry["decimals"] = decimals
        label = spec.get("label")
        if isinstance(label, str) and label.strip():
            entry["label"] = label.strip()[:60]
        currency = spec.get("currency")
        if isinstance(currency, str):
            code = currency.strip().upper()
            if code == "":
                # An EMPTY code is a statement — "this column does not carry a
                # currency I can name" — and must survive. Dropping it as
                # falsy restores the dollar default to a column that said it
                # was not dollars, which is the bug this field exists to fix.
                entry["currency"] = ""
            elif len(code) == 3 and code.isascii() and code.isalpha():
                entry["currency"] = code
            # Anything else is dropped rather than passed on: printed beside
            # the number it would read as a unit that does not exist ("US
            # 1,200"), and a reader has no way to tell it is nonsense.
        if entry:
            out[column.strip()] = entry
    return out or None


# --- running one dashboard's panel queries together -----------------------------

_T = TypeVar("_T")
_R = TypeVar("_R")

#: Upper bound on DASHBOARD_QUERY_CONCURRENCY, whatever it is set to.
_MAX_QUERY_CONCURRENCY = 8

#: Panel queries running at once across EVERY dashboard in this process: many
#: people opening dashboards together must queue here, not open an unbounded
#: number of threads and warehouse queries. Production runs four worker
#: processes, so at most 32 in all. A query waits for its slot BEFORE it is
#: sent, so time queued here never counts against its 30-second timeout. Held
#: by worker threads only, never by the thread waiting on them, so it cannot
#: deadlock.
_PROCESS_QUERY_SLOTS = threading.BoundedSemaphore(8)


def _query_concurrency() -> int:
    """How many of one dashboard's panel queries run at once.

    DASHBOARD_QUERY_CONCURRENCY, read on every call. 1 -- the default -- is the
    original behaviour: one panel after another on the calling thread.
    """
    try:
        wanted = int(os.environ.get("DASHBOARD_QUERY_CONCURRENCY", "1"))
    except ValueError:
        return 1
    return max(1, min(wanted, _MAX_QUERY_CONCURRENCY))


def _each_panel(work: Callable[[_T], _R], items: list[_T]) -> list[_R]:
    """`work` over every item, results in item order.

    Serial unless DASHBOARD_QUERY_CONCURRENCY allows more. In parallel, each
    worker runs in a copy of the caller's context, which is what carries the
    SIGNED-IN USER'S connection (use_connection) and audit login: a worker
    without it would fall back to the shared service-account connection and run
    the panel as the wrong identity. `work` must handle its own errors, as the
    per-panel functions below do, so one panel never affects another.
    """
    limit = min(_query_concurrency(), len(items))
    if limit <= 1:
        return [work(item) for item in items]

    def in_slot(context: contextvars.Context, item: _T) -> _R:
        with _PROCESS_QUERY_SLOTS:
            return context.run(work, item)

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="dashboard-query") as pool:
        futures = [pool.submit(in_slot, contextvars.copy_context(), item) for item in items]
        return [future.result() for future in futures]


def _panel_config(panel: dict[str, Any]) -> dict[str, Any]:
    """The render config a panel carries (everything except its SQL)."""
    return _normalise_layout({k: panel.get(k) for k in _PANEL_CONFIG_FIELDS})


def _validate_panels(panels: list[dict[str, Any]],
                     parameters: list[dict[str, Any]] | None = None,
                     columns: dict[int, list[str]] | None = None,
                     ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Validate every panel's SQL (read-only safety + a zero-row Snowflake dry-run
    so columns/joins resolve). Returns (kept, skipped[{title,reason}]). Shared by
    create_plan (new plans) and preview_edit (edited plans) so both apply the
    exact same gate.

    For a parameterized panel (SQL containing {{name}} placeholders), each
    placeholder is first substituted with a safe representative value matching its
    declared type (sql_params.sample_values), then the RENDERED SQL is dry-run —
    exactly the Excel-report validation approach. The rendered statement still
    passes through validate_sql inside dry_run_query, so genuinely unsafe SQL is
    still rejected; substitution only fills the placeholders so parsing succeeds.

    `columns`, when given, is filled with the names each panel's query returns,
    keyed by the panel's position in `panels`."""
    samples = sql_params.sample_values(parameters) if parameters else {}

    def check(numbered: tuple[int, dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
        i, panel = numbered
        title = panel.get("title") or f"panel {i}"
        sql = panel.get("sql")
        if not sql:
            return False, {"title": title, "reason": "panel has no SQL"}
        try:
            probe_sql = sql
            if sql_params.has_placeholders(sql):
                probe_sql = sql_params.render_sql(sql, parameters or [], samples)
            returned = snowflake_client.dry_run_query(probe_sql)  # validate_sql + compile
            if columns is not None and isinstance(returned, (list, tuple)):
                columns[i - 1] = [str(name) for name in returned]
        except sql_params.ParameterError as exc:
            return False, {"title": title, "reason": f"parameter error: {exc}"}
        except UnsafeSQLError as exc:
            return False, {"title": title, "reason": f"SQL rejected by safety layer: {exc}"}
        except Exception as exc:  # noqa: BLE001 - record the compile error, keep going
            return False, {"title": title, "reason": f"SQL failed to compile: {exc}"}
        # Normalise here rather than at render time so what gets STORED is
        # already within the grid's range — every path that creates or edits a
        # plan comes through this function.
        return True, _normalise_layout(panel)

    outcomes = _each_panel(check, list(enumerate(panels, start=1)))
    kept = [value for ok, value in outcomes if ok]
    skipped = [value for ok, value in outcomes if not ok]
    return kept, skipped


def create_plan(description: str, database: str) -> dict[str, Any]:
    """Generate, validate, dry-run, and cache a dashboard plan.

    Every panel's SQL is checked twice — sql_safety.validate_sql (read-only) then
    a zero-row dry-run against Snowflake (columns/joins resolve). Panels that fail
    are DROPPED (not fatal): the valid panels are cached and returned, and each
    dropped panel is reported in `skipped_panels` [{title, reason}]. Only if EVERY
    panel fails is DashboardError raised (nothing to plan).

    Returns {plan_id, plan:{title, panels:[...]}, database, panel_count,
    skipped_panels}.
    """
    context = metadata_store.build_context(database)
    heuristics = metadata_store.dashboard_heuristics(database)

    plan = llm.generate_dashboard_plan(description, context, heuristics)  # may raise LLMError

    # The planner flags when it can't bind a date parameter without asking; surface
    # that instead of saving a guess.
    if plan.needs_clarification:
        return {"status": "needs_clarification", "database": database,
                "questions": [plan.needs_clarification],
                "message": "The dashboard could not be planned without clarification:"}

    if not plan.panels:
        raise DashboardError("The planner returned no panels; try a more specific description.")

    parameters = plan.parameters or []

    # Enforce metadata-driven date-parameter binding BEFORE anything is cached: a
    # date parameter must bind to a column the semantic layer declares, per table
    # — never a guess. Unsupported/ambiguous bindings stop here and ask the user.
    binding_problems = _validate_date_bindings(database, parameters, plan.panels)
    if binding_problems:
        return {"status": "needs_clarification", "database": database,
                "questions": binding_problems,
                "message": ("Date parameters must bind to columns declared in the semantic layer. "
                            "Resolve these with the user before the dashboard can be saved:")}

    kept_panels, skipped_panels = _validate_panels(plan.panels, parameters)

    if not kept_panels:
        reasons = "; ".join(f"'{s['title']}': {s['reason']}" for s in skipped_panels)
        raise DashboardError(f"No panels could be validated. {reasons}")

    # Tabs are held to the layout rules here rather than trusted to the prompt.
    planned_panels = kept_panels
    kept_panels = consolidate_sections(kept_panels)
    layout_note = consolidation_note(planned_panels, kept_panels)
    plan_obj = {"title": plan.title, "panels": kept_panels, "parameters": parameters}
    plan_id = cache_plan(plan_obj, database)
    return {
        "plan_id": plan_id,
        "plan": plan_obj,
        "database": database,
        "panel_count": len(kept_panels),
        "parameters": parameters,
        "skipped_panels": skipped_panels,
        **({"layout_note": layout_note} if layout_note else {}),
    }


def save_plan(plan_id: str, report_title: str,
              description: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    """Persist a cached plan. Raises DashboardError if plan_id isn't cached."""
    entry = get_cached_plan(plan_id)
    if entry is None:
        raise DashboardError(
            f"No cached plan for plan_id '{plan_id}' (it may have expired). "
            "Call plan_dashboard again to regenerate a plan, then save that."
        )

    report_id = uuid.uuid4().hex
    created_by, _role = snowflake_client.session_identity()
    snowflake_client.write_saved_dashboard({
        "report_id": report_id,
        "version_id": f"{report_id}_v1",   # VERSION_ID is the unique row key
        "version": 1,
        "report_title": report_title,
        "description": description or None,
        "tags": tags or [],
        "dashboard_plan": entry["plan"],
        "created_by": created_by,
        "edit_note": None,
        # Stored rather than inferred later: a list of 74 dashboards cannot
        # afford to re-read every plan to work out where each one came from.
        "database_name": (entry.get("database") or "").upper() or None,
    })
    return {
        "report_id": report_id,
        "report_title": report_title,
        "version": 1,
        "saved": True,
        "webapp_url": f"{WEBAPP_BASE_URL}/dashboards/{report_id}",
    }


def set_group(report_id: str, group: str | None) -> dict[str, Any]:
    """File a saved dashboard under `group`; None restores the default."""
    if snowflake_client.read_saved_dashboard(report_id) is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")
    snowflake_client.set_dashboard_group(report_id, group)
    return {"report_id": report_id, "group": group}


def list_dashboards(limit: int = 50) -> list[dict[str, Any]]:
    """Saved dashboards (newest first), metadata only."""
    rows = snowflake_client.list_saved_dashboards(max(1, min(int(limit), 500)))
    return [
        {
            "report_id": r.get("REPORT_ID"),
            "report_title": r.get("REPORT_TITLE"),
            "version": r.get("VERSION"),
            # OWNER is the first version's author; CREATED_BY on the current
            # row is whoever last edited it, which is a different fact.
            "created_by": r.get("OWNER"),
            "created_at": r.get("CREATED_AT"),
            "updated_by": r.get("UPDATED_BY"),
            "updated_at": r.get("UPDATED_AT"),
            "database": r.get("DATABASE_NAME"),
            # Only set when somebody deliberately filed it elsewhere.
            "data_group_override": r.get("DATA_GROUP"),
            "last_run_at": r.get("LAST_RUN_AT"),
            "run_count": r.get("RUN_COUNT"),
        }
        for r in rows
    ]


def _resolve_param_values(parameters: list[dict[str, Any]],
                          values: dict[str, Any] | None) -> dict[str, Any]:
    """The effective value for each declared parameter: the supplied value, else
    the declared default. Echoed back so the caller/webapp knows what ran."""
    values = values or {}
    out: dict[str, Any] = {}
    for p in parameters:
        name = p.get("name")
        if name in values and values[name] is not None:
            out[name] = values[name]
        else:
            out[name] = p.get("default")
    return out


def run_dashboard(report_id: str,
                  parameter_values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Re-execute a saved dashboard's CURRENT version live (zero LLM calls) and
    return the data plus each panel's saved config unchanged. Increments
    RUN_COUNT / LAST_RUN_AT. Raises DashboardError if the report doesn't exist.

    If the dashboard declares parameters, each panel's {{name}} placeholders are
    substituted (supplied value, else declared default) via the SAME sql_params
    renderer the Excel reports use, and the rendered SQL runs through the normal
    read-only run_query gate. Running with different parameter values is NOT an
    edit and never creates a new version.
    """
    rec = snowflake_client.read_saved_dashboard(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    plan = rec.get("DASHBOARD_PLAN") or {}
    parameters = plan.get("parameters") or []
    # Attached BESIDE each panel's config, never folded into its rows: a
    # passthrough test asserts run_dashboard returns exactly what the warehouse
    # produced, and a renderer that has the code can format the numbers itself.
    default_currency = metadata_store.currency(_infer_database(plan))

    def run_panel(panel: dict[str, Any]) -> dict[str, Any]:
        config = _panel_config(panel)
        sql = panel.get("sql")
        try:
            run_sql = sql
            if sql and parameters and sql_params.has_placeholders(sql):
                run_sql = sql_params.render_sql(sql, parameters, parameter_values)
            result = snowflake_client.run_query(
                run_sql, max_rows=snowflake_client.DASHBOARD_MAX_ROWS)
            # Rows are returned exactly as Snowflake produced them — no server-side
            # shaping. Any year/quarter formatting/ordering is baked into the SQL.
            # A dashboard panel is capped at DASHBOARD_MAX_ROWS, not the chat cap:
            # the page draws every row, and the table pages through them. The
            # warehouse says nothing when the cap bites. Chart SQL is ordered oldest-first, so
            # the rows dropped are the most RECENT ones — the end of the line a
            # reader is looking at. A short panel and a cut-off panel are
            # indistinguishable on screen unless the cut is stated.
            truncated = result["row_count"] >= snowflake_client.DASHBOARD_MAX_ROWS
            return {**config, "columns": result["columns"],
                    "rows": result["rows"], "row_count": result["row_count"],
                    "truncated": truncated,
                    "default_currency": default_currency}
        except Exception as exc:  # noqa: BLE001 - one bad panel shouldn't sink the report
            return {**config, "columns": [], "rows": [], "row_count": 0,
                    "truncated": False,
                    "error": str(exc),
                    "default_currency": default_currency}

    panels_out = _each_panel(run_panel, list(plan.get("panels", [])))

    # Count this as a run even if some panels errored (matches "every live fetch
    # is a run"): the dashboard was executed.
    run_count, last_run_at = snowflake_client.touch_saved_dashboard_run(report_id)
    return {
        "report_id": report_id,
        "title": plan.get("title"),
        "run_count": run_count,
        "last_run_at": last_run_at,
        "parameters": parameters,
        "parameter_values": _resolve_param_values(parameters, parameter_values),
        "panels": panels_out,
    }


def get_dashboard_config(report_id: str) -> dict[str, Any]:
    """The saved plan WITHOUT running a single panel — metadata only.

    Deliberately separate from run_dashboard: that one re-executes every panel
    and counts as a run. The dashboard-scoped chat needs the *configuration* on
    every turn (what the panels are, how they are charted, what SQL backs them)
    and that must not cost a full re-execution, nor silently inflate RUN_COUNT
    each time someone asks a question. Live figures come from the model calling
    run_saved_dashboard, which is a run and should be counted as one.
    """
    rec = snowflake_client.read_saved_dashboard(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    plan = rec.get("DASHBOARD_PLAN") or {}
    return {
        "report_id": report_id,
        "title": plan.get("title") or rec.get("REPORT_TITLE"),
        "description": rec.get("DESCRIPTION") or "",
        "database": _infer_database(plan),
        "parameters": plan.get("parameters") or [],
        "panels": [
            {**_panel_config(panel), "sql": panel.get("sql")}
            for panel in plan.get("panels", [])
        ],
    }


# --- Editing + version history ---------------------------------------------

# Fields compared panel-by-panel when diffing an edited plan against the current
# one. Kept in sync with the panel config the renderer uses (plus sql/title).
_DIFF_FIELDS = ("title", "sql", "chart_type", "x", "y", "color", "size",
                "orientation", "display", "section", "span", "row_span")


def _infer_database(plan: dict[str, Any]) -> str | None:
    """Best-effort: which documented database a saved plan targets. Panel SQL uses
    fully-qualified DATABASE.SCHEMA.TABLE names, so scan for a documented db name
    used as a qualifier. Needed to rebuild business_context for an edit, since the
    database isn't stored as its own column."""
    blob = " ".join((p.get("sql") or "") for p in plan.get("panels", [])).upper()
    for db in metadata_store.documented_databases():
        if f"{db.upper()}." in blob:
            return db
    return None


#: A database named in quotes at the END of a dashboard's title, e.g.
#: `Sales & Funnel Performance "ECOMMERCE"`. Tolerates what the titles actually
#: contain: an unclosed quote, trailing punctuation after the closing one.
_TITLE_DATABASE_RE = re.compile(r'"\s*([^"]+?)\s*"?[\s)\].,:;-]*$')


def _squash(text: str) -> str:
    """Upper case, letters and digits only — so "E-Commerce" and ECOMMERCE_DB
    can be compared without caring about punctuation or the _DB suffix."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def infer_database_from_title(title: str | None) -> str | None:
    """The database a dashboard's TITLE claims it was built from.

    A fallback for rows saved before the database was stored as a column. The
    convention is the planner's own: the database goes in quotes at the end of
    the title. It is a claim by a title, not a fact about the SQL, so it is only
    consulted when the stored column has nothing.

    Returns None unless exactly one documented database matches — an ambiguous
    token groups nowhere rather than guessing wrong and hiding a dashboard in
    the wrong folder.
    """
    match = _TITLE_DATABASE_RE.search(title or "")
    if not match:
        return None
    token = _squash(match.group(1))
    if not token:
        return None
    hits = {
        name
        for name in metadata_store.documented_databases()
        if _squash(name) in (token, token + "DB", "DB" + token)
    }
    return next(iter(hits)) if len(hits) == 1 else None


_QUALIFIED_TABLE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b")


def _referenced_tables(sql: str) -> set[str]:
    """Fully-qualified DB.SCHEMA.TABLE names referenced in a panel's SQL (upper)."""
    return {m.group(1).upper() for m in _QUALIFIED_TABLE_RE.finditer(sql or "")}


def _uses_param(sql: str, name: str) -> bool:
    return re.search(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", sql or "") is not None


def _extract_placeholder_columns(sql: str, param_name: str) -> set[str]:
    """The set of column names a {{param_name}} placeholder is COMPARED AGAINST in
    this SQL — e.g. 'WHERE MARK_DATE <= {{as_of}}' -> {'MARK_DATE'}. Determines
    what column the parameter actually binds to, so the binding can be checked
    against the metadata rather than trusted.

    Substitutes this parameter's placeholder with a unique sentinel literal (and
    any other parameters with a different sentinel) so the query parses, then
    walks comparison/BETWEEN nodes for the column on the opposite side of the
    sentinel. Best-effort: returns an empty set if the SQL can't be parsed."""
    import sqlglot
    from sqlglot import exp

    this_sent, other_sent = "__MCP_DATE_BIND__", "__MCP_OTHER_PARAM__"
    probe = re.sub(r"\{\{\s*" + re.escape(param_name) + r"\s*\}\}", f"'{this_sent}'", sql or "")
    probe = sql_params.PLACEHOLDER_RE.sub(f"'{other_sent}'", probe)
    try:
        tree = sqlglot.parse_one(probe, dialect="snowflake")
    except Exception:  # noqa: BLE001 - unparseable -> nothing we can assert
        return set()

    def has_sentinel(node) -> bool:
        return node is not None and any(
            isinstance(n, exp.Literal) and n.this == this_sent for n in node.walk())

    def cols_in(node) -> set[str]:
        return {c.name.upper() for c in node.find_all(exp.Column)} if node is not None else set()

    found: set[str] = set()
    for node in tree.walk():
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            left, right = node.left, node.right
            if has_sentinel(left) and not has_sentinel(right):
                found |= cols_in(right)
            elif has_sentinel(right) and not has_sentinel(left):
                found |= cols_in(left)
        elif isinstance(node, exp.Between):
            low, high = node.args.get("low"), node.args.get("high")
            if (has_sentinel(low) or has_sentinel(high)) and not has_sentinel(node.this):
                found |= cols_in(node.this)
    return found


def _binding_problem(param_name: str, table: str, role: str | None,
                     res: dict[str, Any]) -> str:
    status = res.get("status")
    if status == "unsupported":
        return (f"Date parameter '{param_name}' cannot be bound to {table}: it has no date column "
                f"in the semantic layer, so it cannot support an as-of/date filter. Ask the user "
                f"how (or whether) to filter this table before saving.")
    if status == "ambiguous":
        return (f"Date parameter '{param_name}' is ambiguous for {table}: more than one date column "
                f"could apply ({', '.join(res.get('candidates', []))}). Ask the user which column to bind to.")
    if status == "role_not_found":
        return (f"Date parameter '{param_name}' asked for role '{role}' on {table}, which has no such "
                f"date column. Available date columns: {', '.join(res.get('candidates', [])) or 'none'}.")
    return f"Date parameter '{param_name}' could not be resolved for {table}."


def _validate_date_bindings(database: str, parameters: list[dict[str, Any]],
                            panels: list[dict[str, Any]]) -> list[str]:
    """Enforce that every DATE parameter binds to a metadata-declared column, per
    table — never a guess. Returns a list of human-readable clarification problems
    (empty when everything binds cleanly). The caller turns a non-empty list into
    a 'needs_clarification' result instead of saving the plan."""
    problems: list[str] = []
    for p in parameters or []:
        if p.get("type") != "date":
            continue
        name = p.get("name")
        bindings = p.get("bindings") or []

        # 1. Every declared binding must resolve to exactly one metadata column.
        resolved: dict[str, str] = {}   # TABLE_UPPER -> COLUMN_UPPER
        for b in bindings:
            table = b.get("table")
            role = b.get("role")
            if not table:
                problems.append(f"Date parameter '{name}' has a binding with no table.")
                continue
            res = metadata_store.resolve_date_column(database, table, role)
            if res.get("status") != "ok":
                problems.append(_binding_problem(name, table, role, res))
            else:
                resolved[str(table).upper()] = str(res["column"]).upper()

        # 2. Every panel that USES the parameter must compare it against exactly a
        #    resolved column of a table that panel references (catches a planner
        #    that wrote a different/guessed column than it declared).
        for panel in panels:
            sql = panel.get("sql") or ""
            if not _uses_param(sql, name):
                continue
            title = panel.get("title") or "panel"
            if not bindings:
                problems.append(
                    f"Date parameter '{name}' is used in panel '{title}' but declares no metadata "
                    f"bindings — the column it filters must come from the semantic layer.")
                continue
            ref_tables = _referenced_tables(sql)
            allowed = {col for tbl, col in resolved.items() if tbl in ref_tables}
            for col in _extract_placeholder_columns(sql, name):
                if col not in allowed:
                    problems.append(
                        f"Panel '{title}' binds date parameter '{name}' to column {col}, which is not "
                        f"the metadata-declared date column for its table. Bind it to the column the "
                        f"table's Date-columns metadata specifies, or ask the user.")
    return problems


def _diff_plans(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Structured diff of two plans so an edit can be reviewed before commit.
    Panels are matched by position; per changed panel we list exactly which
    fields changed (old -> new). Added/removed panels are reported separately —
    for a narrow edit these lists should normally be empty."""
    old_p = old.get("panels") or []
    new_p = new.get("panels") or []
    diff: dict[str, Any] = {
        "title_changed": None, "panels_changed": [],
        "panels_added": [], "panels_removed": [],
    }
    if (old.get("title") or "") != (new.get("title") or ""):
        diff["title_changed"] = {"old": old.get("title"), "new": new.get("title")}
    for i in range(min(len(old_p), len(new_p))):
        op, np_ = old_p[i], new_p[i]
        fields = {f: {"old": op.get(f), "new": np_.get(f)}
                  for f in _DIFF_FIELDS if op.get(f) != np_.get(f)}
        if fields:
            diff["panels_changed"].append(
                {"index": i, "title": np_.get("title") or op.get("title"), "fields": fields})
    for i in range(len(old_p), len(new_p)):
        diff["panels_added"].append({"index": i, "title": new_p[i].get("title")})
    for i in range(len(new_p), len(old_p)):
        diff["panels_removed"].append({"index": i, "title": old_p[i].get("title")})
    old_params = old.get("parameters") or []
    new_params = new.get("parameters") or []
    diff["parameters_changed"] = (
        {"old": old_params, "new": new_params} if old_params != new_params else None)
    diff["has_changes"] = bool(
        diff["title_changed"] or diff["panels_changed"]
        or diff["panels_added"] or diff["panels_removed"] or diff["parameters_changed"])
    return diff


# A change that swaps one literal value for another (a date/period/filter value)
# rather than restructuring the dashboard. Structural keywords below OVERRIDE the
# value signals — "add a parameter", "add/remove a panel", "change the chart
# type", "fix the SQL" are edits, not value swaps.
_STRUCTURAL_KW = (
    "parameter", "add panel", "add a panel", "another panel", "new panel",
    "remove panel", "delete panel", "drop panel", "chart type", "bar chart",
    "line chart", "pie chart", "scatter", "bubble", "stacked", "heatmap", "funnel",
    "kpi", "redesign", "rename", "reorder", "restyle", "column", "fix ", "broken",
    "title",
)
_VALUE_SIGNAL_RE = re.compile(
    r"(instead of|rather than|\bas of\b|run (?:it|this) for|show (?:the )?data for)",
    re.IGNORECASE)
_DATEISH_RE = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2}|\b\d{1,2}(?:st|nd|rd|th)\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\bquarter\b|\bQ[1-4]\b)",
    re.IGNORECASE)


def _looks_like_value_change(text: str) -> bool:
    """Heuristic: does this edit request read like swapping a parameter VALUE
    (e.g. 'show data for the 24th instead of the 23rd') rather than a structural
    edit? Conservative — any structural keyword vetoes it, so a genuine
    add/remove/redesign is never mistaken for a value change."""
    t = (text or "").lower()
    if any(kw in t for kw in _STRUCTURAL_KW):
        return False
    if _VALUE_SIGNAL_RE.search(t):
        return True
    # a bare date/period reference combined with a "for/to" phrasing
    return bool(_DATEISH_RE.search(t) and (" for " in t or " to " in t or "instead" in t))


def _parameter_redirect(changes_description: str,
                        current_params: list[dict[str, Any]]) -> dict[str, Any] | None:
    """If an edit request looks like a parameter VALUE change, return a redirect
    result instead of editing (no new version). Otherwise None (proceed to edit).

    - dashboard already has parameters -> tell the user to use the parameter
      control (run_saved_dashboard with parameter_values), don't edit.
    - dashboard has none            -> tell the user to add the value AS A
      PARAMETER (re-run edit_dashboard to declare one), not hardcode it."""
    if not _looks_like_value_change(changes_description):
        return None
    if current_params:
        names = [p.get("name") for p in current_params]
        return {
            "status": "use_parameter",
            "parameters": current_params,
            "message": (
                f"This looks like a parameter VALUE change, and this dashboard already "
                f"declares parameter(s) {names}. Don't create a new version — re-run it "
                f"with run_saved_dashboard(report_id, parameter_values={{...}}) using the "
                f"dashboard's own control (or the webapp's input). No edit is needed."),
        }
    return {
        "status": "suggest_parameter",
        "message": (
            "This looks like a parameter VALUE change (e.g. a specific date/period), but "
            "this dashboard has no matching parameter yet. Do NOT hardcode the new value as "
            "a new version. Instead add it AS A PARAMETER: re-run edit_dashboard with a "
            "change like 'add a date parameter as_of_date (default = the current value) and "
            "use it in the relevant panels' WHERE clauses via {{as_of_date}}'. After that, "
            "changing the date is just a parameter input, never a new version."),
    }


def _resolve_one(report_id_or_title: str) -> dict[str, Any]:
    """Resolve an id-or-title to EXACTLY one current report_id, or an explicit
    non-match result. Never guesses: 0 -> not_found, >1 -> ambiguous."""
    matches = snowflake_client.resolve_report_ids(report_id_or_title)
    if not matches:
        return {"status": "not_found", "message": (
            f"No current dashboard matches '{report_id_or_title}'. Check the exact "
            "title (case-insensitive) or pass the report_id — see list_saved_dashboards.")}
    if len(matches) > 1:
        return {"status": "ambiguous", "matches": matches, "message": (
            f"'{report_id_or_title}' matches {len(matches)} dashboards. Re-run with the "
            "exact report_id of the one you mean — I won't guess."), }
    return {"status": "ok", "report_id": matches[0]["report_id"]}


def preview_edit(report_id_or_title: str, changes_description: str) -> dict[str, Any]:
    """Regenerate ONLY the requested change against the current version and return
    a diff for review. Does NOT write anything — the new version is committed by
    commit_edit after the user approves. Rejects the edit if any panel fails
    validation (an edit must never silently drop a panel)."""
    resolved = _resolve_one(report_id_or_title)
    if resolved["status"] != "ok":
        return resolved
    report_id = resolved["report_id"]

    rec = snowflake_client.read_saved_dashboard(report_id)
    if rec is None:
        return {"status": "not_found",
                "message": f"No current version found for report_id '{report_id}'."}
    current_plan = rec.get("DASHBOARD_PLAN") or {}
    current_version = int(rec.get("VERSION") or 1)
    current_params = current_plan.get("parameters") or []

    # Redirect check (BEFORE any LLM work): a request to change a parameter VALUE
    # should use the parameter control, or become a parameter — not a hardcoded
    # new version. Returns without editing when it applies.
    redirect = _parameter_redirect(changes_description, current_params)
    if redirect is not None:
        return {**redirect, "report_id": report_id, "report_title": rec.get("REPORT_TITLE")}

    database = _infer_database(current_plan)
    context = metadata_store.build_context(database) if database else ""
    heuristics = metadata_store.dashboard_heuristics(database) if database else ""

    edited = llm.edit_dashboard_plan(current_plan, changes_description, context, heuristics)  # may raise LLMError
    if edited.needs_clarification:
        return {"status": "needs_clarification", "report_id": report_id,
                "report_title": rec.get("REPORT_TITLE"),
                "questions": [edited.needs_clarification],
                "message": "The edit could not be completed without clarification:"}
    if not edited.panels:
        raise DashboardError("The edit produced no panels; rephrase the change.")

    new_params = edited.parameters or []

    # Same metadata-driven date-binding enforcement as create_plan: an edit that
    # adds or changes a date parameter must bind to a semantic-layer column.
    if database:
        binding_problems = _validate_date_bindings(database, new_params, edited.panels)
        if binding_problems:
            return {"status": "needs_clarification", "report_id": report_id,
                    "report_title": rec.get("REPORT_TITLE"), "questions": binding_problems,
                    "message": ("Date parameters must bind to columns declared in the semantic "
                                "layer. Resolve these with the user before committing the edit:")}

    kept, skipped = _validate_panels(edited.panels, new_params)
    if skipped:
        # An edit must not quietly shrink a dashboard: reject wholesale so the
        # user never loses a panel to a bad edit.
        reasons = "; ".join(f"'{s['title']}': {s['reason']}" for s in skipped)
        raise DashboardError(
            f"The edited plan has {len(skipped)} panel(s) that failed validation, "
            f"so the edit was not staged (no panel is dropped on an edit). {reasons}")

    # Only tabs this edit invents are checked; existing ones are out of scope.
    proposed_panels = kept
    kept = settle_new_sections(current_plan.get("panels") or [], kept)
    layout_note = settlement_note(proposed_panels, kept)
    new_plan = {"title": edited.title, "panels": kept, "parameters": new_params}
    diff = _diff_plans(current_plan, new_plan)

    _cache_edit(report_id, {
        "plan": new_plan,
        "changes_description": changes_description,
        "base_version": current_version,
        "report_title": rec.get("REPORT_TITLE"),
        "description": rec.get("DESCRIPTION"),
        "tags": rec.get("TAGS") or [],
        "diff": diff,
    })
    return {
        "status": "preview",
        "report_id": report_id,
        "report_title": rec.get("REPORT_TITLE"),
        "database": database,
        "current_version": current_version,
        "new_version": current_version + 1,
        "edit_note": changes_description,
        "diff": diff,
        "parameters": new_params,
        "proposed_plan": new_plan,
        **({"layout_note": layout_note} if layout_note else {}),
        "message": ("Review this diff against the requested change. Confirm the ONLY "
                    "differences are the ones you asked for — flag any panel or field "
                    "that changed outside the request — then call edit_dashboard again "
                    "with confirm=true to commit this as a new version."),
    }


def commit_edit(report_id_or_title: str, changes_description: str) -> dict[str, Any]:
    """Commit the previewed edit as a NEW version (previous version preserved,
    demoted to IS_CURRENT=FALSE). Requires a matching preview from preview_edit."""
    resolved = _resolve_one(report_id_or_title)
    if resolved["status"] != "ok":
        return resolved
    report_id = resolved["report_id"]

    cached = _get_cached_edit(report_id)
    if cached is None or cached["changes_description"] != changes_description:
        raise DashboardError(
            "No previewed edit found for this dashboard and change. Call edit_dashboard "
            "WITHOUT confirm first to preview the diff, then confirm with the same "
            "report and changes_description.")

    new_version = cached["base_version"] + 1
    created_by, _role = snowflake_client.session_identity()
    written = snowflake_client.insert_dashboard_version({
        "report_id": report_id,
        "version_id": f"{report_id}_v{new_version}",
        "version": new_version,
        "report_title": cached["report_title"],
        "description": cached["description"],
        "tags": cached["tags"],
        "dashboard_plan": cached["plan"],
        "created_by": created_by,
        "edit_note": changes_description,
        # Carried on EVERY version. Without it an edit would insert a row with
        # a null database and the dashboard would fall out of its group.
        "database_name": _infer_database(cached["plan"]),
    })
    # The number actually written: past the highest version, not the current one.
    new_version = written if isinstance(written, int) else new_version
    _drop_cached_edit(report_id)
    return {
        "status": "committed",
        "report_id": report_id,
        "report_title": cached["report_title"],
        "version": new_version,
        "version_id": f"{report_id}_v{new_version}",
        "edit_note": changes_description,
        "diff": cached["diff"],
        "webapp_url": f"{WEBAPP_BASE_URL}/dashboards/{report_id}",
    }


def list_versions(report_id: str) -> dict[str, Any]:
    """Every version of a report_id, oldest first, metadata only."""
    rows = snowflake_client.list_dashboard_versions(report_id)
    if not rows:
        raise DashboardError(f"No versions found for report_id '{report_id}'.")
    versions = [
        {
            "version": r.get("VERSION"),
            "version_id": r.get("VERSION_ID"),
            "edit_note": r.get("EDIT_NOTE"),
            "created_by": r.get("CREATED_BY"),
            "created_at": r.get("CREATED_AT"),
            "is_current": r.get("IS_CURRENT"),
        }
        for r in rows
    ]
    return {"report_id": report_id, "count": len(versions), "versions": versions}


def update_plan(report_id: str, plan: dict[str, Any],
                edit_note: str = "") -> dict[str, Any]:
    """Replace a dashboard's plan with one edited directly, as a NEW version.

    The sibling of preview_edit/commit_edit, for a caller that already knows
    exactly what it wants rather than describing a change in words. It skips the
    model entirely but goes through the SAME gates, because those gates are
    about the plan, not about who wrote it:

      * every panel's SQL is validate_sql'd and dry-run against Snowflake
      * a date parameter must still bind to a semantic-layer column
      * one bad panel rejects the whole edit rather than being dropped, so an
        edit can never quietly shrink a dashboard
      * the previous version is preserved and demoted, so any save is revertible

    Only known panel fields are carried across. The plan arrives from a client,
    and echoing arbitrary keys straight into stored JSON is how a renderer ends
    up trusting something nobody validated.
    """
    rec = snowflake_client.read_saved_dashboard(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    current_plan = rec.get("DASHBOARD_PLAN") or {}
    current_version = int(rec.get("VERSION") or 1)

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

    database = _infer_database({"panels": panels}) or _infer_database(current_plan)
    if database:
        binding_problems = _validate_date_bindings(database, parameters, panels)
        if binding_problems:
            raise DashboardError(
                "Date parameters must bind to a column declared in the semantic "
                "layer:\n  " + "\n  ".join(binding_problems)
            )

    returned: dict[int, list[str]] = {}
    kept, skipped = _validate_panels(panels, parameters, returned)
    if skipped:
        reasons = "; ".join(f"'{s['title']}': {s['reason']}" for s in skipped)
        raise DashboardError(
            f"{len(skipped)} panel(s) failed validation, so nothing was saved "
            f"(no panel is dropped on an edit). {reasons}"
        )
    warnings = _save_warnings(kept, returned, current_plan)

    title = plan.get("title") or current_plan.get("title") or rec.get("REPORT_TITLE")
    new_plan = {"title": title, "panels": kept, "parameters": parameters}
    new_version = current_version + 1
    created_by, _role = snowflake_client.session_identity()
    written = snowflake_client.insert_dashboard_version({
        "report_id": report_id,
        "version_id": f"{report_id}_v{new_version}",
        "version": new_version,
        "report_title": title,
        "description": plan.get("description", rec.get("DESCRIPTION")),
        "tags": rec.get("TAGS") or [],
        "dashboard_plan": new_plan,
        "created_by": created_by,
        "edit_note": edit_note or "edited directly",
        "database_name": _infer_database(new_plan),
    })
    # The number actually written: past the highest version, not the current one.
    new_version = written if isinstance(written, int) else new_version

    return {
        "report_id": report_id,
        "report_title": title,
        "version": new_version,
        "version_id": f"{report_id}_v{new_version}",
        "diff": _diff_plans(current_plan, new_plan),
        "saved": True,
        "warnings": warnings,
    }


def _save_warnings(kept: list[dict[str, Any]], returned: dict[int, list[str]],
                   current_plan: dict[str, Any]) -> list[str]:
    """What the saved panels will not draw. Advisory: the save still happens,
    and a failure here must never be what stops it."""
    from app.engine import panel_capabilities

    try:
        previous = [
            _normalise_layout({**{k: p.get(k) for k in _PANEL_CONFIG_FIELDS}, "sql": p.get("sql")})
            for p in current_plan.get("panels") or [] if isinstance(p, dict)
        ]
        return panel_capabilities.save_warnings(kept, returned, previous)
    except Exception:  # noqa: BLE001
        log.warning("could not check the saved panels", exc_info=True)
        return []


def _apply_layout(panels: list[dict[str, Any]],
                  layout: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Apply layout entries to `panels`. Returns (reordered panels, changes).

    Entries are matched BY TITLE, the identity the rest of this module already
    uses for a panel (see _diff_plans). Titles repeat occasionally, so each
    entry is consumed once and matching walks in order — the second "Exposure"
    entry lands on the second "Exposure" panel rather than overwriting the first.

    The ENTRY ORDER becomes the stored panel order. The client sends panels in
    the order it is displaying them, and a panel's position is as much a layout
    decision as its width; keeping the two in step also means a section that was
    only ever laid out by the renderer's packing keeps that arrangement once it
    is saved, instead of springing back to the order the planner emitted.

    A title with no matching panel is ignored rather than an error, and a panel
    no entry mentions keeps its place at the end — a client holding a slightly
    stale dashboard should not have its whole edit rejected, and must not be
    able to drop a panel it did not know about.
    """
    remaining = list(panels)
    ordered: list[dict[str, Any]] = []
    changed = 0

    for entry in layout:
        if not isinstance(entry, dict):
            continue
        panel = next(
            (p for p in remaining if p.get("title") == entry.get("title")), None
        )
        if panel is None:
            continue
        remaining.remove(panel)
        before = (panel.get("section"), panel.get("span"), panel.get("row_span"))
        for field in ("section", "span", "row_span"):
            if field in entry:
                panel[field] = entry[field]
        _normalise_layout(panel)
        if (panel.get("section"), panel.get("span"), panel.get("row_span")) != before:
            changed += 1
        ordered.append(panel)

    ordered.extend(remaining)
    if [p.get("title") for p in ordered] != [p.get("title") for p in panels]:
        changed += 1
    return ordered, changed


def update_layout(report_id: str, layout: list[dict[str, Any]],
                  title: str | None = None) -> dict[str, Any]:
    """Save section/span/row_span for a dashboard's panels as a new version.

    Deliberately NOT routed through update_plan. That function re-validates and
    dry-runs every panel against Snowflake, which is right when SQL might have
    changed and pure cost when it cannot have: this path can only write three
    presentation fields onto panels whose SQL is already stored and already
    validated. It never touches SQL, parameters, or which panels exist.

    Still versioned, so a layout change is revertible like any other edit. That
    is also why the UI saves on an explicit action rather than on every drag —
    one version per save, not one per mouse-up.
    """
    rec = snowflake_client.read_saved_dashboard(report_id)
    if rec is None:
        raise DashboardError(f"No active saved dashboard with report_id '{report_id}'.")

    current_plan = rec.get("DASHBOARD_PLAN") or {}
    panels = [dict(p) for p in (current_plan.get("panels") or [])]
    if not panels:
        raise DashboardError("This dashboard has no panels to lay out.")

    panels, changed = _apply_layout(panels, layout or [])
    renamed = bool(title and title.strip() and title.strip() != current_plan.get("title"))
    if not changed and not renamed:
        return {"report_id": report_id, "saved": False, "reason": "no layout change"}

    new_title = title.strip() if renamed else (
        current_plan.get("title") or rec.get("REPORT_TITLE")
    )
    new_plan = {
        "title": new_title,
        "panels": panels,
        "parameters": current_plan.get("parameters") or [],
    }
    new_version = int(rec.get("VERSION") or 1) + 1
    created_by, _role = snowflake_client.session_identity()
    written = snowflake_client.insert_dashboard_version({
        "report_id": report_id,
        "version_id": f"{report_id}_v{new_version}",
        "version": new_version,
        "report_title": new_title,
        "description": rec.get("DESCRIPTION"),
        "tags": rec.get("TAGS") or [],
        "dashboard_plan": new_plan,
        "created_by": created_by,
        "edit_note": "layout changed",
        "database_name": _infer_database(new_plan),
    })
    # The number actually written: past the highest version, not the current one.
    new_version = written if isinstance(written, int) else new_version
    return {
        "report_id": report_id,
        "report_title": new_title,
        "version": new_version,
        "panels_changed": changed,
        "saved": True,
    }


def delete_dashboard(report_id: str) -> dict[str, Any]:
    """Retire a saved dashboard so it stops appearing anywhere.

    Deliberately a soft delete — see client.deactivate_saved_dashboard. The
    caller (a route) is responsible for restricting this to administrators;
    this layer knows nothing about roles, in keeping with every other engine
    function here.
    """
    title = None
    matches = snowflake_client.resolve_report_ids(report_id)
    for m in matches:
        if str(m.get("report_id")) == report_id:
            title = m.get("report_title")

    retired = snowflake_client.deactivate_saved_dashboard(report_id)
    if not retired:
        raise DashboardError(
            f"No active dashboard with report_id '{report_id}'; nothing deleted."
        )
    return {
        "report_id": report_id,
        "report_title": title,
        "versions_retired": retired,
        "deleted": True,
    }


def revert(report_id: str, version: int) -> dict[str, Any]:
    """Make an existing version current again (pointer change; nothing deleted)."""
    ok = snowflake_client.revert_dashboard_version(report_id, int(version))
    if not ok:
        raise DashboardError(
            f"No version {version} exists for report_id '{report_id}'; nothing reverted.")
    return {
        "report_id": report_id,
        "reverted_to_version": int(version),
        "is_current_version": int(version),
        "reverted": True,
        "webapp_url": f"{WEBAPP_BASE_URL}/dashboards/{report_id}",
    }

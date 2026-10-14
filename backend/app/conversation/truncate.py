"""Tool-result truncation for stored history and model context.

The problem this solves: an agentic transcript carries tool RESULTS, not just
messages. One `run_saved_dashboard` call can return 6 panels x 100 rows. Store
that verbatim on every turn and Redis bloats, the context window fills, and by
a handful of turns the conversation stops fitting at all.

The policy, and the reasoning behind it:

- **Tool CALLS are kept verbatim.** They are small, and they are the record of
  what the assistant actually decided to do — the audit-relevant part.
- **Tool RESULTS are truncated.** The model does not need 100 rows to answer a
  follow-up; it needs the shape, a sample, and an honest note about what was
  cut. The full result already went to the UI over the stream, and anything
  persisted (a saved dashboard) can be re-run on demand.
- **Truncation is explicit, never silent.** A trimmed payload carries a marker
  saying what was removed, so the model never treats a partial result as the
  whole answer and quietly reasons from it.

Truncation happens once, before the result is appended to the conversation, so
what is stored and what is replayed to the model are identical. Storing the
full payload and trimming only on the way out would let the two drift.
"""

from __future__ import annotations

import json
from typing import Any

# Per-result ceiling. Generous enough for a KPI panel or a small aggregate to
# survive intact; small enough that a dozen of them still fit comfortably.
MAX_TOOL_RESULT_CHARS = 4000

# When a result is row-shaped, keep whole rows rather than slicing mid-JSON —
# a truncated row is worse than no row, because it reads as real data.
MAX_ROWS_KEPT = 20



def _truncate_rows(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Trim a {columns, rows, ...} result to MAX_ROWS_KEPT whole rows."""
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) <= MAX_ROWS_KEPT:
        return payload, False

    trimmed = dict(payload)
    trimmed["rows"] = rows[:MAX_ROWS_KEPT]
    trimmed["_truncated"] = (
        f"showing {MAX_ROWS_KEPT} of {len(rows)} rows; "
        "re-run the query if you need the rest"
    )
    return trimmed, True


def _truncate_panels(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Trim every panel's rows inside a dashboard-run result."""
    panels = payload.get("panels")
    if not isinstance(panels, list):
        return payload, False

    any_trimmed = False
    new_panels = []
    for panel in panels:
        if isinstance(panel, dict):
            trimmed_panel, did = _truncate_rows(panel)
            any_trimmed = any_trimmed or did
            new_panels.append(trimmed_panel)
        else:
            new_panels.append(panel)

    if not any_trimmed:
        return payload, False

    trimmed = dict(payload)
    trimmed["panels"] = new_panels
    return trimmed, True


#: What the assistant has to be able to read in a plan or an edit preview, put
#: first so that no amount of shortening further down can reach it.
_PLAN_PRIORITY_KEYS = (
    "status", "needs_clarification", "questions", "plan_id", "report_id",
    "report_title", "panel_count", "skipped_panels", "layout_note",
    "current_version", "new_version", "edit_note", "message", "parameters", "diff",
)

#: A panel reduced to what describes it, for plans too large to show otherwise.
_PANEL_SUMMARY_FIELDS = ("title", "chart_type", "display", "section", "span", "x", "y")


def _without_nulls(panel: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in panel.items() if value is not None}


def _sql_shortened(keep: int):
    def shape(panel: dict[str, Any]) -> dict[str, Any]:
        out = _without_nulls(panel)
        sql = out.get("sql")
        if isinstance(sql, str) and len(sql) > keep:
            out["sql"] = f"{sql[:keep]}… [{len(sql)} chars]" if keep else f"[{len(sql)} chars, omitted]"
        return out
    return shape


def _summarised(panel: dict[str, Any]) -> dict[str, Any]:
    return {key: panel[key] for key in _PANEL_SUMMARY_FIELDS if panel.get(key) is not None}


def _titled(panel: dict[str, Any]) -> Any:
    return panel.get("title")


#: Tried in order until the result fits. Each keeps less of every panel.
_PLAN_SHAPES = (
    ("Panel SQL is shortened", _sql_shortened(200)),
    ("Panel SQL is omitted", _sql_shortened(0)),
    ("Panels are summarised to title, chart type, tab and size", _summarised),
    ("Only panel titles are listed", _titled),
)


def _plan_first(payload: dict[str, Any], plan_key: str, plan: dict[str, Any],
                what_was_shortened: str) -> dict[str, Any]:
    out = {key: payload[key] for key in _PLAN_PRIORITY_KEYS if key in payload}
    out["_truncated"] = (
        f"{what_was_shortened} here to save context. The full plan is held "
        "server-side and saves intact. Every field above the plan, including "
        "panel_count, skipped_panels and layout_note, is complete."
    )
    for key, value in payload.items():
        if key not in out and key != plan_key:
            out[key] = value
    out[plan_key] = plan
    return out


def _truncate_plan(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Shrink a dashboard PLAN, or an edit preview's proposed plan, so the facts
    the assistant acts on always survive.

    What went wrong before: SQL was shortened, and when a plan still did not fit
    — twenty panels do not, even with no SQL at all — the blunt character cut
    removed the END of the payload. That is where panel_count and skipped_panels
    sat, along with a note promising skipped_panels was complete. Unable to see
    whether anything had been dropped, the assistant planned the whole dashboard
    again to find out, paid for it, and hit the same cut. An edit preview lost
    its review instructions the same way, behind a proposed plan carrying full
    SQL.

    So the facts go first and the panels last, and the panels are reduced as far
    as it takes — never cut mid-way. What is saved is unaffected: plans and
    staged edits are held server-side in full.
    """
    plan_key = next(
        (key for key in ("plan", "proposed_plan")
         if isinstance(payload.get(key), dict) and isinstance(payload[key].get("panels"), list)),
        None,
    )
    if plan_key is None or len(json.dumps(payload, default=str)) <= MAX_TOOL_RESULT_CHARS:
        return payload, False

    plan = payload[plan_key]
    panels = plan["panels"]
    for what_was_shortened, shape in _PLAN_SHAPES:
        shaped = {**plan, "panels": [shape(p) if isinstance(p, dict) else p for p in panels]}
        candidate = _plan_first(payload, plan_key, shaped, what_was_shortened)
        if len(json.dumps(candidate, default=str)) <= MAX_TOOL_RESULT_CHARS:
            return candidate, True

    listless = {key: value for key, value in plan.items() if key != "panels"}
    listless["panels_omitted"] = len(panels)
    return _plan_first(payload, plan_key, listless, "The panel list is omitted"), True


def truncate_tool_result(result: Any) -> tuple[str, bool]:
    """Render a tool result as the string the model will see, trimmed if large.

    Returns (content, was_truncated). Structure-aware first (whole rows, whole
    panels, a plan's SQL); a blunt character cut is the last resort for shapes we
    do not recognise, so unknown tools still cannot blow the budget.
    """
    was_truncated = False

    if isinstance(result, dict):
        result, was_truncated = _truncate_plan(result)
        if not was_truncated:
            result, was_truncated = _truncate_panels(result)
        if not was_truncated:
            result, was_truncated = _truncate_rows(result)

    content = result if isinstance(result, str) else json.dumps(result, default=str)

    if len(content) > MAX_TOOL_RESULT_CHARS:
        content = (
            content[:MAX_TOOL_RESULT_CHARS]
            + f"\n…[truncated: {len(content) - MAX_TOOL_RESULT_CHARS} more characters]"
        )
        was_truncated = True

    return content, was_truncated

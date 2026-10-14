"""The assistant inside a dashboard's Edit panel.

Works on the person's unsaved DRAFT, not on the saved dashboard. The draft
arrives with every request and the result goes straight back, so nothing is
cached between calls and any of the four workers can answer. Saving stays where
it was: the Edit panel's "Save and refresh", through update_plan, with all of
its checks.

The checks here are the ones an edit needs before it reaches the screen:
every panel the assistant added, or whose SQL it changed, is dry-run against
the source, and a change that fails is not applied at all. Panels it did not
touch are handed back exactly as they were in the draft, so a hand edit in
progress is never normalised or rewritten underneath the person.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.engine import llm, panel_capabilities, sql_params
from app.engine.dashboard_sections import settle_new_sections, settlement_note
from app.engine.dashboards import (
    _PANEL_CONFIG_FIELDS,
    _normalise_layout,
    _validate_date_bindings,
)
from app.engine.sql_safety import UnsafeSQLError

#: What each panel field is called when a change is described to the person.
_FIELD_NAMES = {
    "title": "title",
    "sql": "query",
    "chart_type": "chart type",
    "x": "columns",
    "y": "columns",
    "color": "colour column",
    "size": "size column",
    "orientation": "orientation",
    "display": "display",
    "section": "tab",
    "span": "width",
    "row_span": "height",
    "column_formats": "column formats",
    "zero_baseline": "axis baseline",
    "palette": "colours",
    "format": "labels and axes",
}

#: Longest message accepted, and longest draft title kept.
MAX_MESSAGE_CHARS = 4000

#: Fields whose change needs the columns the query returns to be checked.
_COLUMN_CHECKED = ("sql", "chart_type", "x", "y", "color", "size", "format")

#: Longest value shown when the draft is compared with the saved version.
_DIFF_VALUE_CHARS = 160


def clean_draft(raw: Any) -> dict[str, Any]:
    """The draft as it may be handed to the model: known panel fields only.

    It arrives from the browser, so anything the renderer does not know is
    dropped rather than passed into a prompt and back into stored JSON.
    """
    raw = raw if isinstance(raw, dict) else {}
    panels = []
    for panel in raw.get("panels") or []:
        if not isinstance(panel, dict):
            continue
        panels.append({
            **{field: panel.get(field) for field in _PANEL_CONFIG_FIELDS},
            "sql": panel.get("sql"),
        })
    parameters = raw.get("parameters")
    return {
        "title": str(raw.get("title") or "").strip()[:200],
        "panels": panels,
        "parameters": parameters if isinstance(parameters, list) else [],
    }


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _describe(draft: dict[str, Any], plan: dict[str, Any], origins: list[int | None],
              changed: list[bool]) -> list[str]:
    """What happened to the draft, one line per panel, naming panels by title."""
    lines: list[str] = []
    if (plan.get("title") or "") != (draft.get("title") or ""):
        lines.append(f'Renamed the dashboard to "{plan.get("title")}"')

    draft_panels = draft.get("panels") or []
    for panel, origin, was_changed in zip(plan["panels"], origins, changed):
        title = panel.get("title") or "Untitled panel"
        if origin is None:
            lines.append(f'Added "{title}"')
            continue
        if not was_changed:
            continue
        before = draft_panels[origin]
        fields: list[str] = []
        for field in _PANEL_CONFIG_FIELDS + ("sql",):
            name = _FIELD_NAMES.get(field, field)
            if not _same(before.get(field), panel.get(field)) and name not in fields:
                fields.append(name)
        if fields:
            lines.append(f'Changed "{before.get("title") or title}": {", ".join(fields)}')

    kept = {origin for origin in origins if origin is not None}
    for index, panel in enumerate(draft_panels):
        if index not in kept:
            lines.append(f'Removed "{panel.get("title") or "Untitled panel"}"')

    order = [origin for origin in origins if origin is not None]
    if order != sorted(order):
        lines.append("Reordered panels")
    if not _same(plan.get("parameters") or [], draft.get("parameters") or []):
        lines.append("Changed the dashboard's parameters")
    return lines


def _shown(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= _DIFF_VALUE_CHARS else text[:_DIFF_VALUE_CHARS] + "..."


def unsaved_changes(saved: dict[str, Any] | None, draft: dict[str, Any]) -> list[str]:
    """How the draft differs from the saved version, one line per difference.

    Panels are matched by title, the identity the rest of the dashboard code
    uses; a renamed panel shows as one removed and one added.
    """
    if not saved:
        return []
    lines: list[str] = []
    if (saved.get("title") or "") != (draft.get("title") or ""):
        lines.append(f'dashboard title: draft {_shown(draft.get("title"))}, '
                     f'saved {_shown(saved.get("title"))}')

    remaining = list(saved.get("panels") or [])
    for index, panel in enumerate(draft.get("panels") or []):
        title = panel.get("title") or "Untitled panel"
        match = next((p for p in remaining
                      if (p.get("title") or "") == (panel.get("title") or "")), None)
        if match is None:
            lines.append(f'[{index}] "{title}": not in the saved version (added in the draft)')
            continue
        remaining.remove(match)
        for field in _PANEL_CONFIG_FIELDS + ("sql",):
            if _same(panel.get(field), match.get(field)):
                continue
            if field == "sql":
                lines.append(f'[{index}] "{title}": query edited, not saved')
            else:
                lines.append(f'[{index}] "{title}": {field} is {_shown(panel.get(field))} '
                             f"in the draft, {_shown(match.get(field))} saved")
    for panel in remaining:
        lines.append(f'"{panel.get("title") or "Untitled panel"}": in the saved version, '
                     "removed from the draft")
    if not _same(saved.get("parameters") or [], draft.get("parameters") or []):
        lines.append("parameters: changed in the draft, not saved")
    return lines


def grounding(draft: dict[str, Any], saved: dict[str, Any] | None = None,
              saved_version: int | None = None) -> str:
    """What the model is told about the draft beyond its JSON: what each panel's
    chart can use, and what is not saved yet."""
    lines = ["WHAT EACH PANEL CAN USE - from its chart type in the draft. A setting not "
             "listed for a panel does nothing on that chart:"]
    for index, panel in enumerate(draft.get("panels") or []):
        lines.append(f'[{index}] "{panel.get("title") or "Untitled panel"}" - '
                     + panel_capabilities.describe_panel(panel))
    if saved is not None:
        name = f"version {saved_version}" if saved_version else "the saved version"
        changes = unsaved_changes(saved, draft)
        lines.append("")
        if changes:
            lines.append(f"NOT SAVED YET - how the draft differs from {name}. The draft is "
                         "what the person sees in the form; the dashboard page still shows "
                         f"{name}:")
            lines += [f"- {line}" for line in changes]
        else:
            lines.append(f"The draft matches {name}; nothing is waiting to be saved.")
    return "\n".join(lines)


def _check_problems(panel: dict[str, Any], before: dict[str, Any] | None,
                    columns: list[str] | None) -> list[str]:
    """What would not show as set, leaving out anything the draft panel already had."""
    found = panel_capabilities.check_panel(panel, columns)
    if before is not None:
        before_columns = columns if before.get("sql") == panel.get("sql") else None
        existing = {p.message for p in panel_capabilities.check_panel(before, before_columns)}
        found = [p for p in found if p.message not in existing]
    return [p.message for p in found]


def _apply(draft: dict[str, Any], data: dict[str, Any],
           dry_run: Callable[[str], Any], database: str | None) -> dict[str, Any]:
    """One model answer applied to the draft and checked."""
    reply = str(data.get("reply") or "").strip()
    plan, origins = llm.apply_assist_operations(draft, data)

    if plan.needs_clarification and not plan.panels:
        return {"status": "needs_clarification",
                "reply": reply or plan.needs_clarification}

    draft_panels = draft["panels"]
    parameters = plan.parameters or []
    parameters_changed = not _same(parameters, draft["parameters"])
    samples = sql_params.sample_values(parameters) if parameters else {}

    panels: list[dict[str, Any]] = []
    changed: list[bool] = []
    problems: list[str] = []
    for proposed, origin in zip(plan.panels, origins):
        before = draft_panels[origin] if origin is not None else None
        if before is not None and not parameters_changed and _same(proposed, before):
            # Untouched: returned as the person left it, not re-normalised.
            panels.append(before)
            changed.append(False)
            continue

        panel = {**{field: proposed.get(field) for field in _PANEL_CONFIG_FIELDS},
                 "sql": proposed.get("sql")}
        if before is not None and panel.get("chart_type") != before.get("chart_type"):
            panel = panel_capabilities.fit_to_chart_type(before, panel)
        title = panel.get("title") or "Untitled panel"
        sql = panel.get("sql")
        sql_changed = before is None or sql != before.get("sql") or parameters_changed
        needs_columns = sql_changed or any(
            not _same(panel.get(field), before.get(field)) for field in _COLUMN_CHECKED)
        columns: list[str] | None = None
        if sql_changed and not sql:
            problems.append(f'"{title}" has no query')
        elif sql and needs_columns:
            try:
                probe = sql
                if sql_params.has_placeholders(sql):
                    probe = sql_params.render_sql(sql, parameters, samples)
                returned = dry_run(probe)
                if isinstance(returned, (list, tuple)):
                    columns = [str(column) for column in returned]
            # A query this change did not touch is not this change's fault, so
            # its failure only costs the column check.
            except sql_params.ParameterError as exc:
                if sql_changed:
                    problems.append(f'"{title}": parameter error: {exc}')
            except UnsafeSQLError as exc:
                if sql_changed:
                    problems.append(f'"{title}": query rejected: {exc}')
            except Exception as exc:  # noqa: BLE001 - the compile error is the message
                if sql_changed:
                    problems.append(f'"{title}": query failed: {exc}')
        problems += [f'"{title}": {message}'
                     for message in _check_problems(panel, before, columns)]
        panels.append(_normalise_layout(panel))
        changed.append(True)

    if problems:
        return {"status": "rejected", "reply": reply, "problems": problems}

    if parameters_changed and database:
        binding = _validate_date_bindings(database, parameters, panels)
        if binding:
            return {"status": "needs_clarification",
                    "reply": "Date parameters must bind to a column the semantic layer "
                             "declares: " + "; ".join(binding)}

    settled = settle_new_sections(draft_panels, panels)
    new_plan = {"title": plan.title or draft["title"], "panels": settled,
                "parameters": parameters}
    changes = _describe(draft, new_plan, origins, changed)
    if not changes:
        # Operations that change nothing: whatever the reply says, the draft
        # already had it.
        attempted = bool(data.get("operations")) or bool(data.get("panels"))
        return {"status": "no_change", "reply": reply, "attempted": attempted}
    return {
        "status": "applied",
        "reply": reply,
        "plan": new_plan,
        "origins": origins,
        "changed": changed,
        "changes": changes,
        "layout_note": settlement_note(panels, settled),
    }


def assist(draft: dict[str, Any], message: str, history: list[dict[str, Any]], *,
           business_context: str, heuristics: str = "", source: str = "snowflake",
           dry_run: Callable[[str], Any], database: str | None = None,
           saved: dict[str, Any] | None = None,
           saved_version: int | None = None) -> dict[str, Any]:
    """Answer one message from the Edit panel.

    Returns {"status", "reply", ...}. status is "applied" (with "plan",
    "origins", "changed", "changes"), "no_change" (advice or a question),
    "needs_clarification", or "rejected" (with "problems"; the draft is left
    alone). A rejected change is sent back to the model once with the reason,
    since a wrong column name is usually fixed on a second look.

    `saved` is the dashboard's saved plan, so the model can tell a setting that
    is only in the draft from one that is saved.

    Raises llm.LLMError when the model cannot be reached or read.
    """
    request = message
    context = grounding(draft, saved, saved_version)
    outcome: dict[str, Any] = {}
    for attempt in (1, 2):
        data = llm.assist_dashboard_edit(draft, request, history, business_context,
                                         heuristics, source=source, grounding=context)
        outcome = _apply(draft, data, dry_run, database)
        if outcome["status"] != "rejected" or attempt == 2:
            break
        request = (
            f"{message}\n\nYour previous answer was not applied because it failed "
            f"validation: {'; '.join(outcome['problems'])}. Correct it using only what "
            "WHAT EACH PANEL CAN USE lists, and answer again. If the request cannot be "
            "met that way, change nothing and say so in the reply."
        )
    return outcome


def turn_text(result: dict[str, Any]) -> str:
    """The assistant's side of a turn, as shown in the panel and stored.

    Stored as written text so a later turn — and the person reopening the
    panel — can see what each earlier message actually changed.
    """
    parts = [result.get("reply") or ""]
    if result.get("status") == "applied":
        parts.append("**Changed in the draft:**\n" + "\n".join(
            f"- {line}" for line in result.get("changes") or []))
        if result.get("layout_note"):
            parts.append(result["layout_note"])
    elif result.get("status") == "no_change" and result.get("attempted"):
        parts.append("**Nothing in the draft changed** — it already had these settings.")
    elif result.get("status") == "rejected":
        parts.append("**Nothing was changed** — the edit failed its checks:\n" + "\n".join(
            f"- {problem}" for problem in result.get("problems") or []))
    return "\n\n".join(part for part in parts if part).strip() or "No change was needed."

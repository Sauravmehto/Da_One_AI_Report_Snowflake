"""Renders a saved dashboard's configuration as a context block.

This is the "looking at the dashboard configuration" half of the dashboard-
scoped chat. It describes what the dashboard IS — its panels, how each is
charted, the SQL behind it, and its runtime parameters — without executing
anything.

The live *numbers* are deliberately not in here. Injecting them would mean
re-running every panel on every turn: slow, billed, and it would inflate the
dashboard's RUN_COUNT each time somebody asked a question. Instead the model is
told the report_id and calls run_saved_dashboard when a question actually needs
figures, which is a run and is honestly counted as one.

SQL is included because most real questions about a dashboard ("what counts as
watch-list here?", "does this include matured deals?") are answered by the
filter in the panel's WHERE clause and by nothing else.
"""

from __future__ import annotations

from typing import Any

# Ceiling on one panel's SQL. A pathological panel must not crowd out the
# other panels' descriptions; the model can re-read the full SQL by running
# the dashboard if it ever needs to.
_MAX_SQL_CHARS = 1400


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n-- …[{len(text) - limit} more characters]"


def _describe_axes(panel: dict[str, Any]) -> str:
    """The chart's encoding, in one line, omitting what is not set."""
    y = panel.get("y")
    y_text = ", ".join(str(v) for v in y) if isinstance(y, list) else y

    parts = [
        f"{label}={value}"
        for label, value in (
            ("x", panel.get("x")),
            ("y", y_text),
            ("color", panel.get("color")),
            ("size", panel.get("size")),
            ("orientation", panel.get("orientation")),
        )
        if value
    ]
    return ", ".join(parts)


def _describe_parameter(param: dict[str, Any]) -> list[str]:
    name = param.get("name") or "?"
    out = [f"- {name} ({param.get('type') or 'string'})"]
    if param.get("default") is not None:
        out.append(f"  default: {param['default']}")
    options = param.get("options")
    if isinstance(options, list) and options:
        out.append(f"  allowed values: {', '.join(str(o) for o in options)}")
    bindings = param.get("bindings")
    if isinstance(bindings, list) and bindings:
        bound = "; ".join(
            f"{b.get('table')} by role {b.get('role')}"
            for b in bindings
            if isinstance(b, dict)
        )
        if bound:
            out.append(f"  binds to: {bound}")
    return out


def build_dashboard_context(config: dict[str, Any]) -> str:
    """Render `dashboards.get_dashboard_config()` output as prompt text."""
    lines: list[str] = [
        f"# Dashboard: {config.get('title') or 'Untitled'}",
        f"report_id: {config.get('report_id')}",
    ]
    if config.get("description"):
        lines.append(str(config["description"]).strip())

    parameters = config.get("parameters") or []
    if parameters:
        lines.append(
            "\n## Runtime parameters\n"
            "Pass these to run_saved_dashboard as parameter_values to see the "
            "dashboard for different inputs. Omitted ones use their default."
        )
        for param in parameters:
            if isinstance(param, dict):
                lines.extend(_describe_parameter(param))
    else:
        lines.append(
            "\n## Runtime parameters\nNone — this dashboard takes no inputs."
        )

    panels = config.get("panels") or []
    lines.append(f"\n## Panels ({len(panels)}), in the order they appear on screen")
    for index, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            continue
        lines.append(f"\n### {index}. {panel.get('title') or 'Untitled panel'}")

        rendering = panel.get("display") or "chart"
        chart_type = panel.get("chart_type")
        lines.append(
            f"Rendered as: {rendering}"
            + (f" ({chart_type})" if chart_type else "")
        )

        axes = _describe_axes(panel)
        if axes:
            lines.append(f"Encoding: {axes}")

        if panel.get("sql"):
            lines.append("SQL:")
            lines.append(_clip(str(panel["sql"]), _MAX_SQL_CHARS))

    return "\n".join(lines)

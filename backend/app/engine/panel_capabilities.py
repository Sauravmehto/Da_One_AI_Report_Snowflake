"""What each chart type reads, and whether a panel's settings will show.

The Edit panel decides which inputs to offer per chart type from its own
lists (frontend/src/components/dashboard/dashboard-editor.tsx: CHART_FIELDS,
FITTABLE_AXIS, PALETTED, AXIS_TITLED, LABELLED, NAMED_POINTS, BAR_POSITIONED,
MARKED, and panelSeriesShapes / dataLabelsFor in lib/chart-standard.ts). This
is the backend copy of those lists, kept equal to them by
tests/engine/test_panel_capabilities.py.

It is used in two ways. `describe_panel` tells the edit assistant what a panel
in the draft can use, so it works from the chart that is there rather than
from what it remembers. `check_panel` finds a setting that would do nothing
once drawn: a column the query does not return, a value column a chart
ignores, a setting the chart type has no use for, a value no renderer accepts,
or a setting that depends on another one that is off.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from app.engine.dashboards import (
    FORMAT_CHOICES,
    FORMAT_REFERENCE_KINDS,
    PALETTE_NAMES,
    _normalise_format,
)

#: Every chart type a person can pick in the editor.
CHART_TYPES = (
    "bar", "stacked_bar", "grouped_bar", "line", "area", "pie", "donut",
    "scatter", "bubble", "heatmap", "radar", "treemap", "funnel", "combo",
    "area_line", "geo_us_states", "geo_countries", "kpi",
)

#: Optional columns and settings beyond x and y, by chart type.
CHART_FIELDS: dict[str, tuple[str, ...]] = {
    "bar": ("color", "orientation"),
    "grouped_bar": ("color", "orientation"),
    "stacked_bar": ("orientation",),
    "line": ("color",),
    "area": ("color",),
    "scatter": ("color", "size"),
    "bubble": ("color", "size"),
    "heatmap": ("color",),
}

#: Drawn against a value axis that can be lifted off zero.
FITTABLE_AXIS = frozenset({
    "bar", "grouped_bar", "line", "area", "scatter", "bubble", "combo", "radar",
    "area_line",
})

#: Draw distinct coloured series, so a palette applies.
PALETTED = frozenset({
    "bar", "grouped_bar", "stacked_bar", "line", "area", "pie", "donut",
    "doughnut", "scatter", "bubble", "radar", "treemap", "funnel", "combo",
    "area_line",
})

#: Titled axes, a number unit and a legend.
AXIS_TITLED = frozenset({
    "bar", "grouped_bar", "stacked_bar", "line", "area", "scatter", "bubble",
    "combo", "area_line",
})

#: Write values, or names, beside bars, points or places.
LABELLED = frozenset({
    "bar", "grouped_bar", "stacked_bar", "line", "area", "combo", "area_line",
    "scatter", "bubble", "geo_countries", "geo_us_states",
})

#: Labels name the point rather than give its value; axis bounds, reference
#: lines and a trend line apply.
NAMED_POINTS = frozenset({"scatter", "bubble"})

#: Bars whose value label can sit inside or outside their end.
BAR_POSITIONED = frozenset({"bar", "grouped_bar", "combo"})

#: A line whose points can be marked.
MARKED = frozenset({"line", "area", "combo", "area_line"})

#: Charts that draw ONE value column; any further y columns are ignored.
SINGLE_VALUE = frozenset({
    "pie", "donut", "treemap", "funnel", "geo_us_states", "geo_countries",
    "scatter", "bubble",
})

#: Charts that need at least this many value columns to draw what they are.
MIN_VALUES = {"combo": 2, "area_line": 2}

#: Values the orientation field accepts ("v" is an older spelling of automatic).
ORIENTATIONS = frozenset({"v", "vertical", "h", "horizontal"})

#: Panel fields that name a column of the query.
COLUMN_FIELDS = ("x", "y", "color", "size")


class Problem(NamedTuple):
    #: "column", "shape", "setting", "value" or "dependency".
    kind: str
    message: str


def _values(y: Any) -> list[str]:
    if isinstance(y, list):
        return [str(v) for v in y if v]
    return [str(y)] if y else []


def _chart(panel: dict[str, Any]) -> str:
    return str(panel.get("chart_type") or "")


def _draws_chart(chart_type: str) -> bool:
    return bool(chart_type) and chart_type not in ("table", "kpi")


def series_shapes(chart_type: str, y: Any, color: Any) -> list[dict[str, Any]]:
    """The series a panel draws from its value columns (panelSeriesShapes).

    Empty when a colour column splits the value instead, or there is one value.
    """
    keys = _values(y)
    if color or len(keys) < 2:
        return []
    last = len(keys) - 1
    if chart_type in ("bar", "grouped_bar"):
        return [{"key": k, "kind": "bar"} for k in keys]
    if chart_type == "stacked_bar":
        return [{"key": k, "kind": "segment"} for k in keys]
    if chart_type in ("line", "area"):
        return [{"key": k, "kind": "line"} for k in keys]
    if chart_type == "combo":
        return [{"key": k, "kind": "line" if i == last else "bar"} for i, k in enumerate(keys)]
    if chart_type == "area_line":
        return [{"key": k, "kind": "line", "reference": i == last} for i, k in enumerate(keys)]
    return []


def default_data_labels(chart_type: str) -> str:
    """What data_labels means when unset (dataLabelsFor). Scatter and bubble
    name every point up to 15 and the extremes beyond; "all" is the usual case."""
    if chart_type in ("geo_countries", "geo_us_states"):
        return "extremes"
    if chart_type in NAMED_POINTS:
        return "all"
    if chart_type in ("line", "area", "area_line"):
        return "max"
    return "none"


def panel_fields(chart_type: str) -> list[str]:
    """Panel fields beyond title, sql and layout that this chart type reads."""
    fields = list(CHART_FIELDS.get(chart_type, ()))
    if chart_type in FITTABLE_AXIS:
        fields.append("zero_baseline")
    if chart_type in PALETTED:
        fields.append("palette")
    return fields


def format_keys(chart_type: str, y: Any = None, color: Any = None) -> list[str]:
    """The `format` keys this chart type reads (formatForType)."""
    keys: list[str] = []
    if chart_type in AXIS_TITLED:
        keys += ["x_title", "y_title", "unit", "legend"]
    if chart_type in LABELLED:
        keys.append("data_labels")
    if chart_type in BAR_POSITIONED:
        keys.append("label_position")
    if chart_type in MARKED:
        keys.append("markers")
    if chart_type == "stacked_bar":
        keys.append("stack_totals")
    if chart_type == "funnel":
        keys.append("funnel_percent")
    if chart_type in NAMED_POINTS:
        keys += ["x_min", "x_max", "y_min", "y_max", "x_reference", "y_reference",
                 "trend_line"]
    if series_shapes(chart_type, y, color):
        keys.append("series")
    return keys


def series_keys(shape: dict[str, Any]) -> list[str]:
    """The settings one series can override."""
    keys = ["data_labels"]
    if shape["kind"] == "bar":
        keys.append("label_position")
    if shape["kind"] == "line":
        keys.append("markers")
    return keys


def _shown_labels(chart_type: str, fmt: dict[str, Any], key: str | None,
                  reference: bool = False) -> str:
    """The labels a series actually shows (seriesLabels)."""
    own = ((fmt.get("series") or {}).get(key) or {}).get("data_labels") if key else None
    if isinstance(own, str) and own:
        return own
    if reference:
        return "none"
    chart_level = fmt.get("data_labels")
    return chart_level if isinstance(chart_level, str) and chart_level else default_data_labels(chart_type)


def describe_panel(panel: dict[str, Any]) -> str:
    """One line on what a panel can use and what its unset settings mean."""
    chart_type = _chart(panel)
    if not chart_type or chart_type == "table":
        return "table: shows the query's rows; no chart settings apply"
    if chart_type == "kpi":
        return ("kpi: tiles read the query's METRIC, VALUE and FORMAT columns; "
                "no chart settings apply")
    if chart_type not in CHART_TYPES:
        return f"{chart_type}: not a chart type this app draws; it shows as a table"

    fmt = panel.get("format") if isinstance(panel.get("format"), dict) else {}
    y, color = panel.get("y"), panel.get("color")
    columns = ["x", "y"] + [f for f in CHART_FIELDS.get(chart_type, ()) if f in ("color", "size")]
    parts = [f"{chart_type}: columns {', '.join(columns)}"]
    if chart_type in SINGLE_VALUE:
        parts.append("draws only the first y column")
    if chart_type in MIN_VALUES:
        parts.append(f"needs at least {MIN_VALUES[chart_type]} y columns, the last drawn as the line")

    fields = [f for f in panel_fields(chart_type) if f not in ("color", "size")]
    if fields:
        parts.append("fields " + ", ".join(fields))

    notes = []
    for key in format_keys(chart_type, y, color):
        if key == "data_labels":
            shown = fmt.get("data_labels") or f"unset, which means {default_data_labels(chart_type)}"
            notes.append(f"data_labels ({shown})")
        elif key == "label_position":
            notes.append("label_position (moves labels data_labels shows; nothing to move when none)")
        elif key == "series":
            shapes = series_shapes(chart_type, y, color)
            notes.append("series per value column: " + "; ".join(
                f"{s['key']}: {', '.join(series_keys(s))}"
                + (" (target line, unlabelled unless set)" if s.get("reference") else "")
                for s in shapes))
        elif key == "legend":
            notes.append("legend (unset: hidden for one series, bottom for several)")
        else:
            notes.append(key)
    if notes:
        parts.append("format " + ", ".join(notes))
    if chart_type in FITTABLE_AXIS and panel.get("zero_baseline") is not False:
        parts.append("value axis starts at zero")
    return "; ".join(parts)


def fit_to_chart_type(before: dict[str, Any], panel: dict[str, Any]) -> dict[str, Any]:
    """A panel whose chart type changed, without the settings it carried over
    from the old type that the new type ignores.

    The same clean-up the editor's chart-type select does, applied only to
    values left as they were: a setting chosen for the new chart is kept, and
    check_panel reports it if it does not apply.
    """
    chart_type = _chart(panel)
    out = dict(panel)
    for field in ("color", "size", "orientation", "zero_baseline", "palette"):
        carried = out.get(field) == before.get(field)
        if carried and out.get(field) is not None and field not in panel_fields(chart_type):
            out[field] = None

    fmt, old = out.get("format"), before.get("format")
    if isinstance(fmt, dict) and isinstance(old, dict):
        allowed = set(format_keys(chart_type, out.get("y"), out.get("color")))
        kept = {key: value for key, value in fmt.items()
                if key in allowed or old.get(key) != value}
        out["format"] = kept or None
    return out


def _column_problems(panel: dict[str, Any], columns: list[str]) -> list[Problem]:
    chart_type = _chart(panel)
    by_upper = {str(c).upper(): str(c) for c in columns}
    problems: list[Problem] = []
    returned = ", ".join(str(c) for c in columns)

    def check(field: str, name: str) -> None:
        if name in columns:
            return
        actual = by_upper.get(name.upper())
        if actual is not None:
            problems.append(Problem(
                "column", f'{field} "{name}" must be written exactly as the query returns it: "{actual}"'))
        else:
            problems.append(Problem(
                "column", f'{field} "{name}" is not a column the query returns ({returned})'))

    if not panel.get("x"):
        problems.append(Problem("column", f"{chart_type} needs an x column"))
    else:
        check("x", str(panel["x"]))
    values = _values(panel.get("y"))
    if not values:
        problems.append(Problem("column", f"{chart_type} needs a y column"))
    for name in values:
        check("y", name)
    for field in ("color", "size"):
        if panel.get(field) and field in CHART_FIELDS.get(chart_type, ()):
            check(field, str(panel[field]))
    return problems


def _value_problems(panel: dict[str, Any]) -> list[Problem]:
    """Values the renderer would not accept, which saving would silently drop."""
    problems: list[Problem] = []
    orientation = panel.get("orientation")
    if orientation is not None and orientation not in ORIENTATIONS:
        problems.append(Problem("value", f'orientation "{orientation}" is not accepted; '
                                         'use "h", "vertical" or null'))
    palette = panel.get("palette")
    if palette is not None and (not isinstance(palette, str)
                                or palette.strip().lower() not in PALETTE_NAMES):
        problems.append(Problem("value", f'palette "{palette}" is not accepted; use one of '
                                         + ", ".join(sorted(PALETTE_NAMES)) + " or null"))
    zero = panel.get("zero_baseline")
    if zero is not None and not isinstance(zero, bool):
        problems.append(Problem("value", "zero_baseline must be false or null"))

    raw = panel.get("format")
    if raw is None:
        return problems
    if not isinstance(raw, dict):
        return problems + [Problem("value", "format must be an object")]
    kept = _normalise_format(raw) or {}
    for key, value in raw.items():
        if value is None or key in ("x_title", "y_title") and isinstance(value, str):
            continue
        if key == "series" and isinstance(value, dict):
            kept_series = kept.get("series") or {}
            for column, settings in value.items():
                if not isinstance(settings, dict):
                    problems.append(Problem("value", f'format.series "{column}" must be an object'))
                    continue
                for setting, choice in settings.items():
                    if choice is None:
                        continue
                    if (kept_series.get(str(column).strip()) or {}).get(setting) != choice:
                        problems.append(Problem("value", _choice_message(f"format.series.{setting}", setting, choice)))
            continue
        if key not in kept or kept[key] != value:
            problems.append(Problem("value", _choice_message(f"format.{key}", key, value)))
    return problems


def _choice_message(label: str, key: str, value: Any) -> str:
    if key in FORMAT_CHOICES:
        return (f'{label} "{value}" is not accepted; use one of '
                + ", ".join(sorted(FORMAT_CHOICES[key])))
    if key in ("stack_totals", "trend_line"):
        return f"{label} must be true or false"
    if key in ("x_min", "x_max", "y_min", "y_max"):
        return f"{label} must be a number, below its max"
    if key in ("x_reference", "y_reference"):
        return (f"{label} must be one of "
                + ", ".join(f'{{"kind": "{k}"}}' for k in sorted(FORMAT_REFERENCE_KINDS - {"value"}))
                + ' or {"kind": "value", "value": <number>}')
    if key in ("x_title", "y_title"):
        return f"{label} must be text"
    return f'{label} is not a setting; format has no key "{key}"'


def check_panel(panel: dict[str, Any], columns: list[str] | None = None) -> list[Problem]:
    """Everything about a panel that would not show as set once it is drawn.

    `columns` are the names the panel's query returns; None skips the column
    checks. An empty list means nothing to report.
    """
    chart_type = _chart(panel)
    problems = _value_problems(panel)
    if chart_type and chart_type not in CHART_TYPES and chart_type != "table":
        return problems + [Problem("value", f'chart_type "{chart_type}" is not one this app '
                                            "draws; use one of " + ", ".join(CHART_TYPES))]
    if not _draws_chart(chart_type):
        return problems

    y, color = panel.get("y"), panel.get("color")
    values = _values(y)
    if columns is not None:
        problems += _column_problems(panel, [str(c) for c in columns])

    if chart_type in SINGLE_VALUE and len(values) > 1:
        problems.append(Problem("shape", f"{chart_type} draws one value column; "
                                         f"y lists {len(values)} and all but the first are ignored"))
    if chart_type in MIN_VALUES and len(values) < MIN_VALUES[chart_type]:
        problems.append(Problem("shape", f"{chart_type} needs at least {MIN_VALUES[chart_type]} "
                                         "y columns, the last drawn as the line"))
    if chart_type in ("bar", "grouped_bar", "line", "area") and color and len(values) > 1:
        problems.append(Problem("shape", "a color column splits ONE value into series; "
                                         "with several y columns only the first is drawn"))

    fields = panel_fields(chart_type)
    for field, meaning in (("color", "a color column"), ("size", "a size column"),
                           ("orientation", "orientation"), ("palette", "a palette")):
        if panel.get(field) not in (None, "") and field not in fields:
            problems.append(Problem("setting", f"{chart_type} has no use for {meaning}; set {field} to null"))
    if panel.get("zero_baseline") is False and "zero_baseline" not in fields:
        problems.append(Problem("setting", f"{chart_type} always starts at zero; set zero_baseline to null"))

    fmt = panel.get("format") if isinstance(panel.get("format"), dict) else {}
    allowed = set(format_keys(chart_type, y, color))
    for key, value in fmt.items():
        if value is None or key not in _KNOWN_FORMAT_KEYS:
            continue
        if key not in allowed:
            reason = ("series settings need two or more y columns and no color column"
                      if key == "series" and chart_type in LABELLED
                      else f"{chart_type} does not use it")
            problems.append(Problem("setting", f"format.{key} does nothing here: {reason}"))

    shapes = {s["key"]: s for s in series_shapes(chart_type, y, color)}
    if "series" in allowed and isinstance(fmt.get("series"), dict):
        for column, settings in fmt["series"].items():
            shape = shapes.get(column)
            if shape is None:
                problems.append(Problem("setting", f'format.series "{column}" is not one of the '
                                                   f"value columns ({', '.join(shapes)})"))
                continue
            if isinstance(settings, dict):
                for setting, choice in settings.items():
                    if choice is not None and setting not in series_keys(shape):
                        problems.append(Problem("setting", f'format.series "{column}" cannot set '
                                                           f"{setting}; it accepts {', '.join(series_keys(shape))}"))

    problems += _dependency_problems(chart_type, fmt, shapes)
    return problems


_KNOWN_FORMAT_KEYS = frozenset({
    "x_title", "y_title", "unit", "legend", "data_labels", "label_position", "markers",
    "stack_totals", "funnel_percent", "x_min", "x_max", "y_min", "y_max",
    "x_reference", "y_reference", "trend_line", "series",
})


def _dependency_problems(chart_type: str, fmt: dict[str, Any],
                         shapes: dict[str, dict[str, Any]]) -> list[Problem]:
    """A label position with no labels to position."""
    if chart_type not in BAR_POSITIONED:
        return []
    problems: list[Problem] = []
    position = fmt.get("label_position")
    series = fmt.get("series") if isinstance(fmt.get("series"), dict) else {}
    bars = [s for s in shapes.values() if s["kind"] == "bar"]
    if isinstance(position, str) and position != "auto":
        if bars:
            shown = any(_shown_labels(chart_type, fmt, s["key"]) != "none" for s in bars)
        else:
            shown = _shown_labels(chart_type, fmt, None) != "none"
        if not shown:
            problems.append(Problem(
                "dependency",
                "format.label_position only moves value labels, and this chart shows none "
                f"(data_labels is {fmt.get('data_labels') or 'unset, which means none'}); "
                "set data_labels as well"))
    for s in bars:
        own = (series.get(s["key"]) or {}) if isinstance(series.get(s["key"]), dict) else {}
        if own.get("label_position") not in (None, "auto") \
                and _shown_labels(chart_type, fmt, s["key"]) == "none":
            problems.append(Problem(
                "dependency",
                f'format.series "{s["key"]}" sets label_position but shows no labels; '
                "set its data_labels as well"))
    return problems


def save_warnings(panels: list[dict[str, Any]], columns: dict[int, list[str]],
                  previous: list[dict[str, Any]] | None = None) -> list[str]:
    """What a saved edit will not draw, for panels the edit changed.

    Only the problems that leave a chart unable to draw what it names: a
    column the query does not return, and value columns the chart ignores or
    lacks. `columns` holds each panel's returned names by position. Panels
    identical to one in `previous` are left out, so a save of one change does
    not report on every older panel.
    """
    import json

    def key(panel: dict[str, Any]) -> str:
        return json.dumps(panel, sort_keys=True, default=str)

    unchanged = {key(panel) for panel in previous or []}
    warnings: list[str] = []
    for index, panel in enumerate(panels):
        if key(panel) in unchanged:
            continue
        title = panel.get("title") or f"Panel {index + 1}"
        for problem in check_panel(panel, columns.get(index)):
            if problem.kind in ("column", "shape"):
                warnings.append(f"'{title}': {problem.message}")
    return warnings

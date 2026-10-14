"""panel_capabilities is a copy of the Edit panel's per-chart lists; they must agree.

The editor decides from its lists which inputs a chart type offers, and the
assistant is told and checked from this copy. If the two drift, the assistant
is refused a setting the form offers, or allowed one the chart ignores.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.engine import panel_capabilities as caps

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend" / "src"


def _source(path: str) -> str:
    text = (FRONTEND / path).read_text(encoding="utf-8")
    # Comments quote chart types in passing ("aliased to \"bar\""); drop them.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


EDITOR = "components/dashboard/dashboard-editor.tsx"
STANDARD = "lib/chart-standard.ts"


def _block(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


@pytest.mark.parametrize("name", ["FITTABLE_AXIS", "PALETTED", "AXIS_TITLED", "LABELLED",
                                  "NAMED_POINTS", "BAR_POSITIONED", "MARKED"])
def test_chart_type_sets_match_the_editor(name):
    block = _block(_source(EDITOR), f"const {name} = new Set([", "])")
    assert set(re.findall(r'"([a-z_]+)"', block)) == set(getattr(caps, name)), name


def test_chart_types_match_the_editor():
    block = _block(_source(EDITOR), "const CHART_TYPES = [", "]")
    assert re.findall(r'"([a-z_]+)"', block) == list(caps.CHART_TYPES)


def test_chart_fields_match_the_editor():
    block = _block(_source(EDITOR), "const CHART_FIELDS", "\n}")
    found = {
        chart: tuple(re.findall(r'"([a-z]+)"', fields))
        for chart, fields in re.findall(r"(\w+): \[([^\]]*)\]", block)
    }
    assert found == caps.CHART_FIELDS


def test_format_keys_per_chart_match_the_editors_type_change():
    """formatForType keeps, per condition, the format keys that type reads."""
    body = _block(_source(EDITOR), "function formatForType(", "\n}\n")
    kept: dict[str, set[str]] = {}
    for chunk in body.split("\n  if (")[1:]:
        condition = re.match(r'(\w+)\.has\(chartType\)|chartType === "(\w+)"|format\.(series)',
                             chunk)
        if condition is None:
            continue
        name = next(group for group in condition.groups() if group)
        kept[name] = set(re.findall(r"format\.(\w+)", chunk)) | set(
            re.findall(r'"(x_min|x_max|y_min|y_max)"', chunk))

    assert kept == {
        "AXIS_TITLED": {"x_title", "y_title", "unit", "legend"},
        "LABELLED": {"data_labels"},
        "BAR_POSITIONED": {"label_position"},
        "MARKED": {"markers"},
        "stacked_bar": {"stack_totals"},
        "funnel": {"funnel_percent"},
        "NAMED_POINTS": {"x_min", "x_max", "y_min", "y_max", "x_reference", "y_reference",
                         "trend_line"},
        "series": {"series"},
    }

    # And this module gives each chart type exactly those keys.
    assert set(caps.format_keys("bar")) == {"x_title", "y_title", "unit", "legend",
                                            "data_labels", "label_position"}
    assert set(caps.format_keys("stacked_bar", ["A", "B"])) == {
        "x_title", "y_title", "unit", "legend", "data_labels", "stack_totals", "series"}
    assert "funnel_percent" in caps.format_keys("funnel")


def test_default_labels_match_the_chart_standard():
    body = _block(_source(STANDARD), "export function dataLabelsFor(", "\n}\n")
    defaults: dict[str, str] = {}
    pending: list[str] = []
    for line in body.splitlines():
        case = re.search(r'case "([a-z_]+)":', line)
        if case:
            pending.append(case.group(1))
            continue
        returned = re.search(r'return (?:.*\? )?"([a-z]+)"', line)
        if returned and pending:
            for chart in pending:
                defaults[chart] = returned.group(1)
            pending = []
    assert defaults, "could not read dataLabelsFor"
    for chart, label in defaults.items():
        assert caps.default_data_labels(chart) == label, chart
    assert caps.default_data_labels("bar") == "none"
    assert "return \"none\"" in body.split("default:")[1]


def test_series_shapes_match_the_chart_standard():
    body = _block(_source(STANDARD), "export function panelSeriesShapes(", "\n}\n")
    groups = re.findall(r'((?:case "[a-z_]+":\s*)+)return keys\.map\(\((.*?)\n', body)
    assert groups, "could not read panelSeriesShapes"
    for cases, mapping in groups:
        for chart in re.findall(r'case "([a-z_]+)"', cases):
            kinds = {s["kind"] for s in caps.series_shapes(chart, ["A", "B"], None)}
            written = set(re.findall(r'"(bar|line|segment)"', mapping))
            assert kinds == written, chart


# --- what the checks report ------------------------------------------------------


def test_a_correct_panel_has_no_problems():
    panel = {"chart_type": "bar", "x": "MONTH", "y": "TOTAL", "orientation": "h",
             "format": {"data_labels": "all", "label_position": "outside_end"}}
    assert caps.check_panel(panel, ["MONTH", "TOTAL"]) == []


def test_a_label_position_needs_labels_to_move():
    panel = {"chart_type": "bar", "x": "M", "y": "T", "format": {"label_position": "inside_end"}}
    assert [p.kind for p in caps.check_panel(panel)] == ["dependency"]
    # A line labels its highest point unless told otherwise, but has no bars.
    line = {"chart_type": "line", "x": "M", "y": "T", "format": {"label_position": "inside_end"}}
    assert [p.kind for p in caps.check_panel(line)] == ["setting"]


def test_per_series_labels_count_for_a_label_position():
    panel = {"chart_type": "bar", "x": "M", "y": ["A", "B"],
             "format": {"label_position": "outside_end",
                        "series": {"A": {"data_labels": "all"}}}}
    assert caps.check_panel(panel) == []


def test_series_settings_must_suit_the_series():
    panel = {"chart_type": "combo", "x": "M", "y": ["A", "B"],
             "format": {"series": {"A": {"markers": "show"}, "C": {"data_labels": "all"}}}}
    messages = [p.message for p in caps.check_panel(panel)]
    assert any('"A" cannot set markers' in m for m in messages)
    assert any('"C" is not one of the value columns' in m for m in messages)


def test_column_names_must_match_what_the_query_returns():
    panel = {"chart_type": "line", "x": "month", "y": ["TOTAL", "GONE"]}
    messages = [p.message for p in caps.check_panel(panel, ["MONTH", "TOTAL"])]
    assert messages == [
        'x "month" must be written exactly as the query returns it: "MONTH"',
        'y "GONE" is not a column the query returns (MONTH, TOTAL)',
    ]


def test_chart_shapes():
    assert [p.kind for p in caps.check_panel({"chart_type": "pie", "x": "S", "y": ["A", "B"]})] \
        == ["shape"]
    assert [p.kind for p in caps.check_panel({"chart_type": "combo", "x": "S", "y": "A"})] \
        == ["shape"]
    assert [p.kind for p in caps.check_panel(
        {"chart_type": "bar", "x": "S", "y": ["A", "B"], "color": "R"})] == ["shape"]
    # A heatmap reads several value columns as a wide matrix.
    assert caps.check_panel({"chart_type": "heatmap", "x": "S", "y": ["A", "B"]}) == []


def test_tables_and_kpis_are_not_checked_as_charts():
    assert caps.check_panel({"chart_type": None, "x": None, "y": None}, ["A"]) == []
    assert caps.check_panel({"chart_type": "kpi"}, ["METRIC", "VALUE"]) == []


def test_changing_type_keeps_what_was_chosen_for_the_new_chart():
    before = {"chart_type": "bar", "orientation": "h", "format": {"label_position": "inside_end"}}
    after = {"chart_type": "line", "orientation": "h",
             "format": {"label_position": "inside_end", "markers": "show"}}
    assert caps.fit_to_chart_type(before, after) == {
        "chart_type": "line", "orientation": None, "format": {"markers": "show"}}


def test_save_warnings_cover_changed_panels_only():
    broken = {"title": "Old", "chart_type": "bar", "x": "GONE", "y": "T"}
    changed = {"title": "New", "chart_type": "combo", "x": "M", "y": "T"}
    warnings = caps.save_warnings([broken, changed], {0: ["M", "T"], 1: ["M", "T"]},
                                  previous=[broken])
    assert warnings == ["'New': combo needs at least 2 y columns, the last drawn as the line"]

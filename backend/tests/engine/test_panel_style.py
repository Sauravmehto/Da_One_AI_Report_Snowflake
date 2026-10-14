"""The per-panel presentation options: palette and the zero-baseline opt-out.

Both were added as ABSENT-BY-DEFAULT fields so that every dashboard saved
before they existed keeps rendering exactly as it did. The tests below pin that
promise down, and pin the palette names together across the THREE places they
are now declared — the same drift the chart-type vocabulary tests exist for.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.engine import pdf_charts
from app.engine.dashboards import PALETTE_NAMES, _normalise_style, _panel_config

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend" / "src"


# --- the three palette lists must agree -------------------------------------

def frontend_palettes() -> set[str]:
    text = (FRONTEND / "lib" / "dashboard-format.ts").read_text(encoding="utf-8")
    block = re.search(
        r"export const PALETTES: Record<string, readonly string\[\]> = \{(.*?)\n\}",
        text,
        re.S,
    )
    assert block, "could not find PALETTES in dashboard-format.ts"
    return set(re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M))


def test_every_palette_is_declared_in_all_three_places():
    """A name the backend accepts but a renderer has no colours for silently
    falls back to the defaults, which looks like the setting being ignored."""
    assert frontend_palettes() == set(PALETTE_NAMES) == set(pdf_charts.PALETTES)


def test_each_palette_has_a_colour_for_every_default_series():
    """Fewer colours than the default palette means a chart that was legible
    with 8 series starts repeating a colour when a palette is chosen."""
    for name, colours in pdf_charts.PALETTES.items():
        assert len(colours) == len(pdf_charts.SERIES_COLORS), name


def test_the_editor_offers_every_palette_the_renderer_knows():
    text = (FRONTEND / "lib" / "dashboard-format.ts").read_text(encoding="utf-8")
    block = re.search(r"export const PALETTE_OPTIONS = \[(.*?)\] as const", text, re.S)
    assert block, "could not find PALETTE_OPTIONS"
    offered = set(re.findall(r'value: "([a-z_]*)"', block.group(1)))
    # "" is the default entry, which is the absence of a palette.
    assert offered - {""} == set(PALETTE_NAMES)


# --- defaults must not change what already exists ---------------------------

def test_a_panel_that_never_mentions_them_stays_absent():
    config = _panel_config({"title": "Untouched", "chart_type": "bar"})
    assert config["zero_baseline"] is None
    assert config["palette"] is None


def test_both_fields_survive_a_save():
    """They are only stored if _PANEL_CONFIG_FIELDS names them — a field the
    allowlist omits is dropped without a word."""
    config = _panel_config(
        {"title": "T", "chart_type": "line", "zero_baseline": False, "palette": "ocean"}
    )
    assert config["zero_baseline"] is False
    assert config["palette"] == "ocean"


def test_only_false_is_stored_for_the_baseline():
    """True is what absent already means, so storing it would be a second
    spelling of the default."""
    panel = {"zero_baseline": True}
    _normalise_style(panel)
    assert panel["zero_baseline"] is None


def test_an_unknown_palette_is_dropped():
    panel = {"palette": "neon-pink"}
    _normalise_style(panel)
    assert panel["palette"] is None


def test_a_palette_name_is_matched_case_insensitively():
    panel = {"palette": "  Ocean "}
    _normalise_style(panel)
    assert panel["palette"] == "ocean"


# --- the PDF renderer honours both ------------------------------------------

def test_pdf_series_colour_follows_the_chosen_palette():
    token = pdf_charts._panel_style.set((True, "ember"))
    try:
        assert pdf_charts.series_color(0) == pdf_charts.PALETTES["ember"][0]
    finally:
        pdf_charts._panel_style.reset(token)
    # ...and reverts once the panel is done.
    assert pdf_charts.series_color(0) == pdf_charts.SERIES_COLORS[0]


def test_pdf_value_range_anchors_at_zero_by_default():
    data = pdf_charts.ChartData(
        categories=["a", "b"], series=[("s", [95.0, 100.0])]
    )
    lo, hi = pdf_charts._value_range(data, stacked=False)
    assert lo == 0.0


def test_pdf_value_range_fits_the_data_when_the_panel_opts_out():
    data = pdf_charts.ChartData(
        categories=["a", "b"], series=[("s", [95.0, 100.0])]
    )
    token = pdf_charts._panel_style.set((False, None))
    try:
        lo, hi = pdf_charts._value_range(data, stacked=False)
    finally:
        pdf_charts._panel_style.reset(token)
    assert lo > 90.0, "a fitted axis must not reach back to zero"
    assert lo < 95.0 < 100.0 < hi, "the data must sit inside the padded range"


def test_a_stacked_bar_keeps_its_zero_baseline_even_when_opted_out():
    """Its segments only sum visually because the stack starts at zero."""
    data = pdf_charts.ChartData(
        categories=["a"], series=[("s1", [60.0]), ("s2", [40.0])]
    )
    token = pdf_charts._panel_style.set((False, None))
    try:
        lo, _ = pdf_charts._value_range(data, stacked=True)
    finally:
        pdf_charts._panel_style.reset(token)
    assert lo == 0.0


# --- bar direction: "v" must keep meaning "auto" ----------------------------
#
# The planner has emitted "v" for a long time and saved panels carry it. Giving
# it the new "stay upright" meaning would silently re-draw those charts, so the
# lock got its own spelling instead. These pin that down in the renderer, which
# is the only place the distinction exists.

def renderer_source() -> str:
    return (FRONTEND / "components" / "dashboard" / "dashboard-panel.tsx").read_text(
        encoding="utf-8"
    )


def test_only_the_explicit_spelling_locks_the_direction():
    src = renderer_source()
    assert 'const lockVertical = orientation === "vertical"' in src, (
        'the vertical lock must test for "vertical" alone — including "v" would '
        "change how 35 already-saved panels are drawn"
    )


def test_horizontal_still_wins_for_both_spellings():
    src = renderer_source()
    assert 'orientation === "h" || orientation === "horizontal"' in src


def test_the_editor_writes_the_locking_spelling_not_the_legacy_one():
    src = (FRONTEND / "components" / "dashboard" / "dashboard-editor.tsx").read_text(
        encoding="utf-8"
    )
    assert '<option value="vertical">' in src
    assert '<option value="v">' not in src, (
        'the editor must not offer "v" — it reads as a choice but behaves as auto'
    )


def test_the_planner_is_told_not_to_emit_the_legacy_spelling():
    src = (BACKEND / "app" / "engine" / "llm.py").read_text(encoding="utf-8")
    assert 'Do NOT emit \\"v\\"' in src


def test_the_legacy_spelling_shows_as_auto_rather_than_blank():
    """Otherwise the select matches no option, renders empty, and the next
    change writes a direction the panel never had."""
    src = (FRONTEND / "components" / "dashboard" / "dashboard-editor.tsx").read_text(
        encoding="utf-8"
    )
    assert 'value={panel.orientation === "v" ? "" : panel.orientation ?? ""}' in src


# --- the accent colour ------------------------------------------------------
#
# A combo's line took the next series colour, which inside a themed palette is
# a near neighbour of the bars — a green line over green bars. The accent is
# from a contrasting hue, and both renderers must agree on it.

def frontend_accents() -> dict[str, str]:
    text = (FRONTEND / "lib" / "dashboard-format.ts").read_text(encoding="utf-8")
    block = re.search(
        r"export const PALETTE_ACCENTS: Record<string, string> = \{(.*?)\n\}", text, re.S
    )
    assert block, "could not find PALETTE_ACCENTS"
    return dict(re.findall(r'(\w+): "(#[0-9A-Fa-f]{6})"', block.group(1)))


def test_every_palette_declares_an_accent_in_both_renderers():
    front = frontend_accents()
    assert set(front) == set(pdf_charts.PALETTE_ACCENTS)
    assert set(PALETTE_NAMES) | {"default"} == set(front)


def test_an_accent_is_not_one_of_its_own_palettes_series_colours():
    """Otherwise it is the very collision the accent exists to avoid."""
    for name in PALETTE_NAMES:
        assert pdf_charts.PALETTE_ACCENTS[name] not in pdf_charts.PALETTES[name], name


def test_the_default_combo_line_keeps_the_colour_it_always_had():
    """No palette named means nothing about the panel changed, so the export
    must come out in exactly the colours it did before accents existed."""
    token = pdf_charts._panel_style.set((True, None))
    try:
        one_bar = pdf_charts.accent_color([pdf_charts.series_color(0)])
        two_bars = pdf_charts.accent_color(
            [pdf_charts.series_color(0), pdf_charts.series_color(1)]
        )
    finally:
        pdf_charts._panel_style.reset(token)
    assert one_bar == pdf_charts.SERIES_COLORS[1]
    assert two_bars == pdf_charts.SERIES_COLORS[2]


def test_a_themed_combo_line_does_not_reuse_the_bar_colour():
    token = pdf_charts._panel_style.set((True, "meadow"))
    try:
        bar = pdf_charts.series_color(0)
        line = pdf_charts.accent_color([bar])
    finally:
        pdf_charts._panel_style.reset(token)
    assert line != bar
    # ...and differs in hue, not merely in shade: the green channel dominating
    # both is what made the original pair unreadable.
    assert abs(line[0] - bar[0]) > 0.25, "the accent must not be another green"


def test_the_combo_renderer_asks_for_an_accent_rather_than_the_next_series():
    src = (FRONTEND / "components" / "dashboard" / "dashboard-panel.tsx").read_text(
        encoding="utf-8"
    )
    assert "const lineColour = accentColor(panel.palette, barColours)" in src
    assert "seriesColor(barFields.length" not in src, (
        "the combo line must not fall back to index arithmetic over the palette"
    )


# --- charts below the fold must still draw ----------------------------------

def test_no_series_relies_on_recharts_mount_animation():
    """Recharts animates by growing a clip rect from zero. A chart that mounts
    off-screen never runs that animation, so the clip stays collapsed and the
    panel renders as axes and a legend around an empty plot — which is what
    every chart below the fold on a long dashboard was doing."""
    src = renderer_source()
    series = ("<Bar", "<Line", "<Area", "<Scatter", "<Radar", "<Pie", "<Funnel")
    for tag in series:
        for match in re.finditer(re.escape(tag) + r"(?![A-Za-z])", src):
            window = src[match.start(): match.start() + 400]
            assert "isAnimationActive={false}" in window, (
                f"{tag} does not opt out of the mount animation — charts below "
                "the fold will render empty"
            )


# --- format: a panel's label and axis settings ------------------------------
#
# Absent means "follow the chart standard", so it must stay absent for every
# panel saved before it existed, and survive a save once someone sets it.

from app.engine.dashboards import _normalise_format  # noqa: E402


def test_a_panel_without_format_keeps_none():
    assert _panel_config({"title": "T", "chart_type": "bar"})["format"] is None


def test_format_survives_a_save():
    """Only fields _PANEL_CONFIG_FIELDS names are stored; anything else is
    dropped without a word, which would read as the settings being ignored."""
    settings = {"x_title": "Week", "y_title": "Net Sales (AED M)", "unit": "M",
                "data_labels": "extremes", "legend": "right"}
    assert _panel_config({"title": "T", "chart_type": "line", "format": settings})["format"] == settings


def test_an_empty_title_is_kept_because_it_means_hidden():
    assert _normalise_format({"x_title": "  "}) == {"x_title": ""}


def test_an_unknown_value_drops_only_that_setting():
    out = _normalise_format({"unit": "trillions", "legend": "right", "data_labels": "most"})
    assert out == {"legend": "right"}


def test_series_overrides_are_kept_by_column():
    out = _normalise_format({"series": {
        "BUDGET_GMV_AED": {"data_labels": "none", "markers": "hide"},
        "ACTUAL_GMV_AED": {"label_position": "inside_end"},
    }})
    assert out == {"series": {
        "BUDGET_GMV_AED": {"data_labels": "none", "markers": "hide"},
        "ACTUAL_GMV_AED": {"label_position": "inside_end"},
    }}


def test_a_series_override_drops_unknown_values_and_empty_entries():
    out = _normalise_format({"series": {
        "A": {"data_labels": "most", "legend": "right"},
        "B": "not settings",
        "": {"markers": "show"},
        "C": {"markers": "show"},
    }})
    assert out == {"series": {"C": {"markers": "show"}}}


def test_stack_totals_is_kept_only_as_a_boolean():
    assert _normalise_format({"stack_totals": True}) == {"stack_totals": True}
    assert _normalise_format({"stack_totals": "yes"}) is None


def test_series_overrides_survive_a_save():
    settings = {"data_labels": "all", "markers": "show",
                "series": {"BUDGET": {"data_labels": "none"}}}
    config = _panel_config({"title": "T", "chart_type": "area_line", "format": settings})
    assert config["format"] == settings


def test_scatter_settings_are_kept():
    settings = {"trend_line": True, "x_min": 0, "x_max": 20.5, "y_min": -1.5,
                "x_reference": {"kind": "median"},
                "y_reference": {"kind": "value", "value": 12}}
    assert _normalise_format(settings) == settings


def test_a_min_at_or_above_its_max_drops_both():
    assert _normalise_format({"x_min": 5, "x_max": 5, "y_max": 9}) == {"y_max": 9}


def test_axis_bounds_must_be_real_numbers():
    assert _normalise_format({"x_min": "0", "y_min": True, "y_max": float("nan")}) is None


def test_a_typed_reference_needs_its_number():
    assert _normalise_format({"x_reference": {"kind": "value"}}) is None
    assert _normalise_format({"y_reference": {"kind": "mode"}}) is None
    assert _normalise_format({"y_reference": {"kind": "mean", "value": 3}}) == {
        "y_reference": {"kind": "mean"}}


def test_the_editor_and_backend_offer_the_same_reference_kinds():
    from app.engine.dashboards import FORMAT_REFERENCE_KINDS

    text = (FRONTEND / "lib" / "chart-standard.ts").read_text(encoding="utf-8")
    block = re.search(r"export const REFERENCE_CHOICES = \[(.*?)\]", text, re.S)
    assert block, "REFERENCE_CHOICES not found in chart-standard.ts"
    assert set(re.findall(r'"([a-z]+)"', block.group(1))) == set(FORMAT_REFERENCE_KINDS)


def test_every_value_label_choice_is_kept():
    for choice in ("none", "max", "min", "extremes", "all"):
        assert _normalise_format({"data_labels": choice}) == {"data_labels": choice}


def test_nothing_usable_is_stored_as_none():
    assert _normalise_format({"unit": "x", "bogus": 1}) is None
    assert _normalise_format("not a dict") is None


def test_titles_are_capped():
    assert len(_normalise_format({"y_title": "x" * 500})["y_title"]) == 80


def test_the_editor_and_backend_offer_the_same_choices():
    """The frontend switches on these words; one the backend drops would read
    as a setting that does nothing after saving."""
    from app.engine.dashboards import FORMAT_CHOICES

    text = (FRONTEND / "lib" / "chart-standard.ts").read_text(encoding="utf-8")
    for key, allowed in FORMAT_CHOICES.items():
        block = re.search(rf"export const {key.upper()}_CHOICES = \[(.*?)\]", text, re.S)
        assert block, f"{key.upper()}_CHOICES not found in chart-standard.ts"
        assert set(re.findall(r'"([A-Za-z_]+)"', block.group(1))) == set(allowed), key


def test_a_funnel_percentage_setting_is_kept_and_a_bad_one_dropped():
    for choice in ("first", "previous", "none"):
        assert _normalise_format({"funnel_percent": choice}) == {"funnel_percent": choice}
    assert _normalise_format({"funnel_percent": "sideways"}) is None


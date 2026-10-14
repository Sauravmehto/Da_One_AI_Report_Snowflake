"""The PDF export renders the dashboard, not a transcript of its numbers.

Every test here pins a defect the export actually shipped with: no charts at
all, "?" wherever a title used an em dash, headings orphaned at the foot of a
page, a pie drawn as overlapping wedges, and axis ticks placed outside their own
plot (which drew them over the panel above).
"""

from __future__ import annotations

import pymupdf
import pytest

from app.engine import pdf_charts
from app.engine.dashboard_export import build_dashboard_pdf

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]


def bar_panel(title="Rent by Sector", **kw):
    panel = {
        "title": title,
        "chart_type": "bar",
        "display": "chart",
        "x": "SECTOR",
        "y": "TOTAL",
        "columns": ["SECTOR", "TOTAL"],
        "rows": [["Office", 1_240_000], ["Retail", 880_000], ["Industrial", 610_000]],
        "row_count": 3,
    }
    panel.update(kw)
    return panel


def render(panels, **kw) -> pymupdf.Document:
    return pymupdf.open("pdf", build_dashboard_pdf("Portfolio", panels, **kw))


def all_text(doc) -> str:
    return "\n".join(page.get_text() for page in doc)


def drawing_count(doc) -> int:
    return sum(len(page.get_drawings()) for page in doc)


# --- charts exist at all -----------------------------------------------------


def test_a_chart_panel_is_drawn_as_vector_geometry():
    """The export used to be text only: a dashboard of charts arrived as a wall
    of pipe-separated rows, while the Excel export got real charts."""
    doc = render([bar_panel()])
    assert drawing_count(doc) > 3, "no chart geometry — the panel came out as text"


def test_a_chart_type_with_no_renderer_falls_back_to_its_table():
    """A wrong chart is worse than the figures, so an unsupported type shows
    the numbers rather than guessing at a shape."""
    panel = bar_panel(chart_type="sankey", display="both")
    doc = render([panel])
    text = all_text(doc)
    assert "Office" in text and "1,240,000" in text


@pytest.mark.parametrize(
    "chart_type",
    ["bar", "stacked_bar", "line", "area", "pie", "donut", "scatter"],
)
def test_every_supported_chart_type_renders(chart_type):
    panel = bar_panel(chart_type=chart_type, y=["TOTAL", "SHARE"],
                      columns=["SECTOR", "TOTAL", "SHARE"],
                      rows=[["Office", 120, 40], ["Retail", 90, 33],
                            ["Industrial", 60, 27]])
    doc = render([panel])
    assert drawing_count(doc) > 3, f"{chart_type} drew nothing"


def test_declared_renderable_types_all_have_a_renderer():
    """RENDERABLE is what the fallback checks, so an entry with no branch behind
    it would silently produce an empty panel instead of a table."""
    for chart_type in sorted(pdf_charts.RENDERABLE):
        panel = bar_panel(chart_type=chart_type)
        assert pdf_charts.extract(panel) is not None, chart_type


# --- text that used to come out as "?" ---------------------------------------


def test_typographic_characters_survive_the_base14_font():
    """The base-14 PDF fonts are Latin-1. Planner-written titles routinely carry
    an em dash, which rendered as "?" for every affected panel."""
    doc = render(
        [bar_panel(title="Rent by Sector — “core” assets")],
        narrative="Occupancy held at 94% • arrears fell — see notes…",
    )
    text = all_text(doc)
    assert "?" not in text
    assert "Rent by Sector" in text


def test_headings_are_drawn_in_a_bold_face():
    """`bold=True` was accepted and then ignored, so headings differed from body
    text by size alone."""
    doc = render([bar_panel()])
    fonts = {f[3] for page in doc for f in page.get_fonts(full=True)}
    assert any("Bold" in name for name in fonts), fonts


# --- pagination --------------------------------------------------------------


def test_a_heading_is_never_the_last_thing_on_a_page():
    """Space for the heading and for its chart was checked separately, which
    stranded titles at the foot of a page while the chart began the next one."""
    doc = render([bar_panel(title=f"Panel {i}") for i in range(1, 13)])
    assert doc.page_count > 1, "not enough panels to force a page break"
    titles = {f"Panel {i}" for i in range(1, 13)}
    for index, page in enumerate(doc):
        lines = [line for line in page.get_text().splitlines() if line.strip()]
        if index < doc.page_count - 1 and lines:
            assert lines[-1].strip() not in titles, f"page {index + 1} ends on a heading"


def test_every_row_is_written():
    """The export stands in for the dashboard. It used to stop at 25 rows, so a
    reader had to go back to the screen for the rest."""
    panel = {
        "title": "Lease Detail", "chart_type": None, "display": "table",
        "columns": ["PROPERTY", "RENT"],
        "rows": [[f"Property {i}", 1000 * i] for i in range(1, 61)],
        "row_count": 60,
    }
    text = all_text(render([panel]))
    for i in (1, 26, 45, 60):
        assert f"Property {i}\n" in text + "\n", f"row {i} missing"
    assert "Showing 25 of" not in text


def test_a_table_running_onto_a_second_page_repeats_its_header():
    panel = {
        "title": "Lease Detail", "chart_type": None, "display": "table",
        "columns": ["PROPERTY", "RENT"],
        "rows": [[f"Property {i}", 1000 * i] for i in range(1, 121)],
        "row_count": 120,
    }
    doc = render([panel])
    assert doc.page_count > 1
    for index, page in enumerate(doc):
        assert "PROPERTY" in page.get_text(), f"page {index + 1} has no header"


def test_a_table_is_ruled():
    """Columns were separated by nothing at all, which read as a list."""
    plain = drawing_count(render([{
        "title": "Empty-ish", "chart_type": None, "display": "table",
        "columns": ["A"], "rows": [["x"]], "row_count": 1,
    }]))
    assert plain >= 3, "no header band, row rules or border drawn"


def test_a_numeric_column_uses_one_decimal_convention():
    """135,688.1 beside 87,999.8 does not line up; both want two places."""
    panel = {
        "title": "Balances", "chart_type": None, "display": "table",
        "columns": ["CLIENT", "BALANCE"],
        "rows": [["A", 135688.1], ["B", 87999.8], ["C", 104451.7]],
        "row_count": 3,
    }
    text = all_text(render([panel]))
    assert "135,688.10" in text and "87,999.80" in text


def test_an_upstream_row_cap_is_reported_not_hidden():
    """When the dashboard itself returned fewer rows than exist, say so rather
    than implying the file holds everything."""
    panel = {
        "title": "Detail", "chart_type": None, "display": "table",
        "columns": ["ID"], "rows": [[i] for i in range(100)], "row_count": 4321,
    }
    assert "100 of 4,321 rows" in all_text(render([panel]))


def test_a_very_long_table_stops_at_the_pdf_limit_and_says_so():
    """A dashboard panel can return thousands of rows; drawn whole, one table
    ran to a hundred pages."""
    from app.engine.dashboard_export import _MAX_PDF_TABLE_ROWS

    total = _MAX_PDF_TABLE_ROWS + 250
    panel = {
        "title": "Detail", "chart_type": None, "display": "table",
        "columns": ["ID"], "rows": [[f"Row {i}"] for i in range(1, total + 1)],
        "row_count": total,
    }
    text = all_text(render([panel]))
    assert f"Row {_MAX_PDF_TABLE_ROWS}\n" in text
    assert f"Row {_MAX_PDF_TABLE_ROWS + 1}\n" not in text
    assert f"{_MAX_PDF_TABLE_ROWS:,} of {total:,} rows" in text
    assert "The Excel export has every row." in text


# --- the narrative the page already shows ------------------------------------


def test_the_narrative_reaches_the_document():
    """The builder always accepted one; the route never passed it, so the
    summary on screen was missing from every export."""
    doc = render([bar_panel()], narrative="Occupancy held at 94 percent.")
    text = all_text(doc)
    assert "Summary" in text and "Occupancy held at 94 percent." in text


def test_no_summary_section_without_a_narrative():
    assert "Summary" not in all_text(render([bar_panel()]))


# --- axis maths --------------------------------------------------------------


@pytest.mark.parametrize(
    "low,high",
    [(0, 1_240_000), (1.1, 2.42), (0, 1), (-50, 50), (0.001, 0.009), (0, 7)],
)
def test_ticks_stay_inside_their_own_range(low, high):
    """A tick above the maximum is drawn above the plot — over whichever panel
    happens to sit there. A range of 1.1 to 2.42 used to emit a tick at 3.1."""
    ticks = pdf_charts._tick_values(low, high)
    assert ticks
    assert all(low - 1e-9 <= t <= high + 1e-9 for t in ticks), ticks


def test_a_flat_series_does_not_divide_by_zero():
    panel = bar_panel(rows=[["Office", 5], ["Retail", 5], ["Industrial", 5]])
    assert drawing_count(render([panel])) > 3


def test_booleans_are_not_charted_as_ones():
    """A flag column is not a measure; True would otherwise plot as 1."""
    assert pdf_charts._num(True) is None
    assert pdf_charts._num(False) is None
    assert pdf_charts._num("1,250.5") == 1250.5


# --- data extraction ---------------------------------------------------------


def test_a_colour_column_splits_one_measure_into_series():
    panel = {
        "title": "Rent by Sector and Year", "chart_type": "bar", "display": "chart",
        "x": "SECTOR", "y": "TOTAL", "color": "YEAR",
        "columns": ["SECTOR", "YEAR", "TOTAL"],
        "rows": [["Office", "2025", 10], ["Office", "2026", 12],
                 ["Retail", "2025", 8], ["Retail", "2026", 9]],
        "row_count": 4,
    }
    data = pdf_charts.extract(panel)
    assert data is not None
    assert data.categories == ["Office", "Retail"]
    assert [name for name, _ in data.series] == ["2025", "2026"]
    assert [values for _, values in data.series] == [[10.0, 8.0], [12.0, 9.0]]


def test_a_pie_keeps_every_wedge_when_the_panel_also_sets_a_colour_column():
    """A planner routinely sets x AND color to the same dimension. That sent the
    pie down the long-form pivot, where each group holds one value and the rest
    None — so it drew its first group as a single wedge filling the circle.
    """
    panel = {
        "title": "Payment Status Mix", "chart_type": "donut", "display": "chart",
        "x": "PAYMENT_STATUS", "y": "BALANCE_DUE_USD", "color": "PAYMENT_STATUS",
        "columns": ["PAYMENT_STATUS", "BALANCE_DUE_USD"],
        "rows": [["Outstanding", 1_403_837.0], ["Overdue", 1_048_595.0],
                 ["Disputed", 1_031_887.0], ["Partially Paid", 365_990.53]],
        "row_count": 4,
    }
    data = pdf_charts.extract(panel, single_series=True)
    assert data is not None
    assert data.categories == ["Outstanding", "Overdue", "Disputed", "Partially Paid"]
    assert len(data.series) == 1
    values = data.series[0][1]
    assert all(v is not None for v in values), values
    assert len(values) == 4

    # And every wedge reaches the page, not just the first.
    text = all_text(render([panel]))
    for name in ("Outstanding", "Overdue", "Disputed", "Partially Paid"):
        assert name in text


def test_every_page_carries_a_footer_with_its_number():
    panel = {
        "title": "Detail", "chart_type": None, "display": "table",
        "columns": ["ID"], "rows": [[i] for i in range(200)], "row_count": 200,
    }
    doc = render([panel])
    assert doc.page_count > 1
    for number, page in enumerate(doc, start=1):
        assert f"Page {number} of {doc.page_count}" in page.get_text()


def test_sections_become_headings():
    """The dashboard's tabs are its structure; the report keeps them."""
    panels = [
        bar_panel(title="Exposure", section="Overview"),
        bar_panel(title="Breaches", section="Risk"),
    ]
    text = all_text(render(panels))
    assert "Overview" in text and "Risk" in text


def test_a_single_section_is_not_given_a_heading():
    """One section is not a structure, and a lone heading is just noise."""
    panels = [bar_panel(title="Exposure", section="Overview")]
    assert "Overview" not in all_text(render(panels))


def test_each_panel_gets_its_own_page():
    """Panels were packed two-up to mirror the dashboard's grid. On screen that
    works because a chart's data is one click away; on paper the tables ended
    up detached from the charts they explained and a reader could not tell
    which numbers belonged to which picture."""
    panels = [
        bar_panel(title="First", display="chart"),
        bar_panel(title="Second", display="chart"),
        bar_panel(title="Third", display="chart"),
    ]
    doc = render(panels)
    assert doc.page_count == 3, f"expected a page each, got {doc.page_count}"
    for page, title in zip(doc, ["First", "Second", "Third"]):
        assert title in page.get_text()


def test_the_first_panel_does_not_start_on_a_blank_page():
    """It continues under the summary and KPI band; only later panels break."""
    doc = render([bar_panel(title="Only")], narrative="A summary.")
    assert doc.page_count == 1
    text = doc[0].get_text()
    assert "A summary." in text and "Only" in text


def test_a_chart_and_its_table_land_on_the_same_page():
    """The whole point of a page per panel: the numbers sit under the picture
    they explain."""
    doc = render([bar_panel(title="Sector", display="both")])
    page = doc[0].get_text()
    assert "Sector" in page
    assert "Office" in page and "1,240,000" in page


def test_declared_column_formats_reach_the_file():
    """Formatting visible on screen and gone in the export is the worst of both:
    the PDF used the measured decimals and ignored the authored format."""
    panel = {
        "title": "By State", "chart_type": None, "display": "table",
        "columns": ["STATE", "VOLUME", "SHARE"],
        "rows": [["TX", 685400, 40.5]], "row_count": 1,
        "column_formats": {
            "VOLUME": {"format": "currency"},
            "SHARE": {"format": "percent", "decimals": 1},
            "STATE": {"label": "State"},
        },
    }
    text = all_text(render([panel]))
    assert "$685,400" in text, "currency not applied"
    assert "40.5%" in text, "percent not applied"
    assert "State" in text and "STATE" not in text, "header label not applied"


def test_axis_labels_never_overprint_each_other():
    """Long date labels on a half-width chart used to run into the chart beside
    it; clamping them inside the plot then stacked them on their neighbour."""
    months = ["2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"]
    panels = [
        {"title": "Invoiced", "chart_type": "area", "display": "chart", "span": 2,
         "x": "MONTH", "y": ["INVOICED_USD"],
         "columns": ["MONTH", "INVOICED_USD"],
         "rows": [[m, 3_000_000 + i * 600_000] for i, m in enumerate(months)],
         "row_count": 4},
        {"title": "Aging", "chart_type": "bar", "display": "chart", "span": 2,
         "x": "BUCKET", "y": "BALANCE", "columns": ["BUCKET", "BALANCE"],
         "rows": [["Current", 1_403_837], ["1-30 days", 680_000],
                  ["31-60 days", 420_000], ["61-90 days", 340_000]],
         "row_count": 4},
    ]
    page = render(panels)[0]
    baselines: dict[float, list] = {}
    for word in page.get_text("words"):
        baselines.setdefault(round(word[3], 1), []).append(word)
    for _, words in baselines.items():
        words.sort(key=lambda w: w[0])
        for left, right in zip(words, words[1:]):
            assert right[0] >= left[2] - 0.5, f"{left[4]!r} overlaps {right[4]!r}"


def test_kpi_values_use_their_declared_format():
    """The KPI rows carry FORMAT so the renderer need not guess. Ignoring it
    printed a currency total as a bare 3850309."""
    panel = {
        "title": "Headline", "chart_type": "kpi", "display": "kpi",
        "columns": ["METRIC", "VALUE", "FORMAT"],
        "rows": [["Outstanding", 3_850_309, "currency"],
                 ["Occupancy", 94, "percent"],
                 ["Invoices", 157, "count"]],
        "row_count": 3,
    }
    text = all_text(render([panel]))
    # Written as the dashboard's tile writes it.
    assert "$3.9M" in text
    assert "94%" in text
    assert "157" in text
    assert "3850309" not in text


def _capture_source() -> str:
    from pathlib import Path as _Path

    return (
        _Path(__file__).resolve().parents[3]
        / "frontend" / "src" / "components" / "dashboard" / "pdf-capture.tsx"
    ).read_text(encoding="utf-8")


def test_the_pictures_do_not_depend_on_which_tab_is_open():
    """A dashboard mounts one tab at a time.

    Pictures were once taken of the panels already on the page, so every chart
    on another tab exported without one. The capture now draws every chart
    itself, off screen, and must not go back to reading the mounted page.
    """
    source = _capture_source()
    assert "createRoot" in source and "<PanelChart" in source
    assert "data-panel-title" not in source, (
        "the capture must draw its own charts, not photograph the ones on screen"
    )


def test_a_chart_without_a_picture_is_still_drawn():
    """The pictures are an improvement, not a dependency: a plain download link
    or a capture that failed still gets every chart."""
    doc = render([bar_panel()])
    assert drawing_count(doc) > 3


def test_a_picture_from_the_page_is_used_for_the_chart():
    import pymupdf as _pymupdf

    pixmap = _pymupdf.Pixmap(_pymupdf.csRGB, _pymupdf.IRect(0, 0, 400, 200), False)
    pixmap.clear_with(200)
    doc = render([bar_panel()], panel_images={"Rent by Sector": pixmap.tobytes("png")})
    assert any(page.get_images() for page in doc), "the page's picture was not placed"


def test_a_dashboard_of_chart_pictures_stays_small():
    """Twenty charts from the page, each a different 1440-pixel picture.

    Saved uncompressed, each picture was stored as raw pixels -- about 3 MB --
    and a real 20-chart dashboard exported at 70 MB. Compressed, the same file
    is well under a megabyte, and every picture is still placed at full size.
    """
    import pymupdf as _pymupdf

    panels, images = [], {}
    for i in range(20):
        title = f"Chart {i}"
        panels.append(bar_panel(title=title))
        pixmap = _pymupdf.Pixmap(_pymupdf.csRGB, _pymupdf.IRect(0, 0, 1440, 720), False)
        pixmap.clear_with(255)
        for x in range(0, 1440, 60):   # bars of different heights per chart
            height = 60 + ((x // 60) * (i + 7)) % 600
            pixmap.set_rect(_pymupdf.IRect(x + 10, 720 - height, x + 40, 720), (40 + i * 9, 90, 200))
        images[title] = pixmap.tobytes("png")

    data = build_dashboard_pdf("Portfolio", panels, panel_images=images)
    assert len(data) < 2 * 1024 * 1024, f"PDF is {len(data) / 1024 / 1024:.1f} MB"
    doc = pymupdf.open("pdf", data)
    placed = [info for page in doc for info in page.get_image_info()]
    assert len(placed) == 20
    pages_with_pictures = [page for page in doc if page.get_images()]
    image = doc.extract_image(pages_with_pictures[0].get_images()[0][0])
    assert (image["width"], image["height"]) == (1440, 720)


def test_a_table_written_by_the_page_is_printed_as_written():
    """Headers and cells as the dashboard shows them, not the SQL aliases and
    bare numbers of the raw rows."""
    panel = bar_panel(columns=["SECTOR", "RETURN_RATE_PCT"],
                      rows=[["Office", 2.86], ["Retail", 3.1]], y="RETURN_RATE_PCT")
    prepared = {"headers": ["Sector", "Return Rate %"], "numeric": [False, True],
                "rows": [["Office", "2.86%"], ["Retail", "3.1%"]]}
    text = all_text(render([panel], panel_tables={"Rent by Sector": prepared}))
    assert "Return Rate %" in text and "2.86%" in text
    assert "RETURN_RATE_PCT" not in text


def test_a_long_table_does_not_squeeze_its_chart():
    """The chart's height used to be what was left after reserving the WHOLE
    table, which for a long table was the minimum."""
    import pymupdf as _pymupdf

    rows = [[f"Row {i}", i] for i in range(200)]
    panel = bar_panel(columns=["SECTOR", "TOTAL"], rows=rows, row_count=200)
    pixmap = _pymupdf.Pixmap(_pymupdf.csRGB, _pymupdf.IRect(0, 0, 720, 360), False)
    pixmap.clear_with(180)
    doc = render([panel], panel_images={"Rent by Sector": pixmap.tobytes("png")})
    placed = doc[0].get_image_info()
    assert placed, "no picture placed"
    height = placed[0]["bbox"][3] - placed[0]["bbox"][1]
    assert height > 200, f"chart squeezed to {height:.0f}pt"


def test_a_chart_named_on_a_table_panel_is_still_drawn():
    """The dashboard draws the chart when a panel names one, whatever display
    says, and the file must show the same thing."""
    doc = render([bar_panel(display="table")])
    assert drawing_count(doc) > 3


def test_the_pdf_can_draw_every_chart_type_the_planner_may_emit():
    """The planner's vocabulary and the PDF's renderers must not drift apart.

    They already had: `heatmap` was on the planner's list with no renderer
    behind it, so those panels silently became tables and a dashboard's later
    tabs looked like they had lost their charts.
    """
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "engine" / "llm.py"
    text = source.read_text(encoding="utf-8")
    match = re.search(r'"chart_type":\s*"([a-z_|]+)', text)
    assert match, "could not find the planner's chart_type vocabulary in llm.py"

    declared = {t for t in match.group(1).split("|") if t}
    # KPI panels are a tile band, not a chart, and tables declare no type.
    # There is no other exception any more: the backend carries its own
    # boundary geometry (app/engine/geo/), so maps draw here too.
    missing = declared - pdf_charts.RENDERABLE - {"kpi"}
    assert not missing, f"planner may emit {sorted(missing)} but the PDF cannot draw them"


def test_a_heatmap_is_drawn_as_a_shaded_grid():
    panel = {
        "title": "Aging by Client", "chart_type": "heatmap", "display": "chart",
        "x": "BUCKET", "y": "BALANCE", "color": "CLIENT",
        "columns": ["BUCKET", "CLIENT", "BALANCE"],
        "rows": [[b, c, v]
                 for b, row in zip(["Current", "1-30", "31-60"],
                                   [[120, 80], [90, 70], [60, 50]])
                 for c, v in zip(["Acme", "Borealis"], row)],
        "row_count": 6,
    }
    doc = render([panel])
    assert drawing_count(doc) >= 6, "no grid cells drawn"
    text = all_text(doc)
    assert "Acme" in text and "Current" in text


def test_an_undrawable_chart_type_says_so():
    """Falling back to a table is fine; doing it silently is not."""
    panel = bar_panel(chart_type="sunburst", display="chart")
    assert "shown as a table" in all_text(render([panel]))


def test_a_full_width_chart_reserves_its_own_height():
    """Reserving by column-pairing rather than by height gave a full-width
    chart 60 points for a 165-point chart, so it drew over the page edge."""
    filler = {
        "title": "Filler", "chart_type": None, "display": "table",
        "columns": ["ID"], "rows": [[i] for i in range(38)], "row_count": 38,
    }
    panels = [filler, bar_panel(title="Pushed Chart", display="both")]
    doc = render(panels)
    for page in doc:
        if "Pushed Chart" not in page.get_text():
            continue
        heading = next(w for w in page.get_text("words") if w[4] == "Pushed")
        # The chart must fit under its heading on the same page.
        assert heading[3] + 165 < 842, "chart would run off the page"


def test_a_wide_table_keeps_its_narrow_columns_intact():
    """Scaling every column by the same ratio truncated columns that would
    have fit: an eight-column table rendered every invoice id as "INV-900..."
    and every date as "2026-0...", destroying the data it was there to show."""
    columns = ["INVOICE_ID", "CLIENT_NAME", "INVOICE_DATE", "DUE_DATE",
               "PAYMENT_STATUS", "DAYS_OVERDUE", "TOTAL_INVOICE_AMOUNT",
               "AMOUNT_PAID"]
    rows = [[f"INV-9001{i:02d}", "Merritt, Hudson and Brown", "2026-07-03",
             "2026-08-02", "Partially Paid", 63 - i, 127513.90, 12813.43]
            for i in range(20)]
    text = all_text(render([{
        "title": "Overdue Invoice Detail", "chart_type": None, "display": "table",
        "columns": columns, "rows": rows, "row_count": 20,
    }]))
    # Dates appear in the table's own MM-DD-YYYY form; the point here is that
    # nothing is LOST to column truncation, not which format it lands in.
    for probe in ("INV-900100", "07-03-2026", "08-02-2026", "Partially Paid",
                  "127,513.90", "12,813.43"):
        assert probe in text, f"{probe} was truncated"


def test_column_widths_take_space_only_from_the_greedy_columns():
    from app.engine.dashboard_export import _fit_widths

    # One very wide column beside three that comfortably fit.
    widths = _fit_widths([300.0, 40.0, 40.0, 40.0], available=200.0, floor=20.0)
    assert widths[1:] == [40.0, 40.0, 40.0], "narrow columns were shrunk too"
    assert widths[0] == pytest.approx(80.0)
    assert sum(widths) <= 200.0 + 1e-9


def test_a_narrow_table_is_stretched_to_fill_the_page():
    """A two-column table drew itself 100 points wide against the left margin
    of a 515-point page, leaving four fifths of the sheet blank beside it. It
    read as a broken export, and no two tables in the document shared an edge."""
    from app.engine.dashboard_export import _fit_widths

    widths = _fit_widths([50.0, 60.0], available=500.0, floor=20.0)
    assert sum(widths) == pytest.approx(500.0)


def test_the_surplus_goes_to_the_label_column_not_the_numbers():
    """A right-aligned number given more room only drifts further from its own
    header; a label is what actually uses the space."""
    from app.engine.dashboard_export import _fit_widths

    widths = _fit_widths([80.0, 50.0], available=500.0, floor=20.0,
                         numeric=[False, True])
    assert widths[1] == pytest.approx(50.0), "the numeric column was widened"
    assert widths[0] == pytest.approx(450.0)


def test_an_all_numeric_table_still_fills_the_width():
    """With no label column to absorb it, the numbers share the surplus rather
    than the table staying narrow."""
    from app.engine.dashboard_export import _fit_widths

    widths = _fit_widths([40.0, 60.0], available=400.0, floor=20.0,
                         numeric=[True, True])
    assert sum(widths) == pytest.approx(400.0)
    assert widths[1] > widths[0], "the wider column should stay the wider one"


def test_every_table_in_the_document_shares_its_edges():
    """"Make sure the tables are perfectly aligned with each other" — which for
    tables with different columns means the same left and right edge."""
    panels = [
        {"title": "Classes", "chart_type": None, "display": "table",
         "columns": ["PROPERTY_CLASS", "PROPERTY_COUNT"],
         "rows": [["Residential", 4428423]], "row_count": 1},
        {"title": "Liens", "chart_type": None, "display": "table",
         "columns": ["STATE", "TOTAL_AMOUNT_DUE"],
         "rows": [["FL", 12301018401.04]], "row_count": 1},
    ]
    doc = render(panels)
    spans = []
    for page in doc:
        # The header band is the widest filled rectangle on the page.
        fills = [d["rect"] for d in page.get_drawings() if d.get("fill")]
        assert fills, "no table was drawn"
        widest = max(fills, key=lambda r: r.width)
        spans.append((round(widest.x0, 1), round(widest.x1, 1)))
    assert len(set(spans)) == 1, f"tables do not line up: {spans}"
    assert spans[0] == (40.0, 555.0), f"tables do not span the page: {spans[0]}"


def test_a_chart_panel_also_gets_its_data_table():
    """On screen a chart's numbers are one disclosure click away; on paper
    there is nowhere to click, so a chart-only panel exported as a picture of
    data a reader could not read off it."""
    doc = render([bar_panel(title="Sector", display="chart")])
    text = all_text(doc)
    assert "Office" in text and "1,240,000" in text, "the chart shipped without its data"


def _chart_height(doc, title: str) -> float:
    """Vertical gap between a panel's heading and the top of its table."""
    for page in doc:
        words = page.get_text("words")
        head = next((w for w in words if w[4] == title.split()[0]), None)
        if head is None:
            continue
        bands = [d["rect"] for d in page.get_drawings()
                 if d.get("fill") and d["rect"].width > 300]
        assert bands, f"no table found under {title}"
        return min(r.y0 for r in bands) - head[3]
    raise AssertionError(f"{title} is not in the document")


def test_a_three_slice_pie_is_not_given_a_whole_page():
    """Height was handed out by what the page had spare rather than what the
    chart wanted, so a three-slice donut filled a sheet of A4."""
    panel = {
        "title": "Lien Share", "chart_type": "donut", "display": "chart",
        "x": "CATEGORY", "y": "LIEN_COUNT",
        "columns": ["CATEGORY", "LIEN_COUNT"],
        "rows": [["State", 581744], ["Judgment", 475324], ["Federal", 149272]],
        "row_count": 3,
    }
    assert _chart_height(render([panel]), "Lien") < 300


def test_a_horizontal_bar_chart_is_sized_by_its_bar_count():
    """Twelve bars stretched over a page became slabs a centimetre thick
    separated by empty aisles. Bars run down the page, so the height is a row
    count — not whatever was left over."""
    from app.engine.pdf_charts import natural_height

    def bars(n: int) -> dict:
        return {
            "title": "States", "chart_type": "bar", "display": "chart",
            "orientation": "h", "x": "STATE", "y": "N",
            "columns": ["STATE", "N"],
            "rows": [[f"S{i}", 100 - i] for i in range(n)], "row_count": n,
        }

    four, twelve = natural_height(bars(4), 515.0), natural_height(bars(12), 515.0)
    assert four < twelve, "the height ignored the number of bars"
    # Each extra bar is worth roughly one row, not a share of the page.
    assert 15 < (twelve - four) / 8 < 30


def test_a_vertical_chart_keeps_its_proportions_whatever_the_row_count():
    """A line chart's height is an aspect ratio: stretching it to fill the page
    makes the trend steeper for no reason."""
    from app.engine.pdf_charts import natural_height

    def line(n: int) -> dict:
        return {
            "title": "Trend", "chart_type": "line", "display": "chart",
            "x": "MONTH", "y": "N", "columns": ["MONTH", "N"],
            "rows": [[f"2025-{i % 12 + 1:02d}-01", i] for i in range(n)],
            "row_count": n,
        }

    assert natural_height(line(6), 515.0) == natural_height(line(40), 515.0)
    # Wider than tall, or it reads as a poster.
    assert natural_height(line(6), 515.0) < 515.0


def test_a_chart_never_outgrows_the_room_left_on_its_page():
    """The content asks; the page still has the final say. A forty-row table
    under a chart that wanted its natural height would run off the sheet."""
    panel = {
        "title": "Wide", "chart_type": "bar", "display": "chart",
        "orientation": "h", "x": "K", "y": "N", "columns": ["K", "N"],
        "rows": [[f"K{i}", i] for i in range(40)], "row_count": 40,
    }
    doc = render([panel])
    height = _chart_height(doc, "Wide")
    assert 0 < height < 600, f"chart took {height:.0f}pt"


def test_a_section_heading_arrives_with_its_own_panels():
    """A page per panel meant every heading was written at the foot of the
    PREVIOUS panel's page, then its panels opened a new one — so "Liens" sat
    alone under a lien-free page."""
    panels = [
        bar_panel(title="Sales Chart", section="Sales", display="chart"),
        bar_panel(title="Lien Chart", section="Liens", display="chart"),
    ]
    doc = render(panels)
    for page in doc:
        text = page.get_text()
        if "Liens" not in text:
            continue
        assert "Lien Chart" in text, "the heading was stranded without its panel"
        break
    else:
        raise AssertionError("the Liens heading never reached the document")


def test_dropped_columns_are_reported_rather_than_lost():
    """A table capped at eight columns looked complete and was not."""
    columns = [f"COL_{i}" for i in range(12)]
    panel = {
        "title": "Wide", "chart_type": None, "display": "table",
        "columns": columns, "rows": [list(range(12))], "row_count": 1,
    }
    text = all_text(render([panel]))
    assert "first 8 of 12 columns" in text


def test_a_long_header_wraps_instead_of_losing_a_word():
    """Capping at two lines turned TOTAL_INVOICE_AMOUNT into "TOTAL_ INVOICE_",
    which does not read as shortened - it reads as a different column."""
    from app.engine.dashboard_export import _wrap_header

    lines = _wrap_header("TOTAL_INVOICE_AMOUNT", avail=48.0, size=7.5)
    joined = "".join(lines)
    assert "AMOUNT" in joined or joined.endswith("..."), lines
    assert len(lines) <= 3


def test_a_heatmap_with_many_rows_is_capped_and_says_so():
    """Sixty clients in a fixed-height block gives two points per row: the
    labels collapse into a smear and the cells vanish."""
    clients = [f"Client {i:02d}" for i in range(60)]
    buckets = ["Current", "0-30", "31-60", "61-90"]
    rows = [[b, c, 1000.0 * (i + 1)] for i, c in enumerate(clients) for b in buckets]
    text = all_text(render([{
        "title": "Aging", "chart_type": "heatmap", "display": "chart",
        "x": "BUCKET", "y": "BALANCE", "color": "CLIENT",
        "columns": ["BUCKET", "CLIENT", "BALANCE"],
        "rows": rows, "row_count": len(rows),
    }]))
    assert "of 60" in text and "not shown" in text


def test_a_panel_without_resolvable_columns_is_not_charted():
    panel = bar_panel(x="MISSING_COLUMN")
    assert pdf_charts.extract(panel) is None


def test_an_errored_panel_reports_the_error_and_keeps_going():
    doc = render([{"title": "Broken", "error": "SQL failed to compile",
                   "columns": [], "rows": []},
                  bar_panel(title="Healthy")])
    text = all_text(doc)
    assert "SQL failed to compile" in text
    assert "Healthy" in text


def test_kpi_panels_become_a_band_of_tiles():
    """Headline numbers led as a bulleted "metric: value" list, which buried the
    figures a reader opens the report for."""
    panel = {
        "title": "Headline", "chart_type": "kpi", "display": "kpi",
        "columns": ["METRIC", "VALUE", "FORMAT"],
        "rows": [["Total Rent", 2_200_000, "currency"], ["Occupancy", 94, "percent"]],
        "row_count": 2,
    }
    doc = render([panel])
    text = all_text(doc)
    assert "Total Rent" in text and "Occupancy" in text
    assert "- Total Rent:" not in text, "still rendering the old bullet list"
    # A tile is a filled rounded box behind each figure.
    assert drawing_count(doc) >= 2


# --- a capped panel is not a complete one ------------------------------------


def _capped_table(**kw) -> dict:
    """100 rows back from a warehouse that stops at 100: nothing about the
    numbers themselves says any are missing."""
    panel = {
        "title": "Weekly Volume", "chart_type": None, "display": "table",
        "columns": ["WEEK", "SHIPMENTS"],
        "rows": [[f"2025-W{i}", 100 + i] for i in range(1, 101)],
        "row_count": 100,
        "truncated": True,
    }
    panel.update(kw)
    return panel


def test_a_capped_panel_says_so():
    """row_count EQUALS the rows handed over when the cap bites, so the export's
    "X of Y rows" test can never fire — and the PDF outlives the screen it came
    from, with no way to ask whether June was the end of the data or the end of
    the query."""
    text = all_text(render([_capped_table()]))
    assert "row limit" in text


def test_an_uncapped_panel_stays_quiet():
    """The note is a warning; on a panel that returned everything it is noise."""
    text = all_text(render([_capped_table(truncated=False, rows=[["2025-W1", 101]],
                                          row_count=1)]))
    assert "row limit" not in text


def test_a_panel_from_before_the_flag_stays_quiet():
    """Saved runs and older clients carry no such key at all."""
    panel = _capped_table()
    del panel["truncated"]
    assert "row limit" not in all_text(render([panel]))

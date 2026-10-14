"""The chart shapes that were drawing wrongly, or not at all.

Each test stands for a specific defect seen in a real export.
"""

from __future__ import annotations

import pymupdf

from app.engine.dashboard_export import build_dashboard_pdf


def render(panels) -> pymupdf.Document:
    return pymupdf.open(stream=build_dashboard_pdf("T", panels), filetype="pdf")


def all_text(doc) -> str:
    return "\n".join(page.get_text() for page in doc)


def fill_colours(doc) -> set:
    out = set()
    for page in doc:
        for drawing in page.get_drawings():
            if drawing.get("fill") is not None:
                out.add(tuple(round(c, 3) for c in drawing["fill"]))
    return out


def panel(**kw):
    base = {
        "title": "P", "chart_type": None, "x": None, "y": None, "color": None,
        "size": None, "orientation": None, "display": "chart",
        "columns": [], "rows": [], "row_count": 0,
    }
    base.update(kw)
    base["row_count"] = len(base["rows"])
    return base


BUBBLE = panel(
    title="Margin vs take rate", chart_type="bubble",
    x="TAKE_RATE", y="MARGIN", size="REVENUE", color="SEGMENT",
    columns=["SEGMENT", "TAKE_RATE", "MARGIN", "REVENUE"],
    rows=[["Retail", 4.71, 18.2, 2400000], ["Industrial", 4.83, 15.6, 3100000],
          ["Pharma", 5.02, 24.1, 1200000], ["Auto", 4.95, 12.9, 2800000],
          ["Food", 5.18, 20.3, 900000], ["Chemicals", 4.77, 16.8, 1500000],
          ["Textiles", 5.09, 11.4, 700000], ["Electronics", 4.88, 22.7, 2100000]],
)


def circles(doc) -> list:
    """Filled circles only.

    Counting fill COLOURS instead was the first attempt and it proved nothing:
    the legend draws one swatch per series, so eight colours reached the page
    whether or not any point did. Curve items exclude those rectangles.
    """
    out = []
    for page in doc:
        for d in page.get_drawings():
            if d.get("fill") is None or not d["items"]:
                continue
            if all(item[0] == "c" for item in d["items"]):
                out.append(d)
    return out


def test_a_bubble_chart_plots_every_row_not_one_point():
    """It read `data.series[0]`, and with a colour column `extract()` pivots N
    rows into N series each holding ONE value and N-1 Nones — so eight segments
    drew as a single dot."""
    drawn = circles(render([BUBBLE]))
    assert len(drawn) == len(BUBBLE["rows"]), (
        f"{len(drawn)} points drawn for {len(BUBBLE['rows'])} rows"
    )


def test_the_axis_covers_the_data_it_plots():
    """The range came from that lone point, so ticks landed at 5/5.25/5.5/5.75
    while the data spanned 4.71-5.18 — most of it outside the plot.

    Asserted on the drawn points, not on page text: the table underneath prints
    "4.83" too, so a text search passes even when the chart is one dot.
    """
    drawn = circles(render([BUBBLE]))
    xs = [d["rect"].x0 + d["rect"].width / 2 for d in drawn]
    assert max(xs) - min(xs) > 100, (
        "the points are bunched — the axis was scaled from a subset of them"
    )


def test_bubble_size_is_read_rather_than_ignored():
    """Every bubble was 2.2pt regardless of the size column."""
    with_size = render([BUBBLE])
    without = render([panel(**{**BUBBLE, "size": None, "title": "No size"})])

    def radii(doc):
        """Widths of the filled CIRCLES only.

        Curve items, never rects: a legend swatch is a small filled square and
        would otherwise be counted as a bubble.
        """
        out = []
        for page in doc:
            for d in page.get_drawings():
                if d.get("fill") is None:
                    continue
                if not d["items"] or any(item[0] != "c" for item in d["items"]):
                    continue
                out.append(round(d["rect"].width, 2))
        return set(out)

    assert len(radii(with_size)) > 1, "all bubbles came out the same size"
    assert len(radii(without)) <= 1, "points varied with no size column declared"


FUNNEL = panel(
    title="Stages", chart_type="funnel", x="STAGE", y="COUNT",
    columns=["STAGE", "COUNT"],
    # Real production data: it WIDENS at Departed, and the last stage is three
    # orders of magnitude smaller than the first.
    rows=[["Quoted", 175800000], ["Booked", 71500000], ["Departed", 159700000],
          ["Delivered", 58200000], ["Invoiced", 51000000], ["Settled", 120000]],
)


def test_a_funnel_that_widens_again_still_draws():
    """Trapezoids joining one stage's width to the next cross into a bow-tie
    the moment the data is not monotonic, and real funnels are not."""
    doc = render([FUNNEL])
    assert "shown as a table" not in all_text(doc)
    bars = [d for page in doc for d in page.get_drawings()
            if d.get("fill") is not None and d["rect"].width > 20]
    assert len(bars) >= 6, f"only {len(bars)} stage bars drawn"


def test_the_smallest_stage_is_still_visible():
    """120K against 175.8M is 0.07% — without a floor it is a hairline."""
    doc = render([FUNNEL])
    widths = sorted(d["rect"].width for page in doc for d in page.get_drawings()
                    if d.get("fill") is not None and d["rect"].height > 8)
    assert widths and widths[0] > 6, (
        f"narrowest stage was {widths[0] if widths else 0:.1f}pt — invisible"
    )



def test_a_funnel_is_drawn_largest_stage_first():
    """As the dashboard draws it: widest at the top, narrowing down, whatever
    order the query returned the stages in."""
    doc = render([FUNNEL])
    bars = sorted(
        (d["rect"] for page in doc for d in page.get_drawings()
         if d.get("fill") is not None and d["rect"].height > 8 and d["rect"].width > 6),
        key=lambda r: r.y0,
    )[:6]
    widths = [round(r.width, 1) for r in bars]
    assert widths == sorted(widths, reverse=True), f"stage widths top to bottom: {widths}"

HEATMAP = panel(
    title="Aging", chart_type="heatmap", x="BUCKET", y="OUTSTANDING", color="CLIENT",
    columns=["CLIENT", "BUCKET", "OUTSTANDING"],
    rows=[["Meridian", "0-30", 420000], ["Meridian", "31-60", 180000],
          ["Nordwind", "0-30", 310000], ["Nordwind", "31-60", 240000]],
)


def test_heatmap_cells_carry_their_value():
    """A cell with only a colour tells a reader which is darkest and not what
    any of them is worth."""
    text = all_text(render([HEATMAP]))
    assert "420K" in text, "cell values are not printed"
    assert "310K" in text


def test_a_row_with_no_category_is_excluded_and_reported():
    """GROUPING SETS returns subtotal rows with one axis NULL. Admitted to the
    grid they build an unlabelled band belonging to nothing."""
    with_subtotal = panel(**{
        **HEATMAP,
        "rows": HEATMAP["rows"] + [[None, "0-30", 730000], ["Meridian", None, 600000]],
    })
    text = all_text(render([with_subtotal]))
    assert "no category excluded" in text, "subtotal rows were dropped silently"
    assert "2 rows" in text


def test_chart_labels_survive_the_latin_1_fonts():
    """The base-14 fonts are Latin-1. The tables were sanitised; the charts
    never were, so a euro sign in a category reached the page as a box.

    Asserted on `_fit` itself — every label a chart draws goes through it. A
    whole-page text search cannot tell a sanitised chart label from the table
    cell below it, which already said EUR.
    """
    from app.engine import pdf_charts

    assert pdf_charts._fit("€ zone", 500, 7.0) == "EUR  zone"
    # $ £ ¥ ¢ ARE in Latin-1 and must survive untouched.
    for symbol in ("$", "£", "¥", "¢"):
        assert symbol in pdf_charts._fit(f"{symbol} total", 500, 7.0), symbol


def test_the_currency_fallbacks_reach_the_page():
    """And end to end, since the substitution table is shared with the tables."""
    doc = render([panel(
        title="By currency", chart_type="bar", x="CCY", y="AMOUNT",
        columns=["CCY", "AMOUNT"],
        rows=[["€ zone", 100], ["£ sterling", 90], ["$ dollar", 80]],
    )])
    text = all_text(doc)
    assert "EUR" in text and "€" not in text
    assert "£" in text, "the pound sign is Latin-1 and should pass through"


# --- area_line: the target must not be filled over the measure ---------------

def _area_line_panel():
    return panel(
        title="OTIF vs target",
        chart_type="area_line",
        x="MONTH",
        y=["OTIF_PCT", "TARGET_PCT"],
        columns=["MONTH", "OTIF_PCT", "TARGET_PCT"],
        rows=[["Feb", 86.0, 92.0], ["Mar", 87.5, 92.0], ["Apr", 88.0, 92.0],
              ["May", 86.5, 92.0], ["Jun", 89.0, 92.0], ["Jul", 90.0, 92.0]],
    )


def _filled_areas(doc) -> list:
    """Closed filled polygons, which is what an area's fill is drawn as."""
    out = []
    for page in doc:
        for drawing in page.get_drawings():
            if drawing.get("fill") is None:
                continue
            if any(item[0] == "l" for item in drawing["items"]):
                out.append(drawing)
    return out


def test_only_the_measure_is_filled_not_the_target():
    """The defect this guards: _draw_lines fills EVERY series, so a flat target
    at the top of the range lays an opaque block across the whole plot and the
    measure underneath it disappears."""
    doc = render([_area_line_panel()])
    areas = _filled_areas(doc)
    assert len(areas) == 1, (
        f"expected one filled series, got {len(areas)} — the reference line is "
        "being filled as well"
    )


def test_the_target_is_drawn_dashed():
    doc = render([_area_line_panel()])
    dashed = [
        d for page in doc for d in page.get_drawings()
        if d.get("dashes") and d["dashes"] != "[] 0"
    ]
    assert dashed, "the reference line is not dashed, so it reads as a measurement"


def test_the_measure_still_reaches_the_page():
    """A fill-only fix that also dropped the stroke would pass the test above."""
    doc = render([_area_line_panel()])
    strokes = [
        d for page in doc for d in page.get_drawings()
        if d.get("color") is not None and any(i[0] == "l" for i in d["items"])
    ]
    assert len(strokes) >= 2, "expected a stroked measure and a stroked target"

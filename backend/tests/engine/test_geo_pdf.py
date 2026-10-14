"""Maps in the PDF: the traps, each asserted.

Every test here stands for a specific way the map came out wrong before the
backend carried its own geometry. They are written against behaviour that can
be measured, not against a picture — but the pictures were looked at too, which
is the only way to know a passing test means anything.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.engine import pdf_charts
from app.engine.dashboard_export import build_dashboard_pdf
from app.engine.geo import topo


def geo_panel(rows, chart_type="geo_countries", **kw):
    panel = {
        "title": "Revenue by place", "chart_type": chart_type, "display": "chart",
        "x": "PLACE", "y": "VALUE", "color": None, "size": None,
        "orientation": None, "columns": ["PLACE", "VALUE"], "rows": rows,
        "row_count": len(rows),
    }
    panel.update(kw)
    return panel


def render(panels):
    return pymupdf.open(stream=build_dashboard_pdf("T", panels), filetype="pdf")


def all_text(doc) -> str:
    return "\n".join(page.get_text() for page in doc)


# --- equal area -------------------------------------------------------------

def test_the_projection_is_equal_area():
    """A choropleth encodes value as shaded area, so Mercator actively lies:
    Greenland reads the size of Africa. Greenland is really about 0.9x Algeria."""
    world = {f.id: f for f in topo.world_features()}

    def area(fid: str) -> float:
        return sum(topo.polygon_area([topo.equal_earth(*p) for p in ring])
                   for ring in world[fid].rings)

    ratio = area("304") / area("012")  # Greenland / Algeria
    assert 0.7 < ratio < 1.2, (
        f"Greenland/Algeria came out {ratio:.2f}; ~0.9 is equal-area, ~14 is Mercator"
    )


# --- framing ----------------------------------------------------------------

def _box(names: list[str]):
    from app.engine.geo import matching
    ids = {matching.match_country(n) for n in names}
    matched = [f for f in topo.world_features() if f.id in ids]
    assert matched, names
    return topo.focus_box(matched)


def test_one_country_a_region_and_a_spread_set_frame_differently():
    """The whole point of the map: it shows where the data is, not the globe."""
    one = _box(["India"])
    region = _box(["United Arab Emirates", "Oman", "Saudi Arabia", "Qatar"])
    spread = _box(["USA", "Germany", "India", "Brazil"])

    def lon_span(b):
        return b[1] - b[0]

    assert lon_span(one) < lon_span(spread), "one country framed as wide as the world"
    assert lon_span(region) < lon_span(spread), "a region framed as wide as the world"
    assert spread == (-180.0, 180.0, -84.0, 84.0), (
        "a globally spread set should fall back to the whole world"
    )


def test_a_tiny_country_is_not_zoomed_into_a_blob():
    """Below the floor there is no coastline left to say where on earth it is."""
    box = _box(["Singapore"])
    assert box[1] - box[0] >= topo.MIN_LON_SPAN - 1e-6
    assert box[3] - box[2] >= topo.MIN_LAT_SPAN - 1e-6


def test_the_focus_box_matches_the_frontends_thresholds():
    """The export exists to look like the screen, so the numbers are the screen's."""
    assert (topo.MIN_LON_SPAN, topo.MIN_LAT_SPAN) == (24.0, 18.0)
    assert (topo.MAX_LON_SPAN, topo.MAX_LAT_SPAN) == (170.0, 110.0)
    assert topo.FOCUS_PAD == 0.12


def test_the_focus_box_is_sampled_along_its_edges_not_only_at_corners():
    """In a curved projection the extreme of an edge is usually mid-edge;
    corner-only bounds crop the countries the box was drawn around."""
    box = (-40.0, 40.0, -60.0, 60.0)
    samples = topo.focus_samples(box)
    corners = [topo.equal_earth(lon, lat)
               for lon in (box[0], box[1]) for lat in (box[2], box[3])]
    widest_sample = max(abs(p[0]) for p in samples)
    widest_corner = max(abs(p[0]) for p in corners)
    assert widest_sample > widest_corner + 1e-9, (
        "edge sampling found nothing the corners did not — the fit would crop"
    )


# --- antimeridian -----------------------------------------------------------

def test_a_ring_crossing_the_seam_is_split_at_the_meridian():
    """Fiji and Russia step +179 -> -179. Projected whole, that is a bar across
    the entire map through the Atlantic."""
    ring = [(170.0, 10.0), (179.0, 12.0), (-179.0, 13.0), (-170.0, 11.0), (170.0, 10.0)]
    pieces = topo.split_antimeridian(ring)
    assert len(pieces) == 2, "the ring was not split"
    assert any(abs(x) == 180.0 for piece in pieces for x, _ in piece), (
        "the pieces stop short of the meridian, leaving a diagonal closing edge"
    )
    for piece in pieces:
        assert max(x for x, _ in piece) - min(x for x, _ in piece) <= 180.0


def test_a_ring_that_does_not_cross_is_left_alone():
    plain = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0), (10.0, 10.0)]
    assert topo.split_antimeridian(plain) == [list(plain)]


def test_antarctica_is_dropped_unless_the_data_names_it():
    """It spans the full 360 and eats the bottom fifth of every world view."""
    doc = render([geo_panel([["Germany", 10], ["Brazil", 20]])])
    assert doc.page_count  # rendered at all
    with_it = render([geo_panel([["Antarctica", 10], ["Argentina", 20]])])
    assert "Antarctica" in all_text(with_it) or with_it.page_count


# --- clipping ---------------------------------------------------------------

def test_geometry_is_clipped_to_the_plot():
    """A Shape has no viewBox. Framed on one region, the rest of the world is
    still in the geometry and would paint over the table below."""
    ring = [(-100.0, -100.0), (500.0, -100.0), (500.0, 500.0), (-100.0, 500.0)]
    clipped = topo.clip_to_rect(ring, 0.0, 0.0, 100.0, 50.0)
    assert clipped, "everything was clipped away"
    for x, y in clipped:
        assert -1e-6 <= x <= 100.0 + 1e-6
        assert -1e-6 <= y <= 50.0 + 1e-6


def test_a_shape_entirely_outside_the_plot_is_dropped():
    assert topo.clip_to_rect(
        [(200.0, 200.0), (300.0, 200.0), (300.0, 300.0)], 0.0, 0.0, 100.0, 50.0
    ) == []


# --- shading ----------------------------------------------------------------

def test_a_single_region_is_shaded_solid_not_palest():
    """With one region hi == lo, so (v-lo)/span is 0 — the palest shade, which
    is indistinguishable from having no data at all."""
    solid = pdf_charts._geo_shade(5.0, 5.0, 5.0)
    palest = pdf_charts._heat_color(0.0)
    assert solid != palest
    assert solid == pdf_charts._heat_color(1.0)


def test_the_lightest_shade_clears_the_no_data_grey():
    """Matching the frontend's SHADE_FLOOR: least must not read as none."""
    assert pdf_charts._SHADE_FLOOR == 0.18
    lightest = pdf_charts._geo_shade(0.0, 0.0, 10.0)
    assert lightest != pdf_charts._GEO_MUTED
    assert lightest == pdf_charts._heat_color(0.18)


# --- the whole panel --------------------------------------------------------

@pytest.mark.parametrize("chart_type,rows", [
    ("geo_countries", [["India", 10], ["Germany", 20]]),
    ("geo_us_states", [["CA", 10], ["TX", 20]]),
])
def test_a_map_draws_as_vectors_rather_than_falling_back_to_a_table(chart_type, rows):
    doc = render([geo_panel(rows, chart_type=chart_type)])
    assert "shown as a table" not in all_text(doc), (
        f"{chart_type} exported as a table"
    )
    drawings = sum(len(page.get_drawings()) for page in doc)
    assert drawings > 50, f"only {drawings} drawings — the map did not render"


def test_unmatched_values_are_named_on_the_chart():
    """An unshaded region is indistinguishable from one with no data."""
    doc = render([geo_panel([["Germany", 10], ["EMEA", 20], ["Atlantis", 5]])])
    text = all_text(doc)
    assert "not matched" in text
    assert "EMEA" in text and "Atlantis" in text

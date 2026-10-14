"""Boundary geometry for the PDF: decode TopoJSON, project it, frame it, clip it.

The browser gets all of this from d3-geo and a `viewBox`. A PDF has neither, so
each piece is here, and each one is a trap that was hit:

* **Antimeridian.** Fiji and Russia have rings that step +179 to -179. Projected
  naively that is a band straight across the map through the Atlantic. d3 clips
  it; here the rings are split at the seam. Antarctica legitimately spans the
  full 360 and is dropped unless the data names it — otherwise it eats the
  bottom fifth of every world view.
* **Clipping.** An SVG viewBox clips for free; a `Shape` does not. Framed on one
  region, the rest of the world is still in the geometry and would draw over the
  table below, so every ring is clipped to the plot with Sutherland-Hodgman.
* **Equal area.** A choropleth encodes value as shaded area, so Mercator — where
  Greenland reads as the size of Africa — actively misleads. Equal Earth keeps
  areas true; `tests/engine/test_geo_pdf.py` asserts Greenland comes out about
  0.9x Algeria rather than 14x.
* **Edge sampling.** The focus box is projected by sampling ALONG its edges, not
  just at its corners: in a curved projection the extreme of an edge is usually
  somewhere in the middle of it, and corner-only bounds crop the very countries
  the box was drawn around.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

HERE = Path(__file__).parent
WORLD_FILE = HERE / "countries-50m.json"
US_FILE = HERE / "states-albers-10m.json"

#: Mirrors `focusArea` in frontend/src/components/dashboard/geo-panel.tsx. The
#: export exists to look like the screen, so these are the screen's numbers.
MIN_LON_SPAN = 24.0
MIN_LAT_SPAN = 18.0
MAX_LON_SPAN = 170.0
MAX_LAT_SPAN = 110.0
FOCUS_PAD = 0.12
EDGE_STEPS = 10

#: Antarctica's ISO numeric. Present in the atlas, almost never the subject.
ANTARCTICA = "010"

Point = tuple[float, float]
Ring = list[Point]


@dataclass(frozen=True)
class Feature:
    """One country or state: its id, its name, and its rings in source units."""

    id: str
    name: str
    rings: tuple[tuple[Point, ...], ...]


# --- TopoJSON ---------------------------------------------------------------

def _decode_arcs(topology: dict) -> list[list[Point]]:
    """Delta-decode every arc and apply the topology's quantisation transform."""
    transform = topology.get("transform")
    if transform:
        (sx, sy) = transform["scale"]
        (tx, ty) = transform["translate"]
    else:
        sx = sy = 1.0
        tx = ty = 0.0

    arcs: list[list[Point]] = []
    for arc in topology["arcs"]:
        x = y = 0.0
        out: list[Point] = []
        for dx, dy in arc:
            # Quantised topologies store every point as a delta from the last.
            x += dx
            y += dy
            out.append((x * sx + tx, y * sy + ty))
        arcs.append(out)
    return arcs


def _ring(arc_indexes: Sequence[int], arcs: list[list[Point]]) -> Ring:
    """Stitch arc indexes into one closed ring.

    A negative index means the arc runs backwards, and is encoded as ~i — so
    arc -1 is arcs[0] reversed, not arcs[-1].
    """
    points: Ring = []
    for i in arc_indexes:
        arc = arcs[~i] if i < 0 else arcs[i]
        segment = list(reversed(arc)) if i < 0 else arc
        # Consecutive arcs share an endpoint; keeping both leaves a duplicate
        # vertex that shows as a nick when the ring is stroked.
        points.extend(segment[1:] if points else segment)
    return points


def _features(topology: dict, object_key: str) -> list[Feature]:
    arcs = _decode_arcs(topology)
    out: list[Feature] = []
    for geom in topology["objects"][object_key]["geometries"]:
        gtype = geom.get("type")
        if gtype == "Polygon":
            rings = [_ring(r, arcs) for r in geom["arcs"]]
        elif gtype == "MultiPolygon":
            rings = [_ring(r, arcs) for poly in geom["arcs"] for r in poly]
        else:
            continue
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            continue
        out.append(
            Feature(
                id=str(geom.get("id") or ""),
                name=str((geom.get("properties") or {}).get("name") or ""),
                rings=tuple(tuple(r) for r in rings),
            )
        )
    return out


_world_cache: list[Feature] | None = None
_us_cache: list[Feature] | None = None


def world_features() -> list[Feature]:
    """Countries in lon/lat. Parsed once — the file is three quarters of a MB."""
    global _world_cache
    if _world_cache is None:
        topology = json.loads(WORLD_FILE.read_text(encoding="utf-8"))
        _world_cache = _features(topology, "countries")
    return _world_cache


def us_features() -> list[Feature]:
    """US states, already projected (Albers with the AK/HI insets baked in)."""
    global _us_cache
    if _us_cache is None:
        topology = json.loads(US_FILE.read_text(encoding="utf-8"))
        _us_cache = _features(topology, "states")
    return _us_cache


# --- projection -------------------------------------------------------------

_A1, _A2, _A3, _A4 = 1.340264, -0.081106, 0.000893, 0.003796
_M = math.sqrt(3.0) / 2.0


def equal_earth(lon: float, lat: float) -> Point:
    """Equal Earth: equal-area, and shaped like a world map rather than a box."""
    lam = math.radians(lon)
    phi = math.radians(max(-89.9, min(89.9, lat)))
    theta = math.asin(max(-1.0, min(1.0, _M * math.sin(phi))))
    t2 = theta * theta
    t6 = t2 * t2 * t2
    x = lam * math.cos(theta) / (
        _M * (_A1 + 3 * _A2 * t2 + t6 * (7 * _A3 + 9 * _A4 * t2))
    )
    y = theta * (_A1 + _A2 * t2 + t6 * (_A3 + _A4 * t2))
    return (x, -y)  # PDF y grows downward


def split_antimeridian(ring: Sequence[Point]) -> list[Ring]:
    """Break a lon/lat ring wherever it steps across the 180th meridian.

    Without this, Fiji and Russia project into a bar spanning the whole map:
    the polygon is drawn from +179 straight back to -179 through the Atlantic.

    Each piece is carried out to the meridian rather than simply cut, so the
    fill closes along the edge of the map. Cutting between the last two points
    instead leaves a diagonal across Chukotka where the closing edge falls.
    """
    if not ring:
        return []
    pieces: list[Ring] = []
    current: Ring = [ring[0]]
    for prev, point in zip(ring, ring[1:]):
        if abs(point[0] - prev[0]) > 180.0:
            # Eastward over +180, or westward over -180.
            east = prev[0] > 0
            span = (point[0] + 360.0 - prev[0]) if east else (point[0] - 360.0 - prev[0])
            t = ((180.0 if east else -180.0) - prev[0]) / (span or 1e-12)
            lat = prev[1] + (point[1] - prev[1]) * t
            current.append((180.0 if east else -180.0, lat))
            pieces.append(current)
            current = [(-180.0 if east else 180.0, lat), point]
        else:
            current.append(point)
    pieces.append(current)
    return [p for p in pieces if len(p) >= 3]


# --- framing ----------------------------------------------------------------

def bounds(features: Iterable[Feature]) -> tuple[float, float, float, float] | None:
    """(west, east, south, north) over every ring."""
    west = south = math.inf
    east = north = -math.inf
    seen = False
    for feature in features:
        for ring in feature.rings:
            for x, y in ring:
                seen = True
                west, east = min(west, x), max(east, x)
                south, north = min(south, y), max(north, y)
    return (west, east, south, north) if seen else None


def focus_box(matched: Sequence[Feature]) -> tuple[float, float, float, float]:
    """The lon/lat box to frame, padded — or the whole world.

    Mirrors the frontend exactly, thresholds included, because a map that frames
    differently on paper than on screen is not the same map.
    """
    whole = (-180.0, 180.0, -84.0, 84.0)
    box = bounds(matched)
    if box is None:
        return whole
    west, east, south, north = box
    lon_span, lat_span = east - west, north - south
    if lon_span > MAX_LON_SPAN or lat_span > MAX_LAT_SPAN:
        return whole

    lon_pad = max(lon_span * FOCUS_PAD, (MIN_LON_SPAN - lon_span) / 2, 2.0)
    lat_pad = max(lat_span * FOCUS_PAD, (MIN_LAT_SPAN - lat_span) / 2, 2.0)
    return (
        max(-180.0, west - lon_pad),
        min(180.0, east + lon_pad),
        max(-84.0, south - lat_pad),
        min(84.0, north + lat_pad),
    )


def focus_samples(box: tuple[float, float, float, float]) -> list[Point]:
    """The focus box as projected points, sampled ALONG its edges.

    Corner-only bounds crop the countries the box was drawn around: in a curved
    projection the extreme of an edge is usually mid-edge, not at its end.
    """
    west, east, south, north = box
    out: list[Point] = []
    for i in range(EDGE_STEPS + 1):
        t = i / EDGE_STEPS
        lon = west + (east - west) * t
        lat = south + (north - south) * t
        out.append(equal_earth(lon, south))
        out.append(equal_earth(lon, north))
        out.append(equal_earth(west, lat))
        out.append(equal_earth(east, lat))
    return out


def fit(points: Sequence[Point], x0: float, y0: float, x1: float, y1: float):
    """A scale/translate mapping `points` into the box, aspect preserved."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    src_w = (max(xs) - min(xs)) or 1e-9
    src_h = (max(ys) - min(ys)) or 1e-9
    scale = min((x1 - x0) / src_w, (y1 - y0) / src_h)
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    ox = (x0 + x1) / 2
    oy = (y0 + y1) / 2

    def transform(pt: Point) -> Point:
        return (ox + (pt[0] - cx) * scale, oy + (pt[1] - cy) * scale)

    return transform


# --- clipping ---------------------------------------------------------------

def clip_to_rect(points: Sequence[Point], x0: float, y0: float,
                 x1: float, y1: float) -> Ring:
    """Sutherland-Hodgman against an axis-aligned rectangle.

    A PDF Shape has no viewBox: framed on the Gulf, Asia and the Americas are
    still in the geometry and would be painted straight over the table below.
    """
    def inside(p: Point, edge: int) -> bool:
        if edge == 0:
            return p[0] >= x0
        if edge == 1:
            return p[0] <= x1
        if edge == 2:
            return p[1] >= y0
        return p[1] <= y1

    def cut(a: Point, b: Point, edge: int) -> Point:
        ax, ay = a
        bx, by = b
        if edge in (0, 1):
            xe = x0 if edge == 0 else x1
            t = (xe - ax) / ((bx - ax) or 1e-12)
            return (xe, ay + (by - ay) * t)
        ye = y0 if edge == 2 else y1
        t = (ye - ay) / ((by - ay) or 1e-12)
        return (ax + (bx - ax) * t, ye)

    output = list(points)
    for edge in range(4):
        if not output:
            return []
        buf: Ring = []
        for a, b in zip(output, output[1:] + output[:1]):
            a_in, b_in = inside(a, edge), inside(b, edge)
            if a_in:
                buf.append(a)
                if not b_in:
                    buf.append(cut(a, b, edge))
            elif b_in:
                buf.append(cut(a, b, edge))
        output = buf
    return output


def polygon_area(points: Sequence[Point]) -> float:
    """Unsigned shoelace area, for deciding what is too small to see."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for (ax, ay), (bx, by) in zip(points, list(points[1:]) + [points[0]]):
        total += ax * by - bx * ay
    return abs(total) / 2.0


def centroid(points: Sequence[Point]) -> Point:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (sum(xs) / len(xs), sum(ys) / len(ys))

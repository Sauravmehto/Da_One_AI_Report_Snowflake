"""Vector charts for the dashboard PDF export.

Drawn with pymupdf's own shape primitives rather than rendered to a bitmap by a
plotting library. Three reasons that matters here:

  * no new dependency — matplotlib pulls numpy and a font cache, which is real
    weight in the container for one feature;
  * the output is vector, so a chart stays sharp at any zoom instead of going
    soft the moment someone prints the report;
  * axis labels stay real text, so the PDF is searchable and a reader can copy
    a category name out of it.

Colours mirror the app's SERIES_COLORS (see frontend/src/lib/dashboard-format.ts)
so an exported chart is recognisably the same chart the user was looking at.
"""

from __future__ import annotations

import math
import re
from contextvars import ContextVar
from typing import Any, Sequence

import pymupdf

#: Same order as the frontend's SERIES_COLORS, as 0-1 RGB triples.
SERIES_COLORS: tuple[tuple[float, float, float], ...] = (
    (0.333, 0.478, 0.635),   # #3B6EA5
    (0.722, 0.427, 0.275),   # #C45C26
    (0.278, 0.62, 0.58),   # #2A9D8F
    (0.792, 0.631, 0.306),   # #E9A825
    (0.420, 0.357, 0.584),   # #6B5B95
    (0.290, 0.486, 0.349),   # #4A7C59
    (0.761, 0.506, 0.486),   # #D4726A
    (0.361, 0.420, 0.478),   # #5C6B7A
)
_OVERFLOW = (0.557, 0.549, 0.529)

AXIS = (0.62, 0.63, 0.66)
GRID = (0.90, 0.91, 0.93)
LABEL = (0.35, 0.36, 0.40)

_FONT = "helv"

#: Rows a heatmap can show before its cells are too thin to read. At the block
#: height a chart gets, this is about the point where a label stops fitting
#: between one row and the next.
MAX_HEATMAP_ROWS = 12


#: Named palettes, mirroring PALETTES in frontend/src/lib/dashboard-format.ts.
#: Same names, same order, same colours — a panel exported to PDF has to come
#: out in the colours the person chose on screen.
PALETTES: dict[str, tuple[tuple[float, float, float], ...]] = {
    "ocean": (
        (0.224, 0.439, 0.584), (0.427, 0.694, 0.694), (0.161, 0.353, 0.49),
        (0.612, 0.718, 0.8), (0.294, 0.608, 0.647), (0.38, 0.482, 0.631),
        (0.698, 0.831, 0.839), (0.204, 0.514, 0.498),
    ),
    "ember": (
        (0.682, 0.4, 0.247), (0.804, 0.659, 0.4), (0.537, 0.282, 0.231),
        (0.765, 0.514, 0.427), (0.647, 0.451, 0.294), (0.459, 0.247, 0.204),
        (0.875, 0.788, 0.624), (0.718, 0.345, 0.294),
    ),
    "meadow": (
        (0.333, 0.482, 0.306), (0.631, 0.69, 0.396), (0.286, 0.533, 0.435),
        (0.725, 0.62, 0.286), (0.467, 0.604, 0.341), (0.231, 0.451, 0.376),
        (0.788, 0.773, 0.592), (0.529, 0.655, 0.475),
    ),
    "slate": (
        (0.345, 0.412, 0.486), (0.596, 0.541, 0.514), (0.282, 0.333, 0.388),
        (0.529, 0.573, 0.624), (0.431, 0.478, 0.427), (0.561, 0.518, 0.565),
        (0.678, 0.659, 0.635), (0.373, 0.471, 0.494),
    ),
}

#: Contrasting colour per palette — see PALETTE_ACCENTS in the frontend.
PALETTE_ACCENTS: dict[str, tuple[float, float, float]] = {
    "ocean": (0.749, 0.533, 0.322),
    "ember": (0.235, 0.49, 0.58),
    "meadow": (0.71, 0.412, 0.286),
    "slate": (0.659, 0.463, 0.396),
    "default": (0.722, 0.427, 0.275),
}

#: The panel currently being drawn, as (zero_baseline, palette).
#:
#: A ContextVar rather than a module global because an export runs in a worker
#: thread and two of them can overlap — a global would let one panel's palette
#: colour another export's chart. Same reasoning as the per-request connection
#: binding in connectors/snowflake/client.py.
_panel_style: ContextVar[tuple[bool, str | None]] = ContextVar(
    "pdf_panel_style", default=(True, None)
)


def accent_color(used: "list[tuple[float, float, float]] | None" = None
                 ) -> tuple[float, float, float]:
    """The colour for one element that must read against a field of others.

    Mirrors accentColor in frontend/src/lib/dashboard-format.ts, including the
    fallback: with no palette named and a single bar series the accent IS the
    second default series colour, so an exported combo keeps the colours it has
    always had.
    """
    used = used or []
    _, palette = _panel_style.get()
    accent = PALETTE_ACCENTS.get(palette or "default")
    if accent and accent not in used:
        return accent
    pool = PALETTES.get(palette or "") or SERIES_COLORS
    for colour in pool:
        if colour not in used:
            return colour
    return _OVERFLOW


def series_color(index: int) -> tuple[float, float, float]:
    _, palette = _panel_style.get()
    chosen = PALETTES.get(palette or "")
    if chosen:
        return chosen[index % len(chosen)]
    if index < len(SERIES_COLORS):
        return SERIES_COLORS[index]
    return _OVERFLOW


def _num(value: Any) -> float | None:
    """A float, or None for anything that is not really a number.

    Booleans are excluded deliberately: `True` is an int in Python and would
    otherwise plot as 1, turning a flag column into a silent bar of ones.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _latin1(text: str) -> str:
    """Chart labels, made safe for the base-14 fonts.

    `dashboard_export` has sanitised TABLE cells for a while; the charts never
    did, so a category, legend entry or axis label carrying a euro sign or a
    smart quote reached the page as a box. Everything a reader sees passes
    through `_fit`, which makes it the one place to put this.
    """
    from app.engine.dashboard_export import _pdf_text

    return _pdf_text(text)


def _fit(text: str, width: float, size: float) -> str:
    """Truncate to fit `width`, with an ellipsis when it does not."""
    text = _latin1(text)
    if pymupdf.get_text_length(text, fontname=_FONT, fontsize=size) <= width:
        return text
    out = text
    while out and pymupdf.get_text_length(out + "...", fontname=_FONT,
                                          fontsize=size) > width:
        out = out[:-1]
    return (out + "...") if out else ""


def _tick_values(low: float, high: float, count: int = 4) -> list[float]:
    """Round tick values INSIDE [low, high].

    The step is snapped to 1, 2, 2.5 or 5 times a power of ten, which is what
    makes an axis read as 0/300K/600K rather than 0/317K/634K. An earlier
    version forced the step to at least 1, so a range like 1.1 to 2.4 produced
    a tick at 3.1 — outside the plot, and drawn over whatever sat above it.
    """
    if high <= low:
        return [low]
    raw = (high - low) / max(count, 1)
    power = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * power
        if step >= raw:
            break

    ticks: list[float] = []
    first = math.ceil(low / step) * step
    v = first
    # Guard the loop on the count as well as the bound: a denormal step would
    # otherwise spin.
    while v <= high + step * 1e-9 and len(ticks) <= count + 2:
        # -0.0 and float dust like 2.9999999996 both read badly on an axis.
        ticks.append(round(v + 0.0, 10))
        v += step
    return ticks or [low, high]


def _fmt_tick(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B"
    if a >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if a >= 1_000:
        return f"{v/1_000:.0f}K"
    if a >= 10:
        return f"{v:.0f}"
    if a == 0:
        return "0"
    return f"{v:.2f}".rstrip("0").rstrip(".")


class ChartData:
    """Categories plus one or more named numeric series."""

    def __init__(self, categories: list[str], series: list[tuple[str, list[float | None]]]):
        self.categories = categories
        self.series = series

    @property
    def ok(self) -> bool:
        return bool(self.categories) and any(
            any(v is not None for v in values) for _, values in self.series
        )


def extract(panel: dict[str, Any], *, single_series: bool = False) -> ChartData | None:
    """Pull categories and series out of a panel's rows, using its saved config.

    `single_series` is for the shapes that can only show one measure — a pie is
    one ring over its categories. Without it, a panel that sets BOTH x and a
    colour column (a planner does this routinely) goes down the long-form pivot
    below, every group ends up holding one value and the rest None, and the pie
    draws its first group as a single wedge covering the whole circle.

    Returns None when the panel does not describe something chartable — the
    caller then falls back to a table, which is what the app does too.
    """
    columns = [str(c) for c in (panel.get("columns") or [])]
    rows = panel.get("rows") or []
    if not columns or not rows:
        return None

    index_of = {c.upper(): i for i, c in enumerate(columns)}

    def idx(name: Any) -> int | None:
        if name is None:
            return None
        return index_of.get(str(name).upper())

    xi = idx(panel.get("x"))
    if xi is None:
        return None

    raw_y = panel.get("y")
    y_names = raw_y if isinstance(raw_y, list) else [raw_y]
    y_idx = [(str(n), idx(n)) for n in y_names if idx(n) is not None]
    if not y_idx:
        return None

    categories = [str(r[xi]) if xi < len(r) and r[xi] is not None else ""
                  for r in rows]

    if single_series:
        name, vi = y_idx[0]
        return ChartData(
            categories,
            [(name, [_num(r[vi]) if vi < len(r) else None for r in rows])],
        )

    ci = idx(panel.get("color"))
    # A colour column with ONE value column means the rows are long-form: each
    # distinct colour becomes its own series, and the x values collapse to the
    # distinct categories. With several value columns the series already come
    # from those, and the colour column is ignored (matching stacked_bar).
    if ci is not None and len(y_idx) == 1:
        _, vi = y_idx[0]
        cats: list[str] = []
        for c in categories:
            if c not in cats:
                cats.append(c)
        groups: list[str] = []
        for r in rows:
            g = str(r[ci]) if ci < len(r) and r[ci] is not None else ""
            if g not in groups:
                groups.append(g)
        table = {g: [None] * len(cats) for g in groups}
        for r, cat in zip(rows, categories):
            g = str(r[ci]) if ci < len(r) and r[ci] is not None else ""
            table[g][cats.index(cat)] = _num(r[vi]) if vi < len(r) else None
        return ChartData(cats, [(g, table[g]) for g in groups])

    series = [
        (name, [_num(r[i]) if i < len(r) else None for r in rows])
        for name, i in y_idx
    ]
    return ChartData(categories, series)


def _draw_legend(page, shape, rect: pymupdf.Rect, names: Sequence[str]) -> float:
    """Legend row along the top of `rect`. Returns the height it used."""
    if len(names) < 2:
        return 0.0
    size, x, y = 7.0, rect.x0, rect.y0
    for i, name in enumerate(names):
        swatch = pymupdf.Rect(x, y + 1.5, x + 6, y + 7.5)
        shape.draw_rect(swatch)
        shape.finish(fill=series_color(i), color=None)
        label = _fit(str(name), 90, size)
        page.insert_text((x + 9, y + 7), label, fontsize=size,
                         fontname=_FONT, color=LABEL)
        x += 9 + pymupdf.get_text_length(label, fontname=_FONT, fontsize=size) + 12
        if x > rect.x1 - 60:
            break
    # Clears the plot's topmost value label, which sits on the first gridline.
    return 16.0


def _draw_frame(page, shape, plot: pymupdf.Rect, lo: float, hi: float,
                horizontal: bool = False) -> None:
    """Gridlines and value labels along the value axis."""
    size = 6.5
    for t in _tick_values(lo, hi):
        if hi == lo:
            continue
        frac = (t - lo) / (hi - lo)
        # Belt and braces: nothing is drawn outside the plot, whatever the
        # tick generator produced.
        if frac < -1e-9 or frac > 1 + 1e-9:
            continue
        if horizontal:
            x = plot.x0 + frac * plot.width
            shape.draw_line(pymupdf.Point(x, plot.y0), pymupdf.Point(x, plot.y1))
            shape.finish(color=GRID, width=0.5)
            page.insert_text((x - 8, plot.y1 + 9), _fmt_tick(t), fontsize=size,
                             fontname=_FONT, color=LABEL)
        else:
            y = plot.y1 - frac * plot.height
            shape.draw_line(pymupdf.Point(plot.x0, y), pymupdf.Point(plot.x1, y))
            shape.finish(color=GRID, width=0.5)
            page.insert_text((plot.x0 - 26, y + 2), _fmt_tick(t), fontsize=size,
                             fontname=_FONT, color=LABEL)


def _bar_base(lo: float, hi: float) -> float:
    """Where a bar starts, given the value axis actually drawn.

    Zero whenever the axis spans it, which is every axis anchored at zero and
    any fitted one that happens to cross it. Otherwise the nearer edge of the
    axis, so the bar begins at the foot of the plot instead of off the page.
    """
    return min(max(0.0, lo), hi)


def _value_range(data: ChartData, stacked: bool) -> tuple[float, float]:
    values: list[float] = []
    if stacked:
        for i in range(len(data.categories)):
            total = sum((s[1][i] or 0.0) for s in data.series if i < len(s[1]))
            values.append(total)
    else:
        for _, vals in data.series:
            values.extend(v for v in vals if v is not None)
    if not values:
        return 0.0, 1.0
    lo, hi = min(values), max(values)

    # A panel may opt out of the zero baseline (see zero_baseline in
    # dashboards._normalise_style). Stacked bars never do: their segments only
    # read as parts of a whole because the stack starts at zero, which is the
    # same rule the editor applies by not offering the option for them.
    zero_baseline, _ = _panel_style.get()
    if not zero_baseline and not stacked:
        if hi == lo:
            pad = max(abs(hi) * 0.1, 1.0)
        else:
            pad = (hi - lo) * 0.08
        return lo - pad, hi + pad

    if lo > 0:
        lo = 0.0          # bars read as proportions; a floating baseline lies
    if hi < 0:
        hi = 0.0
    if hi == lo:
        hi = lo + 1.0
    return lo, hi


def _draw_bars(page, shape, plot, data: ChartData, stacked: bool,
               horizontal: bool) -> None:
    lo, hi = _value_range(data, stacked)
    _draw_frame(page, shape, plot, lo, hi, horizontal)
    span = hi - lo
    n_cat = len(data.categories)
    n_ser = len(data.series)
    if n_cat == 0:
        return

    thickness = (plot.height if horizontal else plot.width) / n_cat
    pad = thickness * 0.18
    inner = max(thickness - 2 * pad, 1.0)
    bar_w = inner if stacked else max(inner / max(n_ser, 1), 0.8)

    for c in range(n_cat):
        base = (plot.y0 if horizontal else plot.x0) + c * thickness + pad
        run_pos = 0.0
        for s, (_, vals) in enumerate(data.series):
            v = vals[c] if c < len(vals) else None
            if v is None:
                continue
            if stacked:
                start, end = run_pos, run_pos + v
                run_pos = end
            else:
                # A fitted axis need not include zero. Anchoring the bar at an
                # off-plot zero draws it past the bottom of its own plot and
                # over whatever the page puts next; the axis floor is where the
                # bar visually starts in that case.
                start, end = _bar_base(lo, hi), v
            if horizontal:
                x0 = plot.x0 + ((start - lo) / span) * plot.width
                x1 = plot.x0 + ((end - lo) / span) * plot.width
                y0 = base if stacked else base + s * bar_w
                r = pymupdf.Rect(min(x0, x1), y0, max(x0, x1), y0 + bar_w)
            else:
                y0 = plot.y1 - ((start - lo) / span) * plot.height
                y1 = plot.y1 - ((end - lo) / span) * plot.height
                x0 = base if stacked else base + s * bar_w
                r = pymupdf.Rect(x0, min(y0, y1), x0 + bar_w, max(y0, y1))
            if r.width > 0 and r.height > 0:
                shape.draw_rect(r)
                shape.finish(fill=series_color(s), color=None)

    _draw_categories(page, data.categories, plot, thickness, horizontal)


#: A date column stored as the first day of its period. Monthly and yearly SQL
#: produces exactly that, so an axis reads "2025-01-01" where "Jan 2025" was
#: meant — and the day number eats the width that would have fit more labels.
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
def _date_granularity(values: list[str]) -> str | None:
    """"year", "month", "day", or None when the column is not wholly dates.

    Mirrors detectDateGranularity in the frontend so the export and the screen
    label an axis the same way. Quarterly is deliberately NOT inferred: monthly
    data covering Jan/Apr/Jul/Oct looks identical to it, and a wrong "Q1" is
    worse than a right "Jan".
    """
    seen = 0
    first_of_month = True
    first_of_year = True
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        match = _ISO_DATE.match(text)
        if not match:
            return None
        seen += 1
        _, month, day = match.group(1), match.group(2), match.group(3)
        if day != "01":
            first_of_month = first_of_year = False
        elif month != "01":
            first_of_year = False
    if not seen:
        return None
    if first_of_year:
        return "year"
    if first_of_month:
        return "month"
    return "day"


def _date_label(value: str, grain: str | None) -> str:
    """Numeric MM-YYYY rather than "Feb 2025".

    A reader does not have to map a month name back to its position, which is
    the whole job an axis label has at a glance. Mirrors formatDateLabel in the
    frontend so the export and the screen read identically.
    """
    if not grain:
        return str(value)
    match = _ISO_DATE.match(str(value).strip())
    if not match:
        return str(value)
    year, month, day = match.group(1), match.group(2), match.group(3)
    if grain == "year":
        return year
    if grain == "month":
        return f"{month}-{year}"
    # Year dropped on a daily axis: it repeats on every tick and is the noise
    # this exists to remove. Month first, matching the monthly label's order.
    return f"{month}-{day}"


def _draw_categories(page, categories: list[str], plot, thickness: float,
                     horizontal: bool) -> None:
    """Category labels, thinned out when they would collide."""
    size = 6.5
    # Converted before measuring: the thinning budget should be spent on the
    # label a reader will see, not on the raw timestamp behind it.
    grain = _date_granularity(categories)
    if grain:
        categories = [_date_label(c, grain) for c in categories]
    # One label per category only while they fit; otherwise show every nth.
    step = 1
    if not horizontal:
        while thickness * step < 26 and step < len(categories):
            step += 1

    # Right edge of the last label drawn, so a label is dropped rather than
    # printed on top of its neighbour. Clamping alone is not enough: pulling
    # the final label inside the plot pushes it onto the one before it.
    occupied = float("-inf")
    for i, cat in enumerate(categories):
        if i % step:
            continue
        text = _fit(str(cat), (thickness * step) - 2 if not horizontal else 54, size)
        if horizontal:
            y = plot.y0 + i * thickness + thickness / 2 + 2
            page.insert_text((plot.x0 - 56, y), text, fontsize=size,
                             fontname=_FONT, color=LABEL)
            continue

        x = plot.x0 + i * thickness + thickness / 2
        w = pymupdf.get_text_length(text, fontname=_FONT, fontsize=size)
        # Centred on the slot, but never past the plot's own edges — on a chart
        # sharing a row, an overhanging label lands on the chart beside it.
        left = min(max(x - w / 2, plot.x0 - 2), plot.x1 - w)
        if left < occupied + 4:
            continue
        page.insert_text((left, plot.y1 + 10), text, fontsize=size,
                         fontname=_FONT, color=LABEL)
        occupied = left + w


def _draw_lines(page, shape, plot, data: ChartData, area: bool,
                reference_last: bool = False) -> None:
    """Lines, optionally filled.

    `reference_last` marks the final series as a target or threshold: it is
    never filled and is drawn dashed. Without it an area_line panel fills
    EVERY series, so a flat target at the top of the range lays an opaque
    block over the whole plot and hides the measure underneath it.
    """
    lo, hi = _value_range(data, stacked=False)
    _draw_frame(page, shape, plot, lo, hi)
    span = hi - lo
    n = len(data.categories)
    if n == 0:
        return
    gap = plot.width / max(n - 1, 1) if n > 1 else plot.width

    last = len(data.series) - 1
    # The reference line has to read AGAINST the fill, so it takes the accent
    # rather than the next series colour — a near neighbour of the fill inside
    # a themed palette is invisible over it.
    reference_col = accent_color([series_color(i) for i in range(last)])         if reference_last else None

    for s, (_, vals) in enumerate(data.series):
        is_reference = reference_last and s == last
        colour = reference_col if is_reference else series_color(s)
        points = []
        for i in range(n):
            v = vals[i] if i < len(vals) else None
            if v is None:
                continue
            x = plot.x0 + (i * gap if n > 1 else plot.width / 2)
            y = plot.y1 - ((v - lo) / span) * plot.height
            points.append(pymupdf.Point(x, y))
        if len(points) < 2:
            if points:
                shape.draw_circle(points[0], 1.8)
                shape.finish(fill=colour, color=None)
            continue
        if area and not is_reference:
            poly = points + [pymupdf.Point(points[-1].x, plot.y1),
                             pymupdf.Point(points[0].x, plot.y1)]
            shape.draw_polyline(poly)
            # No stroke on the fill: the line itself is drawn over it below.
            shape.finish(fill=colour, color=None, fill_opacity=0.18,
                         closePath=True)
        shape.draw_polyline(points)
        shape.finish(color=colour, width=1.4, closePath=False,
                     dashes="[3 2] 0" if is_reference else None)

    _draw_categories(page, data.categories, plot, gap, horizontal=False)


def _draw_pie(page, shape, plot, data: ChartData, donut: bool) -> None:
    """Wedges clockwise from twelve o'clock.

    pymupdf measures a sector from a START POINT on the circumference and sweeps
    by an angle, with the page's y-axis pointing down. Clockwise is therefore a
    NEGATIVE sweep, and the start point for an angle `a` measured clockwise from
    twelve is (cx + r*sin a, cy - r*cos a). Getting either half of that wrong
    produces overlapping wedges and a gap, which is what it did before.
    """
    _, vals = data.series[0]
    pairs = [(c, v) for c, v in zip(data.categories, vals)
             if v is not None and v > 0]
    total = sum(v for _, v in pairs)
    if total <= 0:
        return

    radius = min(plot.width, plot.height) / 2 - 4
    if radius <= 2:
        return
    centre = pymupdf.Point(plot.x0 + plot.width / 2, plot.y0 + plot.height / 2)

    def rim(degrees: float) -> pymupdf.Point:
        rad = math.radians(degrees)
        return pymupdf.Point(centre.x + radius * math.sin(rad),
                             centre.y - radius * math.cos(rad))

    # Fill only, and a separate Shape per wedge. Stroking a sector outlines the
    # CHORD as well as the arc, which drew a straight line across every wedge
    # and made the circle look like a folded paper fan. Sharing one Shape across
    # the wedges merges their paths and does the same.
    angle = 0.0
    for i, (_, v) in enumerate(pairs):
        sweep = 360.0 * (v / total)
        wedge = page.new_shape()
        # A single 360-degree sector degenerates into nothing; hold it just under.
        wedge.draw_sector(centre, rim(angle), -min(sweep, 359.9), fullSector=True)
        wedge.finish(fill=series_color(i), color=None)
        wedge.commit()
        angle += sweep

    # Dividers as radii, which is what the stroke was meant to achieve.
    if len(pairs) > 1:
        angle = 0.0
        for _, v in pairs:
            shape.draw_line(centre, rim(angle))
            shape.finish(color=(1, 1, 1), width=1.2)
            angle += 360.0 * (v / total)

    if donut:
        shape.draw_circle(centre, radius * 0.55)
        shape.finish(fill=(1, 1, 1), color=None)


#: Bubble radii. The floor keeps the smallest entity visible; the ceiling stops
#: one outlier swallowing its neighbours.
_DOT_R = 2.2
_BUBBLE_R_MIN = 1.8
_BUBBLE_R_MAX = 7.5

#: Fraction of each axis left as margin, so a point at the exact minimum or
#: maximum sits inside the plot instead of straddling its edge.
_SCATTER_PAD = 0.04


def _draw_scatter(page, shape, plot, panel: dict[str, Any],
                  data: ChartData) -> None:
    """One point per ROW — a scatter is not a categorical series.

    This used to read `data.series[0]`, and that is the bug it exists to avoid
    repeating. When a panel sets a colour column, `extract()` pivots N rows into
    N series each holding ONE value and N-1 Nones, so series[0] was a single
    point — and the axis range was then taken from that lone point, putting the
    ticks somewhere the data was not. Eight segments spanning x 4.7-5.2 drew as
    one dot with ticks at 5, 5.25, 5.5, 5.75.

    `data` is still used for the group order, so the colours here line up with
    the legend the caller drew from the same object.
    """
    columns = [str(c) for c in (panel.get("columns") or [])]
    rows = panel.get("rows") or []
    index_of = {c.upper(): i for i, c in enumerate(columns)}

    def idx(name: Any) -> int | None:
        return index_of.get(str(name).upper()) if name is not None else None

    xi = idx(panel.get("x"))
    raw_y = panel.get("y")
    yi = idx(raw_y[0] if isinstance(raw_y, list) and raw_y else raw_y)
    if xi is None or yi is None:
        return
    si = idx(panel.get("size"))
    ci = idx(panel.get("color"))

    # The legend was built from data.series, so honour that order.
    group_index = {name: i for i, (name, _) in enumerate(data.series)}

    pts: list[tuple[float, float, float | None, int]] = []
    for row in rows:
        x = _num(row[xi]) if xi < len(row) else None
        y = _num(row[yi]) if yi < len(row) else None
        if x is None or y is None:
            continue
        mag = _num(row[si]) if si is not None and si < len(row) else None
        group = (str(row[ci]) if ci is not None and ci < len(row)
                 and row[ci] is not None else "")
        pts.append((x, y, mag, group_index.get(group, 0)))
    if not pts:
        return

    xlo, xhi = min(p[0] for p in pts), max(p[0] for p in pts)
    ylo, yhi = min(p[1] for p in pts), max(p[1] for p in pts)
    if xhi == xlo:
        xhi, xlo = xlo + 1, xlo - 1
    if yhi == ylo:
        yhi, ylo = ylo + 1, ylo - 1
    xpad = (xhi - xlo) * _SCATTER_PAD
    ypad = (yhi - ylo) * _SCATTER_PAD
    xlo, xhi = xlo - xpad, xhi + xpad
    ylo, yhi = ylo - ypad, yhi + ypad

    _draw_frame(page, shape, plot, ylo, yhi)

    # Radius by AREA, not by diameter: the eye reads a bubble's area, and
    # scaling the radius linearly overstates a large value four-fold.
    mags = [m for _, _, m, _ in pts if m is not None]
    mlo, mhi = (min(mags), max(mags)) if mags else (0.0, 0.0)

    def radius(mag: float | None) -> float:
        if mag is None or not mags:
            return _DOT_R
        if mhi == mlo:
            return (_BUBBLE_R_MIN + _BUBBLE_R_MAX) / 2
        t = (mag - mlo) / (mhi - mlo)
        area_lo, area_hi = _BUBBLE_R_MIN ** 2, _BUBBLE_R_MAX ** 2
        return math.sqrt(area_lo + t * (area_hi - area_lo))

    # Largest first, so a big bubble cannot hide a small one drawn later.
    for x, y, mag, gi in sorted(pts, key=lambda p: -(p[2] or 0.0)):
        px = plot.x0 + ((x - xlo) / (xhi - xlo)) * plot.width
        py = plot.y1 - ((y - ylo) / (yhi - ylo)) * plot.height
        shape.draw_circle(pymupdf.Point(px, py), radius(mag))
        shape.finish(fill=series_color(gi), color=None, fill_opacity=0.7)

    size = 6.5
    for t in _tick_values(xlo, xhi):
        px = plot.x0 + ((t - xlo) / (xhi - xlo)) * plot.width
        page.insert_text((px - 8, plot.y1 + 10), _fmt_tick(t), fontsize=size,
                         fontname=_FONT, color=LABEL)


def _heat_color(t: float) -> tuple[float, float, float]:
    """Light-to-deep blue, matching the app's heatColor ramp (#E8F1FA→#3B6EA5)."""
    x = max(0.0, min(1.0, t))
    return (
        (232 + (59 - 232) * x) / 255,
        (241 + (110 - 241) * x) / 255,
        (250 + (165 - 250) * x) / 255,
    )


def _draw_heatmap(page, shape, plot, panel: dict[str, Any]) -> bool:
    """A grid of cells shaded by value: x across, the colour column down.

    The planner may emit heatmap panels, so a PDF that cannot draw one turned a
    whole panel into a table without saying why — which is what made charts
    disappear from a dashboard's later tabs.
    """
    columns = [str(c) for c in (panel.get("columns") or [])]
    rows = panel.get("rows") or []
    index_of = {c.upper(): i for i, c in enumerate(columns)}

    def idx(name: Any) -> int | None:
        return index_of.get(str(name).upper()) if name is not None else None

    xi, ci = idx(panel.get("x")), idx(panel.get("color"))
    raw_y = panel.get("y")
    vi = idx(raw_y[0] if isinstance(raw_y, list) and raw_y else raw_y)
    if xi is None or vi is None:
        return False

    cols: list[str] = []
    band: list[str] = []
    cells: dict[tuple[str, str], float] = {}
    # SQL written with GROUPING SETS returns subtotal rows carrying NULL on one
    # axis. Admitted to the grid they become an unlabelled band belonging to
    # nothing, sitting alongside real rows as though it were one of them. They
    # are excluded — and counted, because silently dropping data is how a
    # reader ends up trusting a total that is not there.
    unlabelled = 0
    for row in rows:
        cx = str(row[xi]).strip() if xi < len(row) and row[xi] is not None else ""
        cy = (str(row[ci]).strip() if ci is not None and ci < len(row)
              and row[ci] is not None else "")
        value = _num(row[vi]) if vi < len(row) else None
        if value is None:
            continue
        if not cx or (ci is not None and not cy):
            unlabelled += 1
            continue
        if cx not in cols:
            cols.append(cx)
        if cy not in band:
            band.append(cy)
        cells[(cx, cy)] = value
    if not cols or not band:
        return False

    # A heatmap is given a fixed block of height like every other chart, so a
    # dimension with sixty members gets two points per row: the labels collapse
    # into a smear and the cells vanish. Keep the largest rows, which are the
    # ones a reader is looking for, and say what was left out.
    if len(band) > MAX_HEATMAP_ROWS:
        weight = {
            name: sum(abs(v) for (_, cy), v in cells.items() if cy == name)
            for name in band
        }
        band = sorted(band, key=lambda n: weight.get(n, 0.0), reverse=True)
        dropped = len(band) - MAX_HEATMAP_ROWS
        band = band[:MAX_HEATMAP_ROWS]
        page.insert_text(
            (plot.x0, plot.y1 + 19),
            f"Top {MAX_HEATMAP_ROWS} of {len(band) + dropped} by total; "
            f"{dropped} not shown. The table below lists them all.",
            fontsize=6.5, fontname=_FONT, color=LABEL,
        )
        keep = set(band)
        cells = {k: v for k, v in cells.items() if k[1] in keep}
        if not cells:
            return False

    values = list(cells.values())
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    label_w = 0.0
    for name in band:
        label_w = max(label_w, pymupdf.get_text_length(_fit(name, 70, 6.5),
                                                       fontname=_FONT, fontsize=6.5))
    grid_x0 = plot.x0 + label_w + 6
    cell_w = (plot.x1 - grid_x0) / len(cols)
    cell_h = plot.height / len(band)
    if cell_w <= 1 or cell_h <= 1:
        return False

    for r, cy in enumerate(band):
        for c, cx in enumerate(cols):
            value = cells.get((cx, cy))
            if value is None:
                continue
            rect = pymupdf.Rect(grid_x0 + c * cell_w, plot.y0 + r * cell_h,
                                grid_x0 + (c + 1) * cell_w, plot.y0 + (r + 1) * cell_h)
            fill = _heat_color((value - lo) / span)
            tile = page.new_shape()
            tile.draw_rect(rect)
            tile.finish(fill=fill, color=(1, 1, 1), width=0.5)
            # Committed BEFORE the text: page.insert_text paints immediately
            # while a Shape's fills land on commit(), so a value written first
            # is painted over by its own cell.
            tile.commit()
            # A cell that carries only a colour tells a reader which is darkest
            # and not what any of them is worth. The on-screen heatmap prints
            # the number; so does this.
            text = _fit(_fmt_tick(value), cell_w - 4, 6.0)
            tw = pymupdf.get_text_length(text, fontname=_FONT, fontsize=6.0)
            if tw <= cell_w - 4 and cell_h >= 11:
                page.insert_text(
                    (rect.x0 + (cell_w - tw) / 2, rect.y0 + cell_h / 2 + 2),
                    text, fontsize=6.0, fontname=_FONT,
                    color=_label_color(fill),
                )
        page.insert_text((plot.x0, plot.y0 + r * cell_h + cell_h / 2 + 2),
                         _fit(cy, label_w, 6.5), fontsize=6.5, fontname=_FONT,
                         color=LABEL)

    occupied = float("-inf")
    for c, cx in enumerate(cols):
        text = _fit(str(cx), cell_w - 2, 6.5)
        w = pymupdf.get_text_length(text, fontname=_FONT, fontsize=6.5)
        left = grid_x0 + c * cell_w + (cell_w - w) / 2
        if left < occupied + 3:
            continue
        page.insert_text((left, plot.y1 + 10), text, fontsize=6.5,
                         fontname=_FONT, color=LABEL)
        occupied = left + w

    if unlabelled:
        page.insert_text(
            (plot.x0, plot.y1 + 19),
            f"{unlabelled} row{'s' if unlabelled != 1 else ''} with no category "
            "excluded (subtotal rows). The table below lists them.",
            fontsize=6.5, fontname=_FONT, color=_OVERFLOW,
        )
    return True



# --- shapes that had no vector renderer -------------------------------------
#
# funnel, combo, radar and treemap used to fall through to a table, and the
# browser was expected to post a PNG instead. It cannot: a dashboard mounts one
# tab at a time, so any panel on another tab has no DOM node to photograph and
# the capture silently returned nothing. They are drawn here now.
#
# All four commit their own Shape BEFORE writing labels. `page.insert_text`
# paints immediately while a Shape's fills land on `commit()`, so text written
# first is covered by the very rectangle it belongs to.

def _label_color(fill: tuple[float, float, float]) -> tuple[float, float, float]:
    """Black or white, whichever stays legible on `fill`."""
    r, g, b = fill
    return (1, 1, 1) if (0.299 * r + 0.587 * g + 0.114 * b) < 0.6 else (0.15, 0.16, 0.2)


#: A stage worth three orders of magnitude less than the widest is still a
#: stage; without a floor it renders as a hairline nobody can see or click.
_FUNNEL_MIN_W = 0.06


def _draw_funnel(page, plot, data: ChartData) -> bool:
    """Ordered stages as contiguous centred bars.

    Deliberately NOT trapezoids joining one stage's width to the next. Real
    funnels are not monotonic — production data steps 175.8M -> 71.5M -> 159.7M
    — and trapezoids across a widening step cross over into a bow-tie. Centred
    bars stay readable whatever the sequence does, and the shape still reads as
    a funnel when the data really does narrow.
    """
    name, values = data.series[0]
    pairs = [(c, v) for c, v in zip(data.categories, values) if v is not None]
    if not pairs:
        return False
    # Largest stage first, as the dashboard draws it (sortFunnelStages in
    # chart-standard.ts); stages of equal size keep their order.
    pairs.sort(key=lambda pair: -pair[1])

    top = max(abs(v) for _, v in pairs) or 1.0
    band = plot.height / len(pairs)
    gap = min(3.0, band * 0.18)

    shape = page.new_shape()
    boxes: list[tuple[pymupdf.Rect, str, float, tuple[float, float, float]]] = []
    for i, (label, value) in enumerate(pairs):
        frac = max(abs(value) / top, _FUNNEL_MIN_W)
        w = plot.width * frac
        cx = plot.x0 + plot.width / 2
        y0 = plot.y0 + i * band + gap / 2
        rect = pymupdf.Rect(cx - w / 2, y0, cx + w / 2, y0 + band - gap)
        fill = series_color(i)
        shape.draw_rect(rect)
        shape.finish(fill=fill, color=None)
        boxes.append((rect, str(label), value, fill))
    shape.commit()

    for rect, label, value, fill in boxes:
        text = f"{_fit(label, rect.width - 8, 7.0)}  {_fmt_tick(value)}"
        w = pymupdf.get_text_length(text, fontname=_FONT, fontsize=7.0)
        y = rect.y0 + rect.height / 2 + 2.5
        if w <= rect.width - 6:
            page.insert_text((rect.x0 + (rect.width - w) / 2, y), text,
                             fontsize=7.0, fontname=_FONT, color=_label_color(fill))
        else:
            # Too narrow to write inside — put it beside the bar instead of
            # spilling white text over the page.
            page.insert_text((rect.x1 + 4, y),
                             _fit(f"{label}  {_fmt_tick(value)}",
                                  plot.x1 - rect.x1 - 6, 7.0),
                             fontsize=7.0, fontname=_FONT, color=LABEL)
    return True


def _draw_combo(page, shape, plot, data: ChartData) -> bool:
    """Bars plus a line on a SECOND axis — the one two-scale chart we allow.

    The last y column is the line, matching the planner's contract ("list the
    line measure LAST in y"). Each axis is scaled independently, which is the
    whole point: a count and a rate share an x axis and nothing else.
    """
    if len(data.series) < 2:
        return False
    bar_series = data.series[:-1]
    line_name, line_values = data.series[-1]

    bar_vals = [v for _, vals in bar_series for v in vals if v is not None]
    line_vals = [v for v in line_values if v is not None]
    if not bar_vals or not line_vals:
        return False

    blo, bhi = min(bar_vals), max(bar_vals)
    zero_baseline, _ = _panel_style.get()
    if zero_baseline:
        blo = min(0.0, blo)
    elif bhi != blo:
        bpad = (bhi - blo) * 0.08
        blo, bhi = blo - bpad, bhi + bpad
    if bhi == blo:
        bhi = blo + 1.0
    llo, lhi = min(line_vals), max(line_vals)
    if lhi == llo:
        llo, lhi = llo - 1.0, lhi + 1.0
    pad = (lhi - llo) * 0.1
    llo, lhi = llo - pad, lhi + pad

    _draw_frame(page, shape, plot, blo, bhi)

    n = len(data.categories)
    if n == 0:
        return False
    slot = plot.width / n
    group = len(bar_series)
    bar_w = min(slot * 0.7 / max(group, 1), 26.0)

    for si, (_, vals) in enumerate(bar_series):
        for i, v in enumerate(vals):
            if v is None:
                continue
            centre = plot.x0 + slot * (i + 0.5)
            left = centre - (group * bar_w) / 2 + si * bar_w
            base_v = _bar_base(blo, bhi)
            zero = plot.y1 - ((base_v - blo) / (bhi - blo)) * plot.height
            top = plot.y1 - ((v - blo) / (bhi - blo)) * plot.height
            shape.draw_rect(pymupdf.Rect(left, min(top, zero),
                                         left + bar_w * 0.92, max(top, zero)))
            shape.finish(fill=series_color(si), color=None)

    pts = []
    for i, v in enumerate(line_values):
        if v is None:
            continue
        x = plot.x0 + slot * (i + 0.5)
        y = plot.y1 - ((v - llo) / (lhi - llo)) * plot.height
        pts.append(pymupdf.Point(x, y))
    # Not the next series colour: inside a themed palette that is a near
    # neighbour of the bars, and the line disappears into them.
    line_col = accent_color([series_color(i) for i in range(len(bar_series))])
    for a, b in zip(pts, pts[1:]):
        shape.draw_line(a, b)
        shape.finish(color=line_col, width=1.4)
    for pt in pts:
        shape.draw_circle(pt, 1.8)
        shape.finish(fill=line_col, color=None)

    # The right-hand axis, so the line's units are readable at all.
    for t in _tick_values(llo, lhi):
        y = plot.y1 - ((t - llo) / (lhi - llo)) * plot.height
        if y < plot.y0 - 1 or y > plot.y1 + 1:
            continue
        page.insert_text((plot.x1 + 3, y + 2), _fmt_tick(t), fontsize=6.5,
                         fontname=_FONT, color=line_col)
    _draw_categories(page, data.categories, plot, slot, horizontal=False)
    return True


def _draw_radar(page, plot, data: ChartData) -> bool:
    """One entity's profile across 3-8 named dimensions.

    Fewer than three axes is a bar chart; more than eight and the spokes collide
    into a scribble, which is why the planner is told 3-8 and why this refuses
    outside that.
    """
    axes = data.categories
    if not 3 <= len(axes) <= 12:
        return False

    values = [v for _, vals in data.series for v in vals if v is not None]
    if not values:
        return False
    hi = max(values)
    lo = min(0.0, min(values))
    if hi == lo:
        hi = lo + 1.0

    cx = plot.x0 + plot.width / 2
    cy = plot.y0 + plot.height / 2
    radius = min(plot.width, plot.height) / 2 - 26
    if radius <= 10:
        return False

    n = len(axes)
    angles = [(-math.pi / 2) + (2 * math.pi * i / n) for i in range(n)]

    def point(i: int, frac: float) -> pymupdf.Point:
        return pymupdf.Point(cx + radius * frac * math.cos(angles[i]),
                             cy + radius * frac * math.sin(angles[i]))

    web = page.new_shape()
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = [point(i, ring) for i in range(n)]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            web.draw_line(a, b)
        web.finish(color=GRID, width=0.5)
    for i in range(n):
        web.draw_line(pymupdf.Point(cx, cy), point(i, 1.0))
    web.finish(color=GRID, width=0.5)

    for si, (_, vals) in enumerate(data.series):
        pts = []
        for i in range(n):
            v = vals[i] if i < len(vals) else None
            pts.append(point(i, 0.0 if v is None else (v - lo) / (hi - lo)))
        for a, b in zip(pts, pts[1:] + pts[:1]):
            web.draw_line(a, b)
        web.finish(color=series_color(si), width=1.3)
        for pt in pts:
            web.draw_circle(pt, 1.6)
            web.finish(fill=series_color(si), color=None)
    web.commit()

    for i, name in enumerate(axes):
        anchor = point(i, 1.0)
        text = _fit(str(name), 78, 6.5)
        w = pymupdf.get_text_length(text, fontname=_FONT, fontsize=6.5)
        dx = math.cos(angles[i])
        x = anchor.x + (4 if dx > 0.2 else (-w - 4 if dx < -0.2 else -w / 2))
        y = anchor.y + (8 if math.sin(angles[i]) > 0.2 else -3)
        page.insert_text((x, y), text, fontsize=6.5, fontname=_FONT, color=LABEL)
    return True


def _squarify(values: list[float], rect: pymupdf.Rect) -> list[pymupdf.Rect]:
    """Squarified treemap layout — rectangles whose aspect ratios stay near 1.

    Slice-and-dice is simpler but degenerates into slivers the moment one value
    dominates, and a sliver cannot carry a label.
    """
    total = sum(values) or 1.0
    out: list[pymupdf.Rect] = []
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    remaining = list(values)
    scale = ((x1 - x0) * (y1 - y0)) / total

    while remaining:
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            break
        short = min(w, h)
        row: list[float] = []
        best = float("inf")
        while remaining:
            trial = row + [remaining[0]]
            area = sum(trial) * scale
            side = area / short if short else 0.0
            worst = max(
                max(side / (v * scale / side), (v * scale / side) / side)
                for v in trial
                if v * scale > 0 and side > 0
            ) if side > 0 else float("inf")
            if row and worst > best:
                break
            row, best = trial, worst
            remaining.pop(0)

        area = sum(row) * scale
        side = area / short if short else 0.0
        offset = 0.0
        for v in row:
            length = (v * scale / side) if side else 0.0
            if w >= h:
                out.append(pymupdf.Rect(x0, y0 + offset, x0 + side, y0 + offset + length))
            else:
                out.append(pymupdf.Rect(x0 + offset, y0, x0 + offset + length, y0 + side))
            offset += length
        if w >= h:
            x0 += side
        else:
            y0 += side
    return out


def _draw_treemap(page, plot, data: ChartData) -> bool:
    """Part-to-whole with more parts than a donut can carry."""
    _, values = data.series[0]
    pairs = [(str(c), v) for c, v in zip(data.categories, values)
             if v is not None and v > 0]
    if not pairs:
        return False
    pairs.sort(key=lambda p: -p[1])
    total = sum(v for _, v in pairs)

    rects = _squarify([v for _, v in pairs], plot)
    shape = page.new_shape()
    placed: list[tuple[pymupdf.Rect, str, float, tuple[float, float, float]]] = []
    for i, ((label, value), rect) in enumerate(zip(pairs, rects)):
        fill = _heat_color(0.25 + 0.7 * (value / (pairs[0][1] or 1.0)))
        shape.draw_rect(rect)
        shape.finish(fill=fill, color=(1, 1, 1), width=0.6)
        placed.append((rect, label, value, fill))
    shape.commit()

    for rect, label, value, fill in placed:
        if rect.width < 26 or rect.height < 14:
            continue
        colour = _label_color(fill)
        page.insert_text((rect.x0 + 3, rect.y0 + 9),
                         _fit(label, rect.width - 6, 6.8),
                         fontsize=6.8, fontname=_FONT, color=colour)
        if rect.height >= 24:
            share = f"{_fmt_tick(value)}  {value / total * 100:.0f}%"
            page.insert_text((rect.x0 + 3, rect.y0 + 18),
                             _fit(share, rect.width - 6, 6.2),
                             fontsize=6.2, fontname=_FONT, color=colour)
    return True



# --- maps -------------------------------------------------------------------

#: "No data" grey, matching the app's var(--muted).
_GEO_MUTED = (0.925, 0.937, 0.953)

#: The palest shade the ramp may start at. With a single region hi == lo, so
#: (v-lo)/span is 0 and the one country with data renders in the same colour as
#: the ones without. The frontend uses span === 0 ? 1 and a floor of 0.18; both
#: are mirrored here, because a map that shades differently on paper than on
#: screen is not the same map.
_SHADE_FLOOR = 0.18

#: Projected area below which a matched region is drawn as a dot instead. The
#: UAE and Singapore are exactly the places this data is about, and a shape a
#: pixel across beside Russia reads as no data at all.
_GEO_MIN_AREA = 6.0


def _geo_shade(value: float, lo: float, hi: float) -> tuple[float, float, float]:
    span = hi - lo
    t = 1.0 if span == 0 else _SHADE_FLOOR + (1 - _SHADE_FLOOR) * ((value - lo) / span)
    return _heat_color(t)


def _draw_geo(page, rect: pymupdf.Rect, panel: dict[str, Any], scope: str) -> bool:
    """A choropleth, drawn from boundary geometry the backend carries itself.

    Maps used to depend on the browser posting a PNG, which cannot work: a
    dashboard mounts one tab at a time, so a map on any other tab had no DOM
    node to photograph and silently exported as a table.
    """
    from app.engine.geo import matching, topo

    columns = [str(c) for c in (panel.get("columns") or [])]
    rows = panel.get("rows") or []
    index_of = {c.upper(): i for i, c in enumerate(columns)}

    def idx(name: Any) -> int | None:
        return index_of.get(str(name).upper()) if name is not None else None

    xi = idx(panel.get("x"))
    raw_y = panel.get("y")
    yi = idx(raw_y[0] if isinstance(raw_y, list) and raw_y else raw_y)
    if xi is None or yi is None:
        return False

    countries = scope == "countries"
    matcher = matching.match_country if countries else matching.match_state
    name_of = matching.country_name if countries else matching.state_name

    totals: dict[str, float] = {}
    places: list[Any] = []
    for row in rows:
        place = row[xi] if xi < len(row) else None
        places.append(place)
        key = matcher(place)
        value = _num(row[yi]) if yi < len(row) else None
        if key is None or value is None:
            continue
        # Summed, not last-wins: a query grouped by place AND something else
        # yields several rows per place.
        totals[key] = totals.get(key, 0.0) + value
    missed = matching.unmatched(places, matcher)

    features = topo.world_features() if countries else topo.us_features()
    if countries:
        # Antarctica spans the whole 360 and is almost never the subject; drawn,
        # it eats the bottom fifth of every world view.
        features = [f for f in features
                    if f.id != topo.ANTARCTICA or f.id in totals]

    plot = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y1 - 16)
    if plot.width < 40 or plot.height < 40:
        return False

    if countries:
        matched = [f for f in features if f.id in totals]
        box = topo.focus_box(matched)
        transform = topo.fit(topo.focus_samples(box),
                             plot.x0, plot.y0, plot.x1, plot.y1)

        def screen_rings(feature):
            out = []
            for ring in feature.rings:
                for piece in topo.split_antimeridian(ring):
                    out.append([transform(topo.equal_earth(lon, lat))
                                for lon, lat in piece])
            return out
    else:
        # states-albers-10m is already projected, insets and all.
        every = [p for f in features for r in f.rings for p in r]
        transform = topo.fit(every, plot.x0, plot.y0, plot.x1, plot.y1)

        def screen_rings(feature):
            return [[transform(p) for p in ring] for ring in feature.rings]

    values = list(totals.values())
    lo, hi = (min(values), max(values)) if values else (0.0, 0.0)

    shape = page.new_shape()
    dots: list[tuple[float, float, tuple[float, float, float]]] = []
    for feature in features:
        value = totals.get(feature.id)
        fill = _GEO_MUTED if value is None else _geo_shade(value, lo, hi)
        drawn_area = 0.0
        biggest: list[tuple[float, float]] = []
        for ring in screen_rings(feature):
            # A Shape has no viewBox: unclipped, the rest of the world paints
            # straight over the table below.
            clipped = topo.clip_to_rect(ring, plot.x0, plot.y0, plot.x1, plot.y1)
            if len(clipped) < 3:
                continue
            area = topo.polygon_area(clipped)
            drawn_area += area
            if area >= topo.polygon_area(biggest):
                biggest = clipped
            shape.draw_polyline([pymupdf.Point(*p) for p in clipped] +
                                [pymupdf.Point(*clipped[0])])
            shape.finish(fill=fill, color=(1, 1, 1), width=0.25, closePath=True)
        if value is not None and biggest and drawn_area < _GEO_MIN_AREA:
            cx, cy = topo.centroid(biggest)
            dots.append((cx, cy, fill))
    for cx, cy, fill in dots:
        shape.draw_circle(pymupdf.Point(cx, cy), 2.6)
        shape.finish(fill=fill, color=(1, 1, 1), width=0.5)

    # The legend swatch, drawn before the commit so its labels land on top.
    bar = pymupdf.Rect(plot.x0 + 26, plot.y1 + 5, plot.x0 + 74, plot.y1 + 10)
    steps = 24
    for i in range(steps):
        seg = pymupdf.Rect(bar.x0 + bar.width * i / steps, bar.y0,
                           bar.x0 + bar.width * (i + 1) / steps, bar.y1)
        shape.draw_rect(seg)
        shape.finish(fill=_heat_color(_SHADE_FLOOR +
                                      (1 - _SHADE_FLOOR) * (i / (steps - 1))),
                     color=None)
    shape.commit()

    page.insert_text((plot.x0, plot.y1 + 10), _fmt_tick(lo), fontsize=6.5,
                     fontname=_FONT, color=LABEL)
    page.insert_text((bar.x1 + 4, plot.y1 + 10), _fmt_tick(hi), fontsize=6.5,
                     fontname=_FONT, color=LABEL)

    if missed:
        # An unshaded region is indistinguishable from one with no data, so the
        # values that reached no region are named rather than dropped.
        shown = ", ".join(missed[:6]) + ("…" if len(missed) > 6 else "")
        note = (f"{len(missed)} value{'s' if len(missed) != 1 else ''} not matched "
                f"to a {'country' if countries else 'state'}: {shown}")
        page.insert_text((plot.x0, plot.y1 + 20),
                         _fit(note, plot.width, 6.5),
                         fontsize=6.5, fontname=_FONT, color=_OVERFLOW)
    return True


#: Chart types that have a vector renderer here. Anything else falls back to a
#: table, which is honest: a wrong chart is worse than the numbers. Keep this in
#: step with the planner's own list in llm.py, or a type it is told to emit
#: silently becomes a table.
RENDERABLE = {
    "bar", "stacked_bar", "grouped_bar", "line", "area", "pie", "donut",
    "doughnut", "scatter", "bubble", "heatmap",
    "funnel", "combo", "area_line", "radar", "treemap",
    "geo_us_states", "geo_countries",
}


#: Height the legend row takes when there is more than one series.
_LEGEND_H = 16.0

#: Vertical room one horizontal bar wants: enough for its category label beside
#: it, and no more. Twelve bars given a whole page became slabs a centimetre
#: thick separated by empty aisles.
_ROW_PITCH = 21.0

#: Plot height as a fraction of its width, for the shapes whose height is NOT
#: set by a row count. Wider than tall reads as a chart; stretched to square it
#: reads as a poster, and the trend it is meant to show gets steeper for no
#: reason.
_PLOT_ASPECT = 0.52

#: A pie says the same thing at any size, so it gets a diameter rather than a
#: share of the page. A three-slice donut filling a sheet of A4 was the clearest
#: case of height being handed out by what was spare instead of what was wanted.
_PIE_DIAMETER = 240.0


def natural_height(panel: dict[str, Any], width: float) -> float:
    """The height this chart wants for its own content, before the page's say.

    The caller then fits this to the room actually left on the page. Charts were
    previously given whatever was spare, which is how twelve bars and three pie
    slices both ended up 460 points tall.
    """
    chart_type = str(panel.get("chart_type") or "").lower()

    if chart_type not in RENDERABLE:
        # A client-rendered picture (a map, a treemap). It is placed to fit and
        # keeps its own aspect ratio, so this is only an upper bound.
        return width * _PLOT_ASPECT

    if chart_type in ("geo_us_states", "geo_countries"):
        # Wide and shallow, like the panel on screen, plus room for the legend
        # and the unmatched note under it.
        return min(width * 0.58, 300.0) + 24.0

    if chart_type in ("pie", "donut", "doughnut"):
        data = extract(panel, single_series=True)
        legend = _LEGEND_H if data and len(data.categories) > 1 else 0.0
        return _PIE_DIAMETER + legend

    data = extract(panel)
    if data is None or not data.ok:
        return width * _PLOT_ASPECT

    legend = _LEGEND_H if len(data.series) > 1 else 0.0

    if chart_type == "funnel":
        return max(120.0, len(data.categories) * 26.0 + 20.0)
    if chart_type in ("radar", "treemap"):
        return min(width * 0.72, 300.0) + legend

    if str(panel.get("orientation") or "").lower() == "h":
        # Bars run down the page, so the height is a row count. Several series
        # per category means several bars sharing one row's pitch.
        pitch = max(_ROW_PITCH, len(data.series) * 8.0 + 6.0)
        return len(data.categories) * pitch + 34.0 + legend

    return width * _PLOT_ASPECT + legend


def draw_panel_chart(page, rect: pymupdf.Rect, panel: dict[str, Any]) -> bool:
    """Draw a panel's chart inside `rect`. False if it is not chartable."""
    chart_type = str(panel.get("chart_type") or "").lower()
    if chart_type not in RENDERABLE:
        return False

    # Bound here, reset in the finally below, so a panel's colours and baseline
    # cannot leak into the next panel drawn on the same page.
    token = _panel_style.set(
        (panel.get("zero_baseline") is not False,
         panel.get("palette") if isinstance(panel.get("palette"), str) else None)
    )
    try:
        return _draw_panel_chart(page, rect, panel, chart_type)
    finally:
        _panel_style.reset(token)


def _draw_panel_chart(page, rect: pymupdf.Rect, panel: dict[str, Any],
                      chart_type: str) -> bool:
    """The body of draw_panel_chart, with the panel style already bound."""
    if chart_type in ("geo_us_states", "geo_countries"):
        # Its own geometry and its own legend; nothing categorical about it.
        return _draw_geo(page, rect, panel,
                         "countries" if chart_type == "geo_countries" else "us")

    if chart_type == "heatmap":
        # Its own shape: a heatmap is read from three columns (x, colour, value)
        # rather than the categories-and-series form everything else uses.
        shape = page.new_shape()
        plot = pymupdf.Rect(rect.x0, rect.y0 + 2, rect.x1 - 4, rect.y1 - 14)
        drew = _draw_heatmap(page, shape, plot, panel)
        shape.commit()
        return drew

    circular = chart_type in ("pie", "donut", "doughnut")
    single = circular or chart_type in ("funnel", "treemap")
    data = extract(panel, single_series=single)
    if data is None or not data.ok:
        return False

    if chart_type in ("funnel", "radar", "treemap"):
        # These draw and commit their own shapes so their labels land ON TOP of
        # their fills rather than under them, and they need no axis frame.
        body = pymupdf.Rect(rect.x0 + 4, rect.y0 + 2, rect.x1 - 4, rect.y1 - 6)
        if chart_type == "funnel":
            return _draw_funnel(page, body, data)
        if chart_type == "treemap":
            return _draw_treemap(page, body, data)
        shape = page.new_shape()
        legend_h = _draw_legend(page, shape, rect, [n for n, _ in data.series])
        shape.commit()
        return _draw_radar(
            page,
            pymupdf.Rect(body.x0, body.y0 + legend_h, body.x1, body.y1),
            data,
        )

    shape = page.new_shape()
    if circular:
        # A pie's legend names its CATEGORIES — each is a wedge — where every
        # other chart's legend names its series.
        legend_h = _draw_legend(page, shape, rect, data.categories)
        body = pymupdf.Rect(rect.x0, rect.y0 + legend_h, rect.x1, rect.y1)
        _draw_pie(page, shape, body, data, donut=chart_type != "pie")
        shape.commit()
        return True

    names = [n for n, _ in data.series]
    legend_h = _draw_legend(page, shape, rect, names)
    body = pymupdf.Rect(rect.x0, rect.y0 + legend_h, rect.x1, rect.y1)

    horizontal = str(panel.get("orientation") or "").lower() == "h"
    # Room for value labels on the left (or category labels when horizontal),
    # and for the category strip underneath.
    plot = pymupdf.Rect(
        body.x0 + (60 if horizontal else 30),
        body.y0 + 2,
        body.x1 - 4,
        body.y1 - 14,
    )
    if plot.width <= 10 or plot.height <= 10:
        shape.commit()
        return False

    if chart_type == "combo":
        if not _draw_combo(page, shape, plot, data):
            shape.commit()
            return False
    elif chart_type == "area_line":
        # One shared axis, so this is _draw_lines rather than _draw_combo:
        # combo fits each series to its own scale, which would put a target
        # line below the measure it is meant to sit above.
        _draw_lines(page, shape, plot, data, area=True, reference_last=True)
    elif chart_type in ("scatter", "bubble"):
        _draw_scatter(page, shape, plot, panel, data)
    elif chart_type in ("line", "area"):
        _draw_lines(page, shape, plot, data, area=chart_type == "area")
    else:
        _draw_bars(page, shape, plot, data,
                   stacked=chart_type == "stacked_bar", horizontal=horizontal)

    # Axis lines last so they sit over the gridlines.
    shape.draw_line(pymupdf.Point(plot.x0, plot.y1), pymupdf.Point(plot.x1, plot.y1))
    shape.finish(color=AXIS, width=0.7)
    shape.draw_line(pymupdf.Point(plot.x0, plot.y0), pymupdf.Point(plot.x0, plot.y1))
    shape.finish(color=AXIS, width=0.7)
    shape.commit()
    return True

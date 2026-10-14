"""Export a live dashboard run to Excel (Data sheet + Summary sheet).

Sheet ``Data`` holds every panel's query result in labeled blocks.
Sheet ``Summary`` holds the dashboard title, run stamp, KPI values, and a
panel index so the workbook is useful without opening the app.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import UTC, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

log = logging.getLogger(__name__)


def _safe_sheet_title(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", (name or "Panel").strip())[:31]
    return cleaned or "Panel"


def _write_panel_block(
    ws: Worksheet,
    start_row: int,
    title: str,
    columns: list[str],
    rows: list[list[Any]],
) -> tuple[int, int, int]:
    """Write one panel block. Returns (title_row, header_row, next_free_row)."""
    title_row = start_row
    ws.cell(title_row, 1, title).font = Font(bold=True, size=12)
    header_row = title_row + 1
    for c, name in enumerate(columns, start=1):
        cell = ws.cell(header_row, c, name)
        cell.font = Font(bold=True)
    for r_i, row in enumerate(rows):
        for c_i, value in enumerate(row):
            ws.cell(header_row + 1 + r_i, c_i + 1, value)
    next_row = header_row + 1 + max(len(rows), 0) + 2
    return title_row, header_row, next_row


def _is_kpi_panel(panel: dict[str, Any]) -> bool:
    return (
        (panel.get("chart_type") or "").lower() == "kpi"
        or (panel.get("display") or "").lower() == "kpi"
    )


def _add_chart_if_possible(
    summary: Worksheet,
    data: Worksheet,
    panel: dict[str, Any],
    header_row: int,
    n_rows: int,
    n_cols: int,
    anchor_row: int,
) -> int:
    """Best-effort native Excel chart. Returns rows consumed below the anchor."""
    if n_rows < 1 or n_cols < 2:
        return 0
    chart_type = (panel.get("chart_type") or "").lower()
    cats = Reference(data, min_col=1, min_row=header_row + 1, max_row=header_row + n_rows)
    vals = Reference(
        data,
        min_col=2,
        min_row=header_row,
        max_col=min(n_cols, 4),
        max_row=header_row + n_rows,
    )
    chart = None
    if chart_type in ("bar", "stacked_bar"):
        chart = BarChart()
        chart.type = "bar" if (panel.get("orientation") or "").lower() == "h" else "col"
        if chart_type == "stacked_bar":
            chart.grouping = "stacked"
    elif chart_type in ("line", "area"):
        chart = LineChart()
    elif chart_type in ("pie", "donut", "doughnut"):
        chart = PieChart()
    if chart is None:
        return 0
    chart.title = panel.get("title") or "Chart"
    chart.style = 10
    chart.width = 15
    chart.height = 8
    if isinstance(chart, PieChart):
        chart.add_data(vals, titles_from_data=True)
        chart.set_categories(cats)
    else:
        chart.add_data(vals, titles_from_data=True)
        chart.set_categories(cats)
        chart.shape = 4
    summary.add_chart(chart, f"A{anchor_row}")
    return 16


def build_dashboard_workbook(
    title: str,
    panels: list[dict[str, Any]],
    *,
    report_id: str | None = None,
) -> bytes:
    """Build an .xlsx from an already-run dashboard payload."""
    wb = Workbook()
    data = wb.active
    data.title = "Data"
    summary = wb.create_sheet("Summary", 0)

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    summary["A1"] = title or "Dashboard"
    summary["A1"].font = Font(bold=True, size=16)
    summary["A2"] = f"Exported {stamp}"
    if report_id:
        summary["A3"] = f"Report ID: {report_id}"

    # KPI strip on Summary
    kpi_row = 5
    summary.cell(kpi_row, 1, "Key metrics").font = Font(bold=True, size=12)
    kpi_row += 1
    wrote_kpi = False
    for panel in panels:
        if not _is_kpi_panel(panel) or panel.get("error"):
            continue
        columns = [str(c) for c in (panel.get("columns") or [])]
        rows = panel.get("rows") or []
        mi = next((i for i, c in enumerate(columns) if c.upper() == "METRIC"), 0)
        vi = next((i for i, c in enumerate(columns) if c.upper() == "VALUE"), 1)
        for row in rows:
            if not row:
                continue
            metric = row[mi] if mi < len(row) else None
            value = row[vi] if vi < len(row) else None
            summary.cell(kpi_row, 1, str(metric) if metric is not None else "")
            summary.cell(kpi_row, 2, value)
            kpi_row += 1
            wrote_kpi = True
    if not wrote_kpi:
        summary.cell(kpi_row, 1, "(No KPI panels)")
        kpi_row += 1

    # Panel index
    kpi_row += 1
    summary.cell(kpi_row, 1, "Panels").font = Font(bold=True, size=12)
    kpi_row += 1
    summary.cell(kpi_row, 1, "Title").font = Font(bold=True)
    summary.cell(kpi_row, 2, "Chart").font = Font(bold=True)
    summary.cell(kpi_row, 3, "Rows").font = Font(bold=True)
    summary.cell(kpi_row, 4, "Data block start").font = Font(bold=True)
    kpi_row += 1

    data_row = 1
    chart_anchor = kpi_row + len(panels) + 3
    for panel in panels:
        ptitle = str(panel.get("title") or "Panel")
        columns = [str(c) for c in (panel.get("columns") or [])]
        rows = list(panel.get("rows") or [])
        if panel.get("error"):
            columns = ["error"]
            rows = [[str(panel["error"])]]
        if not columns:
            columns = ["(empty)"]
            rows = []

        _, header_row, data_row = _write_panel_block(
            data, data_row, ptitle, columns, rows
        )
        # Widen a bit for readability
        for c in range(1, min(len(columns), 12) + 1):
            data.column_dimensions[get_column_letter(c)].width = max(
                data.column_dimensions[get_column_letter(c)].width or 10,
                min(len(columns[c - 1]) + 2, 28),
            )

        summary.cell(kpi_row, 1, ptitle)
        summary.cell(kpi_row, 2, panel.get("chart_type") or panel.get("display") or "")
        summary.cell(kpi_row, 3, len(rows))
        summary.cell(kpi_row, 4, header_row)
        kpi_row += 1

        if (
            not _is_kpi_panel(panel)
            and not panel.get("error")
            and (panel.get("display") or "chart") != "table"
        ):
            used = _add_chart_if_possible(
                summary,
                data,
                panel,
                header_row,
                len(rows),
                len(columns),
                chart_anchor,
            )
            if used:
                chart_anchor += used

    for c in range(1, 5):
        summary.column_dimensions[get_column_letter(c)].width = 28

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(title: str | None, report_id: str, ext: str = "xlsx") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (title or report_id).strip())[:60]
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    suffix = ext.lstrip(".") or "xlsx"
    return f"{slug or report_id}_{stamp}.{suffix}"


def _fmt_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if abs(value) >= 1_000_000:
            return f"{value:,.0f}"
        if abs(value) >= 1:
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:.4g}"
    return str(value)


#: The base-14 PDF fonts are Latin-1. Anything outside it renders as "?", and
#: planner-written titles routinely contain an em dash, so the characters that
#: actually turn up get folded to an ASCII equivalent rather than mangled.
_TEXT_SUBSTITUTIONS = {
    "•": "-", "·": "-", "–": "-", "—": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", "→": "->", " ": " ", "≥": ">=",
    "≤": "<=", "×": "x", "−": "-",
    # The base-14 PDF fonts are Latin-1. $ £ ¥ ¢ are in it and pass
    # through untouched; the euro is NOT — U+20AC arrived after Latin-1 was
    # fixed — and every symbol below would otherwise reach the page as "?",
    # turning a currency total into a number with no unit. The ISO code is
    # ugly and unambiguous, which is the right trade for money.
    "€": "EUR ", "₹": "INR ", "₽": "RUB ", "₩": "KRW ",
    "₪": "ILS ", "₺": "TRY ", "₫": "VND ", "฿": "THB ",
    "₦": "NGN ", "₴": "UAH ", "₱": "PHP ", "₡": "CRC ",
    "₸": "KZT ", "₮": "MNT ", "৳": "BDT ", "₲": "PYG ",
    "₼": "AZN ", "﷼": "SAR ",
}

_BODY = "helv"
_BOLD = "hebo"
_PAGE_W, _PAGE_H = 595.0, 842.0   # A4 portrait, points
_MARGIN = 40.0
#: Smallest chart worth drawing. The height itself comes from the chart's own
#: content (see pdf_charts.natural_height) and is then capped by the room left
#: on the page; this is only the floor below which axis labels stop being
#: readable. It was a fixed 165 when two panels shared a row.
_CHART_H_MIN = 120.0

#: Reserved at the foot of every page for the rule, the title and "Page n of m".
#: Content stops here rather than at the margin, or a table would run under it.
_FOOTER_H = 34.0

#: Columns one table can carry. Past this the cells are narrower than the text
#: in them at 7.5pt, so more columns means less readable data, not more. What is
#: dropped is reported under the table rather than lost quietly.
_MAX_TABLE_COLUMNS = 8

#: Rows one PDF table draws. A dashboard panel can return thousands of rows,
#: and drawing them all would put a hundred pages under one chart. The rest
#: are reported under the table; the Excel export carries every row.
_MAX_PDF_TABLE_ROWS = 500

_TABLE_FONT_SIZE = 7.5
#: One table row, header line included. Module level because the page layout
#: needs a table's height before the table is drawn, to know what is left for
#: the chart above it.
_TABLE_LINE_H = _TABLE_FONT_SIZE * 1.65

_INK = (0.10, 0.11, 0.14)
_INK_SOFT = (0.24, 0.25, 0.29)
_MUTED = (0.45, 0.46, 0.50)
_HEADER_FILL = (0.945, 0.951, 0.961)
_TILE_FILL = (0.965, 0.970, 0.977)
_RULE = (0.78, 0.79, 0.82)
_RULE_LIGHT = (0.88, 0.89, 0.91)


def _pdf_text(value: Any) -> str:
    text = str(value if value is not None else "")
    for bad, good in _TEXT_SUBSTITUTIONS.items():
        text = text.replace(bad, good)
    # Anything still outside Latin-1 would render as a black box or "?".
    return text.encode("latin-1", "replace").decode("latin-1")


def _cell_text(value: Any) -> str:
    return _pdf_text(_fmt_cell(value))


def _as_number(value: Any) -> float | None:
    """A float, or None. Booleans excluded — a flag is not a measure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _is_numeric_column(rows: list[list[Any]], index: int) -> bool:
    seen = False
    for row in rows:
        if index >= len(row) or row[index] is None or row[index] == "":
            continue
        if _as_number(row[index]) is None:
            return False
        seen = True
    return seen


def _decimal_places(rows: list[list[Any]], index: int) -> int:
    """2 when any value in the column has a fractional part, else 0.

    Applied per COLUMN rather than per cell so a money column reads
    135,688.10 / 87,999.80 instead of 135,688.1 / 87,999.8.
    """
    for row in rows:
        if index >= len(row):
            continue
        number = _as_number(row[index])
        if number is not None and abs(number - round(number)) > 1e-9:
            return 2
    return 0


def _fmt_fixed(value: Any, places: int) -> str:
    number = _as_number(value)
    if number is None:
        return _cell_text(value)
    return f"{number:,.{places}f}"


def _header_words(name: str) -> list[str]:
    """A column name split where it can legibly break.

    Warehouse columns are SNAKE_CASE, so the underscore is the natural break —
    and the separator is kept on the leading part so a reader can see the name
    was cut rather than guess whether a word is missing.
    """
    text = _pdf_text(name)
    parts = [p for p in text.split("_") if p]
    if len(parts) <= 1:
        return [text]
    return [p + "_" for p in parts[:-1]] + [parts[-1]]


def _wrap_header(name: str, avail: float, size: float,
                 max_lines: int = 3) -> list[str]:
    """Header text over up to `max_lines` lines, ellipsised if it still spills.

    Never drops a segment silently: stopping at two lines turned
    TOTAL_INVOICE_AMOUNT into "TOTAL_ INVOICE_", which does not read as
    shortened — it reads as a different column.
    """
    import pymupdf

    def fits(candidate: str) -> bool:
        return pymupdf.get_text_length(
            candidate, fontname=_BOLD, fontsize=size) <= avail

    text = _pdf_text(name)
    if fits(text):
        return [text]

    lines: list[str] = []
    current = ""
    for word in _header_words(name):
        trial = current + word
        if fits(trial) or not current:
            current = trial
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)

    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    kept[-1] = kept[-1] + "..."
    return kept


def _fit_widths(natural: list[float], available: float, floor: float,
                numeric: list[bool] | None = None,
                ideal: list[float] | None = None) -> list[float]:
    """Column widths that span `available` exactly, whichever way they miss it.

    TOO WIDE. Scaling every column by the same ratio is the obvious approach and
    the wrong one: it shrinks a 50-point invoice-id column by the same 40% as a
    200-point name column, so BOTH end up truncated and a date reads "2026-0...".
    Here a column that already fits inside an equal share keeps its full width,
    and the columns that do not repeatedly divide what is left. Only genuinely
    wide text gets clipped, which is the only place clipping loses nothing.

    TOO NARROW. Leaving the natural widths alone drew a two-column table 100
    points wide against the left margin of a 515-point page, with four fifths of
    the sheet blank beside it — which reads as a broken export rather than a
    small table, and left no two tables in the document sharing an edge. The
    surplus is handed to the TEXT columns: a label is what benefits from room,
    where a right-aligned number would only drift further from its own header.
    """
    if not natural:
        return []

    if sum(natural) > available:
        widths = list(natural)
        unsettled = set(range(len(natural)))
        remaining = available

        while unsettled:
            share = remaining / len(unsettled)
            fits = {i for i in unsettled if natural[i] <= share}
            if not fits:
                # Everyone left wants more than their share: split it evenly.
                for i in unsettled:
                    widths[i] = max(share, floor)
                break
            for i in fits:
                widths[i] = natural[i]
                remaining -= natural[i]
            unsettled -= fits
        return widths

    widths = list(natural)
    surplus = available - sum(natural)

    # First buy every column a header that fits on ONE line. A column sized by
    # its data alone is often narrower than its own name, so PROPERTY_COUNT
    # broke over two lines beside 400 points of unused paper.
    if ideal:
        need = [max(0.0, ideal[i] - widths[i]) for i in range(len(widths))]
        wanted = sum(need)
        if wanted > 0:
            take = min(surplus, wanted)
            for i, want in enumerate(need):
                widths[i] += take * (want / wanted)
            surplus -= take

    flags = numeric if numeric and len(numeric) == len(natural) else [False] * len(natural)
    # An all-numeric table has no label column to absorb the room, so there the
    # columns share it out between them.
    growers = [i for i, is_number in enumerate(flags) if not is_number]
    if not growers:
        growers = list(range(len(natural)))

    base = sum(widths[i] for i in growers)
    for i in growers:
        share = (widths[i] / base) if base else (1.0 / len(growers))
        widths[i] += surplus * share
    return widths


#: Currency symbols the base-14 PDF fonts can actually print.
#:
#: Those fonts are Latin-1. "$" (U+0024), "£" (U+00A3) and "¥" (U+00A5) are in
#: it; "€" (U+20AC) is NOT — it postdates the encoding — and reaches the page as
#: "?", which is the same failure that once turned every em dash into a question
#: mark. So the symbol is used only where it survives, and every other currency
#: prints its ISO code instead.
_PDF_CURRENCY_SYMBOL = {"USD": "$", "GBP": "\u00a3", "JPY": "\u00a5"}


def _currency_prefix(code: Any) -> str:
    """What goes before the digits, for the PDF specifically.

    Absent means dollars — the default every dashboard written before the
    currency field relies on. An empty string is the author saying they cannot
    name one, and prints nothing. A malformed code prints nothing either: a
    bare number is unhelpful, but "US 1,200" reads as a unit that does not
    exist.
    """
    if code is None:
        return "$"
    text = str(code).strip().upper()
    if text == "":
        return ""
    if len(text) != 3 or not (text.isascii() and text.isalpha()):
        return ""
    # A trailing space so a three-letter code does not run into the digits.
    return _PDF_CURRENCY_SYMBOL.get(text, f"{text} ")


def _format_pdf_cell(value: Any, column: str, formats: dict[str, Any],
                     is_numeric: bool, places: int,
                     date_grain: str | None = None,
                     default_currency: str | None = None) -> str:
    """One table cell, preferring the panel's DECLARED format for its column.

    The same rule the on-screen table follows: an authored format wins, and a
    column with none falls back to the measured decimals. Without this a
    dashboard whose columns were formatted as currency exported as bare
    numbers — the formatting was visible on screen and gone in the file.
    """
    spec = formats.get(column) if isinstance(formats, dict) else None
    if isinstance(spec, dict):
        number = _as_number(value)
        if number is not None:
            declared = str(spec.get("format") or "").lower()
            decimals = spec.get("decimals")
            if declared == "currency":
                dp = decimals if isinstance(decimals, int) else (
                    2 if abs(number) % 1 else 0
                )
                sign = "-" if number < 0 else ""
                # `in` not truthiness: a declared EMPTY code means "no
                # symbol" and must not fall through to the database default.
                prefix = _currency_prefix(
                    spec["currency"] if "currency" in spec else default_currency
                )
                return f"{sign}{prefix}{abs(number):,.{dp}f}"
            if declared == "percent":
                dp = decimals if isinstance(decimals, int) else 1
                return f"{number:,.{dp}f}%"
            if declared == "ratio":
                return f"{number:,.{decimals if isinstance(decimals, int) else 2}f}x"
            if declared in ("number", "date", "text") or decimals is not None:
                dp = decimals if isinstance(decimals, int) else places
                return f"{number:,.{dp}f}"
    if date_grain:
        from app.engine.pdf_charts import _date_label

        # A table keeps the year on a daily value; only an axis drops it, and
        # only because tick width is scarce.
        shown = _date_label(value, date_grain)
        if date_grain == "day":
            text = str(value).strip()[:10]
            if len(text) == 10:
                shown = f"{text[5:7]}-{text[8:10]}-{text[0:4]}"
        return _pdf_text(shown)
    if is_numeric:
        return _fmt_fixed(value, places)
    return _cell_text(value)


def _fmt_kpi(value: Any, fmt: Any, default_currency: str | None = None) -> str:
    """A headline number rendered the way the dashboard renders it.

    The KPI rows carry a FORMAT column precisely so the renderer does not have
    to guess from the label. Ignoring it printed a currency total as a bare
    3850309, which is the one figure a reader opens the report for.
    """
    number = _as_number(value)
    if number is None:
        return _cell_text(value)

    # A KPI row has no column_formats to hang a currency on, so the code rides
    # with the word: "currency:AED". Bare "currency" is dollars, and
    # "currency:" with nothing after it prints no symbol at all.
    raw = str(fmt or "").strip()
    word, _, code = raw.partition(":")
    kind = word.strip().lower()
    # No colon at all means the row said nothing about currency, so the
    # database's declared code applies. A colon — even with nothing after it —
    # is a statement, and wins.
    currency = code.strip() if ":" in raw else default_currency

    if kind == "percent":
        return f"{number:,.1f}%".replace(".0%", "%")
    if kind == "ratio":
        return f"{number:,.2f}x"
    if kind == "count":
        return f"{number:,.0f}"
    if kind == "currency":
        # The sign goes OUTSIDE the symbol. This branch formatted the signed
        # number straight after the "$" and produced "$-5.20M", which the rule
        # right above it had forbidden since it was written.
        prefix = _currency_prefix(currency)
        sign = "-" if number < 0 else ""
        magnitude = abs(number)
        if magnitude >= 1_000_000_000:
            return f"{sign}{prefix}{magnitude / 1_000_000_000:,.2f}B"
        if magnitude >= 1_000_000:
            return f"{sign}{prefix}{magnitude / 1_000_000:,.2f}M"
        if magnitude >= 10_000:
            return f"{sign}{prefix}{magnitude:,.0f}"
        return f"{sign}{prefix}{magnitude:,.2f}"
    return _fmt_cell(value)


def _image_aspect(data: bytes) -> float | None:
    """Height over width of a PNG, or None when it will not decode."""
    import pymupdf

    try:
        pixmap = pymupdf.Pixmap(data)
        return pixmap.height / pixmap.width if pixmap.width > 0 else None
    except Exception:  # noqa: BLE001 - an unreadable picture has no shape
        return None


def _draw_panel_image(page, rect, data: bytes) -> bool:
    """Place a client-rendered PNG inside `rect`, keeping its aspect ratio.

    False when the bytes are not a usable image, so the caller falls through to
    the vector renderer and then to the table — a corrupt upload must not cost
    the panel entirely.
    """
    import pymupdf

    try:
        pixmap = pymupdf.Pixmap(data)
        if pixmap.width <= 0 or pixmap.height <= 0:
            return False
        # Fit rather than fill: stretching a map to a fixed box distorts the
        # geography, which is the one thing a map is for.
        scale = min(rect.width / pixmap.width, rect.height / pixmap.height)
        width = pixmap.width * scale
        height = pixmap.height * scale
        left = rect.x0 + (rect.width - width) / 2
        top = rect.y0 + (rect.height - height) / 2
        page.insert_image(
            pymupdf.Rect(left, top, left + width, top + height), stream=data
        )
        return True
    except Exception:  # noqa: BLE001 - any decode failure is just "no image"
        log.exception("could not place a panel image")
        return False


def _panels_by_section(
    panels: list[dict[str, Any]]
) -> list[tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]]:
    """(section, kpi panels, body panels) in the order the sections appear.

    Mirrors the dashboard's own tabs: a reader who arranged panels into
    Overview / Risk / Detail should get that structure on paper rather than one
    long undifferentiated run.
    """
    order: list[str | None] = []
    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for panel in panels:
        raw = panel.get("section")
        name = raw.strip() if isinstance(raw, str) and raw.strip() else None
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        buckets[name].append(panel)
    return [
        (name,
         [p for p in buckets[name] if _is_kpi_panel(p)],
         [p for p in buckets[name] if not _is_kpi_panel(p)])
        for name in order
    ]


def _wants_full_width(panel: dict[str, Any]) -> bool:
    """A panel that cannot share a row.

    Tables need the width for their columns, and a panel showing BOTH a chart
    and its table stays whole so the two are never split across a row boundary.
    A stored span of 3 or 4 is the author saying the same thing.
    """
    display = str(panel.get("display") or "").lower()
    if display in ("table", "both") or not panel.get("chart_type"):
        return True
    span = panel.get("span")
    return not isinstance(span, int) or span > 2


def build_dashboard_pdf(
    title: str,
    panels: list[dict[str, Any]],
    *,
    report_id: str | None = None,
    narrative: str | None = None,
    panel_images: dict[str, bytes] | None = None,
    panel_tables: dict[str, dict[str, Any]] | None = None,
) -> bytes:
    """A report of the live dashboard: KPI tiles, charts, and ruled tables.

    Laid out rather than listed. Panels keep the dashboard's own sections as
    headings, half-width charts sit two to a row the way they do on screen, and
    every page carries a footer, so the file reads as a document instead of a
    scroll of panels.

    `panel_images` carries a PNG of each chart as the dashboard draws it, keyed
    by panel title. A chart with a picture uses it; one without is drawn here,
    and failing that its table stands in and says so. The pictures are what
    keep the file's charts identical to the screen's: labels, markers,
    reference lines and axis settings exist in one renderer only.

    `panel_tables` carries each table's headers and cells as the dashboard
    writes them, so the file's "Net Sales" reads 2.86% where the screen does.
    A panel without one has its rows formatted here. Titles are the key because
    that is what a reader matches a picture to, and panel order is not stable
    across a re-run.
    """
    import pymupdf

    from app.engine import pdf_charts

    doc = pymupdf.open()
    page = doc.new_page(width=_PAGE_W, height=_PAGE_H)
    y = _MARGIN
    width = _PAGE_W - 2 * _MARGIN
    gutter = 16.0

    def new_page() -> None:
        nonlocal page, y
        page = doc.new_page(width=_PAGE_W, height=_PAGE_H)
        y = _MARGIN

    def room(need: float) -> None:
        if y + need > _PAGE_H - _FOOTER_H:
            new_page()

    def wrap(text: str, avail: float, size: float, font: str) -> list[str]:
        """Lines that fit `avail`, measured in the real font.

        The caller needs the height BEFORE drawing or a block cannot be
        paginated, which is why this is not left to insert_textbox.
        """
        out: list[str] = []
        for paragraph in text.splitlines() or [""]:
            current = ""
            for word in paragraph.split():
                trial = f"{current} {word}".strip()
                if pymupdf.get_text_length(trial, fontname=font, fontsize=size) <= avail:
                    current = trial
                    continue
                if current:
                    out.append(current)
                current = word
            out.append(current)
        return out or [""]

    def write(text: str, *, size: float = 9.5, bold: bool = False,
              color=_INK, x: float = _MARGIN, avail: float | None = None,
              gap: float = 4.0) -> None:
        nonlocal y
        font = _BOLD if bold else _BODY
        leading = size * 1.35
        for line in wrap(_pdf_text(text), avail or width, size, font):
            room(leading)
            page.insert_text((x, y + size), line, fontsize=size,
                             fontname=font, color=color)
            y += leading
        y += gap

    def rule(colour=_RULE, thickness: float = 0.6) -> None:
        nonlocal y
        shape = page.new_shape()
        shape.draw_line(pymupdf.Point(_MARGIN, y), pymupdf.Point(_MARGIN + width, y))
        shape.finish(color=colour, width=thickness)
        shape.commit()
        y += 8

    # One dashboard reports in one currency, so the first panel that names
    # one speaks for the document. Panels carry it from run_dashboard.
    doc_currency = next(
        (p.get("default_currency") for p in panels if p.get("default_currency")),
        None,
    )

    def kpi_tiles(kpis: list[dict[str, Any]]) -> None:
        """Headline numbers as a band of tiles, the way the dashboard shows them.

        A bulleted list of "metric: value" buried the figures a reader opens the
        report for; tiles put them where the eye lands first.
        """
        nonlocal y
        # The dashboard's own tile formatting, so the file's figure matches the
        # tile a reader saw: AED 235.9M, not AED 235.87M.
        from app.engine.narrative_figures import show_kpi

        facts: list[tuple[str, str]] = []
        for panel in kpis:
            if panel.get("error"):
                continue
            columns = [str(c) for c in (panel.get("columns") or [])]
            mi = next((i for i, c in enumerate(columns) if c.upper() == "METRIC"), 0)
            vi = next((i for i, c in enumerate(columns) if c.upper() == "VALUE"), 1)
            fi = next((i for i, c in enumerate(columns) if c.upper() == "FORMAT"), -1)
            for row in panel.get("rows") or []:
                if not row:
                    continue
                label = _pdf_text(row[mi] if mi < len(row) else "")
                fmt = row[fi] if 0 <= fi < len(row) else None
                value = _pdf_text(
                    show_kpi(row[vi] if vi < len(row) else "", fmt, doc_currency)
                )
                facts.append((label, value))
        if not facts:
            return

        per_row = 3 if len(facts) % 3 == 0 or len(facts) > 4 else max(len(facts), 1)
        tile_w = (width - gutter * (per_row - 1)) / per_row
        tile_h = 42.0
        for start in range(0, len(facts), per_row):
            chunk = facts[start:start + per_row]
            room(tile_h + 6)
            box = page.new_shape()
            for i, _ in enumerate(chunk):
                x = _MARGIN + i * (tile_w + gutter)
                box.draw_rect(pymupdf.Rect(x, y, x + tile_w, y + tile_h))
                box.finish(fill=_TILE_FILL, color=_RULE_LIGHT, width=0.5)
            box.commit()
            for i, (label, value) in enumerate(chunk):
                x = _MARGIN + i * (tile_w + gutter)
                page.insert_text((x + 9, y + 15), label[:38], fontsize=7.5,
                                 fontname=_BODY, color=_MUTED)
                page.insert_text((x + 9, y + 33), value, fontsize=13,
                                 fontname=_BOLD, color=_INK)
            y += tile_h + 8
        y += 4

    def table(columns: list[str], rows: list[list[Any]],
              formats: dict[str, Any] | None = None,
              prepared: dict[str, Any] | None = None) -> None:
        """A ruled table: header band, row separators, and an outer border.

        Every row is drawn, and the header repeats on each page a long table
        runs onto: the export stands in for the dashboard, so a reader should
        not have to go back to the screen for the rows below a cut.
        """
        nonlocal y
        size = _TABLE_FONT_SIZE
        line_h = _TABLE_LINE_H
        if prepared is not None:
            # Written by the page: headers and cells exactly as the dashboard's
            # own table shows them.
            labels = [_pdf_text(h) for h in prepared["headers"][:_MAX_TABLE_COLUMNS]]
            shown = labels
            numeric = list(prepared["numeric"][:len(shown)])
            cells = [[_pdf_text(c) for c in row[:len(shown)]] for row in prepared["rows"]]
        else:
            shown = columns[:_MAX_TABLE_COLUMNS]
            formats = formats or {}
            numeric = [_is_numeric_column(rows, i) for i in range(len(shown))]
            # Per column, so a SALE_MONTH column reads 02-2025 here exactly as it
            # does on the chart above it. A day that is a storage convention is
            # not information, and showing it contradicts the column's own name.
            from app.engine.pdf_charts import _date_granularity
            grains = [
                _date_granularity([str(r[i]) for r in rows if i < len(r)])
                for i in range(len(shown))
            ]
            # One decimal convention per column: a money column reading 135,688.10
            # and 87,999.80 lines up, where stripping trailing zeros does not.
            places = [_decimal_places(rows, i) if numeric[i] else 0
                      for i in range(len(shown))]
            cells = [
                [_format_pdf_cell(r[i], shown[i], formats, numeric[i], places[i],
                                  grains[i], doc_currency)
                 if i < len(r) else ""
                 for i in range(len(shown))]
                for r in rows
            ]

            # A declared label replaces the SQL alias in the header, matching the
            # on-screen table. The alias itself is untouched, so column lookups and
            # cross-filters still match.
            labels = [(formats.get(shown[i]) or {}).get("label") or shown[i]
                      for i in range(len(shown))]

        # Column width is driven by the DATA, not the header. A name like
        # TOTAL_INVOICE_AMOUNT is far wider than the numbers under it, and
        # letting it set the width steals room from every other column; it
        # wraps onto a second header line instead.

        wants: list[float] = []
        ideal: list[float] = []
        for i in range(len(shown)):
            widest_cell = max(
                [pymupdf.get_text_length(c[i], fontname=_BODY, fontsize=size)
                 for c in cells] or [0.0]
            )
            # A single header word cannot wrap, so it still sets a floor.
            longest_word = max(
                [pymupdf.get_text_length(w, fontname=_BOLD, fontsize=size)
                 for w in _header_words(labels[i])] or [0.0]
            )
            wants.append(max(widest_cell, longest_word) + 12)
            # What the column would need for its whole header on one line —
            # granted only out of space nothing else wants.
            ideal.append(max(
                wants[i],
                pymupdf.get_text_length(_pdf_text(labels[i]), fontname=_BOLD,
                                        fontsize=size) + 12,
            ))

        # Fills the content width, so every table in the document starts at the
        # left margin and ends at the right one and two tables line up.
        widths = _fit_widths(wants, width, floor=34.0, numeric=numeric,
                             ideal=ideal)
        table_w = sum(widths)

        header_lines = [
            _wrap_header(labels[i], widths[i] - 8, size)
            for i in range(len(shown))
        ]
        header_rows = max((len(h) for h in header_lines), default=1)
        header_h = line_h * header_rows

        def fit(text: str, avail: float, font: str) -> str:
            if pymupdf.get_text_length(text, fontname=font, fontsize=size) <= avail:
                return text
            out = text
            while out and pymupdf.get_text_length(out + "...", fontname=font,
                                                  fontsize=size) > avail:
                out = out[:-1]
            return (out + "...") if out else ""

        def draw_row(values: list[str], font: str, colour, fill=None) -> None:
            nonlocal y
            top = y
            if fill is not None:
                band = page.new_shape()
                band.draw_rect(pymupdf.Rect(_MARGIN, top, _MARGIN + table_w,
                                            top + line_h))
                band.finish(fill=fill, color=None)
                band.commit()
            x = _MARGIN
            for i, value in enumerate(values):
                text = fit(value, widths[i] - 8, font)
                if numeric[i]:
                    tw = pymupdf.get_text_length(text, fontname=font, fontsize=size)
                    page.insert_text((x + widths[i] - 6 - tw, top + size + 2),
                                     text, fontsize=size, fontname=font, color=colour)
                else:
                    page.insert_text((x + 5, top + size + 2), text,
                                     fontsize=size, fontname=font, color=colour)
                x += widths[i]
            y += line_h

        def draw_grid(top: float, bottom: float, rules: list[float]) -> None:
            """Borders per page-slice, so a table split across pages never tries
            to stroke a rectangle taller than the page."""
            grid = page.new_shape()
            for ry in rules:
                grid.draw_line(pymupdf.Point(_MARGIN, ry),
                               pymupdf.Point(_MARGIN + table_w, ry))
                grid.finish(color=_RULE_LIGHT, width=0.4)
            x = _MARGIN
            for w in widths[:-1]:
                x += w
                grid.draw_line(pymupdf.Point(x, top), pymupdf.Point(x, bottom))
                grid.finish(color=_RULE_LIGHT, width=0.4)
            grid.draw_rect(pymupdf.Rect(_MARGIN, top, _MARGIN + table_w, bottom))
            grid.finish(color=_RULE, width=0.6)
            grid.commit()

        def draw_header() -> None:
            nonlocal y
            top = y
            band = page.new_shape()
            band.draw_rect(pymupdf.Rect(_MARGIN, top, _MARGIN + table_w, top + header_h))
            band.finish(fill=_HEADER_FILL, color=None)
            band.commit()
            x = _MARGIN
            for i, lines in enumerate(header_lines):
                for row, text in enumerate(lines):
                    if numeric[i]:
                        tw = pymupdf.get_text_length(text, fontname=_BOLD, fontsize=size)
                        at = x + widths[i] - 6 - tw
                    else:
                        at = x + 5
                    page.insert_text((at, top + size + 2 + row * line_h), text,
                                     fontsize=size, fontname=_BOLD, color=_INK)
                x += widths[i]
            y += header_h

        room(header_h + line_h * 2)
        slice_top = y
        rules: list[float] = []
        draw_header()
        rules.append(y)

        for values in cells:
            if y + line_h > _PAGE_H - _FOOTER_H:
                draw_grid(slice_top, y, rules)
                new_page()
                slice_top = y
                rules = []
                draw_header()
                rules.append(y)
            draw_row(values, _BODY, _INK_SOFT)
            rules.append(y)

        # The line closing the last row is the border itself.
        draw_grid(slice_top, y, rules[:-1])
        y += 8

    def panel_note(panel: dict[str, Any], rows: list[list[Any]],
                   total_columns: int = 0, fetched: int | None = None) -> None:
        notes: list[str] = []
        total_rows = max(panel.get("row_count") or 0, fetched or 0, len(rows))
        if total_rows > len(rows):
            # Fewer rows drawn than the panel has, whether cut here or
            # upstream, so say that rather than implying these are all of them.
            notes.append(f"{len(rows):,} of {total_rows:,} rows")
        if panel.get("truncated"):
            # The warehouse stopped at its own cap, so row_count EQUALS the rows
            # we were handed and the comparison above cannot see it. Unsaid, the
            # export presents a cut-off panel as the whole answer — and a PDF
            # outlives the screen it was taken from.
            notes.append(
                "only what the query's row limit allows, so the rest are missing")
        if total_columns > _MAX_TABLE_COLUMNS:
            # Dropping columns silently is how a table looks complete and is
            # not; past this many the text is too narrow to read anyway.
            notes.append(
                f"first {_MAX_TABLE_COLUMNS} of {total_columns} columns")
        if notes:
            extra = (" The Excel export has every row."
                     if fetched is not None and fetched > len(rows) else "")
            write(f"Showing {' and '.join(notes)}.{extra}",
                  size=7.5, color=_MUTED, gap=6)

    # ---- header -------------------------------------------------------------

    stamp = datetime.now(UTC).strftime("%d %B %Y, %H:%M UTC")
    write(title or "Dashboard", size=18, bold=True, gap=3)
    meta = f"Exported {stamp}"
    if report_id:
        meta += f"   -   {report_id}"
    write(meta, size=8, color=_MUTED, gap=6)
    rule()

    if narrative:
        write("Summary", size=11.5, bold=True, gap=3)
        write(narrative, size=9.5, color=_INK_SOFT, gap=12)

    #: True while the current page carries only preamble — the report header, a
    #: section heading, a band of KPI tiles. The next panel then continues on
    #: this page instead of opening a new one, which is what stops a heading
    #: being left stranded at the foot of a page with its panels overleaf.
    page_is_fresh = True

    sections = _panels_by_section(panels)
    multi_section = len([name for name, _, _ in sections if name]) > 1

    for name, kpis, body in sections:
        if multi_section and name:
            if not page_is_fresh:
                new_page()
            # Mid-page — under the report header or a long narrative — the
            # heading still has to arrive with its KPIs and its first chart, or
            # it reads as a mistake. Ask that chart how tall it will be rather
            # than reserving a nominal block.
            lead = next((p for p in body
                         if not p.get("error") and p.get("rows")
                         and str(p.get("display") or "").lower() != "table"), None)
            lead_h = min(pdf_charts.natural_height(lead, width), 300.0) if lead else 0.0
            room(40 + (52 if kpis else 0) + 22 + lead_h + 44)
            page_is_fresh = True
            write(name, size=13, bold=True, gap=4)
            rule(_RULE_LIGHT, 0.5)

        kpi_tiles(kpis)

        index = 0
        while index < len(body):
            panel = body[index]
            columns = [str(c) for c in (panel.get("columns") or [])]
            rows = list(panel.get("rows") or [])

            if panel.get("error"):
                room(40)
                write(str(panel.get("title") or "Panel"), size=10.5, bold=True, gap=3)
                write(f"Error: {panel['error']}", size=8.5,
                      color=(0.70, 0.15, 0.15), gap=10)
                index += 1
                continue
            if not columns or not rows:
                room(40)
                write(str(panel.get("title") or "Panel"), size=10.5, bold=True, gap=3)
                write("No rows returned.", size=8.5, color=_MUTED, gap=10)
                index += 1
                continue

            # ONE PANEL PER PAGE.
            #
            # Panels used to be packed two-up to mirror the dashboard's grid.
            # On screen that works because the data behind a chart is one click
            # away; on paper the tables ended up detached from the charts they
            # explained, and a reader could not tell which numbers belonged to
            # which picture. A page per panel costs paper and answers that
            # question by construction.
            if not page_is_fresh:
                new_page()
            page_is_fresh = False

            # Reserve for what will actually be drawn. `_wants_full_width` is
            # about column pairing, not height, so keying the reservation off it
            # gave a full-width CHART panel 60 points for a 165-point chart.
            chart_type = str(panel.get("chart_type") or "").lower()
            image = (panel_images or {}).get(str(panel.get("title") or ""))
            prepared = (panel_tables or {}).get(str(panel.get("title") or ""))
            # The dashboard draws a chart whenever the panel names one, whatever
            # `display` says (see DashboardPanelCard), so the file does too.
            draws_chart = bool(chart_type) and chart_type != "table"
            will_chart = draws_chart and (
                chart_type in pdf_charts.RENDERABLE or image is not None
            )
            room(22 + (_CHART_H_MIN + 12 if will_chart else 0) + 44)
            write(str(panel.get("title") or "Panel"), size=10.5, bold=True, gap=3)

            # The chart asks for the height its own content needs; the page
            # only ever takes height away. Handing it the leftover space
            # instead stretched twelve bars into slabs and a three-slice donut
            # across a whole sheet. The table's height is predictable — a
            # header band plus a line per row — so what is left is known
            # before either is drawn.
            shown_rows = rows[:_MAX_PDF_TABLE_ROWS]
            table_rows = len(prepared["rows"]) if prepared is not None else len(shown_rows)
            # Only a short table is kept on the chart's page. A long one runs on
            # to the next pages anyway, and reserving all of it squeezed the
            # chart above it down to its minimum height.
            table_h = _TABLE_LINE_H * (2 + min(table_rows, 6))
            spare = (_PAGE_H - _FOOTER_H) - y - table_h - 20
            aspect = _image_aspect(image) if image is not None else None
            wanted = (width * aspect if aspect
                      else pdf_charts.natural_height(panel, width))
            chart_h = max(_CHART_H_MIN, min(wanted, spare))

            drew_chart = False
            if draws_chart:
                rect = pymupdf.Rect(_MARGIN, y, _MARGIN + width, y + chart_h)
                if image is not None:
                    # A picture the client rendered, for shapes with no vector
                    # renderer here. Preferred over the table fallback: a map
                    # redrawn from its numbers is not a map.
                    drew_chart = _draw_panel_image(page, rect, image)
                if not drew_chart:
                    drew_chart = pdf_charts.draw_panel_chart(page, rect, panel)
                if drew_chart:
                    y += chart_h + 12
                elif chart_type:
                    # Say so rather than quietly substituting a table. A chart
                    # vanishing without explanation is how a whole tab looked
                    # like it had lost its graphs.
                    write(f"[{chart_type} charts are shown as a table in PDF]",
                          size=7.5, color=_MUTED, gap=4)

            # EVERY panel carries its table, not only the ones an author marked
            # "table" or "both". On screen a chart's numbers are one disclosure
            # click away; on paper there is nowhere to click, so a chart-only
            # panel exported as a picture of data nobody could read off it. The
            # page-per-panel layout is what makes this affordable: the table
            # sits under the chart it belongs to.
            if prepared is not None:
                table(columns, [], prepared=prepared)
                panel_note(panel, prepared["rows"], len(prepared["headers"]),
                           fetched=len(rows))
            else:
                table(columns, shown_rows, panel.get("column_formats"))
                panel_note(panel, shown_rows, len(columns), fetched=len(rows))
            y += 8
            index += 1

    # ---- footers ------------------------------------------------------------
    # Added last, when the page count is finally known.
    total_pages = doc.page_count
    footer = _pdf_text(title or "Dashboard")
    for number, each in enumerate(doc, start=1):
        line = each.new_shape()
        line.draw_line(pymupdf.Point(_MARGIN, _PAGE_H - _FOOTER_H + 6),
                       pymupdf.Point(_PAGE_W - _MARGIN, _PAGE_H - _FOOTER_H + 6))
        line.finish(color=_RULE_LIGHT, width=0.5)
        line.commit()
        each.insert_text((_MARGIN, _PAGE_H - _FOOTER_H + 18), footer[:70],
                         fontsize=7.5, fontname=_BODY, color=_MUTED)
        label = f"Page {number} of {total_pages}"
        tw = pymupdf.get_text_length(label, fontname=_BODY, fontsize=7.5)
        each.insert_text((_PAGE_W - _MARGIN - tw, _PAGE_H - _FOOTER_H + 18),
                         label, fontsize=7.5, fontname=_BODY, color=_MUTED)

    buf = io.BytesIO()
    # Compressed. Without it every chart picture is stored as raw pixels, about
    # 3 MB for one 1440-pixel chart: a 20-chart dashboard came out at 70 MB.
    # Deflate is lossless, so the charts are unchanged, and garbage=3 stores a
    # font or picture used on several pages once.
    doc.save(buf, deflate=True, garbage=3)
    doc.close()
    return buf.getvalue()

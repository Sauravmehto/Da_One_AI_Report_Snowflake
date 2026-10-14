"""The figures an AI dashboard summary is written from.

The summary used to be handed raw numbers and the first five rows of each
chart. Both went wrong in front of an executive:

- A KPI of 235930000.12 arrived bare, and the model wrote "AED 235.9 billion"
  under a tile that said AED 235.9M. It had to choose the scale itself.
- Charts are ordered oldest-first, so the first five rows of a monthly series
  are its first five MONTHS. The model described February to June 2025 as the
  trend, "through June", on a dashboard that ran to August 2026.

So the summary now receives what a reader would: every number already written
out in its unit and scale, and for each chart the handful of figures a
paragraph is made from -- where a series started, where it is now, how far it
moved, its high and low; or, for a breakdown, its top and bottom items. The
model quotes these; it never has to compute or rescale one.
"""

from __future__ import annotations

import calendar
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.engine.dashboard_export import _as_number, _currency_prefix

#: Series described per chart. A panel of eight measures is summarised by its
#: first three; the paragraph is two to four sentences for the whole dashboard.
MAX_SERIES = 3
#: Items listed for a breakdown.
TOP_ITEMS = 3
BOTTOM_ITEMS = 2

# The same name rules as classify() in frontend/src/lib/dashboard-format.ts, so
# a figure is labelled the way that number is labelled on screen.
_PERCENT = re.compile(r"(PCT|PERCENT|MARGIN|YIELD|_RATE\b|^RATE|RATE_|DELINQ|\bIRR\b)")
_RATIO = re.compile(r"(DSCR|LTV|TVPI|DPI|RVPI|MOIC|RATIO|LEVERAGE|COVERAGE)")
_CURRENCY = re.compile(
    r"(PRINCIPAL|BALANCE|VALUE|EXPENSE|NAV|AMOUNT|PRICE|REVENUE|EBITDA|PROCEEDS|CASH|FEE|"
    r"GAIN|LOSS|G/L|CAPITAL|COST|EXPOSURE|FAIR_?VALUE|PROFIT|DEBT|EQUITY)"
)
#: A trailing ISO code names the currency outright: GMV_AED is dirhams. The
#: screen's name rules miss GMV; the summary should not repeat that.
_CURRENCY_SUFFIX = re.compile(r"_(AED|BHD|EUR|GBP|INR|KWD|OMR|QAR|SAR|USD)$")

#: Values that do not add up to a total: averages, per-unit figures, scores and
#: durations. A share of "total AOV" is a number with no meaning.
_NOT_ADDITIVE = re.compile(
    r"(AVG|AVERAGE|MEAN|MEDIAN|AOV|ATV|ARPU|PER_|_PER\b|BASKET|SCORE|INDEX|NPS|CSAT|"
    r"RATING|DAYS|HOURS|MINUTES|DURATION|TIME|AGE\b|RANK|SPEND_PER)"
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?")
_MONTH_START = re.compile(r"^(\d{4})-(\d{2})-01(?:[T ]00:00(?::00(?:\.0+)?)?)?$")
_PERIOD_LABEL = re.compile(
    r"^(?:\d{4}-\d{2}(?:-\d{2})?|\d{2}-\d{4}|Q[1-4][ -]?\d{4}|\d{4}[ -]?Q[1-4]|\d{4}|"
    r"[A-Za-z]{3,9}[ -]\d{4})",
    re.IGNORECASE,
)


def _kind(column: str, formats: Any) -> tuple[str, dict[str, Any]]:
    """What a column's numbers are, and the declared spec behind that, if any."""
    spec = formats.get(column) if isinstance(formats, dict) else None
    spec = spec if isinstance(spec, dict) else {}
    declared = str(spec.get("format") or "").lower()
    if declared in {"currency", "percent", "ratio", "number", "text", "date"}:
        return declared, spec
    name = str(column).upper().strip()
    if name.endswith("%") or _PERCENT.search(name):
        return "percent", spec
    if _RATIO.search(name):
        return "ratio", spec
    suffix = _CURRENCY_SUFFIX.search(name)
    if suffix:
        return "currency", {**spec, "currency": suffix.group(1)}
    if _CURRENCY.search(name):
        return "currency", spec
    return "number", spec


# --- the screen's number rules, in Python ------------------------------------------
#
# Mirrors abbreviate(), percent() and money() in frontend/src/lib/dashboard-format.ts.
# The summary quotes figures a reader can see on the tiles above it, so they must
# be written identically: the PDF formatter rounds a 2.95% KPI to "3%", and a
# paragraph saying 3% under a tile saying 2.95% reads as an error.


def _fixed(number: float, places: int, *, as_intl: bool = False) -> Decimal:
    """The number rounded the way the screen rounds it, halves away from zero.

    Python's format() rounds halves to even and wrote 45.25% as "45.2%" where
    the screen shows "45.3%". The two browser functions also differ from each
    other: toFixed (percentages) rounds the double's EXACT value, so 2.675 --
    stored as 2.67499... -- gives 2.67; Intl.NumberFormat (counts, ratios) rounds
    its shortest decimal form, "2.675", and gives 2.68. `as_intl` picks which.
    """
    step = Decimal(1).scaleb(-places)
    source = Decimal(repr(number)) if as_intl else Decimal(number)
    return source.quantize(step, rounding=ROUND_HALF_UP)


def _grouped(number: float, max_fraction: int = 2) -> str:
    """Intl.NumberFormat("en-US", {maximumFractionDigits}) -- grouping, and no
    trailing zeros."""
    text = f"{_fixed(number, max_fraction, as_intl=True):,}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _round_half_up(x: float) -> float:
    """JavaScript's Math.round, which Python's round() is not (it rounds halves
    to even)."""
    return math.floor(x + 0.5)


def _abbreviate(number: float) -> str:
    magnitude = abs(number)
    if magnitude >= 1e9:
        return f"{_round_half_up(number / 1e8) / 10:.1f}B"
    if magnitude >= 1e6:
        return f"{_round_half_up(number / 1e5) / 10:.1f}M"
    if magnitude >= 1e3:
        return f"{_round_half_up(number / 1e2) / 10:.1f}K"
    return _grouped(number)


def _percent(number: float) -> str:
    magnitude = abs(number)
    places = 0 if magnitude == 0 else 1 if magnitude >= 10 else 2 if magnitude >= 1 else 3 if magnitude >= 0.1 else 4
    fixed = f"{_fixed(number, places):f}"
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return f"{fixed}%"


def _money(number: float, code: str | None) -> str:
    prefix = _currency_prefix(code)
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    body = _abbreviate(magnitude) if magnitude >= 1e3 else _grouped(magnitude)
    return f"{sign}{prefix}{body}"


def _plain(number: float) -> str:
    """A count or plain measure: whole below a million, abbreviated above."""
    return _abbreviate(number) if abs(number) >= 1e6 else _grouped(number)


def show(value: Any, column: str, formats: Any, default_currency: str | None) -> str:
    """One chart value, written the way the dashboard writes it."""
    number = _as_number(value)
    if number is None or isinstance(value, bool):
        return str(value)
    kind, spec = _kind(column, formats)
    if kind == "currency":
        code = spec["currency"] if "currency" in spec else default_currency
        if code is None:
            # No currency is known. A bare number is honest; a guessed "$" is not.
            return _plain(number)
        return _money(number, code)
    if kind == "percent":
        return _percent(number)
    if kind == "ratio":
        return _grouped(number)
    return _plain(number)


def show_kpi(value: Any, fmt: Any, default_currency: str | None) -> str:
    """A headline figure exactly as its tile renders it (formatKpi)."""
    number = _as_number(value)
    if number is None or isinstance(value, bool):
        return "" if value is None else str(value)
    raw = str(fmt or "").strip()
    word, colon, code = raw.partition(":")
    kind = word.strip().lower()
    if kind == "currency":
        # A colon is a statement about currency even with nothing after it.
        return _money(number, code.strip() if colon else default_currency)
    if kind == "percent":
        return _percent(number)
    if kind == "count":
        return _grouped(number, 0)
    if kind == "ratio":
        return _grouped(number)
    return show(number, str(fmt or ""), None, default_currency)


def _period(value: Any) -> str:
    text = str(value).strip()
    month = _MONTH_START.match(text)
    if month:
        return f"{calendar.month_abbr[int(month.group(2))]} {month.group(1)}"
    return text[:10] if _ISO_DATE.match(text) else text


def _is_numeric(rows: list[list[Any]], index: int) -> bool:
    present = [row[index] for row in rows if index < len(row) and row[index] is not None]
    if not present:
        return False
    numbers = sum(1 for v in present if not isinstance(v, bool) and _as_number(v) is not None)
    return numbers >= max(1, int(len(present) * 0.8))


def _additive_total(column: str, formats: Any, points: list[tuple[str, float]],
                    snapshot: dict[str, Any]) -> float | None:
    """The chart's total, when a share of it is a true statement.

    Left to itself the model divided figures it was given and wrote "67% of total
    GMV" where the chart's own numbers say 84.9%. So the share is worked out here,
    and only where one exists: money or counts, every item non-negative, and all
    the rows present -- a panel cut off at the row limit has no known total.
    """
    if snapshot.get("truncated") or len(points) < 2:
        return None
    if _kind(column, formats)[0] not in {"currency", "number"}:
        return None
    if _NOT_ADDITIVE.search(str(column).upper()):
        return None
    if any(number < 0 for _, number in points):
        return None
    total = sum(number for _, number in points)
    return total if total > 0 else None


def panel_figures(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """The figures one chart contributes to the summary, or None when it has no
    numbers to describe."""
    columns = [str(c) for c in (snapshot.get("columns") or [])]
    rows = [list(r) for r in (snapshot.get("rows") or []) if isinstance(r, (list, tuple))]
    if not columns or not rows:
        return None
    index = {name: i for i, name in enumerate(columns)}
    numeric = {name for name in columns if _is_numeric(rows, index[name])}
    formats = snapshot.get("column_formats") or {}
    currency = snapshot.get("default_currency")

    x = snapshot.get("x") if snapshot.get("x") in index else None
    wanted = snapshot.get("y")
    wanted = [wanted] if isinstance(wanted, str) else list(wanted or [])
    values = [c for c in wanted if c in numeric and c != x] or [c for c in columns if c in numeric and c != x]
    values = values[:MAX_SERIES]
    if not values:
        return None

    label_column = x if x is not None and x not in numeric else next(
        (c for c in columns if c not in numeric), None
    )
    color = snapshot.get("color")
    color = color if color in index and color not in (label_column, *values) else None

    def label(row: list[Any]) -> str:
        parts = []
        for name in (label_column, color):
            if name is not None and index[name] < len(row):
                parts.append(_period(row[index[name]]))
        return " / ".join(parts) or "row"

    raw_labels = [row[index[label_column]] for row in rows
                  if label_column is not None and index[label_column] < len(row)]
    over_time = (
        color is None and label_column is not None and bool(raw_labels)
        and all(_PERIOD_LABEL.match(str(v).strip()) for v in raw_labels if v is not None)
    )
    if over_time and all(_ISO_DATE.match(str(v).strip()) for v in raw_labels if v is not None):
        # ISO periods sort as text. Do it, rather than trust the SQL's ORDER BY:
        # a series returned newest-first would otherwise swap "first" and "latest".
        rows = sorted(rows, key=lambda row: str(row[index[label_column]]))

    series: dict[str, Any] = {}
    for column in values:
        points = [(label(row), number) for row in rows
                  if index[column] < len(row)
                  and not isinstance(row[index[column]], bool)
                  and (number := _as_number(row[index[column]])) is not None]
        if not points:
            continue
        written = lambda number, c=column: show(number, c, formats, currency)  # noqa: E731
        if over_time:
            (first_at, first), (latest_at, latest) = points[0], points[-1]
            high = max(points, key=lambda p: p[1])
            low = min(points, key=lambda p: p[1])
            figures: dict[str, Any] = {
                "first": f"{written(first)} ({first_at})",
                "latest": f"{written(latest)} ({latest_at})",
                "high": f"{written(high[1])} ({high[0]})",
                "low": f"{written(low[1])} ({low[0]})",
                "periods": len(points),
            }
            if _kind(column, formats)[0] == "percent":
                move = latest - first
                figures["change"] = f"{'+' if move >= 0 else '-'}{abs(move):,.1f} pp"
            elif first != 0:
                move = (latest - first) / abs(first) * 100
                figures["change"] = f"{'+' if move >= 0 else '-'}{abs(move):,.1f}%"
        else:
            ranked = sorted(points, key=lambda p: p[1], reverse=True)
            total = _additive_total(column, formats, points, snapshot) if color is None else None
            figures = {
                "top": [
                    f"{name}: {written(number)}"
                    + (f" ({_fixed(number / total * 100, 1)}% of total)" if total else "")
                    for name, number in ranked[:TOP_ITEMS]
                ],
                "items": len(points),
            }
            if total and len(ranked) > 2:
                # Concentration is what the summary is asked for, so give it the
                # figure: added up from its rounded shares it wrote 84.8% for 84.9%.
                figures["top_share"] = "; ".join(
                    f"top {n}: {_fixed(sum(v for _, v in ranked[:n]) / total * 100, 1)}% of total"
                    for n in range(2, min(TOP_ITEMS, len(ranked) - 1) + 1)
                )
            if len(ranked) > TOP_ITEMS:
                # Never repeat a top item: with four items the bottom is one.
                tail = ranked[max(TOP_ITEMS, len(ranked) - BOTTOM_ITEMS):]
                figures["bottom"] = [f"{name}: {written(number)}" for name, number in tail]
        series[column] = figures

    return {"series": series} if series else None

"""A monthly axis should read "Jan 2025", not "2025-01-01".

Monthly and yearly series are stored as the FIRST DAY of their period, because
that is what the SQL produces. The axis then showed a day number that carries
no information and ate the width that would have let more labels fit.

The granularity is read off the data rather than declared: the column name does
not carry it, and the semantic layer describes meaning rather than resolution.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.engine.pdf_charts import _date_granularity, _date_label


@pytest.mark.parametrize(
    "values,expected",
    [
        (["2025-01-01", "2025-02-01", "2025-03-01"], "month"),
        (["2023-01-01", "2024-01-01", "2025-01-01"], "year"),
        (["2025-01-05", "2025-01-06"], "day"),
        # A single January first is a year as far as anyone can tell.
        (["2025-01-01"], "year"),
        (["2025-03-01"], "month"),
    ],
)
def test_granularity_is_read_off_the_values(values, expected):
    assert _date_granularity(values) == expected


@pytest.mark.parametrize(
    "values",
    [
        ["TX", "CA"],
        ["2025-01-01", "Office"],   # one non-date and it is not a date column
        [],
        ["", "  "],
    ],
)
def test_anything_not_wholly_dates_is_left_alone(values):
    assert _date_granularity(values) is None


def test_labels_match_the_granularity():
    """Numeric MM-YYYY, not "Mar 2025": an axis label should not make a reader
    map a month name back to its position."""
    assert _date_label("2025-03-01", "month") == "03-2025"
    assert _date_label("2025-03-01", "year") == "2025"
    assert _date_label("2025-03-05", "day") == "03-05"


def test_an_unparseable_value_passes_through():
    """One odd row must not blank the whole axis."""
    assert _date_label("not a date", "month") == "not a date"
    assert _date_label("2025-03-01", None) == "2025-03-01"


def test_a_timestamp_is_accepted():
    """Warehouses return DATE columns as timestamps often enough."""
    assert _date_granularity(["2025-01-01 00:00:00", "2025-02-01 00:00:00"]) == "month"
    assert _date_label("2025-02-01T00:00:00", "month") == "02-2025"


def test_quarterly_is_deliberately_not_inferred():
    """Monthly data covering Jan/Apr/Jul/Oct is indistinguishable from
    quarterly, and a wrong "Q1" is worse than a right "Jan"."""
    quarters = ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
    assert _date_granularity(quarters) == "month"
    assert _date_label(quarters[1], "month") == "04-2025"


def test_the_frontend_uses_the_same_rules():
    """The export and the screen must label an axis identically, and they are
    two implementations of one rule — so at least keep their shapes in step."""
    source = (
        Path(__file__).resolve().parents[3]
        / "frontend" / "src" / "lib" / "dashboard-format.ts"
    ).read_text(encoding="utf-8")

    assert "detectDateGranularity" in source
    assert "formatDateLabel" in source
    # Both sides emit MM-YYYY; a month NAME on one and not the other would
    # make the export and the screen disagree about the same axis.
    assert "${month}-${year}" in source, "frontend is not emitting MM-YYYY"
    # The same three buckets, and no quarterly.
    grains = set(re.findall(r'"(year|month|day|quarter)"', source))
    assert "quarter" not in grains, "frontend infers quarterly; the PDF does not"
    assert {"year", "month", "day"} <= grains


# --- the table shows the same granularity as the chart above it -------------


def test_a_month_column_reads_as_a_month_in_the_table_too():
    """A column called SALE MONTH showing 2025-02-01 is displaying a day that
    is a storage convention, not a fact — and it contradicted the chart beside
    it, which had already stopped doing that."""
    import pymupdf

    from app.engine.dashboard_export import build_dashboard_pdf

    panel = {
        "title": "Monthly", "chart_type": None, "display": "table",
        "columns": ["SALE_MONTH", "NUM_SALES"],
        "rows": [[f"2025-{m:02d}-01", 400_000 + m] for m in range(1, 4)],
        "row_count": 3,
    }
    doc = pymupdf.open("pdf", build_dashboard_pdf("X", [panel]))
    text = chr(10).join(page.get_text() for page in doc)

    assert "01-2025" in text and "03-2025" in text
    assert "2025-01-01" not in text, "raw ISO date still in the table"


def test_a_daily_column_keeps_its_year_in_the_table():
    """Only an axis drops the year, and only because tick width is scarce. A
    bare "02-05" in a table is genuinely ambiguous."""
    import pymupdf

    from app.engine.dashboard_export import build_dashboard_pdf

    panel = {
        "title": "Daily", "chart_type": None, "display": "table",
        "columns": ["SALE_DATE", "NUM_SALES"],
        "rows": [["2025-02-05", 10], ["2025-02-06", 12]],
        "row_count": 2,
    }
    doc = pymupdf.open("pdf", build_dashboard_pdf("X", [panel]))
    text = chr(10).join(page.get_text() for page in doc)

    assert "02-05-2025" in text, "the table dropped the year on a daily column"


def test_the_frontend_table_uses_the_same_rule():
    source = (
        Path(__file__).resolve().parents[3]
        / "frontend" / "src" / "lib" / "dashboard-format.ts"
    ).read_text(encoding="utf-8")
    assert "columnDateGrains" in source, "frontend table has no per-column grain"
    # compact=false is what keeps the year in a table cell.
    assert "${month}-${day}-${year}" in source

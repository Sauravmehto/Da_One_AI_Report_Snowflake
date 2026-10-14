"""A bar must stay inside the plot it is drawn in.

A bar is drawn from a baseline to its value. The baseline was always zero,
which is correct only while the value axis includes zero. A panel set to fit
its data does not include zero, and the bar was then anchored below the foot
of its own plot — it ran off the bottom and over the table underneath it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import pdf_charts
from app.engine.dashboard_export import build_dashboard_pdf

WEEKS = [f"2025-{i // 4 % 12 + 1:02d}-{i % 28 + 1:02d}" for i in range(80)]


def _panel(chart_type: str, *, fitted: bool) -> dict:
    """Money well clear of zero — the case a fitted axis is chosen for."""
    return {
        "title": "Revenue and Profit Over Time",
        "chart_type": chart_type,
        "display": "both",
        "columns": ["WEEK_START", "ACTUAL_REVENUE_AED", "GROSS_PROFIT_AED"],
        "rows": [[w, 12_000_000 + (i % 9) * 800_000, 3_000_000 + (i % 7) * 300_000]
                 for i, w in enumerate(WEEKS)],
        "x": "WEEK_START",
        "y": ["ACTUAL_REVENUE_AED", "GROSS_PROFIT_AED"],
        "row_count": 80,
        "zero_baseline": False if fitted else None,
    }


@pytest.mark.parametrize("chart_type", ["bar", "grouped_bar", "combo"])
@pytest.mark.parametrize("fitted", [False, True])
def test_no_bar_is_drawn_below_its_plot(chart_type, fitted):
    panel = _panel(chart_type, fitted=fitted)
    page = pymupdf.open().new_page(width=595, height=842)
    plot = pymupdf.Rect(40, 100, 555, 300)

    assert pdf_charts.draw_panel_chart(page, plot, panel)

    overflow = [
        d["rect"] for d in page.get_drawings()
        if d.get("fill") and d["rect"].y1 > plot.y1 + 0.5
    ]
    assert not overflow, (
        f"{chart_type} (fitted={fitted}): {len(overflow)} shape(s) drawn below "
        f"the plot's bottom edge at y={plot.y1}"
    )


def test_a_fitted_bar_panel_does_not_overwrite_its_own_table():
    """The whole-document version of the same fault: the table is placed after
    the chart, so a chart that overruns lands on top of it."""
    pdf = build_dashboard_pdf("Freight", [_panel("combo", fitted=True)])
    doc = pymupdf.open(stream=pdf, filetype="pdf")

    for page in doc:
        header = [w for w in page.get_text("words") if w[4] == "WEEK_START"]
        if not header:
            continue
        table_top = min(w[1] for w in header)
        bars = [d["rect"] for d in page.get_drawings()
                if d.get("fill") and d["rect"].height > 4 and d["rect"].width < 12]
        clashing = [r for r in bars if r.y1 > table_top + 0.5]
        assert not clashing, (
            f"{len(clashing)} bars run into the table, which starts at {table_top:.1f}"
        )
        return
    pytest.fail("no table page produced")


def test_the_baseline_follows_the_axis():
    assert pdf_charts._bar_base(0.0, 100.0) == 0.0        # ordinary axis
    assert pdf_charts._bar_base(10.6e6, 20e6) == 10.6e6   # fitted, above zero
    assert pdf_charts._bar_base(-50.0, 50.0) == 0.0       # spans zero
    assert pdf_charts._bar_base(-80.0, 0.0) == 0.0        # all negative

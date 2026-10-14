"""What one chart contributes to the AI summary.

See app/engine/narrative_figures.py for the two failures this replaces: raw
numbers the model had to scale, and the five oldest rows standing in for a trend.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine.narrative_figures import panel_figures, show, show_kpi

MONTHS = [f"2025-{m:02d}-01" for m in range(2, 13)] + [f"2026-{m:02d}-01" for m in range(1, 9)]


def trend(values, column="GMV_AED", **extra):
    return {"x": "MONTH_START", "y": column, "columns": ["MONTH_START", column],
            "rows": [[m, v] for m, v in zip(MONTHS, values)], "default_currency": "AED", **extra}


# --- numbers written the way the screen writes them ---------------------------


def test_money_is_written_in_its_currency_and_scale():
    assert show(235_930_000.12, "GMV_AED", None, None) == "AED 235.9M"
    assert show(5_100_131.98, "NET_REVENUE", None, "AED") == "AED 5.1M"


def test_a_declared_format_wins_over_the_name():
    formats = {"GMV_AED": {"format": "number"}}
    assert show(1_500_000, "GMV_AED", formats, "AED") == "1.5M"


def test_rates_and_ratios():
    assert show(10.7, "CONTRIBUTION_MARGIN_PCT", None, "AED") == "10.7%"
    assert show(3.2, "LTV_TO_CAC_RATIO", None, "AED") == "3.2"


def test_no_currency_is_guessed_when_none_is_known():
    """A bare number is honest; a "$" in front of dirhams is not."""
    assert show(2_500_000, "REVENUE", None, None) == "2.5M"


def test_counts_stay_whole():
    assert show(905478, "COMPLETED_ORDERS", None, "AED") == "905,478"


# --- a chart over time ------------------------------------------------------------


def test_latest_is_the_most_recent_period_not_the_fifth_row():
    values = [11_000_000 + i * 150_000 for i in range(len(MONTHS))]
    figures = panel_figures(trend(values))["series"]["GMV_AED"]
    assert figures["latest"].endswith("(Aug 2026)")
    assert figures["first"].endswith("(Feb 2025)")
    assert figures["periods"] == 19


def test_high_low_and_change():
    values = [100, 140, 90, 120]
    snapshot = {"x": "MONTH_START", "y": "GMV_AED", "columns": ["MONTH_START", "GMV_AED"],
                "rows": [[m, v] for m, v in zip(MONTHS, values)], "default_currency": "AED"}
    figures = panel_figures(snapshot)["series"]["GMV_AED"]
    assert figures["high"] == "AED 140 (Mar 2025)"
    assert figures["low"] == "AED 90 (Apr 2025)"
    assert figures["change"] == "+20.0%"


def test_a_rate_moves_in_points_not_percent():
    """Margin going from 12% to 10.7% is -1.3 points; "-10.8%" would read as a
    collapse to anyone skimming."""
    snapshot = trend([12.0, 11.0, 10.7], column="CONTRIBUTION_MARGIN_PCT")
    assert panel_figures(snapshot)["series"]["CONTRIBUTION_MARGIN_PCT"]["change"] == "-1.3 pp"


def test_a_series_returned_newest_first_is_still_read_in_time_order():
    snapshot = trend([100, 110, 120, 130])
    snapshot["rows"] = list(reversed(snapshot["rows"]))
    figures = panel_figures(snapshot)["series"]["GMV_AED"]
    assert figures["first"].endswith("(Feb 2025)")
    assert figures["latest"].endswith("(May 2025)")


def test_several_series_are_capped():
    columns = ["MONTH_START", "A_AED", "B_AED", "C_AED", "D_AED"]
    snapshot = {"x": "MONTH_START", "y": columns[1:], "columns": columns,
                "rows": [[m, 1, 2, 3, 4] for m in MONTHS[:3]]}
    assert list(panel_figures(snapshot)["series"]) == ["A_AED", "B_AED", "C_AED"]


# --- a breakdown ----------------------------------------------------------------


def test_a_breakdown_lists_top_and_bottom():
    snapshot = {"x": "COUNTRY", "y": "GMV_AED", "columns": ["COUNTRY", "GMV_AED"],
                "rows": [["UAE", 127_946_756.3], ["Saudi Arabia", 72_300_000], ["Qatar", 20_000_000],
                         ["Kuwait", 9_000_000], ["Oman", 5_100_131.98]]}
    figures = panel_figures(snapshot)["series"]["GMV_AED"]
    assert figures["top"][0] == "UAE: AED 127.9M (54.6% of total)"
    assert figures["top"][1] == "Saudi Arabia: AED 72.3M (30.9% of total)"
    assert figures["bottom"][-1] == "Oman: AED 5.1M"
    assert figures["items"] == 5


def test_four_items_never_repeat_a_top_item_at_the_bottom():
    snapshot = {"x": "C", "y": "V_AED", "columns": ["C", "V_AED"],
                "rows": [["a", 4], ["b", 3], ["c", 2], ["d", 1]]}
    figures = panel_figures(snapshot)["series"]["V_AED"]
    names = lambda items: {item.split(":")[0] for item in items}  # noqa: E731
    assert not names(figures["top"]) & names(figures["bottom"])


def test_a_chart_split_by_a_category_labels_each_point_with_both():
    snapshot = {"x": "MONTH_START", "y": "ORDERS", "color": "MARKET",
                "columns": ["MONTH_START", "MARKET", "ORDERS"],
                "rows": [["2026-08-01", "Dubai", 500], ["2026-08-01", "Riyadh", 300]]}
    figures = panel_figures(snapshot)["series"]["ORDERS"]
    assert figures["top"][0] == "Aug 2026 / Dubai: 500"


# --- a share of the total, only where one is true ----------------------------------
#
# From a real dashboard: left to divide figures itself, the summary wrote "67% of
# total GMV" where the chart's numbers give 84.9%.


def breakdown(column, values, **extra):
    return {"x": "MARKET", "y": column, "columns": ["MARKET", column],
            "rows": [[f"m{i}", v] for i, v in enumerate(values)], "default_currency": "AED", **extra}


def test_the_share_is_of_the_whole_chart_not_of_the_top_items():
    top = panel_figures(breakdown("GMV_AED", [849, 51, 50, 25, 25]))["series"]["GMV_AED"]["top"]
    assert top[0] == "m0: AED 849 (84.9% of total)"


def test_concentration_is_given_not_added_up():
    """The real GMV by Country chart: the summary added 54.2% and 30.6% and
    wrote 84.8%; the chart's numbers give 84.9%."""
    values = [127_946_756.3, 72_277_516.85, 11_271_755.01, 10_787_305.48, 8_486_283.72, 5_100_131.98]
    figures = panel_figures(breakdown("GMV_AED", values))["series"]["GMV_AED"]
    assert figures["top_share"] == "top 2: 84.9% of total; top 3: 89.7% of total"


def test_concentration_never_covers_every_item():
    three = panel_figures(breakdown("GMV_AED", [5, 3, 2]))["series"]["GMV_AED"]
    two = panel_figures(breakdown("GMV_AED", [5, 3]))["series"]["GMV_AED"]
    assert three["top_share"] == "top 2: 80.0% of total"
    assert "top_share" not in two
    rates = panel_figures(breakdown("MARGIN_PCT", [5, 3, 2, 1]))["series"]["MARGIN_PCT"]
    assert "top_share" not in rates


def test_counts_get_a_share():
    top = panel_figures(breakdown("COMPLETED_ORDERS", [300, 100]))["series"]["COMPLETED_ORDERS"]["top"]
    assert top[0] == "m0: 300 (75.0% of total)"


def test_rates_ratios_and_averages_never_get_a_share():
    for column in ("CONTRIBUTION_MARGIN_PCT", "LTV_TO_CAC_RATIO", "AOV_AED",
                   "AVG_DELIVERY_DAYS", "NPS_SCORE", "REVENUE_PER_ORDER_AED"):
        top = panel_figures(breakdown(column, [30, 10]))["series"][column]["top"]
        assert "of total" not in top[0], column


def test_no_share_when_rows_are_missing_or_signs_are_mixed():
    cut = panel_figures(breakdown("GMV_AED", [30, 10], truncated=True))["series"]["GMV_AED"]["top"]
    mixed = panel_figures(breakdown("PROFIT_AED", [30, -10]))["series"]["PROFIT_AED"]["top"]
    alone = panel_figures(breakdown("GMV_AED", [30]))["series"]["GMV_AED"]["top"]
    assert not any("of total" in item for item in cut + mixed + alone)


def test_a_split_chart_has_no_single_total():
    snapshot = {"x": "MONTH_START", "y": "ORDERS", "color": "MARKET",
                "columns": ["MONTH_START", "MARKET", "ORDERS"],
                "rows": [["2026-08-01", "Dubai", 500], ["2026-08-01", "Riyadh", 300]]}
    assert "of total" not in panel_figures(snapshot)["series"]["ORDERS"]["top"][0]


# --- nothing to describe -------------------------------------------------------------


def test_no_numbers_no_figures():
    assert panel_figures({"columns": ["MARKET"], "rows": [["Dubai"]]}) is None


def test_empty_panel():
    assert panel_figures({"columns": ["A"], "rows": []}) is None


# --- identical to the screen, including the awkward cases -------------------------
#
# Checked against formatKpi / formatAxis in the browser bundle: 41 of 41 matched.
# These pin the cases where a naive Python port disagreed with the tile.


def test_kpi_percent_keeps_the_screen_precision():
    assert show_kpi(2.95, "percent", "AED") == "2.95%"  # the PDF formatter says "3%"


def test_halves_round_away_from_zero_like_the_browser():
    assert show_kpi(45.25, "percent", "AED") == "45.3%"
    assert show_kpi(2_150_000, "currency:AED", "AED") == "AED 2.2M"


def test_ratios_round_like_intl_not_like_to_fixed():
    assert show_kpi(2.675, "ratio", "AED") == "2.68"


def test_negative_money_puts_the_sign_outside():
    assert show_kpi(-5_200_000, "currency:AED", "AED") == "-AED 5.2M"


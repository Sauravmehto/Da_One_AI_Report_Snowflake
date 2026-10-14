"""Column formatting is DECLARED, not guessed from the column name.

The renderer classifies a column by matching its name against patterns —
BALANCE, PCT, DSCR each hit one. That works until a column is called TOTAL, AMT
or OPENING_BAL, which fall through and render as bare numbers with their
currency lost.

KPI tiles already solved this with an authored FORMAT column, and formatKpi
carries the reason: "Debt Count" and "Debt Outstanding" share a word and are a
count and a currency respectively. A name cannot be trusted to carry a unit.
"""

from __future__ import annotations

from app.engine.dashboards import (
    COLUMN_FORMATS,
    _normalise_column_formats,
    _panel_config,
)


def test_a_plain_string_is_read_as_the_format():
    assert _normalise_column_formats({"BALANCE": "currency"}) == {
        "BALANCE": {"format": "currency"}
    }


def test_a_full_spec_survives():
    assert _normalise_column_formats(
        {"SHARE": {"format": "percent", "decimals": 1, "label": "Share of total"}}
    ) == {"SHARE": {"format": "percent", "decimals": 1, "label": "Share of total"}}


def test_an_unknown_format_is_dropped_rather_than_stored():
    """A stored value nothing honours is worse than an absent one: it looks
    configured while behaving as though it is not."""
    assert _normalise_column_formats({"X": {"format": "gigabytes"}}) is None


def test_a_label_alone_is_kept():
    """Renaming a header without changing its unit is a real use."""
    assert _normalise_column_formats({"CLIENT_NM": {"label": "Client"}}) == {
        "CLIENT_NM": {"label": "Client"}
    }


def test_decimals_are_clamped_to_something_renderable():
    assert _normalise_column_formats({"X": {"decimals": 99}}) == {"X": {"decimals": 6}}
    assert _normalise_column_formats({"X": {"decimals": -3}}) == {"X": {"decimals": 0}}


def test_junk_is_refused():
    for raw in (None, "currency", 42, [], {"": "currency"}, {"X": 7}):
        assert _normalise_column_formats(raw) is None, raw


def test_nothing_declared_means_fall_back_to_the_name_classifier():
    """None is meaningful: it tells the renderer to classify by name, which is
    what keeps every dashboard saved before this rendering as it did."""
    assert _normalise_column_formats({}) is None


def test_the_config_a_panel_carries_includes_its_formats():
    """It has to survive save/load, or the formatting is lost the moment the
    dashboard is reopened."""
    panel = {
        "title": "Receivables",
        "chart_type": None,
        "display": "table",
        "column_formats": {"BALANCE_DUE": "currency"},
        "sql": "SELECT 1",
    }
    config = _panel_config(panel)
    assert config["column_formats"] == {"BALANCE_DUE": {"format": "currency"}}
    assert "sql" not in config, "config must not carry the query"


def test_every_advertised_format_is_accepted():
    """The planner is told these words; each must survive normalisation, or a
    panel authored exactly as instructed loses its formatting."""
    for fmt in COLUMN_FORMATS:
        assert _normalise_column_formats({"C": {"format": fmt}}) == {
            "C": {"format": fmt}
        }, fmt

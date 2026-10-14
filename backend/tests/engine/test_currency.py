"""Money must say which money it is.

Every amount rendered as US dollars regardless of what it was. That is not
cosmetic — "$1,234,567.50" over a column of dirhams states a wrong fact about
the amount, and nothing on screen lets a reader catch it.

The distinction these tests exist to protect is between an ABSENT currency and
an EMPTY one. Absent is a default (dollars, so every dashboard written before
the field renders unchanged). Empty is a statement: the author cannot name the
currency. Collapsing the two restores dollars to a column that said it was not
dollars, which is the original bug wearing a new hat.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.engine.dashboard_export import _currency_prefix, _fmt_kpi, build_dashboard_pdf
from app.engine.dashboards import _normalise_column_formats


def money_panel(title: str, spec: dict, value: float = 1234567.5) -> dict:
    return {
        "title": title, "chart_type": "bar", "display": "chart",
        "x": "LABEL", "y": "AMT", "color": None, "size": None,
        "orientation": None, "columns": ["LABEL", "AMT"],
        "rows": [["One", value]], "row_count": 1,
        "column_formats": {"AMT": spec},
    }


def render(panels) -> pymupdf.Document:
    return pymupdf.open(stream=build_dashboard_pdf("T", panels), filetype="pdf")


def all_text(doc) -> str:
    return "\n".join(page.get_text() for page in doc)


# --- the Latin-1 trap -------------------------------------------------------

def test_every_pdf_prefix_survives_latin_1():
    """The base-14 PDF fonts are Latin-1. The euro sign is NOT in it and reaches
    the page as "?" — the same failure that once turned every em dash into a
    question mark. A currency whose symbol will not survive prints its code."""
    for code in [None, "", "USD", "GBP", "JPY", "EUR", "AED", "INR", "CHF", "ZZZ"]:
        prefix = _currency_prefix(code)
        # Raises UnicodeEncodeError if this would reach the page as "?".
        prefix.encode("latin-1")


def test_the_symbol_is_used_only_where_latin_1_allows_it():
    assert _currency_prefix("USD") == "$"
    assert _currency_prefix("GBP") == "£"
    assert _currency_prefix("JPY") == "¥"
    # No euro sign: U+20AC postdates Latin-1.
    assert _currency_prefix("EUR") == "EUR "
    assert _currency_prefix("AED") == "AED "


def test_a_code_plus_a_space_so_it_does_not_run_into_the_digits():
    assert _currency_prefix("AED").endswith(" ")


# --- absent / empty / named -------------------------------------------------

def test_absent_means_dollars_so_old_dashboards_are_untouched():
    assert _currency_prefix(None) == "$"


def test_empty_means_no_symbol_and_is_not_the_same_as_absent():
    assert _currency_prefix("") == ""
    assert _currency_prefix("") != _currency_prefix(None)


def test_a_malformed_code_yields_no_affix_rather_than_a_guess():
    """Printed beside the number it would read as a unit that does not exist:
    "US 1,200". A bare number is unhelpful; a wrong one is a lie."""
    for code in ["US", "USDD", "US1", "12", "dollars"]:
        assert _currency_prefix(code) == "", code


# --- the validator ----------------------------------------------------------

def test_the_validator_keeps_an_empty_code_as_a_real_value():
    out = _normalise_column_formats({"A": {"format": "currency", "currency": ""}})
    assert out == {"A": {"format": "currency", "currency": ""}}, (
        "an empty code was dropped as falsy, which silently restores dollars"
    )


def test_the_validator_uppercases_and_rejects_the_malformed():
    assert _normalise_column_formats(
        {"A": {"format": "currency", "currency": "aed"}}
    ) == {"A": {"format": "currency", "currency": "AED"}}

    for bad in ["US", "USDD", "US1", "  "]:
        out = _normalise_column_formats({"A": {"format": "currency", "currency": bad}})
        assert "currency" not in out["A"] or out["A"]["currency"] == "", bad


# --- KPI tiles carry the code in the FORMAT word ---------------------------

@pytest.mark.parametrize("fmt,expected", [
    ("currency", "$5.20M"),
    ("currency:AED", "AED 5.20M"),
    ("currency:EUR", "EUR 5.20M"),
    ("currency:", "5.20M"),
])
def test_a_kpi_format_word_can_carry_its_currency(fmt, expected):
    """A KPI row is METRIC/VALUE/FORMAT and has no column_formats to hang a
    currency on, so the code rides with the word."""
    assert _fmt_kpi(5_200_000, fmt) == expected


def test_the_sign_goes_outside_the_symbol():
    """Documented since the rule was written, and this branch never followed it
    — it formatted the signed number straight after the "$" and produced
    "$-5.20M"."""
    assert _fmt_kpi(-5_200_000, "currency") == "-$5.20M"
    assert _fmt_kpi(-5_200_000, "currency:AED") == "-AED 5.20M"


# --- end to end -------------------------------------------------------------

def test_a_pdf_prints_each_column_in_its_own_currency():
    doc = render([
        money_panel("Dirhams", {"format": "currency", "currency": "AED"}),
        money_panel("Euros", {"format": "currency", "currency": "EUR"}),
        money_panel("Sterling", {"format": "currency", "currency": "GBP"}),
        money_panel("Unknown", {"format": "currency", "currency": ""}),
        money_panel("Undeclared", {"format": "currency"}),
    ])
    text = all_text(doc)
    assert "AED 1,234,567.50" in text
    assert "EUR 1,234,567.50" in text
    assert "£1,234,567.50" in text
    assert "$1,234,567.50" in text, "an undeclared column stopped rendering as dollars"
    assert "?" not in text, "something outside Latin-1 reached the page"


def test_an_undeclared_currency_column_renders_exactly_as_before():
    """This change has to be invisible to every dashboard already saved."""
    doc = render([money_panel("Old", {"format": "currency"})])
    assert "$1,234,567.50" in all_text(doc)

"""The workbook is the only thing that knows which money it is.

A column of dirhams formatted "[$AED-4001] #,##0.00" was recognised as
currency and then rendered with a dollar sign, because `_CURRENCY` matched the
"[$" that opens the construct and threw away the "AED" inside it. Nothing
downstream can recover the code once that happens: not the column name, not
the semantic layer, not the model. The number format is the single
authoritative statement of the currency in the entire pipeline.

The distinction these tests protect is between a currency that is NOT NAMED
and one that is NAMED AS MIXED. Not named is a default (dollars, so every
workbook mapped before this existed reads unchanged, and a plain "$" format
comes out right). Mixed is a statement, and rendering it as dollars is the
original bug wearing a new hat.
"""

from __future__ import annotations

import pytest

from app.engine.excel_mapping import _currency_code
from app.engine.excel_profiler import (
    Cell,
    ColumnProfile,
    currency_from_format,
    unit_from_format,
)

EURO, POUND, YEN = "€", "£", "¥"


# --- what the format names --------------------------------------------------

@pytest.mark.parametrize("fmt,expected", [
    # The case that started this: Excel's own AED format.
    ("[$AED-4001] #,##0.00", "AED"),
    ("[$AED]#,##0", "AED"),
    # Accounting formats bury it in padding and repeat leaders.
    ("_-[$AED-4001]* #,##0.00_-;-[$AED-4001]* #,##0.00_-", "AED"),
    ("_-[$" + POUND + "-en-GB]* #,##0.00_-", "GBP"),
    # A locale id alone says nothing we should read a currency out of.
    ("[$-409]#,##0.00", None),
    # Bracketed symbols.
    ("[$" + EURO + "-407] #,##0.00", "EUR"),
    ("[$" + YEN + "-411]#,##0", "JPY"),
    # Bare and quoted symbols, no bracket at all.
    (POUND + "#,##0.00", "GBP"),
    ('"' + EURO + '"#,##0', "EUR"),
    # Not currency at all.
    ("#,##0.00", None),
    ("0.0%", None),
    ("", None),
    (None, None),
])
def test_the_code_is_read_from_the_number_format(fmt, expected):
    assert currency_from_format(fmt) == expected


def test_a_bare_dollar_names_nothing_rather_than_guessing_usd():
    """"$" is the dollar of a dozen countries. Reading USD out of it would be
    the same class of error as printing "$" over dirhams -- and it costs
    nothing to leave unnamed, because unnamed already renders as dollars."""
    for fmt in ["$#,##0.00", '"$"#,##0.00', "[$$-409]#,##0.00", "[$$-1009]#,##0"]:
        assert currency_from_format(fmt) is None, fmt


def test_the_unit_is_still_currency_when_the_code_is_unreadable():
    """Losing the code must not lose the fact that it is money at all."""
    assert unit_from_format("[$AED-4001] #,##0.00", "number") == "currency"
    assert unit_from_format("$#,##0.00", "number") == "currency"


# --- a cell carries it ------------------------------------------------------

def money(fmt: str, value: float = 1000.0) -> Cell:
    return Cell(row=2, col=3, value=value, kind="number", number_format=fmt)


def test_a_cell_reports_the_currency_its_format_names():
    assert money("[$AED-4001] #,##0.00").currency == "AED"


def test_a_cell_that_is_not_money_reports_no_currency():
    """A cell's number format survives whatever gets typed into it, so a
    currency-formatted cell holding the word "n/a" is text wearing a currency
    format. Reading a code off it would put an amount's unit on something that
    is not an amount, and the column counts it as one more dirham cell."""
    text = Cell(row=2, col=3, value="n/a", kind="text",
                number_format="[$AED-4001] #,##0.00")
    assert text.unit == "text"
    assert text.currency is None

    empty = Cell(row=2, col=3, value=None, kind="empty",
                 number_format="[$AED-4001] #,##0.00")
    assert empty.currency is None


# --- a column decides for the whole column ----------------------------------

def profile_over(formats: list[str]) -> ColumnProfile:
    """A column profiled the way the records scan does it, cell by cell."""
    profile = ColumnProfile(col=1, header="AMOUNT")
    for fmt in formats:
        cell = money(fmt)
        profile.populated += 1
        profile.units[cell.unit] = profile.units.get(cell.unit, 0) + 1
        if cell.currency:
            profile.currencies[cell.currency] = (
                profile.currencies.get(cell.currency, 0) + 1
            )
    return profile


def test_one_currency_all_the_way_down_is_named():
    profile = profile_over(["[$AED-4001] #,##0.00"] * 20)
    assert profile.dominant_currency == "AED"
    assert profile.mixes_currencies is False


def test_a_column_that_mixes_currencies_names_none_of_them():
    """Not the majority one. A column that is 90% dirhams and 10% euros is a
    column whose total means nothing, and labelling the total AED hides
    exactly that."""
    profile = profile_over(
        ["[$AED-4001] #,##0.00"] * 18 + ["[$" + EURO + "-407] #,##0.00"] * 2
    )
    assert profile.mixes_currencies is True
    assert profile.dominant_currency is None


def test_an_unnamed_column_names_none_and_is_not_mixed():
    profile = profile_over(["$#,##0.00"] * 10)
    assert profile.dominant_currency is None
    assert profile.mixes_currencies is False


# --- the mapper's coercion --------------------------------------------------

def test_the_mapper_keeps_an_empty_code_as_a_real_answer():
    """"" is the model saying the column mixes currencies. Dropped as falsy it
    becomes None, which means dollars -- restoring the exact wrong symbol to
    the one column that has just said it cannot be given one."""
    assert _currency_code("") == ""
    assert _currency_code("") != _currency_code(None)


def test_the_mapper_uppercases_and_rejects_the_malformed():
    assert _currency_code("aed") == "AED"
    assert _currency_code(" aed ") == "AED"
    for bad in ["US", "USDD", "US1", "dollars", 12, 3.5, ["AED"]]:
        assert _currency_code(bad) is None, bad


def test_the_mapper_says_nothing_when_the_model_said_nothing():
    assert _currency_code(None) is None

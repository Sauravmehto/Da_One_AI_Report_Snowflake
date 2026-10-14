"""A database declares the currency its money is in, and everything honours it.

The bug this closes: a KPI tile on FINTECH_DB printed "$330.4M" over dirhams.
Two causes, and both are covered here — the planner prompt contradicted itself
about what a FORMAT may contain, and nothing downstream stopped a non-dollar
database rendering dollars even when the model complied.

The rule the whole file is really testing:

    an explicit code wins  ->  else the database's declared code  ->  else
    nothing, which renders as US dollars exactly as it always has

with one sharp edge: an explicitly EMPTY code means "this money has no nameable
currency" and must NOT fall back to the database default. That is why the
production code tests for absence with `!== undefined` / `"currency" in spec`
rather than truthiness, and why several tests below pin the empty case.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import dashboards, llm
from app.engine.connectors.snowflake import client as snowflake_client
from app.engine.dashboard_export import (
    _fmt_kpi,
    _format_pdf_cell,
    build_dashboard_pdf,
)
from app.engine.metadata_loader import MetadataStore, metadata_store
from app.semantic.document import SemanticDocument
from app.semantic.validation import validate_document

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "metadata"

#: Every GCC database reports in dirhams.
GCC_DATABASES = [
    "ECOMMERCE_DB", "FINTECH_DB", "HOSPITALITY_DB",
    "LOGISTICS_DB", "PAYMENTS_DB", "RETAIL_DB",
]


def _store_with(tmp_path: Path, body: str) -> MetadataStore:
    (tmp_path / "x.toml").write_text(
        'database = "X"\ndisplay_name = "X"\ndescription = "d"\n' + body,
        encoding="utf-8",
    )
    return MetadataStore(config_dir=tmp_path)


# --- MetadataStore.currency -------------------------------------------------

def test_a_declared_code_is_normalised_to_upper_case(tmp_path):
    assert _store_with(tmp_path, 'currency = "aed"\n').currency("X") == "AED"


@pytest.mark.parametrize("literal", ['"US"', '"dirhams"', '"AE1"', '""', "3"])
def test_a_malformed_code_is_treated_as_undeclared(tmp_path, literal):
    """Not an error, but never a currency: a bad code must not become a prefix
    the reader would take for a unit."""
    assert _store_with(tmp_path, f"currency = {literal}\n").currency("X") is None


def test_a_database_that_declares_nothing_has_no_currency(tmp_path):
    assert _store_with(tmp_path, "").currency("X") is None


def test_an_unknown_database_has_no_currency(tmp_path):
    assert _store_with(tmp_path, 'currency = "AED"\n').currency("NOT_A_DB") is None
    assert _store_with(tmp_path, 'currency = "AED"\n').currency(None) is None


@pytest.mark.parametrize("database", GCC_DATABASES)
def test_every_gcc_database_declares_dirhams(database):
    assert metadata_store.currency(database) == "AED"


@pytest.mark.parametrize("database", ["LOAN", "PRIVATECREDIT", "PRIVATEEQUITY"])
def test_the_dollar_databases_stay_undeclared(database):
    """These rendered as dollars before this feature and must still do so."""
    assert metadata_store.currency(database) is None


# --- the model is told, once, in the context --------------------------------

def test_the_context_states_the_currency_and_the_format_to_use():
    ctx = metadata_store.build_context("FINTECH_DB")
    assert "Reporting currency: AED" in ctx
    assert "'currency:AED'" in ctx


def test_an_undeclared_database_says_nothing_about_currency():
    assert "Reporting currency" not in metadata_store.build_context("LOAN")


# --- run_dashboard carries it, and does not touch the rows ------------------

ROWS = [["Total Portfolio", 5_200_000, "currency:AED"]]


def _stub_snowflake(monkeypatch, plan):
    monkeypatch.setattr(
        snowflake_client, "read_saved_dashboard", lambda rid: {"DASHBOARD_PLAN": plan}
    )
    monkeypatch.setattr(
        snowflake_client, "touch_saved_dashboard_run",
        lambda rid: (1, "2026-01-01T00:00:00"),
    )
    monkeypatch.setattr(
        snowflake_client, "run_query",
        lambda sql, max_rows=None: {"columns": ["METRIC", "VALUE", "FORMAT"],
                     "rows": [list(r) for r in ROWS], "row_count": 1, "sql": sql},
    )


def _plan(database: str) -> dict:
    return {
        "title": "T", "database": database,
        "panels": [{"title": "KPIs", "sql": "SELECT 1", "chart_type": "kpi",
                    "x": None, "y": None, "color": None, "size": None,
                    "orientation": None, "display": "kpi"}],
    }


def test_a_declared_database_attaches_its_currency_to_every_panel(monkeypatch):
    _stub_snowflake(monkeypatch, _plan("FINTECH_DB"))
    monkeypatch.setattr(dashboards.metadata_store, "currency", lambda db: "AED")
    panel = dashboards.run_dashboard("rid", None)["panels"][0]
    assert panel["default_currency"] == "AED"


def test_an_undeclared_database_attaches_nothing(monkeypatch):
    _stub_snowflake(monkeypatch, _plan("LOAN"))
    monkeypatch.setattr(dashboards.metadata_store, "currency", lambda db: None)
    panel = dashboards.run_dashboard("rid", None)["panels"][0]
    assert panel["default_currency"] is None


def test_the_rows_are_returned_exactly_as_the_warehouse_produced_them(monkeypatch):
    """The currency rides BESIDE the config, never inside the data. Rewriting a
    row here would put a formatted string where a number belongs and quietly
    break every chart that does arithmetic on it."""
    _stub_snowflake(monkeypatch, _plan("FINTECH_DB"))
    monkeypatch.setattr(dashboards.metadata_store, "currency", lambda db: "AED")
    panel = dashboards.run_dashboard("rid", None)["panels"][0]
    assert panel["rows"] == [list(r) for r in ROWS]


def test_a_failed_panel_still_carries_the_currency(monkeypatch):
    """Otherwise a panel that errors once renders in dollars after a retry."""
    _stub_snowflake(monkeypatch, _plan("FINTECH_DB"))
    monkeypatch.setattr(dashboards.metadata_store, "currency", lambda db: "AED")

    def _boom(sql, max_rows=None):
        raise RuntimeError("warehouse said no")

    monkeypatch.setattr(snowflake_client, "run_query", _boom)
    panel = dashboards.run_dashboard("rid", None)["panels"][0]
    assert panel["error"]
    assert panel["default_currency"] == "AED"


# --- the PDF renders the same numbers the screen does -----------------------

@pytest.mark.parametrize(
    "fmt, default, expected",
    [
        ("currency", "AED", "AED 5.20M"),       # database default applies
        ("currency", None, "$5.20M"),           # undeclared: dollars, as before
        ("currency:EUR", "AED", "EUR 5.20M"),   # the row's own code wins
        ("currency:USD", "AED", "$5.20M"),      # ...including when it is dollars
        ("currency:", "AED", "5.20M"),          # an EMPTY code is a decision
    ],
)
def test_kpi_currency_resolution(fmt, default, expected):
    assert _fmt_kpi(5_200_000, fmt, default) == expected


def test_a_table_cell_takes_the_database_currency_when_the_column_names_none():
    spec = {"TOTAL": {"format": "currency"}}
    out = _format_pdf_cell(1234567.5, "TOTAL", spec, True, 2, None, "AED")
    assert out == "AED 1,234,567.50"


def test_a_column_declaring_an_empty_code_gets_no_symbol():
    """The truthiness bug this guards: "" is falsy, so a careless check would
    hand this cell the database default and print a currency the author
    deliberately removed."""
    spec = {"TOTAL": {"format": "currency", "currency": ""}}
    out = _format_pdf_cell(1234567.5, "TOTAL", spec, True, 2, None, "AED")
    assert out == "1,234,567.50"


def test_a_column_declaring_its_own_code_wins():
    spec = {"TOTAL": {"format": "currency", "currency": "GBP"}}
    out = _format_pdf_cell(1234567.5, "TOTAL", spec, True, 2, None, "AED")
    assert out.startswith("£")


def test_with_no_default_a_table_cell_is_dollars_exactly_as_before():
    spec = {"TOTAL": {"format": "currency"}}
    out = _format_pdf_cell(1234567.5, "TOTAL", spec, True, 2, None, None)
    assert out == "$1,234,567.50"


def test_an_exported_pdf_prints_dirhams_not_dollars():
    panels = [{
        "title": "Headlines", "chart_type": "kpi", "display": "kpi",
        "columns": ["METRIC", "VALUE", "FORMAT"],
        "rows": [["Net Revenue", 1_860_000, "currency"]],
        "row_count": 1, "default_currency": "AED",
    }]
    pdf = build_dashboard_pdf("GCC", panels)

    import pymupdf

    text = "".join(
        page.get_text() for page in pymupdf.open(stream=pdf, filetype="pdf")
    )
    # Written as the dashboard's tile writes it.
    assert "AED 1.9M" in text
    assert "$1.9M" not in text


# --- the planner prompt no longer contradicts itself ------------------------

@pytest.mark.parametrize(
    "phrase",
    ["MUST be exactly one of: 'currency'", "may carry its ISO code",
     "e.g. $2.2M", "SUM/AVG of dollars"],
)
def test_the_old_contradictory_wording_is_gone(phrase):
    assert phrase not in llm._DASHBOARD_RULES


def test_the_rules_now_name_the_coded_form():
    assert "'currency:<ISO code>'" in llm._DASHBOARD_RULES


def test_the_file_dashboard_rules_no_longer_offer_a_bare_currency_word():
    assert "FORMAT (currency|percent|count|ratio)" not in llm._FILE_DASHBOARD_RULES


# --- the narrative is prose, not markdown -----------------------------------

def test_a_heading_and_bold_markers_are_stripped():
    assert llm._plain_prose("# Title\n\nThe platform **grew**.") == "The platform grew."


def test_plain_prose_leaves_ordinary_text_alone():
    assert llm._plain_prose("Revenue rose 4%.") == "Revenue rose 4%."


def test_a_hash_inside_a_sentence_is_not_a_heading():
    assert llm._plain_prose("Ticket #42 closed.") == "Ticket #42 closed."


def test_the_narrative_endpoint_returns_prose(monkeypatch):
    monkeypatch.setattr(llm, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(llm, "_text", lambda resp: "## Overview\n\nRevenue **rose**.")
    out = llm.summarize_dashboard("T", [{"metric": "m", "valueText": "1"}], [])
    assert out == "Revenue rose."


class _FakeMessages:
    def create(self, **kwargs):
        return object()


class _FakeClient:
    messages = _FakeMessages()


# --- authoring a currency through the semantic editor -----------------------

def _doc(currency=None) -> SemanticDocument:
    extra = {} if currency is None else {"currency": currency}
    return SemanticDocument(database="X", display_name="X", extra=extra)


def test_a_nonsense_currency_is_rejected():
    issues = [i for i in validate_document(_doc("dirhams")) if i.field == "currency"]
    assert issues and issues[0].severity == "error"


def test_a_valid_currency_is_accepted():
    assert not [i for i in validate_document(_doc("AED")) if i.field == "currency"]


def test_publishing_keeps_the_currency():
    """It lives in `extra` — a round trip that dropped it would silently
    un-declare the database on the next publish."""
    assert _doc("AED").to_toml_dict()["currency"] == "AED"

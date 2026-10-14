"""Tables the page sends with a PDF export: shape checks on untrusted input."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.api.v1.routes.dashboards import _decode_panel_tables  # noqa: E402
from app.engine.dashboard_export import _MAX_PDF_TABLE_ROWS  # noqa: E402


def test_a_well_formed_table_is_kept():
    table = {"headers": ["Sector", "Return Rate %"], "numeric": [False, True],
             "rows": [["Office", "2.86%"]]}
    assert _decode_panel_tables({"Returns": table}) == {"Returns": table}


def test_short_rows_are_padded_and_long_ones_cut_to_the_headers():
    out = _decode_panel_tables({"T": {"headers": ["A", "B"], "numeric": [True],
                                      "rows": [["1"], ["1", "2", "3"]]}})
    assert out["T"]["rows"] == [["1", ""], ["1", "2"]]
    assert out["T"]["numeric"] == [True, False]


def test_a_malformed_table_is_dropped_not_fatal():
    assert _decode_panel_tables({"T": {"headers": "A,B", "rows": []}}) == {}
    assert _decode_panel_tables({"T": {"headers": ["A"], "rows": ["not a row"]}}) == {}
    assert _decode_panel_tables(["not", "a", "dict"]) == {}


def test_rows_stop_at_the_pdf_limit():
    rows = [[str(i)] for i in range(_MAX_PDF_TABLE_ROWS + 50)]
    out = _decode_panel_tables({"T": {"headers": ["N"], "numeric": [True], "rows": rows}})
    assert len(out["T"]["rows"]) == _MAX_PDF_TABLE_ROWS


def test_cells_are_capped_in_length():
    out = _decode_panel_tables({"T": {"headers": ["N"], "rows": [["x" * 5000]]}})
    assert len(out["T"]["rows"][0][0]) == 400

"""Offline tests for the Excel-report row cap + in-workbook truncation notice.
No Snowflake connection required."""

import openpyxl

from app.engine.connectors.snowflake import client as snowflake_client
from app.engine import excel_reports as er
from app.engine import excel_template_scanner as scanner


def test_excel_cap_is_dedicated_and_higher_than_chat_cap():
    assert er.EXCEL_MAX_ROWS > snowflake_client.MAX_ROWS
    assert er.EXCEL_MAX_ROWS >= 1000   # report-scale, not summary-scale


def _wb_with_title():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DATA"
    ws["A1"] = "Portfolio Extract"          # title band (row 1)
    ws["A2"], ws["B2"] = "ID", "Val"        # header row 2
    for i in range(3):                       # data rows 3..5
        ws.cell(row=3 + i, column=1).value = f"X{i}"
        ws.cell(row=3 + i, column=2).value = 10 + i
    return wb, ws


def test_notice_sits_above_header_and_preserves_title():
    wb, ws = _wb_with_title()
    before_max_row = ws.max_row
    info = er._write_truncation_notice(ws, header_row=2,
                                       header_by_col={"A": "ID", "B": "Val"},
                                       shown=100, total=340)
    assert info["cell"] == "A1"                       # above the header row
    assert "Showing first 100 of 340 rows" in ws["A1"].value
    assert "Portfolio Extract" in ws["A1"].value      # original title preserved
    # it did NOT add or shift any data row, and the header is still row 2
    assert ws.max_row == before_max_row
    assert scanner.header_row_index(ws) == 2
    assert ws["A3"].value == "X0" and ws["B5"].value == 12


def test_notice_does_not_perturb_scanner_classification():
    # a mirror block on a SUMMARY sheet must classify identically with/without
    # the truncation notice present on the data sheet.
    def build():
        wb, ws = _wb_with_title()
        summ = wb.create_sheet("SUMMARY")
        for i in range(3):                   # rows 2..4, cross-sheet mirror
            summ[f"A{2 + i}"] = f"=DATA!A{3 + i}"
        return wb
    plain = scanner.scan_workbook(build(), "t")

    wb2 = build()
    er._write_truncation_notice(wb2["DATA"], 2, {"A": "ID", "B": "Val"}, 100, 340)
    noticed = scanner.scan_workbook(wb2, "t")

    assert plain["formula_classification"]["tier_1b"] == \
        noticed["formula_classification"]["tier_1b"]
    assert plain["tab_roles"] == noticed["tab_roles"]


def test_notice_fallback_when_header_is_row_1():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = "ID", "Val"         # header IS row 1 (no title band)
    info = er._write_truncation_notice(ws, header_row=1,
                                       header_by_col={"A": "ID", "B": "Val"},
                                       shown=50, total=90)
    # placed to the right of the data, not over a header cell
    assert info["cell"] not in ("A1", "B1")
    assert ws["A1"].value == "ID" and ws["B1"].value == "Val"
    assert "Showing first 50 of 90 rows" in ws[info["cell"]].value


# ── Multi-block truncation ────────────────────────────────────────────────
#
# The guard meant to catch this could never fire: the executor capped results at
# EXCEL_MAX_ROWS and `run` then refused only if the count EXCEEDED the same
# number. A block cut off at exactly the cap went through silently, and the
# workbook's own SUM formulas were computed over the rows that happened to fit —
# a report wrong in a way nobody reading it can see.

import pytest

from app.engine import excel_multiblock

_COLS = ["PROPID", "STATE", "COUNTY", "CITY", "SALE_DATE", "SALE_PRICE"]


@pytest.fixture
def one_block_template(tmp_path):
    """A workbook with a single data block and a total that reads from it."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Raw_Sales"
    for c, h in enumerate(_COLS, start=1):
        ws.cell(row=1, column=c, value=h)
    for r in (2, 3):
        for c, h in enumerate(_COLS, start=1):
            ws.cell(row=r, column=c,
                    value=100 * r if h == "SALE_PRICE" else f"{h}{r}")
    path = tmp_path / "one_block.xlsx"
    wb.save(path)
    wb.close()
    return str(path)


def _capped_executor(cap: int, returned: int, total: int | None = None):
    """Behaves like the real executor at its row cap."""
    def execute(sql):
        out = {
            "columns": _COLS,
            "rows": [[200 if c == "SALE_PRICE" else f"{c}{i}" for c in _COLS]
                     for i in range(returned)],
            "row_cap": cap,
        }
        if total is not None:
            out["total_rows"] = total
        return out
    return execute


def test_a_block_cut_off_by_the_row_cap_is_refused(one_block_template):
    """Refused rather than silently shortened — the same stance the engine takes
    on a template column the query does not supply."""
    with pytest.raises(excel_multiblock.MultiBlockError) as exc:
        excel_multiblock.run(
            one_block_template,
            {"Raw_Sales!2": "SELECT * FROM props"},
            _capped_executor(cap=50, returned=50, total=1_284_000),
        )

    message = str(exc.value)
    assert "50 rows written of 1,284,000 that match" in message
    assert "would read as if they covered everything" in message


def test_truncation_can_be_accepted_deliberately(one_block_template):
    """A partial extract is a legitimate thing to want — but on purpose."""
    result = excel_multiblock.run(
        one_block_template,
        {"Raw_Sales!2": "SELECT * FROM props"},
        _capped_executor(cap=50, returned=50, total=1_284_000),
        allow_truncated_rows=True,
    )

    assert result["truncated_blocks"], "the caller was told nothing about it"
    assert "1,284,000" in result["truncated_blocks"][0]


def test_a_result_under_the_cap_is_not_called_truncated(one_block_template):
    """The test is 'reached the cap', so a result that merely fits must not
    trip it — otherwise every ordinary report would warn."""
    result = excel_multiblock.run(
        one_block_template,
        {"Raw_Sales!2": "SELECT * FROM props"},
        _capped_executor(cap=50, returned=49),
    )

    assert result["truncated_blocks"] == []


def test_an_executor_with_no_cap_is_left_alone(one_block_template):
    """Injected executors in tests and scripts report no row_cap. They must not
    start failing because the real one now does."""
    def uncapped(sql):
        return {"columns": _COLS,
                "rows": [[1] * len(_COLS) for _ in range(500)]}

    result = excel_multiblock.run(
        one_block_template, {"Raw_Sales!2": "SELECT 1"}, uncapped
    )

    assert result["truncated_blocks"] == []

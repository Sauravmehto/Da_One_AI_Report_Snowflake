"""Grow a template's blocks to fit real data without destroying what sits below.

The problem, on a real template: LOAN_DATA's loan register occupies rows 3-7 and
the FX RATES table starts at row 10. Two spare rows. A query returning eight
loans overwrites the FX banner; forty destroys the table. Meanwhile RISK_DASHBOARD
and EXPOSURE_ANALYSIS hold 253 formulas pointing at `LOAN_DATA!X3:X7`, all of
which have to follow.

The mechanism is a ROW MAP per sheet — old row -> new row — built from the block
deltas before anything is written:

    row inside a block   first stays put, last moves to the new last row
    row after a block    shifts by the cumulative delta above it
    row before any block unchanged

Both jobs then reduce to one lookup. Moving content is `new = row_map[old]`.
Extending `=SUM(E6:E10)` is rewriting each endpoint through the same map, which
turns it into `=SUM(E6:E45)` — start unchanged because row 6 maps to itself, end
extended because row 10 was the block's last row. A formula that also MOVED gets
its own position from the map too, so a TOTAL row lands below the data it totals.

Pure except for openpyxl: no Snowflake, no filesystem.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from openpyxl.cell.cell import MergedCell
from openpyxl.utils import get_column_letter

# One A1-style reference, optionally sheet-qualified, optionally $-anchored.
# `$` on the ROW pins it against fill-down, but it does NOT exempt it from a
# structural move: an absolute row still refers to a row that has shifted.
# A reference or a RANGE, matched as one unit.
#
# Two failures forced this shape. The sheet name and its `!` must be optional
# TOGETHER: with `!` separately optional, `AC7` parsed as sheet `A` + column
# `C`, and `LOAN_DATA!AC3:AC7` came out as `AC3:C7`. And a range must be one
# match, because in `LOAN_DATA!K3:K7` only the FIRST endpoint carries the sheet
# — parsed alone, `K7` looked like a local row and never followed LOAN_DATA.
_REF = re.compile(
    r"(?:(?P<sheet>'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
    r"(?P<c1abs>\$?)(?P<c1>[A-Z]{1,3})(?P<r1abs>\$?)(?P<r1>\d+)"
    r"(?:\s*:\s*(?P<c2abs>\$?)(?P<c2>[A-Z]{1,3})(?P<r2abs>\$?)(?P<r2>\d+))?"
    r"(?![A-Za-z0-9_(])"
)


class RowMap:
    """old row -> new row for one sheet."""

    def __init__(self, mapping: dict[int, int], shift_beyond: int, delta: int):
        self._map = mapping
        self._shift_beyond = shift_beyond   # rows past this just take `delta`
        self._delta = delta

    def __call__(self, row: int) -> int:
        if row in self._map:
            return self._map[row]
        if row > self._shift_beyond:
            return row + self._delta
        return row

    @property
    def changed(self) -> bool:
        return self._delta != 0 or any(k != v for k, v in self._map.items())


def build_row_map(blocks: list[dict[str, Any]], new_rows: dict[int, int],
                  sheet_max_row: int) -> RowMap:
    """Row map for one sheet.

    `blocks` are that sheet's blocks in row order; `new_rows` maps a block's
    `first_row` to how many records it will hold after the run.
    """
    mapping: dict[int, int] = {}
    cumulative = 0
    last_block_end = 0

    for b in blocks:
        first, last = b["first_row"], b["last_row"]
        old_count = last - first + 1
        new_count = new_rows.get(first, old_count)
        delta = new_count - old_count

        # Everything between the previous block and this one shifts by whatever
        # accumulated above it — including this block's own header and banner.
        for r in range(last_block_end + 1, first):
            mapping[r] = r + cumulative

        # The block itself: its first row moves only by what accumulated above,
        # its last row also absorbs its own growth. Interior rows are covered by
        # the same rule; ranges only ever reference the boundaries.
        for r in range(first, last + 1):
            mapping[r] = r + cumulative + (delta if r == last else 0)

        cumulative += delta
        last_block_end = last

    for r in range(last_block_end + 1, sheet_max_row + 1):
        mapping[r] = r + cumulative

    return RowMap(mapping, sheet_max_row, cumulative)


def rewrite_formula(formula: str, own_sheet: str,
                    row_map_for: Callable[[str], RowMap | None]) -> str:
    """Re-point every reference in a formula through the row maps.

    An unqualified reference belongs to `own_sheet`; a qualified one to the
    sheet it names. A sheet with no map (nothing on it moved) is left alone.
    """
    if not isinstance(formula, str) or not formula.startswith("="):
        return formula

    def _sub(m: re.Match) -> str:
        raw_sheet = m.group("sheet")
        sheet = raw_sheet.strip("'") if raw_sheet else own_sheet
        rmap = row_map_for(sheet)
        if rmap is None:
            return m.group(0)
        prefix = f"{raw_sheet}!" if raw_sheet else ""
        out = (f"{prefix}{m.group('c1abs')}{m.group('c1')}"
               f"{m.group('r1abs')}{rmap(int(m.group('r1')))}")
        if m.group("r2"):
            out += (f":{m.group('c2abs')}{m.group('c2')}"
                    f"{m.group('r2abs')}{rmap(int(m.group('r2')))}")
        return out

    return _REF.sub(_sub, formula)


def _copy_cell(src, dst) -> None:
    """Value plus the formatting that makes a report readable."""
    dst.value = src.value
    if src.has_style:
        dst._style = src._style


def reflow_sheet(ws, blocks: list[dict[str, Any]], new_rows: dict[int, int],
                 row_map: RowMap, row_map_for: Callable[[str], RowMap | None]) -> dict[str, Any]:
    """Move every cell on the sheet to its mapped row and rewrite its formulas.

    Works on a snapshot taken before anything is written, so a block growing
    downward cannot overwrite rows it has not copied yet.
    """
    max_row, max_col = ws.max_row, ws.max_column

    snapshot: list[tuple[int, int, Any, Any]] = []
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            if cell.value is not None or cell.has_style:
                snapshot.append((r, c, cell.value, cell._style if cell.has_style else None))

    # Rows a block will vacate or overwrite must be cleared, or the tail of a
    # shrinking block survives underneath the new data.
    highest = max(max_row, max((row_map(r) for r in range(1, max_row + 1)), default=max_row))
    for r in range(1, highest + 1):
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            # A merged cell's value is owned by the top-left member and is
            # read-only everywhere else; assigning to one raises.
            if not isinstance(cell, MergedCell):
                cell.value = None

    moved = 0
    for old_row, col, value, style in snapshot:
        new_row = row_map(old_row)
        cell = ws.cell(row=new_row, column=col)
        if isinstance(cell, MergedCell):
            continue
        cell.value = rewrite_formula(value, ws.title, row_map_for) if isinstance(value, str) else value
        if style is not None:
            cell._style = style
        if new_row != old_row:
            moved += 1

    charts = reflow_charts(ws, row_map, row_map_for)
    tables = reflow_tables(ws, row_map)

    return {"sheet": ws.title, "cells_moved": moved, "new_max_row": highest,
            "charts_moved": charts, "tables_resized": tables}


def reflow_tables(ws, row_map: RowMap) -> int:
    """Re-point each Excel Table's stored range.

    A Table is NOT a formula — it is a saved range (`ref="A1:H100"`) carrying a
    name. Opening the file recalculates formulas; it does not resize tables. So
    rows added past the stored ref sit OUTSIDE the table, and every structured
    reference (`SUM(Table1[Price])`) keeps summing the original rows without
    saying so. Excel only auto-expands a table when a person types beside it.
    """
    resized = 0
    for name, table in list(getattr(ws, "tables", {}).items()):
        ref = getattr(table, "ref", None)
        if not ref or ":" not in ref:
            continue
        start, end = ref.split(":", 1)
        m_start = _REF.match(start)
        m_end = _REF.match(end)
        if not m_start or not m_end:
            continue
        new_ref = (
            f"{m_start.group('c1abs')}{m_start.group('c1')}"
            f"{m_start.group('r1abs')}{row_map(int(m_start.group('r1')))}"
            f":{m_end.group('c1abs')}{m_end.group('c1')}"
            f"{m_end.group('r1abs')}{row_map(int(m_end.group('r1')))}"
        )
        if new_ref != ref:
            table.ref = new_ref
            resized += 1
    return resized


def reflow_charts(ws, row_map: RowMap,
                  row_map_for: Callable[[str], RowMap | None]) -> int:
    """Move charts down with the rows beneath them, and re-point their series.

    Charts live outside the cell grid: openpyxl anchors them by row/column and
    keeps their data as string references (`TIMESERIES!$E$40:$E$48`). Moving
    cells therefore does nothing to them — a chart stayed at row 52 while the
    table it plots slid down, so it ended up sitting on top of the data and
    still charting the original nine rows.

    Anchor rows are ZERO-based here while the row map is one-based, hence the
    conversions; getting that wrong shifts every chart by a row.
    """
    moved = 0
    for chart in list(getattr(ws, "_charts", [])):
        anchor = getattr(chart, "anchor", None)
        for corner in ("_from", "to"):
            marker = getattr(anchor, corner, None)
            if marker is not None and getattr(marker, "row", None) is not None:
                marker.row = row_map(marker.row + 1) - 1
        if anchor is not None:
            moved += 1

        for series in getattr(chart, "series", []):
            for part in ("val", "cat", "xVal", "yVal"):
                holder = getattr(series, part, None)
                if holder is None:
                    continue
                for kind in ("numRef", "strRef", "multiLvlStrRef"):
                    ref = getattr(holder, kind, None)
                    if ref is None or not getattr(ref, "f", None):
                        continue
                    # `=` so the shared rewriter treats it as a formula; the
                    # stored form has no leading equals sign.
                    ref.f = rewrite_formula("=" + ref.f, ws.title, row_map_for)[1:]
    return moved


def fill_block(ws, block: dict[str, Any], row_map: RowMap,
               columns: list[str], rows: list[list[Any]],
               column_map: dict[str, str]) -> dict[str, Any]:
    """Write query rows into a block that has already been reflowed.

    `column_map` is {template column letter -> query column name}. Columns the
    template computes for itself are NOT in it; they are filled down from the
    block's own first-row formula instead, so the workbook keeps its logic.
    """
    from openpyxl.utils import column_index_from_string

    first = row_map(block["first_row"])
    q_index = {name: i for i, name in enumerate(columns)}

    # The template's own formula columns, captured before we write anything.
    formula_cols = {
        c["column"]: c["formula_template"]
        for c in block["columns"]
        if c["source"] == "formula" and c["formula_template"]
    }

    written = 0
    for i, row in enumerate(rows):
        target_row = first + i
        for col_letter, q_name in column_map.items():
            if q_name not in q_index:
                continue
            cell = ws.cell(row=target_row, column=column_index_from_string(col_letter))
            if isinstance(cell, MergedCell):
                continue
            cell.value = row[q_index[q_name]]
            written += 1
        for col_letter, template in formula_cols.items():
            cell = ws.cell(row=target_row, column=column_index_from_string(col_letter))
            if not isinstance(cell, MergedCell):
                cell.value = _shift_relative_rows(template, i)

    return {
        "sheet": block["sheet"],
        "first_row": first,
        "last_row": first + len(rows) - 1 if rows else first - 1,
        "rows_written": len(rows),
        "cells_written": written,
        "filled_down": sorted(formula_cols),
    }


def _shift_relative_rows(formula: str, delta: int) -> str:
    """Excel fill-down: relative rows advance, absolute (`$3`) rows do not."""
    if delta == 0 or not isinstance(formula, str) or not formula.startswith("="):
        return formula

    def _sub(m: re.Match) -> str:
        raw_sheet = m.group("sheet")
        prefix = f"{raw_sheet}!" if raw_sheet else ""
        r1 = int(m.group("r1")) + (0 if m.group("r1abs") == "$" else delta)
        out = f"{prefix}{m.group('c1abs')}{m.group('c1')}{m.group('r1abs')}{r1}"
        if m.group("r2"):
            r2 = int(m.group("r2")) + (0 if m.group("r2abs") == "$" else delta)
            out += f":{m.group('c2abs')}{m.group('c2')}{m.group('r2abs')}{r2}"
        return out

    return _REF.sub(_sub, formula)


_SINGLE_REF = re.compile(
    r"(?P<sheet>'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!"
    r"\$?(?P<col>[A-Z]{1,3})\$?(?P<row>\d+)(?!\d*\s*:)"
)


def _tracked_rows(formula: Any) -> dict[tuple[str, str], int]:
    """{(sheet, column) -> row} for each single-cell cross-sheet reference."""
    out: dict[tuple[str, str], int] = {}
    if not isinstance(formula, str) or not formula.startswith("="):
        return out
    for m in _SINGLE_REF.finditer(formula):
        out[(m.group("sheet").strip("'"), m.group("col"))] = int(m.group("row"))
    return out


def link_mirror_blocks(wb, all_blocks: list[dict[str, Any]],
                       new_rows: dict[tuple[str, int], int]) -> dict[tuple[str, int], int]:
    """Give every block that mirrors a source block the source's row count.

    EXPOSURE_ANALYSIS rows 6-10 are `=LOAN_DATA!A3` … `=LOAN_DATA!A7` — one
    output row per loan. Grow the register to forty and this block must follow,
    or the report shows five.

    A block mirrors when its reference into the source ADVANCES BY ONE from its
    first row to its second. Merely *containing* a reference is not enough:
    `B. Exposure by Sector` reads `=COUNTIF(LOAN_DATA!E3:E7,A17)` on every row,
    where the LOAN_DATA rows never move and only the local `A17` advances. It is
    five sectors whether there are five loans or four hundred — its ranges
    extend, its height does not. Testing containment grew it to 40 rows.
    """
    extents = [(b["sheet"], b["first_row"], b["last_row"]) for b in all_blocks]
    resolved = dict(new_rows)

    def source_of(sheet: str, row: int) -> tuple[str, int] | None:
        for s, first, last in extents:
            if s == sheet and first <= row <= last:
                return (s, first)
        return None

    for b in all_blocks:
        key = (b["sheet"], b["first_row"])
        if key in resolved or b["first_row"] >= b["last_row"]:
            continue
        from openpyxl.utils import column_index_from_string

        ws = wb[b["sheet"]]
        formula_cols = [c for c in b["columns"]
                        if isinstance(c.get("formula_template"), str)]
        advancing: list[tuple[str, int]] = []

        for col in formula_cols:
            first_refs = _tracked_rows(col["formula_template"])
            if not first_refs:
                continue
            second = ws.cell(row=b["first_row"] + 1,
                             column=column_index_from_string(col["column"])).value
            second_refs = _tracked_rows(second)
            for ref_key, row in first_refs.items():
                if second_refs.get(ref_key) == row + 1:
                    src = source_of(ref_key[0], row)
                    if src and src in resolved:
                        advancing.append(src)
                    break

        # A MAJORITY must advance, not merely one column. `B. Exposure by
        # Sector` is a five-row category list, but its "% of Portfolio" column
        # reads `LOAN_DATA!L6` then `LOAN_DATA!L7` — a formula fill-dragged down
        # a list it does not belong to, which is a defect in the template. One
        # such column made the whole block look like a mirror and grew five
        # sectors to forty rows.
        if advancing and len(advancing) * 2 >= len(formula_cols):
            resolved[key] = resolved[advancing[0]]
    return resolved


def regenerate_block(ws, block: dict[str, Any], row_map: RowMap,
                     new_count: int) -> dict[str, Any]:
    """Re-create a mirror block at `new_count` rows by filling its first row down.

    Used for blocks the workbook computes: every column keeps its formula, so
    the numbers are still Excel's to calculate when the file is opened.
    """
    from openpyxl.utils import column_index_from_string

    first = row_map(block["first_row"])
    templates = {
        c["column"]: c["formula_template"]
        for c in block["columns"]
        if c["source"] == "formula" and c["formula_template"]
    }
    if not templates:
        return {"sheet": block["sheet"], "rows": 0, "columns": []}

    for i in range(new_count):
        for col_letter, template in templates.items():
            cell = ws.cell(row=first + i, column=column_index_from_string(col_letter))
            if not isinstance(cell, MergedCell):
                cell.value = _shift_relative_rows(template, i)

    return {
        "sheet": block["sheet"],
        "first_row": first,
        "rows": new_count,
        "columns": sorted(templates),
    }


def match_columns(block: dict[str, Any], query_columns: list[str]) -> dict[str, str]:
    """{template column letter -> query column} by normalised header name.

    Only value columns are matched; a column the template computes keeps its
    formula even if the query happens to supply a column of the same name.
    """
    def norm(s: Any) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", str(s)).upper()

    by_norm: dict[str, str] = {}
    for q in query_columns:
        by_norm.setdefault(norm(q), q)

    out: dict[str, str] = {}
    for col in block["columns"]:
        if col["source"] != "value" or not col["header"]:
            continue
        q = by_norm.get(norm(col["header"]))
        if q is not None:
            out[col["column"]] = q
    return out


def unmatched_headers(block: dict[str, Any], column_map: dict[str, str]) -> list[str]:
    """Value columns the query did not supply — these would be left blank, and
    silence is the wrong answer for a report someone signs off on."""
    return [
        f"{c['column']}:{c['header']}"
        for c in block["columns"]
        if c["source"] == "value" and c["header"] and c["column"] not in column_map
    ]

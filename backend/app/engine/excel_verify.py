"""Guards that do not depend on knowing what is in a workbook.

Every reflow bug so far was found the same way: someone opened a report and
noticed something missing. Charts were not moved because nobody thought about
charts. The next template will hold a pivot table, a defined name, or data
validation, and the same thing will happen again — because the approach was to
enumerate Excel features, and that list has no end.

Two checks here take the opposite approach.

`inventory` looks for the features a workbook ACTUALLY contains and reports the
ones the reflow does not know how to move. It cannot say what those features
will do; it can say "this workbook has 4 data validations and we do not move
them", which turns a silent corruption into a stated limitation.

`conservation` checks the OUTPUT instead of the input. Whatever the template
held, every non-empty cell must still exist somewhere afterwards. It needs no
knowledge of what a feature is: a row that vanished is a bug whether it held a
number, a formula, or something invented after this was written.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Features the reflow understands. Anything found outside this set is reported.
#
# Kept as data, not scattered through the code, so that adding support for a
# feature and removing its warning are the same edit.
HANDLED = {
    "cells", "formulas", "merged_cells", "charts", "tables",
    # A freeze splits at a cell above the data. Blocks only ever grow BELOW
    # that point, so the split stays where it belongs — reporting it every run
    # would be noise, and a warning nobody can act on trains people to ignore
    # the ones that matter.
    "freeze_panes",
}

# How to count each feature on a worksheet. A feature absent from every sheet is
# never mentioned — the point is what THIS workbook contains, not a lecture.
_PROBES: dict[str, Any] = {
    "charts": lambda ws: len(getattr(ws, "_charts", []) or []),
    "images": lambda ws: len(getattr(ws, "_images", []) or []),
    "merged_cells": lambda ws: len(list(getattr(ws, "merged_cells", []).ranges or [])),
    "conditional_formatting": lambda ws: sum(
        1 for _ in (getattr(ws, "conditional_formatting", []) or [])
    ),
    "data_validation": lambda ws: len(
        getattr(getattr(ws, "data_validations", None), "dataValidation", []) or []
    ),
    "auto_filter": lambda ws: 1 if getattr(getattr(ws, "auto_filter", None), "ref", None) else 0,
    "tables": lambda ws: len(getattr(ws, "tables", {}) or {}),
    "hyperlinks": lambda ws: len(getattr(ws, "_hyperlinks", []) or []),
    "comments": lambda ws: sum(
        1 for row in ws.iter_rows() for c in row if getattr(c, "comment", None)
    ),
    "freeze_panes": lambda ws: 1 if getattr(ws, "freeze_panes", None) else 0,
}

# What each unhandled feature will do if the rows beneath it move. Stated
# plainly, because "unsupported" tells nobody whether their report is wrong.
_CONSEQUENCE = {
    "images": "stay at their original position while the content around them moves",
    "conditional_formatting": "keep their original row ranges, so colouring stops "
                              "at the rows the template had",
    "data_validation": "keep their original row ranges, so new rows get no dropdown",
    "auto_filter": "keeps its original range and will not cover added rows",
    "tables": "keep their original range — an Excel Table will not grow with the data",
    "hyperlinks": "stay on their original rows and may end up on the wrong content",
    "comments": "stay on their original rows",
}


def _counts_from_package(source) -> Counter:
    """Charts and tables counted from the .xlsx ZIP rather than the parsed cells.

    A read-only workbook exposes neither, and a full parse costs a minute on a
    large feed. Both live as their own XML parts, so counting entries answers
    the question without touching a single cell.
    """
    import io
    import zipfile

    out: Counter[str] = Counter()
    try:
        if isinstance(source, (bytes, bytearray)):
            handle: Any = io.BytesIO(source)
        elif source is not None:
            handle = str(source)
        else:
            return out
        with zipfile.ZipFile(handle) as zf:
            for name in zf.namelist():
                if name.startswith("xl/charts/chart") and name.endswith(".xml"):
                    out["charts"] += 1
                elif name.startswith("xl/tables/table") and name.endswith(".xml"):
                    out["tables"] += 1
    except Exception:  # noqa: BLE001 - a probe must never break a scan
        pass
    return out


def inventory(wb, source: Any = None) -> dict[str, Any]:
    """What this workbook contains, and what we will not move."""
    found: Counter[str] = Counter()
    per_sheet: dict[str, dict[str, int]] = {}

    for ws in wb.worksheets:
        counts: dict[str, int] = {}
        for name, probe in _PROBES.items():
            try:
                n = int(probe(ws) or 0)
            except Exception:  # noqa: BLE001 - a probe must never break a run
                n = 0
            if n:
                counts[name] = n
                found[name] += n
        if counts:
            per_sheet[ws.title] = counts

    # Defined names live on the workbook, not a sheet.
    try:
        names = len(list(getattr(wb, "defined_names", []) or []))
    except Exception:  # noqa: BLE001
        names = 0
    if names:
        found["defined_names"] = names

    # Charts and tables are invisible to a read-only workbook; take them from
    # the package instead, and never let a zero there erase a real count.
    for feature, count in _counts_from_package(source).items():
        found[feature] = max(found.get(feature, 0), count)

    unhandled = {k: v for k, v in found.items() if k not in HANDLED}
    warnings = [
        f"{count} {feature.replace('_', ' ')} — these "
        f"{_CONSEQUENCE.get(feature, 'are not adjusted when rows move')}."
        for feature, count in sorted(unhandled.items())
    ]
    return {
        "features": dict(found),
        "per_sheet": per_sheet,
        "unhandled": unhandled,
        "warnings": warnings,
    }


def _cell_census(wb) -> dict[str, Counter]:
    """Per sheet, a multiset of the non-empty cell VALUES.

    Values rather than positions, because positions are supposed to change. A
    formula is normalised to the fact that it is a formula in that column: its
    row references are rewritten by design, so comparing the text would report
    every correct move as a loss.
    """
    census: dict[str, Counter] = {}
    for ws in wb.worksheets:
        bag: Counter = Counter()
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if isinstance(v, str) and v.startswith("="):
                    bag[f"formula:{cell.column_letter}"] += 1
                else:
                    bag[f"value:{v!r}"] += 1
        census[ws.title] = bag
    return census


def conservation(before_wb, after_wb, filled_sheets: set[str] | None = None) -> list[str]:
    """Anything the template held that the output lost.

    Sheets that received query data are skipped for VALUES — their old sample
    rows are supposed to disappear — but their formula columns are still
    checked, since a data refill must never remove the sheet's own calculations.

    This is the check that does not need to know what a chart is.
    """
    filled = {s.upper() for s in (filled_sheets or set())}
    before, after = _cell_census(before_wb), _cell_census(after_wb)
    problems: list[str] = []

    for sheet, bag in before.items():
        if sheet not in after:
            problems.append(f"{sheet}: the whole sheet is missing from the output")
            continue
        got = after[sheet]
        was_filled = sheet.upper() in filled
        for key, count in bag.items():
            if was_filled and key.startswith("value:"):
                continue  # replaced by query data, as intended
            lost = count - got.get(key, 0)
            if lost > 0:
                what = (
                    f"{lost} formula cell(s) in column {key.split(':', 1)[1]}"
                    if key.startswith("formula:")
                    else f"the value {key.split(':', 1)[1]}"
                )
                problems.append(f"{sheet}: {what} did not survive the rebuild")

    # Charts are counted rather than censused: losing one is unambiguous.
    for ws in before_wb.worksheets:
        b = len(getattr(ws, "_charts", []) or [])
        if not b:
            continue
        a = next(
            (len(getattr(w, "_charts", []) or [])
             for w in after_wb.worksheets if w.title == ws.title),
            0,
        )
        if a < b:
            problems.append(f"{ws.title}: {b - a} chart(s) lost")

    return problems

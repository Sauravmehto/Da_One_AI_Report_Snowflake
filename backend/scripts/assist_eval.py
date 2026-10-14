"""Edit-assistant requests run against the real model, with checks on the result.

Not part of pytest: every case is a paid model call. Run it after changing the
assistant's prompt or checks, to see which requests still come out right:

    .venv/Scripts/python.exe -m scripts.assist_eval
    .venv/Scripts/python.exe -m scripts.assist_eval --case labels_outside --show

The dashboard reads an in-memory DuckDB table, so every change goes through the
same dry run and column checks it would on an uploaded file. A case passes
when the draft that comes back has what the request asked for — not when the
reply says so.
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import duckdb

from app.core.config import settings
from app.engine import dashboard_assist

_TABLE = """
CREATE TABLE loans AS
SELECT
    i AS loan_id,
    ['Personal', 'Auto', 'Mortgage'][1 + i % 3] AS product,
    ['North', 'South', 'East', 'West'][1 + i % 4] AS region,
    ['A', 'B', 'C'][1 + i % 3] AS grade,
    ['Current', 'Late', 'Default'][1 + CASE WHEN i % 10 = 0 THEN 2 WHEN i % 7 = 0 THEN 1 ELSE 0 END] AS status,
    DATE '2025-01-01' + INTERVAL (i % 12) MONTH AS origination_month,
    1000 + (i * 37) % 9000 AS balance,
    0.90 + ((i * 13) % 10) / 100.0 AS repayment_rate
FROM range(1, 601) t(i)
"""

_CONTEXT = """TABLE loans — one row per loan.
- loan_id INTEGER: loan identifier
- product VARCHAR: Personal, Auto or Mortgage
- region VARCHAR: North, South, East or West
- grade VARCHAR: credit grade A, B or C
- status VARCHAR: Current, Late or Default
- origination_month DATE: first day of the month the loan started
- balance INTEGER: outstanding balance in USD
- repayment_rate DOUBLE: share of scheduled payments made, between 0.90 and 1.00"""


def _panel(title: str, sql: str, chart_type: str | None, x: Any, y: Any, **extra: Any
           ) -> dict[str, Any]:
    return {"title": title, "sql": sql, "chart_type": chart_type, "x": x, "y": y,
            "color": None, "size": None, "orientation": None,
            "display": "chart" if chart_type else "table", "section": None, "span": 2,
            "row_span": 2, "column_formats": None, "zero_baseline": None, "palette": None,
            "format": None, **extra}


def _dashboard() -> dict[str, Any]:
    return dashboard_assist.clean_draft({
        "title": "Loan book",
        "parameters": [],
        "panels": [
            _panel("Balance by month",
                   "SELECT strftime(origination_month, '%Y-%m') AS MONTH_LABEL, "
                   "SUM(balance) AS TOTAL_BALANCE FROM loans GROUP BY 1 ORDER BY 1",
                   "bar", "MONTH_LABEL", "TOTAL_BALANCE"),
            _panel("Loans by status",
                   "SELECT status AS STATUS, COUNT(*) AS LOANS FROM loans GROUP BY 1 "
                   "ORDER BY 2 DESC",
                   "donut", "STATUS", "LOANS"),
            _panel("Repayment rate",
                   "SELECT origination_month AS MONTH, AVG(repayment_rate) AS RATE "
                   "FROM loans GROUP BY 1 ORDER BY 1",
                   "line", "MONTH", "RATE"),
            _panel("Balance by product and grade",
                   "SELECT product AS PRODUCT, "
                   "SUM(CASE WHEN grade = 'A' THEN balance END) AS GRADE_A, "
                   "SUM(CASE WHEN grade = 'B' THEN balance END) AS GRADE_B, "
                   "SUM(CASE WHEN grade = 'C' THEN balance END) AS GRADE_C "
                   "FROM loans GROUP BY 1 ORDER BY 1",
                   "stacked_bar", "PRODUCT", ["GRADE_A", "GRADE_B", "GRADE_C"]),
            _panel("Defaults by region and month",
                   "SELECT strftime(origination_month, '%Y-%m') AS MONTH_LABEL, "
                   "region AS REGION, AVG(CASE WHEN status = 'Default' THEN 1 ELSE 0 END) "
                   "AS DEFAULT_RATE FROM loans GROUP BY 1, 2 ORDER BY 2, 1",
                   "heatmap", "MONTH_LABEL", "DEFAULT_RATE", color="REGION"),
            _panel("Balance against repayment",
                   "SELECT loan_id AS LOAN, balance AS BALANCE, repayment_rate AS RATE "
                   "FROM loans ORDER BY loan_id LIMIT 200",
                   "scatter", "BALANCE", "RATE"),
        ],
    })


def _format(plan: dict[str, Any], index: int) -> dict[str, Any]:
    return plan["panels"][index].get("format") or {}


@dataclass
class Case:
    name: str
    message: str
    check: Callable[[dict[str, Any]], str | None]
    #: Changes to the draft before the request, as a person's hand edits.
    prepare: Callable[[dict[str, Any]], None] | None = None


def _applied(result: dict[str, Any]) -> dict[str, Any] | None:
    return result.get("plan") if result.get("status") == "applied" else None


def _needs(condition: bool, why: str) -> str | None:
    return None if condition else why


def _labels_outside(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    fmt = _format(plan, 0)
    return _needs(fmt.get("label_position") == "outside_end"
                  and fmt.get("data_labels") not in (None, "none"),
                  f"format is {fmt}")


def _horizontal(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    panel = plan["panels"][1]
    return _needs(panel["chart_type"] in ("bar", "grouped_bar") and panel["orientation"] == "h",
                  f"chart_type {panel['chart_type']}, orientation {panel['orientation']}")


def _not_flat(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    return _needs(plan["panels"][2]["zero_baseline"] is False,
                  f"zero_baseline {plan['panels'][2]['zero_baseline']}")


def _legend_and_totals(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    fmt = _format(plan, 3)
    return _needs(fmt.get("legend") == "none" and fmt.get("stack_totals") is True,
                  f"format is {fmt}")


def _already_set(result):
    return _needs(result["status"] == "no_change", f"status {result['status']}")


def _impossible_position(result):
    if result["status"] == "rejected":
        return "the change was refused by the checks: " + "; ".join(result["problems"])
    plan = _applied(result)
    if plan is not None and "label_position" in _format(plan, 2):
        return "set label_position on a line chart"
    return None


def _heatmap_order(result):
    """Its labels already sort as text, so nothing needs to change; a change that
    keeps the heatmap is tolerated."""
    if result["status"] == "no_change":
        return None
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}: {result.get('problems')}"
    return _needs(plan["panels"][4]["chart_type"] == "heatmap",
                  f"changed: {result.get('changes')}")


def _unsaved(result):
    reply = (result.get("reply") or "").lower()
    return _needs(result["status"] == "no_change" and "save" in reply
                  and ("not" in reply or "n't" in reply or "unsaved" in reply),
                  f"reply: {result.get('reply')}")


def _added(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}: {result.get('problems')}"
    added = plan["panels"][len(_dashboard()["panels"]):]
    return _needs(len(added) == 1 and added[0]["chart_type"] in ("bar", "grouped_bar"),
                  f"added {[p.get('chart_type') for p in added]}")


def _markers(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    return _needs(_format(plan, 2).get("markers") == "show", f"format is {_format(plan, 2)}")


def _palette(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    return _needs(plan["panels"][1]["palette"] == "ember", f"palette {plan['panels'][1]['palette']}")


def _millions_keeps_labels(result):
    plan = _applied(result)
    if plan is None:
        return f"status {result['status']}"
    fmt = _format(plan, 0)
    return _needs(fmt.get("unit") == "M" and fmt.get("data_labels") == "all",
                  f"format is {fmt}")


def _labels_on(draft):
    draft["panels"][0]["format"] = {"data_labels": "all"}


CASES = [
    Case("labels_outside", "Show the values outside the bars on Balance by month.",
         _labels_outside),
    Case("horizontal", "Make Loans by status a horizontal bar chart.", _horizontal),
    Case("not_flat", "The repayment rate line looks completely flat, I can't see any movement.",
         _not_flat),
    Case("legend_and_totals",
         "On the product and grade chart, hide the legend and show the total on each stack.",
         _legend_and_totals),
    Case("already_set", "Turn on all the value labels on Balance by month.", _already_set,
         prepare=_labels_on),
    Case("impossible_position", "Put the labels inside the end on the repayment rate chart.",
         _impossible_position),
    Case("heatmap_order", "Make sure the heatmap months run in calendar order.",
         _heatmap_order),
    Case("unsaved", "Are the labels on Balance by month saved already?", _unsaved,
         prepare=_labels_on),
    Case("add_panel", "Add a bar chart of the number of loans by region.", _added),
    Case("markers", "Show a marker on every point of the repayment rate line.", _markers),
    Case("palette", "Use the ember colours on Loans by status.", _palette),
    Case("millions", "Show Balance by month in millions.", _millions_keeps_labels,
         prepare=_labels_on),
]


def _run(case: Case) -> tuple[Case, dict[str, Any], str | None]:
    connection = duckdb.connect()
    connection.execute(_TABLE)

    def dry_run(sql: str) -> list[str]:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT * FROM ({sql}) AS _probe LIMIT 0")
            return [column[0] for column in cursor.description]
        finally:
            cursor.close()

    saved = _dashboard()
    draft = copy.deepcopy(saved)
    if case.prepare:
        case.prepare(draft)
    try:
        result = dashboard_assist.assist(draft, case.message, [], business_context=_CONTEXT,
                                         source="file", dry_run=dry_run, saved=saved,
                                         saved_version=4)
    except Exception as exc:  # noqa: BLE001 - reported as the case's failure
        return case, {"status": "error", "reply": str(exc)}, f"raised {exc!r}"
    finally:
        connection.close()
    return case, result, case.check(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="run only these cases")
    parser.add_argument("--show", action="store_true", help="print every reply")
    parser.add_argument(
        "--model",
        help="run the assistant on this model instead of MCP_MODEL_DASHBOARD_ASSIST, "
        "to compare a lighter one against the current default",
    )
    args = parser.parse_args()
    from app.engine import llm as engine_llm

    if args.model:
        engine_llm.MODEL_DASHBOARD_ASSIST = args.model
    print(f"model: {engine_llm.MODEL_DASHBOARD_ASSIST}")

    # Run outside a signed-in session, there is no Snowflake connection for the
    # audit rows; each call would warn about it.
    logging.getLogger("app.engine.audit").setLevel(logging.CRITICAL)
    if not settings.ai_enabled:
        print("ANTHROPIC_API_KEY is not set; nothing to run.")
        return 2
    cases = [c for c in CASES if not args.case or c.name in args.case]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(_run, cases))

    failed = 0
    for case, result, failure in outcomes:
        mark = "PASS" if failure is None else "FAIL"
        failed += failure is not None
        print(f"{mark}  {case.name:<22} {result['status']:<20} {failure or ''}")
        if args.show or failure is not None:
            print(f"      reply: {result.get('reply')}")
            if result.get("changes"):
                print(f"      changes: {result['changes']}")
            if result.get("problems"):
                print(f"      problems: {result['problems']}")
    print(f"\n{len(outcomes) - failed} of {len(outcomes)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Live round-trip test for the saved-dashboard feature (TEST_PLAN.md item 6).

Exercises plan -> iterate x3 -> save last -> verify stored plan matches the shown
iteration -> list -> run/refresh x2, asserting distinct plan_ids and climbing run
stats. Hits Snowflake + Anthropic for real; needs a working .env.

Run:  .venv\\Scripts\\python.exe tests\\manual_dashboard_roundtrip.py
"""

import sys
from pathlib import Path

# Load .env and make the project importable (same as server.py does).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from app.engine import dashboards  # noqa: E402
from app.engine.connectors.snowflake import client as snowflake_client  # noqa: E402

DATABASE = "LOAN"
_checks: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> bool:
    _checks.append((bool(cond), label))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def ensure_table() -> None:
    ddl = (Path(__file__).resolve().parents[2] / "sql" / "saved_dashboards.sql").read_text()
    # Strip comment lines; execute the single CREATE TABLE statement.
    stmt = "\n".join(l for l in ddl.splitlines() if not l.strip().startswith("--")).strip().rstrip(";")
    conn = snowflake_client.get_connection()
    cur = conn.cursor()
    try:
        cur.execute(stmt)
    finally:
        cur.close()


def main() -> int:
    print("== 0. Connectivity + table ==")
    user, role = snowflake_client.session_identity()
    print(f"  connected as user={user} role={role}")
    ensure_table()
    check(True, "table exists / created")

    print("== 1-4. Plan three iterations ==")
    descriptions = [
        "A small dashboard of the loan portfolio: loan count by state. Just 1 panel.",
        "A small loan portfolio dashboard: loan count by state, and total balance by "
        "property type. 2 panels.",
        "A small loan portfolio dashboard: loan count by state, total balance by "
        "property type, and a KPI panel for the delinquency rate. 3 panels.",
    ]
    plan_ids: list[str] = []
    cached_plans: list[dict] = []
    for i, desc in enumerate(descriptions, start=1):
        res = dashboards.create_plan(desc, DATABASE)
        pid = res["plan_id"]
        plan_ids.append(pid)
        cached_plans.append(dashboards.get_cached_plan(pid)["plan"])
        print(f"  iteration {i}: plan_id={pid} panels={res['panel_count']} "
              f"title={res['plan']['title']!r}")

    check(len(set(plan_ids)) == 3, "three iterations produced three distinct plan_ids")

    print("== 5. Expired/bogus plan_id is rejected, not silently saved ==")
    try:
        dashboards.save_plan("deadbeef-not-a-real-plan", "should not save")
        check(False, "bogus plan_id raised an error")
    except dashboards.DashboardError as exc:
        check("expired" in str(exc).lower() or "no cached plan" in str(exc).lower(),
              "bogus plan_id -> DashboardError telling caller to re-plan")

    print("== 6. Save the LAST iteration ==")
    last_pid = plan_ids[-1]
    saved = dashboards.save_plan(last_pid, "Roundtrip Loan Portfolio",
                                 description="round-trip test",
                                 tags=["__roundtrip_test__"])
    report_id = saved["report_id"]
    check(saved.get("saved") is True and bool(report_id), f"saved -> report_id={report_id}")

    print("== 7. Stored plan == the iteration that was shown (not an earlier one) ==")
    rec = snowflake_client.read_saved_dashboard(report_id)
    stored_plan = rec["DASHBOARD_PLAN"]
    check(stored_plan == cached_plans[-1],
          "stored DASHBOARD_PLAN matches cached plan for the saved plan_id")
    check(stored_plan != cached_plans[0],
          "stored plan is NOT the first iteration (faithfulness)")

    print("== 9. Listing shows it ==")
    listed = dashboards.list_dashboards(50)
    check(any(d["report_id"] == report_id for d in listed), "report_id appears in list")

    print("== 10-11. Run/refresh twice; run stats climb ==")
    run1 = dashboards.run_dashboard(report_id)
    print(f"  run1: run_count={run1['run_count']} last_run_at={run1['last_run_at']} "
          f"panels={len(run1['panels'])}")
    panel_errors = [p for p in run1["panels"] if p.get("error")]
    for p in panel_errors:
        print(f"    panel error: {p.get('title')!r}: {p.get('error')}")
    check(all(not p.get("error") for p in run1["panels"]),
          "all panels executed live without error")
    check(any(p.get("row_count", 0) > 0 for p in run1["panels"]),
          "at least one panel returned rows")

    run2 = dashboards.run_dashboard(report_id)
    print(f"  run2: run_count={run2['run_count']} last_run_at={run2['last_run_at']}")
    check(run2["run_count"] == run1["run_count"] + 1, "run_count incremented on second refresh")
    check(run2["last_run_at"] >= run1["last_run_at"], "last_run_at advanced (or equal)")

    # Config unchanged between refreshes (faithful reproduction)
    cfg1 = [{k: p.get(k) for k in ("title", "chart_type", "x", "y", "color", "orientation", "display")}
            for p in run1["panels"]]
    cfg2 = [{k: p.get(k) for k in ("title", "chart_type", "x", "y", "color", "orientation", "display")}
            for p in run2["panels"]]
    check(cfg1 == cfg2, "panel config identical across refreshes")

    print("\n== SUMMARY ==")
    passed = sum(1 for ok, _ in _checks if ok)
    total = len(_checks)
    for ok, label in _checks:
        if not ok:
            print(f"  FAILED: {label}")
    print(f"  {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - report, don't traceback-dump
        import traceback
        print("\nROUND TRIP ABORTED with an exception:")
        traceback.print_exc()
        print(f"\n>>> {type(exc).__name__}: {exc}")
        sys.exit(2)

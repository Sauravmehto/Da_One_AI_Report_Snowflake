"""Manual + future golden scenarios for dashboard chart-choice quality.

Run these descriptions through plan_dashboard / the dashboard builder UI and
score whether the planner picked the chart types in `expect_chart_kinds`.
This module does not call the LLM — it is the shared checklist for Phase A/B.
"""

from __future__ import annotations

from typing import TypedDict


class DashboardScenario(TypedDict):
    id: str
    title: str
    description: str
    #: Soft expectations — at least one panel should use each of these kinds
    #: when the source data supports it. Used for manual review, not hard asserts.
    expect_chart_kinds: list[str]
    notes: str


#: Chart types the UI can render today (must stay aligned with llm.py cookbook).
RENDERABLE_CHART_TYPES = frozenset(
    {
        "bar",
        "stacked_bar",
        "line",
        "area",
        "pie",
        "donut",
        "scatter",
        "bubble",
        "heatmap",
        "radar",
        "treemap",
        "funnel",
        "combo",
        "area_line",
        "geo_us_states",
        "geo_countries",
        "kpi",
    }
)

#: Still not renderable. `funnel` was here until a FunnelPanel existed; it now
#: has one, which is why it moved up. `histogram` has no renderer and no
#: planner entry — the model must not be offered a shape nothing can draw.
UNSUPPORTED_CHART_TYPES = frozenset({"histogram"})


SCENARIOS: list[DashboardScenario] = [
    {
        "id": "ar_health",
        "title": "Accounts receivable health",
        "description": (
            "Accounts receivable health dashboard: total AR outstanding, disputed "
            "count and exposure as KPIs; aging by days overdue across clients as a "
            "heatmap; payment status mix; collection rate trend over recent months; "
            "monthly invoiced vs collected; top clients by outstanding balance."
        ),
        "expect_chart_kinds": ["kpi", "heatmap", "donut", "line", "area", "bar"],
        "notes": (
            "Aging × client → heatmap; status mix → donut; collection rate → line; "
            "invoiced/collected volume → area or grouped bar; clients → horizontal bar."
        ),
    },
    {
        "id": "occupancy_rollover",
        "title": "Occupancy and lease rollover",
        "description": (
            "Occupancy and rollover dashboard: occupancy rate and vacant units as KPIs; "
            "occupancy by property; lease expiration buckets (0-12 / 13-24 / 25-36 months); "
            "vacancy by unit type; detail table of soon-to-expire leases."
        ),
        "expect_chart_kinds": ["kpi", "bar", "stacked_bar", "table"],
        "notes": (
            "Property compare → bar (h if names long); expiration buckets → stacked_bar "
            "or bar; lease detail → table."
        ),
    },
    {
        "id": "tenant_credit_risk",
        "title": "Tenant credit and payment risk",
        "description": (
            "Tenant credit and payment risk: headline exposure and past-due counts; "
            "credit score vs outstanding balance by tenant (scatter); past-due amount "
            "by property; status mix of current vs delinquent; table of highest-risk tenants."
        ),
        "expect_chart_kinds": ["kpi", "scatter", "bar", "donut", "table"],
        "notes": "Two numeric metrics → scatter/bubble; rankings → bar; mix → donut.",
    },
    {
        "id": "fund_lp_summary",
        "title": "Fund / LP portfolio summary",
        "description": (
            "Fund performance overview: NAV, IRR, and DPI as KPIs; NAV trend over time; "
            "allocation by sector; performance by vintage; deal-level detail table."
        ),
        "expect_chart_kinds": ["kpi", "line", "area", "donut", "bar", "table"],
        "notes": "NAV level over time → area; rates → line; allocation → donut; vintage → bar.",
    },
    {
        "id": "rent_roll_snapshot",
        "title": "Rent roll snapshot (files source)",
        "description": (
            "Rent roll snapshot from uploaded files: total units and occupancy KPIs; "
            "rent by property; unit status mix; rent vs market by property type; "
            "as_of trend of total rent if multiple periods exist."
        ),
        "expect_chart_kinds": ["kpi", "bar", "donut", "line"],
        "notes": "File/folder sources — keep 3–8 panels; one section unless clearly split.",
    },
    {
        "id": "aging_matrix",
        "title": "AR aging matrix",
        "description": (
            "AR aging matrix: outstanding KPIs; intensity of outstanding balance by "
            "client (rows) and aging bucket 0-30 / 31-60 / 61-90 / 90+ (columns); "
            "status mix donut; top overdue clients as a horizontal bar."
        ),
        "expect_chart_kinds": ["kpi", "heatmap", "donut", "bar"],
        "notes": (
            "Classic heatmap use case — two categoricals + numeric intensity. "
            "Aging bucket labels should sort as 0-30 → 31-60 → 61-90 → 90+; "
            "cap clients with ORDER BY total outstanding LIMIT ~15."
        ),
    },
]

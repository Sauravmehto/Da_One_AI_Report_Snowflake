"""Guardrails for dashboard chart cookbook + scenario checklist."""

from __future__ import annotations

from app.engine import dashboard_scenarios as scenarios
from app.engine import llm


def test_schema_only_lists_renderable_chart_types():
    schema = llm._DASHBOARD_SCHEMA
    for banned in scenarios.UNSUPPORTED_CHART_TYPES:
        assert banned not in schema, f"{banned} still advertised in dashboard schema"
    for allowed in (
        "bar",
        "stacked_bar",
        "line",
        "area",
        "donut",
        "scatter",
        "bubble",
        "heatmap",
        "kpi",
    ):
        assert allowed in schema


def test_snowflake_rules_include_cookbook_and_ban_unsupported():
    rules = llm._DASHBOARD_RULES
    assert "Chart cookbook" in rules
    assert "Allowed chart_type values" in rules
    assert "heatmap" in rules
    for banned in scenarios.UNSUPPORTED_CHART_TYPES:
        assert f"Do NOT emit {banned}" in rules


def test_file_rules_include_cookbook():
    rules = llm._FILE_DASHBOARD_RULES
    assert "Chart cookbook" in rules
    assert "Allowed chart_type values" in rules
    assert "heatmap" in rules


def test_both_cookbooks_tell_the_planner_to_choose_from_the_data():
    """The planner picks the chart; the user is not expected to name one.

    Without this the planner answers a list of metrics literally and returns a
    wall of KPI tiles and trend lines, which is what it did before the decision
    procedure was written."""
    for name, rules in (("snowflake", llm._DASHBOARD_RULES),
                        ("file", llm._FILE_DASHBOARD_RULES)):
        assert "Decide from the SHAPE" in rules, (
            f"the {name} cookbook lost its data-shape decision procedure"
        )


def test_the_rules_do_not_ban_a_type_that_has_a_renderer():
    """The assertion this replaces read `"Do NOT emit funnel" in rules` — it
    pinned the bug in place.

    The cookbook forbade funnel, radar, treemap, combo and both maps long after
    each had a renderer, so the planner emitted none of them and every dashboard
    came back as KPI tiles and line charts. A test asserting the ban made that
    permanent. What matters is the opposite: a type we can draw must not be
    forbidden."""
    drawable = ("funnel", "radar", "treemap", "combo",
                "geo_us_states", "geo_countries")
    for name, rules in (("snowflake", llm._DASHBOARD_RULES),
                        ("file", llm._FILE_DASHBOARD_RULES)):
        for chart_type in drawable:
            assert f"Do NOT emit {chart_type}" not in rules, (
                f"the {name} cookbook bans {chart_type}, which renders fine"
            )


def test_scenarios_cover_core_domains_and_only_renderable_kinds():
    ids = {s["id"] for s in scenarios.SCENARIOS}
    assert {
        "ar_health",
        "occupancy_rollover",
        "tenant_credit_risk",
        "fund_lp_summary",
        "rent_roll_snapshot",
        "aging_matrix",
    } <= ids

    for s in scenarios.SCENARIOS:
        for kind in s["expect_chart_kinds"]:
            if kind == "table":
                continue
            assert kind in scenarios.RENDERABLE_CHART_TYPES, (
                f"scenario {s['id']} expects non-renderable kind {kind}"
            )

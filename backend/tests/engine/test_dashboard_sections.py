"""Tabs on an AI-built dashboard are kept worth clicking into.

Pins the two layouts testers reported: four tabs of two charts on a nine-panel
dashboard, and a sixth tab holding one chart after a run of edits.
"""

from __future__ import annotations

from app.engine.dashboard_sections import (
    MAX_TABS,
    MIN_PANELS_PER_TAB,
    SINGLE_TAB_BELOW,
    consolidate_sections,
    consolidation_note,
    settle_new_sections,
    settlement_note,
)


def chart(section, title=None):
    return {"title": title or f"{section} chart", "chart_type": "bar",
            "display": "chart", "section": section}


def kpi(section):
    return {"title": "Headline", "chart_type": "kpi", "display": "kpi", "section": section}


def tabs(panels):
    seen = []
    for p in panels:
        if p.get("section") and p["section"] not in seen:
            seen.append(p["section"])
    return seen


def per_tab(panels):
    counts = {}
    for p in panels:
        if p.get("chart_type") != "kpi":
            counts[p["section"]] = counts.get(p["section"], 0) + 1
    return counts


# --- new plans ---------------------------------------------------------------


def test_the_reported_freight_layout_becomes_one_tab():
    """Nine panels across four tabs of two or three: a reader saw two charts and
    concluded the dashboard had little in it."""
    plan = ([kpi("Overview")] + [chart("Overview")] * 3 + [chart("Service Quality")] * 2
            + [chart("Cost & D&D")] * 2 + [chart("Cost & Finance")] * 2)
    out = consolidate_sections(plan)
    assert tabs(out) == ["Overview"]
    assert len(out) == len(plan)


def test_a_large_plan_keeps_tabs_that_are_worth_it():
    plan = [chart("Sales")] * 6 + [chart("Customers")] * 7 + [chart("Operations")] * 8
    assert tabs(consolidate_sections(plan)) == ["Sales", "Customers", "Operations"]


def test_a_tab_short_of_the_minimum_is_folded_even_when_it_has_several():
    """Six per tab is the floor. Five charts is a real tab's worth of content,
    but it is under the floor, so it joins its neighbour."""
    plan = [chart("Sales")] * 8 + [chart("Customers")] * 5 + [chart("Operations")] * 7
    out = consolidate_sections(plan)
    assert "Customers" not in tabs(out)
    assert all(n >= MIN_PANELS_PER_TAB for n in per_tab(out).values())
    assert len(out) == len(plan)


def test_the_floor_is_six_per_tab():
    assert MIN_PANELS_PER_TAB == 6
    assert SINGLE_TAB_BELOW == 2 * MIN_PANELS_PER_TAB


def test_a_one_chart_tab_folds_into_the_tab_before_it():
    """The reported "Demand" tab: one chart on a tab of its own."""
    plan = ([chart("Sales")] * 6 + [chart("Customers")] * 6 + [chart("Pipeline")] * 6
            + [chart("Demand")])
    out = consolidate_sections(plan)
    assert "Demand" not in tabs(out)
    assert out[-1]["section"] == "Pipeline"
    assert all(n >= MIN_PANELS_PER_TAB for n in per_tab(out).values())


def test_a_small_first_tab_folds_forward():
    plan = [chart("Intro")] * 2 + [chart("Sales")] * 6 + [chart("Ops")] * 6
    out = consolidate_sections(plan)
    assert tabs(out) == ["Sales", "Ops"]
    assert out[0]["section"] == "Sales"


def test_never_more_than_the_tab_ceiling():
    # Every tab meets the floor, so only the ceiling can be what limits them.
    plan = [chart(name) for name in "ABCDEF" for _ in range(MIN_PANELS_PER_TAB)]
    out = consolidate_sections(plan)
    assert len(tabs(out)) <= MAX_TABS
    assert len(out) == len(plan)


def test_a_kpi_only_tab_does_not_survive():
    """KPIs render in a band above the grid; a tab of nothing but KPIs is an
    empty page under a row of tiles."""
    plan = [kpi("Headlines")] + [chart("Sales")] * 7 + [chart("Ops")] * 7
    out = consolidate_sections(plan)
    assert "Headlines" not in tabs(out)
    assert out[0]["section"] == "Sales"


def test_panel_order_and_content_are_untouched():
    plan = [chart("Sales", f"s{i}") for i in range(6)] + [chart("Ops", f"o{i}") for i in range(2)]
    out = consolidate_sections(plan)
    assert [p["title"] for p in out] == [p["title"] for p in plan]
    assert all(set(a) == set(b) for a, b in zip(out, plan))


def test_the_input_is_not_mutated():
    plan = [chart("A")] * 2 + [chart("B")] * 2
    before = [dict(p) for p in plan]
    consolidate_sections(plan)
    assert plan == before


def test_an_unsectioned_plan_is_left_alone():
    plan = [{"title": "x", "chart_type": "bar", "section": None}] * 3
    assert consolidate_sections(plan) == plan


def test_small_plans_put_unsectioned_panels_on_the_one_tab():
    plan = [chart("Overview"), {"title": "stray", "chart_type": "bar", "section": None},
            chart("Detail")]
    assert tabs(consolidate_sections(plan)) == ["Overview"]
    assert all(p["section"] == "Overview" for p in consolidate_sections(plan))


def test_the_thresholds_match_what_the_planner_is_told():
    """The prompt and this module state the same rule; if one moves, check the
    other."""
    from app.engine import llm

    for rules in (llm._DASHBOARD_RULES, llm._FILE_DASHBOARD_RULES):
        assert f"fewer than {SINGLE_TAB_BELOW}" in rules
        assert f"at least {MIN_PANELS_PER_TAB}" in rules
        assert f"at most {MAX_TABS}" in rules


def test_the_planner_aims_for_six_to_eight_without_padding():
    """The floor is enforced; the aim is only asked for. Asked without the
    second half, a model short of panels would invent filler to reach it."""
    from app.engine import llm

    for rules in (llm._DASHBOARD_RULES, llm._FILE_DASHBOARD_RULES):
        assert "6 to 8" in rules
        assert "fewer tabs rather than thinner" in rules
        assert "never add a panel only to fill" in rules


# --- edits -------------------------------------------------------------------


def test_an_edit_cannot_open_a_one_chart_tab():
    before = [chart("Sales")] * 5 + [chart("Pricing")] * 4
    after = before + [chart("Demand", "RFQs and Bookings by Trade Lane")]
    out = settle_new_sections(before, after)
    assert tabs(out) == ["Sales", "Pricing"]
    assert out[-1]["section"] == "Pricing"


def test_an_edit_may_open_a_tab_it_fills():
    before = [chart("Sales")] * 5 + [chart("Pricing")] * 4
    after = before + [chart("Demand", f"d{i}") for i in range(MIN_PANELS_PER_TAB)]
    assert tabs(settle_new_sections(before, after)) == ["Sales", "Pricing", "Demand"]


def test_an_edit_cannot_pass_the_tab_ceiling():
    before = [chart(name) for name in "ABCD" for _ in range(MIN_PANELS_PER_TAB)]
    # A full tab's worth, so it is the ceiling and not the floor that refuses it.
    after = before + [chart("E", f"e{i}") for i in range(MIN_PANELS_PER_TAB + 1)]
    assert len(tabs(settle_new_sections(before, after))) == MAX_TABS


def test_an_edit_never_touches_tabs_that_already_existed():
    """An edit promises to change nothing outside the request. The six-tab
    dashboard stays six tabs until someone asks for that to change."""
    before = [chart("A")] * 5 + [chart("B")] + [chart("C")]
    after = before + [chart("B", "new B panel")]
    assert settle_new_sections(before, after) == [dict(p) for p in after]


def test_renaming_a_small_tab_is_honoured():
    """The bug found in review: renaming a three-chart tab was mistaken for
    inventing a small new one, and the whole tab was folded into its neighbour."""
    before = [chart("Sales", f"s{i}") for i in range(5)] + [chart("Pricing", f"p{i}") for i in range(3)]
    after = ([chart("Sales", f"s{i}") for i in range(5)]
             + [chart("Commercial", f"p{i}") for i in range(3)])
    assert tabs(settle_new_sections(before, after)) == ["Sales", "Commercial"]


def test_a_panel_added_to_a_tab_renamed_in_the_same_edit_stays_with_it():
    before = [chart("Sales", f"s{i}") for i in range(5)] + [chart("Pricing", f"p{i}") for i in range(3)]
    after = ([chart("Sales", f"s{i}") for i in range(5)]
             + [chart("Commercial", f"p{i}") for i in range(3)]
             + [chart("Commercial", "new demand chart")])
    out = settle_new_sections(before, after)
    assert out[-1]["section"] == "Commercial"
    assert tabs(out) == ["Sales", "Commercial"]


def test_regrouping_existing_panels_is_the_request_itself():
    """Moving existing charts onto a tab is a person's explicit choice about
    panels they already have; the rule is only for panels the AI adds."""
    before = [chart("Sales", f"s{i}") for i in range(6)]
    after = [chart("Sales", f"s{i}") for i in range(4)] + [chart("Focus", "s4"), chart("Focus", "s5")]
    assert tabs(settle_new_sections(before, after)) == ["Sales", "Focus"]


def test_a_retitled_panel_still_counts_as_existing():
    """Matched on SQL when the title changed, so a rename of the chart itself
    does not make it look new."""
    before = [chart("Sales", f"s{i}") for i in range(5)] + [chart("Pricing", "p0")]
    retitled = {**chart("Pricing Detail", "p0 renamed"), "sql": before[-1].get("sql") or "SELECT 1"}
    before[-1]["sql"] = retitled["sql"]
    after = before[:-1] + [retitled]
    assert tabs(settle_new_sections(before, after)) == ["Sales", "Pricing Detail"]


def test_new_panels_at_the_top_join_the_tab_after_them():
    before = [chart("Sales")] * 5
    after = [chart("Intro", "new")] + before
    assert settle_new_sections(before, after)[0]["section"] == "Sales"


# --- the note the assistant receives -----------------------------------------
#
# Found in live testing: shown one tab after asking for three, the assistant
# decided the planner had ignored it and paid for the whole plan again.


def test_a_single_tab_result_is_explained():
    plan = [kpi("Overview")] + [chart("Overview")] * 3 + [chart("Customers")] * 3 + [chart("Operations")] * 2
    note = consolidation_note(plan, consolidate_sections(plan))
    assert note is not None
    assert f"fewer than {SINGLE_TAB_BELOW} charts" in note
    assert '"Overview"' in note and '"Customers"' in note
    assert "do not plan or edit again" in note


def test_a_merge_names_where_each_tab_went():
    plan = ([chart("Sales")] * 6 + [chart("Customers")] * 6 + [chart("Pipeline")] * 6
            + [chart("Demand")])
    note = consolidation_note(plan, consolidate_sections(plan))
    assert '"Demand" into "Pipeline"' in note


def test_no_note_when_the_plan_already_fits():
    plan = [chart("Sales")] * 6 + [chart("Operations")] * 6
    assert consolidation_note(plan, consolidate_sections(plan)) is None


def test_no_note_for_a_plan_that_was_one_tab_already():
    plan = [chart("Overview")] * 5
    assert consolidation_note(plan, consolidate_sections(plan)) is None


def test_an_edit_that_was_redirected_is_explained():
    before = [chart("Sales")] * 5 + [chart("Pricing")] * 4
    proposed = before + [chart("Demand", "RFQs and Bookings by Trade Lane")]
    note = settlement_note(proposed, settle_new_sections(before, proposed))
    assert note is not None
    assert '"Demand"' in note and '"Pricing"' in note
    assert "do not plan or edit again" in note


def test_no_note_for_an_edit_left_as_it_was():
    before = [chart("Sales", f"s{i}") for i in range(5)] + [chart("Pricing", f"p{i}") for i in range(3)]
    renamed = [chart("Sales", f"s{i}") for i in range(5)] + [chart("Commercial", f"p{i}") for i in range(3)]
    assert settlement_note(renamed, settle_new_sections(before, renamed)) is None

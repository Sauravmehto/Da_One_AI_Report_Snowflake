"""The chart types are declared in several places; they must agree.

  - app/engine/llm.py  _DASHBOARD_SCHEMA   what the planner is told it may emit
  - app/engine/llm.py  the chart cookbooks  what the planner is told to emit
  - app/agent/tools.py                      the chat agent's chart tool enum
  - app/engine/dashboard_scenarios.py       what scenario checks call renderable
  - the React renderer                      what can actually be drawn
  - the React dashboard editor              what a person can pick by hand

There were SEVEN such lists by the time they were all found, and I found only
three of them on a first pass — the scenarios file quietly listed `funnel` as
unsupported while a renderer for it was being added. Hence the sweep below
rather than a spot check.

A type in a prompt with no renderer behind it does not fail — the panel
silently falls back to a table. That is exactly how a heatmap panel lost its
chart on another branch and nobody noticed until a PDF looked wrong. These
tests make the drift loud instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend" / "src"


def planner_types() -> set[str]:
    text = (BACKEND / "app" / "engine" / "llm.py").read_text(encoding="utf-8")
    match = re.search(r'"chart_type": "([a-z_|]+)', text)
    assert match, "could not find the planner's chart_type vocabulary"
    return {t for t in match.group(1).split("|") if t}


def agent_types() -> set[str]:
    text = (BACKEND / "app" / "agent" / "tools.py").read_text(encoding="utf-8")
    block = re.search(r'"chart_type":\s*\{.*?"enum":\s*\[(.*?)\]', text, re.S)
    assert block, "could not find the agent tool's chart_type enum"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def cookbook_types(attr: str = "_DASHBOARD_RULES") -> set[str]:
    """Types the chart cookbook tells the planner it may emit.

    A SIXTH list, and the one that actually governed behaviour: the cookbook
    read "Allowed chart_type values: ... Do NOT emit funnel, histogram, or any
    other type" long after funnel, radar, treemap, combo and both maps had
    renderers. The schema offered sixteen types and the rules below it forbade
    six of them, so the planner never emitted one — dashboards came back as
    walls of KPI tiles and line charts and nothing failed.
    """
    from app.engine import llm

    text = getattr(llm, attr)
    match = re.search(r"Allowed chart_type values:(.*?)\.\n", text, re.S)
    assert match, f"could not find the cookbook's allowed list in {attr}"
    return {t.strip() for t in match.group(1).replace("\n", " ").split(",") if t.strip()}


def renderer_types() -> set[str]:
    """Types the panel dispatch actually has a branch for."""
    text = (FRONTEND / "components" / "dashboard" / "dashboard-panel.tsx").read_text(
        encoding="utf-8"
    )
    dispatch = text.split("const chart =", 1)
    assert len(dispatch) == 2, "could not find the chart dispatch"
    return set(re.findall(r'type === "([a-z_]+)"', dispatch[1]))


def test_every_type_the_planner_emits_can_be_drawn():
    missing = planner_types() - renderer_types() - {"kpi"}
    assert not missing, (
        f"the planner may emit {sorted(missing)}, which the renderer cannot draw "
        "— those panels will silently fall back to a table"
    )


def test_every_type_the_agent_emits_can_be_drawn():
    missing = agent_types() - renderer_types()
    assert not missing, f"the chart tool may emit {sorted(missing)}, undrawable"


def test_the_two_prompts_offer_the_same_charts():
    """Beyond kpi, which is a dashboard tile rather than a chart, the chat and
    dashboard surfaces should not disagree about what exists."""
    assert planner_types() - {"kpi"} == agent_types()


@pytest.mark.parametrize("chart_type", ["bar", "line", "pie", "heatmap",
                                        "geo_us_states", "geo_countries"])
def test_the_headline_types_are_present_everywhere(chart_type):
    assert chart_type in planner_types()
    assert chart_type in agent_types()
    assert chart_type in renderer_types()


def test_the_map_is_wired_to_a_real_component():
    """A dispatch branch that renders nothing is the same failure as a missing
    branch, just harder to spot."""
    text = (FRONTEND / "components" / "dashboard" / "dashboard-panel.tsx").read_text(
        encoding="utf-8"
    )
    assert "GeoPanel" in text
    assert (FRONTEND / "components" / "dashboard" / "geo-panel.tsx").is_file()


def test_both_map_types_reach_the_same_component():
    """`GeoPanel` decides US-or-world from the data, so the two map types must
    both route to it. A `geo_countries` panel sent to a US-only renderer would
    draw an empty United States and report every country as unmatched."""
    text = (FRONTEND / "components" / "dashboard" / "dashboard-panel.tsx").read_text(
        encoding="utf-8"
    )
    dispatch = text.split("const chart =", 1)[1]
    branch = re.search(
        r'type === "geo_us_states" \|\| type === "geo_countries" \? \(\s*<GeoPanel',
        dispatch,
    )
    assert branch, "the two geo types no longer share one renderer"


def test_the_scenario_list_matches_what_the_planner_offers():
    """dashboard_scenarios keeps its own idea of what is renderable, and it is
    what the scenario checks assert against."""
    from app.engine import dashboard_scenarios as scenarios

    assert scenarios.RENDERABLE_CHART_TYPES == planner_types(), (
        "dashboard_scenarios.RENDERABLE_CHART_TYPES has drifted from the "
        "planner's own vocabulary"
    )


def test_nothing_is_both_offered_and_declared_unsupported():
    """The contradiction that broke this: `funnel` advertised to the planner
    while still listed as unsupported."""
    from app.engine import dashboard_scenarios as scenarios

    contradiction = scenarios.UNSUPPORTED_CHART_TYPES & planner_types()
    assert not contradiction, (
        f"{sorted(contradiction)} is offered to the planner and simultaneously "
        "declared unsupported"
    )


def test_an_unsupported_type_has_no_renderer_either():
    """Otherwise 'unsupported' is just an out-of-date note."""
    from app.engine import dashboard_scenarios as scenarios

    drawable = scenarios.UNSUPPORTED_CHART_TYPES & renderer_types()
    assert not drawable, f"{sorted(drawable)} is drawable but marked unsupported"


def test_the_cookbook_offers_every_type_the_schema_does():
    """The planner sees the schema and the cookbook in ONE system prompt. When
    they disagree the cookbook wins, because it is the emphatic one — so a type
    missing here is a type the planner will never emit, however prominently the
    schema lists it."""
    missing = planner_types() - {"kpi"} - cookbook_types()
    assert not missing, (
        f"the schema offers {sorted(missing)} but the chart cookbook does not "
        "list them — the planner will never emit them"
    )


def test_the_cookbook_offers_nothing_the_renderer_cannot_draw():
    undrawable = cookbook_types() - renderer_types() - {"kpi"}
    assert not undrawable, (
        f"the cookbook invites {sorted(undrawable)}, which degrade to tables"
    )


def test_the_file_dashboard_cookbook_agrees_with_the_warehouse_one():
    """Uploaded-file dashboards render through the SAME React component, so a
    type good enough for one surface is good enough for the other."""
    assert cookbook_types("_FILE_DASHBOARD_RULES") == cookbook_types()


def test_the_cookbook_does_not_ban_a_type_it_also_offers():
    """The exact contradiction that caused this: `funnel` listed as allowed in
    one line and banned two lines later."""
    from app.engine import llm

    for attr in ("_DASHBOARD_RULES", "_FILE_DASHBOARD_RULES"):
        text = getattr(llm, attr)
        for banned in re.findall(r"Do NOT emit ([a-z_, ]+)", text):
            named = {t.strip() for t in banned.split(",") if t.strip()}
            overlap = named & cookbook_types(attr)
            assert not overlap, f"{attr} both offers and bans {sorted(overlap)}"


def editor_types() -> set[str]:
    """Types a person can pick in the dashboard editor's Chart type field.

    A SEVENTH list. It went stale the same way the cookbook did, and with a
    nastier symptom: the select is bound to `panel.chart_type`, so a panel whose
    type is missing from the list renders as though "bar" were chosen, and one
    click converts a map into a bar chart with nothing to undo it.
    """
    text = (FRONTEND / "components" / "dashboard" / "dashboard-editor.tsx").read_text(
        encoding="utf-8"
    )
    block = re.search(r"const CHART_TYPES = \[(.*?)\]", text, re.S)
    assert block, "could not find CHART_TYPES in the dashboard editor"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


#: Spellings the dispatch accepts for a type it already draws under another
#: name. Offering both in a dropdown would ask a person to choose between two
#: words for one chart, so the editor carries the canonical one only.
CHART_TYPE_ALIASES = {"doughnut"}


def test_the_editor_can_reach_every_type_the_renderer_draws():
    """Otherwise a planned chart cannot be changed, or even safely looked at."""
    missing = renderer_types() - editor_types() - CHART_TYPE_ALIASES
    assert not missing, (
        f"the editor's Chart type field is missing {sorted(missing)} — panels of "
        "those types show the wrong selection and one click destroys them"
    )


# --- a new type must also survive being SAVED --------------------------------
#
# The seven lists above govern what can be offered and drawn. None of them
# covers the save path, where a panel is rebuilt from _PANEL_CONFIG_FIELDS and
# its `display` reconciled against its chart_type — so a type nothing rejects
# can still come back from a round trip as a table.

def test_a_planned_chart_type_survives_the_save_path():
    from app.engine.dashboards import _normalise_display, _panel_config

    for chart_type in sorted(planner_types() - {"kpi"}):
        panel = {
            "title": "P",
            "chart_type": chart_type,
            "x": "MONTH",
            "y": ["VALUE", "TARGET"],
            "sql": "SELECT 1",
        }
        _normalise_display(panel)
        config = _panel_config(panel)
        assert config["chart_type"] == chart_type, (
            f"{chart_type} was rewritten on save"
        )
        assert config.get("display") != "table", (
            f"{chart_type} is downgraded to a table on save, so a planned panel "
            "comes back without its chart"
        )

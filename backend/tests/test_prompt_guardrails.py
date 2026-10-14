"""Instructions added because testers watched the model get them wrong.

Prompt text cannot be unit-tested for effect, only for presence. These pin that
each instruction is still in the prompt that needs it, so a later rewrite cannot
drop one without a test noticing.
"""

from __future__ import annotations

import os

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

import pytest

from app.agent import prompts, tools
from app.engine import llm


def _text(system) -> str:
    return system if isinstance(system, str) else " ".join(b["text"] for b in system)


# --- chart choice ----------------------------------------------------------


@pytest.mark.parametrize("rules", [llm._DASHBOARD_RULES, llm._FILE_DASHBOARD_RULES],
                         ids=["warehouse", "file"])
def test_both_planners_know_two_rates_of_different_size_need_two_scales(rules):
    """A freight dashboard drew OTIF (~90%) and exception rate (~1%) on one axis:
    two flat lines. "Different scales" had been read as "different units"."""
    assert "different scales" in rules
    assert "90%" in rules and "1%" in rules


# --- the assistant's behaviour around dashboards ------------------------------


@pytest.fixture(scope="module")
def main_chat():
    return _text(prompts.build_system_prompt("PRIVATECREDIT", ""))


def test_never_adds_a_test_panel_to_a_real_dashboard(main_chat):
    """It appended a "Shipment Count Trend" panel to a user's saved dashboard to
    check whether the editor worked, creating a real version."""
    assert "Never add a test or probe panel" in main_chat


def test_does_not_repeat_a_failed_call(main_chat):
    """It sent the same failing edit six times, each at full output cost."""
    assert "do not send the same call again" in main_chat


def test_knows_one_edit_can_carry_many_panels(main_chat):
    """It split seven panels into one-at-a-time edits, multiplying the cost."""
    assert "is a single edit" in main_chat
    edit = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "edit_dashboard")
    assert "One call can carry many changes" in edit["description"]


def test_knows_a_layout_note_is_not_a_failure(main_chat):
    """Without this it re-planned a whole dashboard because it got one tab."""
    assert "layout_note" in main_chat
    assert "never plan or edit again" in main_chat

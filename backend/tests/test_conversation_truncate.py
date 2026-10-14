"""Tool-result truncation: keeps history and context bounded without ever
silently presenting a partial result as complete."""

import json

from app.conversation.truncate import (
    MAX_ROWS_KEPT,
    MAX_TOOL_RESULT_CHARS,
    truncate_tool_result,
)


def test_small_result_passes_through_untouched():
    payload = {"columns": ["A"], "rows": [[1], [2]], "row_count": 2}
    content, truncated = truncate_tool_result(payload)
    assert truncated is False
    assert json.loads(content) == payload


def test_large_row_set_is_trimmed_to_whole_rows():
    payload = {"columns": ["A"], "rows": [[i] for i in range(500)], "row_count": 500}
    content, truncated = truncate_tool_result(payload)
    assert truncated is True

    out = json.loads(content)
    assert len(out["rows"]) == MAX_ROWS_KEPT
    # Whole rows only — a half-serialised row would read as real data.
    assert out["rows"][0] == [0]
    assert out["rows"][-1] == [MAX_ROWS_KEPT - 1]


def test_truncation_is_announced_not_silent():
    payload = {"columns": ["A"], "rows": [[i] for i in range(500)], "row_count": 500}
    content, _ = truncate_tool_result(payload)
    out = json.loads(content)
    assert "_truncated" in out
    assert "500" in out["_truncated"]  # the real total is stated


def test_dashboard_panels_are_trimmed_per_panel():
    payload = {
        "report_id": "r1",
        "panels": [
            {"title": "P1", "columns": ["A"], "rows": [[i] for i in range(300)]},
            {"title": "P2", "columns": ["B"], "rows": [[1], [2]]},
        ],
    }
    content, truncated = truncate_tool_result(payload)
    assert truncated is True

    out = json.loads(content)
    assert len(out["panels"][0]["rows"]) == MAX_ROWS_KEPT
    assert "_truncated" in out["panels"][0]
    # A small panel is left alone.
    assert len(out["panels"][1]["rows"]) == 2
    assert "_truncated" not in out["panels"][1]


def test_unknown_shape_still_bounded_by_character_cap():
    """Defence in depth: a tool whose result shape we do not recognise must
    still not be able to blow the context budget."""
    payload = {"blob": "x" * (MAX_TOOL_RESULT_CHARS * 3)}
    content, truncated = truncate_tool_result(payload)
    assert truncated is True
    assert len(content) < MAX_TOOL_RESULT_CHARS + 200
    assert "truncated" in content


def test_plain_string_result_is_supported():
    content, truncated = truncate_tool_result("a short answer")
    assert truncated is False
    assert content == "a short answer"


def test_long_string_result_is_cut():
    content, truncated = truncate_tool_result("y" * (MAX_TOOL_RESULT_CHARS + 500))
    assert truncated is True
    assert "truncated" in content


# --- dashboard plans and edit previews ------------------------------------------
#
# Found in live testing: a 20-panel plan still did not fit after its SQL was
# removed, so the character cut took the end of the payload -- panel_count,
# skipped_panels and the layout note. The assistant could not tell whether any
# panel had been dropped and paid for the whole plan again to find out.


def _panel(i, section="Performance"):
    """Shaped like a stored panel: every field present, most of them empty."""
    return {
        "title": f"Panel {i} Monthly Contribution Margin vs Target",
        "sql": "SELECT DATE_TRUNC('month', WEEK_START) AS MONTH_START, " + "SUM(X) AS Y, " * 40 + "1",
        "chart_type": "combo", "x": "MONTH_START", "y": ["GMV_AED", "CM_PCT"],
        "color": None, "size": None, "orientation": None, "display": "chart",
        "section": section, "span": 2, "row_span": 2, "palette": None,
        "column_formats": {"GMV_AED": {"format": "currency", "currency": "AED"}},
        "zero_baseline": None,
    }


def _plan_result(n, **extra):
    return {
        "plan_id": "p1",
        "plan": {"title": "Big", "panels": [_panel(i) for i in range(n)], "parameters": []},
        "database": "ECOMMERCE_DB",
        "panel_count": n,
        "parameters": [],
        "skipped_panels": [{"title": "Panel X", "reason": "compile error"}],
        **extra,
    }


def _read(result):
    content, truncated = truncate_tool_result(result)
    assert len(content) <= MAX_TOOL_RESULT_CHARS, "a plan must fit without the blunt cut"
    assert "…[truncated:" not in content
    return json.loads(content), truncated


def test_a_twenty_panel_plan_keeps_every_fact_the_assistant_acts_on():
    out, truncated = _read(_plan_result(20, layout_note="Layout rule applied: one tab."))
    assert truncated
    assert out["panel_count"] == 20
    assert out["skipped_panels"] == [{"title": "Panel X", "reason": "compile error"}]
    assert out["layout_note"].startswith("Layout rule applied")
    assert out["plan_id"] == "p1"


def test_every_panel_is_still_named():
    out, _ = _read(_plan_result(20))
    names = [p if isinstance(p, str) else p["title"] for p in out["plan"]["panels"]]
    assert len(names) == 20 and all(names)


def test_the_facts_come_before_the_plan():
    """If anything ever does get cut, it is the end -- so the plan goes there."""
    content, _ = truncate_tool_result(_plan_result(20, layout_note="note"))
    assert content.index('"skipped_panels"') < content.index('"plan"')
    assert content.index('"layout_note"') < content.index('"plan"')


def test_the_notice_says_what_was_shortened():
    out, _ = _read(_plan_result(20))
    assert "held server-side" in out["_truncated"]
    assert "skipped_panels" in out["_truncated"]


def test_a_plan_that_fits_is_left_whole():
    result = _plan_result(1)
    result["plan"]["panels"][0]["sql"] = "SELECT 1"
    content, truncated = truncate_tool_result(result)
    assert not truncated
    assert json.loads(content) == result


def test_an_enormous_plan_still_returns_its_facts():
    out, _ = _read(_plan_result(400, layout_note="note"))
    assert out["panel_count"] == 400
    assert out["skipped_panels"]
    assert out["plan"]["panels_omitted"] == 400


def test_an_edit_preview_keeps_its_review_instructions():
    """The preview used to put a full-SQL proposed plan before its message and
    layout note, so on a large dashboard the instructions were what got cut."""
    preview = {
        "status": "preview", "report_id": "r1", "report_title": "Freight",
        "current_version": 3, "new_version": 4, "edit_note": "add two panels",
        "diff": {"added": ["Panel A", "Panel B"], "removed": [], "changed": []},
        "parameters": [],
        "proposed_plan": {"title": "Freight", "panels": [_panel(i) for i in range(12)]},
        "layout_note": "Layout rule applied: placed on Sales.",
        "message": "Review this diff against the requested change, then confirm.",
    }
    out, truncated = _read(preview)
    assert truncated
    assert out["message"].startswith("Review this diff")
    assert out["layout_note"].startswith("Layout rule applied")
    assert out["diff"]["added"] == ["Panel A", "Panel B"]
    assert out["new_version"] == 4



def test_a_huge_diff_cannot_push_out_the_review_instructions():
    """An edit that rewrites many panels' SQL produces a diff too large to fit on
    its own. The instructions and the layout note must come before it, so the
    unavoidable cut lands on the diff's tail rather than on what to do next."""
    big_change = {"field": "sql", "before": "SELECT " + "A, " * 400, "after": "SELECT " + "B, " * 400}
    preview = {
        "status": "preview", "report_id": "r1", "new_version": 4,
        "diff": {"changed": [{"title": f"Panel {i}", **big_change} for i in range(10)]},
        "proposed_plan": {"title": "Freight", "panels": [_panel(i) for i in range(12)]},
        "layout_note": "Layout rule applied: placed on Sales.",
        "message": "Review this diff against the requested change, then confirm.",
    }
    content, _ = truncate_tool_result(preview)
    assert "Review this diff" in content
    assert "Layout rule applied" in content
    assert content.index("Review this diff") < content.index('"diff"')

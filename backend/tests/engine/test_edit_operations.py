"""A dashboard edit returns the change, not the dashboard.

The editor used to have the model re-write every panel with the change applied.
Output grew with the dashboard rather than the request, so once a freight
dashboard reached ten panels of heavy SQL, adding a single panel no longer fit
and every attempt failed at the same point. The model now names operations
against panel indexes and the server applies them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine import llm


def panel(i, **kw):
    base = {"title": f"Panel {i}", "sql": f"SELECT {i} AS V", "chart_type": "bar",
            "x": "K", "y": "V", "color": None, "palette": None, "size": None,
            "display": "chart", "section": "Overview", "span": 2, "row_span": 2}
    base.update(kw)
    return base


def plan(n, **kw):
    return {"title": "Freight", "panels": [panel(i) for i in range(n)],
            "parameters": [{"name": "mode", "type": "enum", "default": "All"}], **kw}


def apply(current, reply):
    return llm._edited_plan(current, reply, "Dashboard editor")


def titles(result):
    return [p["title"] for p in result.panels]


# --- what each operation does -------------------------------------------------


def test_update_changes_only_the_named_fields():
    current = plan(3)
    result = apply(current, {"operations": [
        {"op": "update", "index": 1, "fields": {"chart_type": "line"}}]})
    assert result.panels[1] == {**current["panels"][1], "chart_type": "line"}
    assert result.panels[0] == current["panels"][0]
    assert result.panels[2] == current["panels"][2]


def test_untouched_panels_come_back_exactly_as_stored():
    """The guarantee the old prompt could only ask for, now structural: a panel
    the operations do not name never passes through the model."""
    current = plan(12)
    result = apply(current, {"operations": [
        {"op": "update", "index": 5, "fields": {"title": "Renamed"}}]})
    for i in (0, 4, 6, 11):
        assert result.panels[i] == current["panels"][i]


def test_add_after_an_index_at_the_start_and_at_the_end():
    result = apply(plan(3), {"operations": [
        {"op": "add", "after": 0, "panel": panel(90, title="after zero")},
        {"op": "add", "after": -1, "panel": panel(91, title="first")},
        {"op": "add", "panel": panel(92, title="last")},
    ]})
    assert titles(result) == ["first", "Panel 0", "after zero", "Panel 1", "Panel 2", "last"]


def test_several_adds_after_one_panel_keep_the_order_given():
    result = apply(plan(2), {"operations": [
        {"op": "add", "after": 0, "panel": panel(90, title="a")},
        {"op": "add", "after": 0, "panel": panel(91, title="b")},
    ]})
    assert titles(result) == ["Panel 0", "a", "b", "Panel 1"]


def test_remove_and_move():
    result = apply(plan(4), {"operations": [
        {"op": "remove", "index": 1},
        {"op": "move", "index": 3, "after": -1},
    ]})
    assert titles(result) == ["Panel 3", "Panel 0", "Panel 2"]


def test_indexes_mean_the_original_positions_whatever_the_order():
    """A removal early in the list must not shift what later operations mean."""
    result = apply(plan(4), {"operations": [
        {"op": "remove", "index": 0},
        {"op": "update", "index": 3, "fields": {"title": "still the fourth"}},
        {"op": "add", "after": 2, "panel": panel(90, title="after the third")},
    ]})
    assert titles(result) == ["Panel 1", "Panel 2", "after the third", "still the fourth"]


def test_an_added_panel_gets_the_quote_repair_every_panel_gets():
    result = apply(plan(1), {"operations": [
        {"op": "add", "panel": panel(9, sql="SELECT * FROM T WHERE MODE = ''Air''")}]})
    assert result.panels[-1]["sql"] == "SELECT * FROM T WHERE MODE = 'Air'"


def test_an_index_the_model_echoes_is_not_stored():
    result = apply(plan(1), {"operations": [
        {"op": "add", "panel": {**panel(9), "index": 7}},
        {"op": "update", "index": 0, "fields": {"index": 3, "title": "t"}},
    ]})
    assert all("index" not in p for p in result.panels)


# --- what the rest of the plan does -------------------------------------------


def test_parameters_and_title_survive_when_not_mentioned():
    current = plan(2)
    result = apply(current, {"operations": []})
    assert result.parameters == current["parameters"]
    assert result.title == "Freight"
    assert result.panels == current["panels"]


def test_a_clarifying_question_returns_no_panels():
    """Callers treat needs_clarification before anything else; no panels keeps a
    half-understood edit from being staged."""
    result = apply(plan(2), {"operations": [], "needs_clarification": "Which lane?"})
    assert result.needs_clarification == "Which lane?"
    assert result.panels == []


def test_a_whole_plan_reply_is_still_accepted():
    """A model that ignores the format and returns a dashboard gets the old
    behaviour — the caller diffs it against the saved version either way."""
    reply = {"panels": [panel(0, title="whole plan")]}
    assert titles(apply(plan(5), reply)) == ["whole plan"]


# --- bad operations fail loudly, never silently ----------------------------------


@pytest.mark.parametrize("op, fragment", [
    ({"op": "update", "index": 9, "fields": {}}, "numbered 0 to 2"),
    ({"op": "remove", "index": -1}, "numbered 0 to 2"),
    ({"op": "update", "index": "2", "fields": {}}, "not a panel index"),
    ({"op": "update", "index": True, "fields": {}}, "not a panel index"),
    ({"op": "update", "index": 0}, "no fields"),
    ({"op": "add"}, "no panel"),
    ({"op": "rename", "index": 0}, "unknown operation"),
    ("add a panel", "not an object"),
])
def test_an_invalid_operation_is_refused(op, fragment):
    with pytest.raises(llm.LLMError) as err:
        apply(plan(3), {"operations": [op]})
    assert fragment in str(err.value)


# --- end to end through the editor call ---------------------------------------


@pytest.fixture
def fake_model(monkeypatch):
    state = {"reply": {}, "requests": []}

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Messages:
        def create(self, **kw):
            state["requests"].append(kw)
            return type("Resp", (), {"content": [_Block(json.dumps(state["reply"]))],
                                     "stop_reason": "end_turn"})()

        def stream(self, **kw):
            # Plan calls stream; the final message is the same response.
            response = self.create(**kw)
            return type("Stream", (), {
                "__enter__": lambda s: s,
                "__exit__": lambda s, *exc: False,
                "get_final_message": lambda s: response,
            })()

    monkeypatch.setattr(llm, "_get_client",
                        lambda: type("Client", (), {"messages": _Messages()})())
    return state


@pytest.mark.parametrize("editor", ["warehouse", "file"])
def test_adding_one_panel_to_a_large_dashboard(fake_model, editor):
    """The reported failure: one more panel on a ten-panel freight dashboard.
    The reply is one operation, however large the dashboard already is."""
    current = plan(20)
    new = panel(20, title="DSO vs Invoice Accuracy")
    fake_model["reply"] = {"operations": [{"op": "add", "panel": new}]}

    call = (llm.edit_dashboard_plan if editor == "warehouse"
            else llm.edit_file_dashboard_plan)
    result = call(current, "add DSO vs Invoice Accuracy", "ctx")

    assert len(result.panels) == 21
    assert result.panels[:20] == current["panels"]
    assert result.panels[20]["title"] == "DSO vs Invoice Accuracy"


def test_the_editor_is_shown_numbered_panels_without_empty_fields(fake_model):
    fake_model["reply"] = {"operations": []}
    llm.edit_dashboard_plan(plan(2), "nothing", "ctx")

    request = fake_model["requests"][-1]
    user = request["messages"][0]["content"]
    shown = json.loads(user.split("\n", 1)[1].split("\n\nREQUESTED CHANGE")[0])
    assert [p["index"] for p in shown["panels"]] == [0, 1]
    assert "palette" not in shown["panels"][0]
    assert "EDIT RESPONSE FORMAT" in request["system"][0]["text"]

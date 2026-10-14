"""Agent tool surface: schema integrity and the dispatch contract.

The contract that matters: dispatch NEVER raises. A tool failure has to come
back as {"error": ...} so the model reads the reason and recovers on the next
turn — an exception would abort the loop and strand the user mid-answer.
"""

import pytest

from app.agent import tools
from app.agent.tools import TOOL_DEFINITIONS, TOOL_NAMES, ToolContext, dispatch

CTX = ToolContext(database="PRIVATECREDIT", snowflake_user="SVC_USER", role="R")


def test_every_schema_has_an_implementation_and_vice_versa():
    assert set(TOOL_NAMES) == set(tools._IMPLEMENTATIONS)


def test_schemas_are_well_formed():
    for t in TOOL_DEFINITIONS:
        assert t["name"] and t["description"], t
        assert t["input_schema"]["type"] == "object"
        # Every declared required field must actually be a declared property.
        props = t["input_schema"].get("properties", {})
        for field in t["input_schema"].get("required", []):
            assert field in props, f"{t['name']}: required '{field}' not in properties"


def test_tool_names_are_unique():
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))


async def test_unknown_tool_returns_error_not_raise():
    out = await dispatch("no_such_tool", {}, CTX)
    assert "error" in out
    # The model is told what it *could* have called, so it can retry sensibly.
    assert "run_safe_sql" in out["error"]


async def test_bad_arguments_return_error_not_raise():
    # explain_concept requires `term`; omitting it must not blow up the loop.
    out = await dispatch("explain_concept", {}, CTX)
    assert "error" in out
    assert "Invalid arguments" in out["error"]


async def test_unexpected_argument_returns_error_not_raise():
    out = await dispatch("explain_concept", {"term": "DSCR", "bogus": 1}, CTX)
    assert "error" in out


async def test_exception_inside_a_tool_becomes_an_error_result(monkeypatch):
    def boom(ctx, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setitem(tools._IMPLEMENTATIONS, "list_tables", boom)
    out = await dispatch("list_tables", {}, CTX)
    assert "error" in out
    assert "engine exploded" in out["error"]


async def test_glossary_lookup_runs_for_real():
    """explain_concept touches no database, so it exercises the whole dispatch
    path end to end without Snowflake."""
    out = await dispatch("explain_concept", {"term": "DSCR"}, CTX)
    assert out.get("found") is True
    assert "DSCR" in out.get("term", "")


async def test_glossary_miss_does_not_fabricate():
    out = await dispatch(
        "explain_concept", {"term": "zzz not a real financial term"}, CTX
    )
    assert out.get("found") is False
    assert "definition" not in out or not out.get("definition")


async def test_get_database_context_returns_semantic_layer():
    out = await dispatch("get_database_context", {}, CTX)
    assert out["database"] == "PRIVATECREDIT"
    assert "PC_MARKS" in out["context"]


def test_descriptions_stay_within_a_sane_budget():
    """Descriptions are resent on every turn; runaway growth is a silent cost
    and hurts selection accuracy. Fail loudly if the surface balloons."""
    total = sum(len(t["description"]) for t in TOOL_DEFINITIONS)
    assert total < 12000, f"tool descriptions total {total} chars"


async def test_plan_dashboard_routes_to_file_engine_for_uploads(monkeypatch):
    from app.engine import file_dashboards

    called = {}

    def fake_create(description, source_key, context, connector):
        called["source_key"] = source_key
        return {"plan_id": "p1", "source_type": "file", "panel_count": 1}

    class FakeConnector:
        def dry_run_query(self, sql):
            return []

    monkeypatch.setattr(file_dashboards, "create_plan", fake_create)
    monkeypatch.setattr(tools, "_collection_context", lambda collection: "folder ctx")
    monkeypatch.setattr(
        tools.ToolContext,
        "connector",
        property(lambda self: FakeConnector()),
    )
    ctx = tools.ToolContext(
        database="FOLDER:abc",
        collection=type("C", (), {"collection_id": "abc"})(),
    )
    out = await dispatch("plan_dashboard", {"description": "loan amounts"}, ctx)
    assert called["source_key"] == "FOLDER:abc"
    assert out["source_type"] == "file"


async def test_edit_dashboard_routes_to_file_engine_for_uploads(monkeypatch):
    from app.engine import file_dashboards

    preview_called = {}

    def fake_preview(report_id_or_title, changes, connector, context):
        preview_called["report_id"] = report_id_or_title
        return {"status": "preview", "source_type": "file"}

    class FakeConnector:
        def dry_run_query(self, sql):
            return []

    monkeypatch.setattr(file_dashboards, "resolve_report_ids", lambda q: [
        {"report_id": "rid1", "report_title": "My dash"}
    ])
    monkeypatch.setattr(file_dashboards, "read_record", lambda rid: {
        "report_id": "rid1", "source_key": "FOLDER:abc"
    })
    monkeypatch.setattr(file_dashboards, "preview_edit", fake_preview)
    monkeypatch.setattr(tools, "_collection_context", lambda collection: "folder ctx")
    monkeypatch.setattr(
        tools.ToolContext,
        "connector",
        property(lambda self: FakeConnector()),
    )
    ctx = tools.ToolContext(
        database="FOLDER:abc",
        collection=type("C", (), {"collection_id": "abc"})(),
    )
    out = await dispatch(
        "edit_dashboard",
        {"report_id_or_title": "rid1", "changes_description": "add a panel"},
        ctx,
    )
    assert preview_called["report_id"] == "rid1"
    assert out["status"] == "preview"


# --- error kinds: whether a retry can help ----------------------------------
#
# Taken from a real session: the assistant retried a closed connection three
# times and blamed its own wording for a quoting bug, because every failure
# arrived as the same bare {"error": ...}.

_CLOSED = ("No panels could be validated. 'Headline Financial KPIs': SQL failed to "
           "compile: 250002 (08003): Connection is closed; 'Revenue by Month': SQL "
           "failed to compile: 250002 (08003): Connection is closed")


@pytest.mark.parametrize("message, kind, retryable", [
    (_CLOSED, "connection_lost", False),
    ("Query failed: 250002 (08003): Connection is closed", "connection_lost", False),
    ("Planning failed: Dashboard planner ran out of room: the plan needed more than "
     "32000 tokens", "too_large", False),
    ("No panels could be validated. 'KPIs': SQL rejected by safety layer: Could not "
     "parse SQL: Expecting ).", "sql_invalid", True),
    ("Query failed: 000904 (42000): SQL compilation error: invalid identifier 'X'",
     "sql_invalid", True),
])
def test_known_failures_say_whether_a_retry_can_help(message, kind, retryable):
    label = tools.classify_error(message)
    assert label["error_kind"] == kind
    assert label["retryable"] is retryable
    assert label["remedy"]


def test_a_closed_connection_wins_over_the_sql_errors_it_causes():
    """Every panel's reason reads as a compile failure when the connection is
    gone. Labelled sql_invalid, the assistant would rewrite SQL that was fine."""
    assert tools.classify_error(_CLOSED)["error_kind"] == "connection_lost"


def test_an_unrecognised_failure_is_left_unlabelled():
    assert tools.classify_error("Unknown tool 'x'") is None


async def test_dispatch_labels_a_failed_result(monkeypatch):
    monkeypatch.setitem(tools._IMPLEMENTATIONS, "list_tables",
                        lambda ctx, **k: {"error": _CLOSED})
    out = await dispatch("list_tables", {}, CTX)
    assert out["error"] == _CLOSED, "the original message must survive"
    assert out["error_kind"] == "connection_lost" and out["retryable"] is False


async def test_dispatch_labels_an_exception_too(monkeypatch):
    def boom(ctx, **kwargs):
        raise RuntimeError("250002 (08003): Connection is closed")

    monkeypatch.setitem(tools._IMPLEMENTATIONS, "list_tables", boom)
    out = await dispatch("list_tables", {}, CTX)
    assert out["error_kind"] == "connection_lost"


async def test_a_successful_result_is_untouched(monkeypatch):
    monkeypatch.setitem(tools._IMPLEMENTATIONS, "list_tables",
                        lambda ctx, **k: {"tables": ["A"]})
    assert await dispatch("list_tables", {}, CTX) == {"tables": ["A"]}


def test_the_model_is_told_what_retryable_false_means():
    from app.agent import prompts

    assert '"retryable": false' in prompts._CORE
    assert '"remedy"' in prompts._CORE

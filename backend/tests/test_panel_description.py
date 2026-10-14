"""The info button's endpoint: what it sends, and what it only sends once.

Two promises worth holding onto. It never sends DATA to the model — only the
chart's names — which is what keeps this feature outside the row-sharing we
describe to clients. And it caches, because a popover that bills a model call
every time somebody opens it is a popover nobody should open.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.api.v1.routes import dashboards as routes
from app.engine import llm as engine_llm
from app.engine import storage as storage_mod
from app.session.base import SessionData


def _session() -> SessionData:
    return SessionData(
        session_id="s1",
        snowflake_user="TESTER",
        access_token="t",
        refresh_token_encrypted="e",
        token_expires_at=datetime.now(UTC),
    )


@pytest.fixture
def fake_backing(monkeypatch):
    """An in-memory stand-in for shared storage, plus a recording model."""
    store: dict[str, object] = {}
    calls: list[dict] = []

    monkeypatch.setattr(storage_mod, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(
        storage_mod, "put_json", lambda k, v: store.__setitem__(k, v)
    )

    def fake_describe(title, chart_type, columns, x, y, dashboard_title=None):
        calls.append({
            "title": title, "chart_type": chart_type, "columns": columns,
            "x": x, "y": y, "dashboard_title": dashboard_title,
        })
        return "A line chart of OTIF (on time in full) against its target."

    monkeypatch.setattr(engine_llm, "describe_panel", fake_describe)
    return store, calls


def _post(payload):
    return asyncio.run(routes.panel_description(payload=payload, session=_session()))


def test_it_describes_a_panel(fake_backing):
    _, calls = fake_backing
    result = _post({"title": "OTIF vs Target", "chart_type": "line",
                    "x": "WEEK", "y": ["OTIF_PCT"], "columns": ["WEEK", "OTIF_PCT"]})
    assert "OTIF (on time in full)" in result["description"]
    assert result["cached"] is False
    assert len(calls) == 1


def test_the_second_ask_is_free(fake_backing):
    _, calls = fake_backing
    payload = {"title": "OTIF vs Target", "chart_type": "line",
               "x": "WEEK", "y": ["OTIF_PCT"], "columns": ["WEEK", "OTIF_PCT"]}
    _post(payload)
    again = _post(payload)
    assert again["cached"] is True
    assert len(calls) == 1, "the model was asked twice for the same chart"


def test_editing_the_panel_retires_the_old_explanation(fake_backing):
    _, calls = fake_backing
    base = {"title": "OTIF vs Target", "chart_type": "line",
            "x": "WEEK", "y": ["OTIF_PCT"], "columns": ["WEEK", "OTIF_PCT"]}
    _post(base)
    _post({**base, "chart_type": "bar"})
    assert len(calls) == 2, "a changed chart must not reuse the old description"


def test_no_data_ever_reaches_the_model(fake_backing):
    """Rows in the body are ignored rather than forwarded. The client has no
    reason to send them, but the guarantee must not depend on that."""
    _, calls = fake_backing
    _post({
        "title": "OTIF vs Target", "chart_type": "line",
        "x": "WEEK", "y": ["OTIF_PCT"], "columns": ["WEEK", "OTIF_PCT"],
        "rows": [["2026-01-01", 91.4], ["2026-01-08", 88.2]],
        "data": {"secret": "value"},
    })
    sent = calls[0]
    assert set(sent) == {"title", "chart_type", "columns", "x", "y", "dashboard_title"}
    flat = repr(sent)
    assert "91.4" not in flat and "secret" not in flat


def test_a_panel_with_no_chart_type_is_rejected(fake_backing):
    with pytest.raises(Exception) as exc:
        _post({"title": "Just a table"})
    assert "chart_type" in str(exc.value)


def test_oversized_input_is_trimmed_before_it_reaches_the_prompt(fake_backing):
    _, calls = fake_backing
    _post({
        "title": "T" * 900, "chart_type": "bar",
        "columns": [f"COL_{i}" for i in range(200)],
        "y": [f"Y_{i}" for i in range(50)],
    })
    sent = calls[0]
    assert len(sent["title"]) == 200
    assert len(sent["columns"]) == 25
    assert len(sent["y"]) == 12

"""The executive line under the KPI band, and why it is only written once.

The UI asks for this on every dashboard load and every refresh, so without a
cache a dashboard somebody leaves open is re-narrated all day for figures that
have not moved. The cache is keyed on the figures rather than on the report,
because the narrative quotes them: a run whose numbers DID move must not keep
answering with last week's position.
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
    store: dict[str, object] = {}
    calls: list[dict] = []

    monkeypatch.setattr(storage_mod, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(
        storage_mod, "put_json", lambda k, v: store.__setitem__(k, v)
    )

    def fake_summarize(title, kpi_facts, panel_snapshots):
        calls.append({"title": title, "kpis": kpi_facts,
                      "panels": panel_snapshots})
        return f"{title} is holding up, with {len(kpi_facts)} headline figures."

    monkeypatch.setattr(engine_llm, "summarize_dashboard", fake_summarize)
    return store, calls


def _post(report_id, payload):
    return asyncio.run(
        routes.dashboard_narrative(
            report_id=report_id, request=None, payload=payload,
            session=_session(),
        )
    )


BODY = {
    "title": "Lane Performance",
    "kpis": [{"metric": "On-time %", "value": 91.4, "format": "percent"}],
    "panels": [{"title": "Cost per lane", "chart_type": "bar",
                "columns": ["LANE", "COST"],
                "sample_rows": [["DXB-JED", 1200]], "row_count": 40}],
}


def test_it_writes_a_narrative(fake_backing):
    _, calls = fake_backing
    result = _post("rpt_1", BODY)

    assert "Lane Performance" in result["narrative"]
    assert result["cached"] is False
    assert len(calls) == 1


def test_reopening_an_unchanged_dashboard_is_free(fake_backing):
    _, calls = fake_backing
    _post("rpt_1", BODY)
    again = _post("rpt_1", BODY)

    assert again["cached"] is True
    assert again["narrative"] == _post("rpt_1", BODY)["narrative"]
    assert len(calls) == 1, "the model was asked again for identical figures"


def test_editing_an_unrelated_chart_does_not_re_narrate(fake_backing):
    """Reported from use: saving a chart-type change on one panel regenerated
    the whole narrative. It cannot have changed the text — summarize_dashboard
    is told not to mention chart types — so it must not change the key."""
    _, calls = fake_backing
    _post("rpt_1", BODY)
    recharted = {**BODY, "panels": [
        {**BODY["panels"][0], "chart_type": "line"}
    ]}
    result = _post("rpt_1", recharted)

    assert result["cached"] is True
    assert len(calls) == 1


def test_chart_type_never_reaches_the_model(fake_backing):
    """The key and the prompt are built together, so a field dropped from one
    has to be absent from the other — otherwise the cache serves a narrative
    written against different input."""
    _, calls = fake_backing
    _post("rpt_1", BODY)

    assert "chart_type" not in calls[0]["panels"][0]
    # Figures, not rows: the model is never handed raw rows to read a trend from.
    assert set(calls[0]["panels"][0]) == {"title", "row_count", "truncated", "figures"}
    # The value is already written out, so the format word has done its job.
    assert set(calls[0]["kpis"][0]) == {"metric", "value"}


def test_a_field_the_client_invents_is_ignored(fake_backing):
    """Normalising both sides means an extra key in the body cannot silently
    become a cache-busting input."""
    _, calls = fake_backing
    _post("rpt_1", BODY)
    noisy = {**BODY, "panels": [
        {**BODY["panels"][0], "display": "bar", "palette": "ember"}
    ]}
    result = _post("rpt_1", noisy)

    assert result["cached"] is True
    assert len(calls) == 1


def test_a_figure_that_moved_gets_a_fresh_narrative(fake_backing):
    """The correctness half. Caching on report_id alone would leave a sentence
    quoting 91.4% attached to a dashboard now reading 78.0%."""
    _, calls = fake_backing
    _post("rpt_1", BODY)
    moved = {**BODY, "kpis": [
        {"metric": "On-time %", "value": 78.0, "format": "percent"}
    ]}
    result = _post("rpt_1", moved)

    assert result["cached"] is False
    assert len(calls) == 2


def test_panel_rows_are_part_of_the_key(fake_backing):
    """KPIs are not the only numbers quoted — the panel snapshots are in the
    payload too, so they have to be in the fingerprint."""
    _, calls = fake_backing
    _post("rpt_1", BODY)
    changed = {**BODY, "panels": [
        {**BODY["panels"][0], "sample_rows": [["DXB-JED", 1750]]}
    ]}
    _post("rpt_1", changed)

    assert len(calls) == 2


def test_two_dashboards_do_not_share_a_narrative(fake_backing):
    _, calls = fake_backing
    _post("rpt_1", BODY)
    result = _post("rpt_2", BODY)

    assert result["cached"] is False
    assert len(calls) == 2


def test_a_storage_failure_does_not_break_the_feature(fake_backing, monkeypatch):
    """Caching is an optimisation. A backend that cannot store must make this
    slower, not broken."""
    _, calls = fake_backing

    def refuse(key, value):
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(storage_mod, "put_json", refuse)

    result = _post("rpt_1", BODY)
    assert "Lane Performance" in result["narrative"]
    assert len(calls) == 1


# --- a capped panel must not be narrated as a finished one --------------------


def test_a_capped_panel_is_flagged_to_the_model(fake_backing):
    """The failure this prevents is an assertion, not an omission: given the
    last rows of a series that stops at the row limit, the model writes them up
    as the current position — "volume held through June" when June is simply
    where the warehouse stopped counting."""
    _, calls = fake_backing
    _post("rpt_1", {**BODY, "panels": [
        {**BODY["panels"][0], "truncated": True}
    ]})

    assert calls[0]["panels"][0]["truncated"] is True


def test_a_panel_that_becomes_capped_gets_a_fresh_narrative(fake_backing):
    """Same reasoning as a figure that moved: the flag changes what the sentence
    is allowed to claim, so it has to be in the fingerprint."""
    _, calls = fake_backing
    _post("rpt_1", BODY)
    _post("rpt_1", {**BODY, "panels": [
        {**BODY["panels"][0], "truncated": True}
    ]})

    assert len(calls) == 2


def test_absence_and_false_are_one_state(fake_backing):
    """The page sends the flag and the fallback run computes it; an older client
    sends neither. Three spellings of "not capped" must not be three cache
    entries for one narrative."""
    _, calls = fake_backing
    _post("rpt_1", BODY)                                   # key absent
    result = _post("rpt_1", {**BODY, "panels": [
        {**BODY["panels"][0], "truncated": False}
    ]})

    assert result["cached"] is True
    assert len(calls) == 1


def test_the_fallback_path_computes_the_flag_itself():
    """The body-less path builds its own snapshot from a fresh run, so it has to
    carry the flag without being handed one."""
    kpis, snaps = routes._snapshot_for_narrative([{
        "title": "Weekly Volume", "chart_type": "line",
        "columns": ["WEEK", "SHIPMENTS"],
        "rows": [["2025-W1", 120]], "row_count": 100, "truncated": True,
    }])

    assert snaps[0]["truncated"] is True


def test_the_model_is_told_what_the_flag_means(monkeypatch):
    """Carrying the flag is half of it: "truncated": true means nothing to a
    model that was never told which sentences it forbids."""
    seen: dict = {}

    class _Block:
        type = "text"
        text = "Fine."

    class _Messages:
        def create(self, **kw):
            seen.update(kw)
            return type("R", (), {"content": [_Block()]})()

    monkeypatch.setattr(engine_llm, "_get_client",
                        lambda: type("C", (), {"messages": _Messages()})())
    engine_llm.summarize_dashboard("T", [], [{"title": "P", "truncated": True}])

    system = seen["system"]
    assert "truncated" in system
    assert "row limit" in system


# --- the figures the summary is written from ---------------------------------------
#
# From a real dashboard: the summary said "AED 235.9 billion" under a tile reading
# AED 235.9M, and described "erosion through June" on data running to August 2026,
# because it was handed a raw number and the first five (oldest) rows of each chart.

MONTHS = [f"2025-{m:02d}-01" for m in range(2, 13)] + [f"2026-{m:02d}-01" for m in range(1, 9)]


def _exec_body(**panel_overrides):
    rows = [[m, 11_000_000 + i * 150_000] for i, m in enumerate(MONTHS)]
    rows[-1][1] = 7_400_000  # the August drop on the real chart
    panel = {"title": "GMV by Month", "chart_type": "line", "x": "MONTH_START", "y": "GMV_AED",
             "columns": ["MONTH_START", "GMV_AED"], "rows": rows, "row_count": len(rows),
             "column_formats": None, "default_currency": "AED", **panel_overrides}
    return {
        "title": "Executive Overview",
        "default_currency": "AED",
        "kpis": [{"metric": "TOTAL GMV", "value": 235930000.12, "format": "currency:AED"},
                 {"metric": "PURCHASE CONVERSION RATE", "value": 2.95, "format": "percent"}],
        "panels": [panel],
    }


def test_a_kpi_reaches_the_model_in_the_scale_its_tile_shows(fake_backing):
    _, calls = fake_backing
    _post("rpt_exec", _exec_body())
    values = {k["metric"]: k["value"] for k in calls[0]["kpis"]}
    assert values["TOTAL GMV"] == "AED 235.9M (overall)"
    assert values["PURCHASE CONVERSION RATE"] == "2.95% (overall)"
    assert "235930000" not in str(calls[0]["kpis"])


def test_a_trend_reaches_the_model_up_to_its_latest_period(fake_backing):
    _, calls = fake_backing
    _post("rpt_exec", _exec_body())
    series = calls[0]["panels"][0]["figures"]["series"]["GMV_AED"]
    assert series["latest"] == "AED 7.4M (Aug 2026)"
    assert series["first"] == "AED 11.0M (Feb 2025)"
    assert series["periods"] == 19
    assert "Jun 2025" not in str(series["latest"])


def test_the_page_currency_applies_when_a_panel_does_not_name_one(fake_backing):
    _, calls = fake_backing
    _post("rpt_exec", _exec_body(default_currency=None, columns=["MONTH_START", "NET_REVENUE"],
                                  y="NET_REVENUE"))
    latest = calls[0]["panels"][0]["figures"]["series"]["NET_REVENUE"]["latest"]
    assert latest.startswith("AED ")


def test_a_panel_without_numbers_still_reaches_the_model_without_figures(fake_backing):
    _, calls = fake_backing
    body = _exec_body(columns=["MARKET"], rows=[["Dubai"], ["Riyadh"]], x="MARKET", y=None)
    _post("rpt_exec", body)
    assert calls[0]["panels"][0]["figures"] is None


def test_a_twenty_panel_dashboard_is_summarised_whole(fake_backing):
    _, calls = fake_backing
    body = _exec_body()
    body["panels"] = [dict(body["panels"][0], title=f"Panel {i}") for i in range(20)]
    _post("rpt_big", body)
    assert len(calls[0]["panels"]) == 20


def test_the_fallback_path_builds_the_same_figures():
    """The body-less path runs the dashboard itself; its snapshot must carry
    what the figures need, so both paths describe the same numbers."""
    body = _exec_body()
    panel = body["panels"][0]
    run_panel = {**panel, "display": "chart"}
    kpis, snaps = routes._snapshot_for_narrative([run_panel])
    _, _, from_run = routes._narrative_payload("T", kpis, snaps, "AED")
    _, _, from_page = routes._narrative_payload("T", [], body["panels"], "AED")
    assert from_run[0]["figures"] == from_page[0]["figures"]


def test_the_model_is_told_to_quote_figures_not_work_them_out(monkeypatch):
    """The prompt half of the fix: a summary invented "67% of total GMV" and
    wrote a margin move as a bare "-14"."""
    import app.engine.llm as llm

    seen = {}

    class _Messages:
        def create(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop before any network call")

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(llm, "_get_client", lambda: _Client())
    try:
        llm.summarize_dashboard("T", [], [])
    except Exception:
        pass
    system = seen["system"]
    assert "Never calculate a figure that is not given" in system
    assert "never a bare number" in system
    # "conversion rose 0.2 pp to 2.95%": the chart's change on the tile's value.
    assert "never pair a chart's change with an (overall) value" in system
    assert "'top_share'" in system
    # "GMV has declined to AED 235.9M": a whole-period total read as a trend's end.
    assert "never say a KPI rose, fell, declined or grew to its value" in system



def test_new_instructions_reach_a_dashboard_already_summarised(fake_backing, monkeypatch):
    """Keyed on the numbers alone, a wording fix never showed: a dashboard viewed
    before it kept its old summary until the data moved."""
    _, calls = fake_backing
    _post("rpt_exec", _exec_body())
    _post("rpt_exec", _exec_body())
    assert len(calls) == 1

    monkeypatch.setattr(engine_llm, "SUMMARY_SYSTEM", engine_llm.SUMMARY_SYSTEM + " Be brief.")
    assert _post("rpt_exec", _exec_body())["cached"] is False
    assert len(calls) == 2


def test_the_model_call_keeps_all_twenty_panels(monkeypatch):
    """The route allows twenty; a second cap of ten inside the model call dropped
    the rest without a word."""
    seen = {}

    class _Messages:
        def create(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop before any network call")

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(engine_llm, "_get_client", lambda: _Client())
    panels = [{"title": f"Panel {i}", "row_count": 1, "truncated": False, "figures": None}
              for i in range(20)]
    try:
        engine_llm.summarize_dashboard("T", [], panels)
    except Exception:
        pass
    assert '"Panel 19"' in seen["messages"][0]["content"]

"""A plan proposed on one worker must be savable on another.

The failure this pins was a live one: "No cached plan for plan_id '…' (it may
have expired)" seconds after planning. Nothing had expired — the caches are
plain dicts, and `uvicorn --workers 4` sends the save to whichever process is
free, which is a different one about three times in four. A backend restart
mid-conversation produced the identical message.
"""

from __future__ import annotations

import pytest

from app.engine import dashboards as dash
from app.engine import storage as storage_mod


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """Storage every 'worker' can see, isolated from the real root."""
    monkeypatch.setattr(
        storage_mod, "_storage", storage_mod.LocalStorage(tmp_path / "store")
    )
    return tmp_path


PLAN = {"title": "Exposure", "panels": [{"title": "P", "sql": "SELECT 1"}],
        "parameters": []}


def test_a_plan_survives_the_jump_to_another_worker(shared, monkeypatch):
    plan_id = dash.cache_plan(PLAN, "LOAN")

    # Worker B: same code, cold process, empty dict.
    monkeypatch.setattr(dash, "_PLAN_CACHE", {})

    found = dash.get_cached_plan(plan_id)

    assert found is not None, "the save worker could not see the plan"
    assert found["plan"]["title"] == "Exposure"
    assert found["database"] == "LOAN"


def test_a_plan_id_that_was_never_issued_is_still_a_miss(shared, monkeypatch):
    monkeypatch.setattr(dash, "_PLAN_CACHE", {})

    assert dash.get_cached_plan("never-existed") is None


def test_each_plan_keeps_its_own_id(shared, monkeypatch):
    """Iterating on a plan must not overwrite the previous one — the user may
    still save the earlier iteration."""
    first = dash.cache_plan({**PLAN, "title": "First"}, "LOAN")
    second = dash.cache_plan({**PLAN, "title": "Second"}, "LOAN")
    monkeypatch.setattr(dash, "_PLAN_CACHE", {})

    assert first != second
    assert dash.get_cached_plan(first)["plan"]["title"] == "First"
    assert dash.get_cached_plan(second)["plan"]["title"] == "Second"


def test_a_previewed_edit_survives_the_jump_too(shared, monkeypatch):
    """Same two-step shape, same failure: preview on one worker, confirm on
    another, and the commit refused an edit the user had just reviewed."""
    dash._cache_edit("r1", {"plan": PLAN, "changes_description": "make it a line",
                            "base_version": 2, "report_title": "T",
                            "description": "", "tags": [], "diff": {}})

    monkeypatch.setattr(dash, "_EDIT_CACHE", {})

    cached = dash._get_cached_edit("r1")
    assert cached is not None
    assert cached["changes_description"] == "make it a line"
    assert cached["base_version"] == 2


def test_committing_an_edit_clears_it_everywhere(shared, monkeypatch):
    """Otherwise a second confirm would re-commit a stale proposal from
    storage after the dict had already forgotten it."""
    dash._cache_edit("r1", {"plan": PLAN, "changes_description": "x"})
    dash._drop_cached_edit("r1")

    monkeypatch.setattr(dash, "_EDIT_CACHE", {})

    assert dash._get_cached_edit("r1") is None


def test_planning_still_works_when_storage_is_unavailable(monkeypatch):
    """The shared copy is an improvement, not a dependency: a broken bucket
    must degrade to the old single-worker behaviour, not break planning."""
    class Broken:
        def put(self, key, data): raise RuntimeError("no bucket")
        def get(self, key): raise RuntimeError("no bucket")
        def exists(self, key): raise RuntimeError("no bucket")
        def list_prefix(self, prefix): raise RuntimeError("no bucket")
        def delete(self, key): raise RuntimeError("no bucket")

    monkeypatch.setattr(storage_mod, "_storage", Broken())

    plan_id = dash.cache_plan(PLAN, "LOAN")

    assert dash.get_cached_plan(plan_id)["plan"]["title"] == "Exposure"

"""Running one dashboard's panel queries together.

A dashboard ran its panels one after another: about 0.6-1 s each, so a
20-panel dashboard took 20 s to open, and planning, editing and saving checked
every panel the same way. DASHBOARD_QUERY_CONCURRENCY lets them run together.

What must not change is anything a user can see or rely on: the panels come
back in the same order with the same data, one failing panel leaves the rest
alone, each query runs as the SIGNED-IN user and never as the service account,
and many people opening dashboards at once cannot open an unbounded number of
queries.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.engine import dashboards
from app.engine.connectors.snowflake import client as sc


class _Warehouse:
    """A stand-in Snowflake: slow enough for queries to overlap, and recording
    how many ran at once, on which thread, and as whom."""

    def __init__(self, delay=0.05, fail=()):
        self.delay = delay
        self.fail = set(fail)
        self.lock = threading.Lock()
        self.running = 0
        self.most = 0
        self.threads = set()
        self.identities = set()
        self.connections = set()

    def _enter(self):
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
            self.threads.add(threading.get_ident())
            self.identities.add(sc.bound_identity())
            self.connections.add(id(sc._request_connection.get()))

    def _leave(self):
        with self.lock:
            self.running -= 1

    def run_query(self, sql, max_rows=None):
        self._enter()
        try:
            time.sleep(self.delay)
            if sql in self.fail:
                raise RuntimeError(f"compile error in {sql}")
            n = int(sql.split("_")[-1])
            return {"columns": ["N", "SQUARE"], "rows": [[n, n * n]], "row_count": 1, "sql": sql}
        finally:
            self._leave()

    def dry_run_query(self, sql):
        self.run_query(sql)
        return ["N", "SQUARE"]


def _plan(count):
    return {"title": "T", "panels": [
        {"title": f"Panel {i}", "chart_type": "bar", "x": "N", "y": "SQUARE", "sql": f"SELECT_{i}"}
        for i in range(count)
    ]}


@pytest.fixture
def warehouse(monkeypatch):
    wh = _Warehouse()
    runs = []
    monkeypatch.setattr(sc, "run_query", wh.run_query)
    monkeypatch.setattr(sc, "dry_run_query", wh.dry_run_query)
    monkeypatch.setattr(sc, "read_saved_dashboard", lambda rid: {"DASHBOARD_PLAN": _plan(12)})
    monkeypatch.setattr(sc, "touch_saved_dashboard_run", lambda rid: runs.append(rid) or (1, None))
    monkeypatch.setattr(dashboards.metadata_store, "currency", lambda db: "AED")
    wh.runs = runs
    return wh


def _concurrency(monkeypatch, value):
    monkeypatch.setenv("DASHBOARD_QUERY_CONCURRENCY", str(value))


# --- the same dashboard, either way -----------------------------------------------


def test_parallel_returns_exactly_what_serial_returns(warehouse, monkeypatch):
    warehouse.fail = {"SELECT_3", "SELECT_7"}
    _concurrency(monkeypatch, 1)
    serial = dashboards.run_dashboard("r1")
    _concurrency(monkeypatch, 4)
    parallel = dashboards.run_dashboard("r1")
    assert parallel == serial
    assert [p["title"] for p in parallel["panels"]] == [f"Panel {i}" for i in range(12)]


def test_a_failing_panel_keeps_its_place_and_leaves_the_rest_alone(warehouse, monkeypatch):
    warehouse.fail = {"SELECT_5"}
    _concurrency(monkeypatch, 4)
    panels = dashboards.run_dashboard("r1")["panels"]
    assert "compile error" in panels[5]["error"]
    assert all("error" not in p for i, p in enumerate(panels) if i != 5)
    assert panels[6]["rows"] == [[6, 36]]


def test_a_load_counts_as_one_run(warehouse, monkeypatch):
    _concurrency(monkeypatch, 4)
    dashboards.run_dashboard("r1")
    assert warehouse.runs == ["r1"]


def test_validation_keeps_the_same_panels_and_reasons_in_order(warehouse, monkeypatch):
    warehouse.fail = {"SELECT_2", "SELECT_9"}
    panels = _plan(12)["panels"]
    panels[4] = {**panels[4], "sql": ""}
    _concurrency(monkeypatch, 1)
    serial = dashboards._validate_panels(panels)
    _concurrency(monkeypatch, 4)
    parallel = dashboards._validate_panels(panels)
    assert parallel == serial
    assert [s["title"] for s in parallel[1]] == ["Panel 2", "Panel 4", "Panel 9"]


# --- how much runs at once ---------------------------------------------------------


def test_the_default_is_the_original_one_at_a_time(warehouse, monkeypatch):
    monkeypatch.delenv("DASHBOARD_QUERY_CONCURRENCY", raising=False)
    dashboards.run_dashboard("r1")
    assert warehouse.most == 1
    assert warehouse.threads == {threading.get_ident()}


def test_never_more_than_the_setting(warehouse, monkeypatch):
    _concurrency(monkeypatch, 4)
    dashboards.run_dashboard("r1")
    assert warehouse.most == 4


@pytest.mark.parametrize("raw, expected", [("abc", 1), ("0", 1), ("-3", 1), ("4", 4), ("100", 8)])
def test_a_bad_setting_cannot_run_away(monkeypatch, raw, expected):
    _concurrency(monkeypatch, raw)
    assert dashboards._query_concurrency() == expected


# --- many users at once -----------------------------------------------------------------


def test_every_query_runs_as_the_signed_in_user(warehouse, monkeypatch):
    """A worker without the caller's context would fall back to the shared
    service-account connection -- the panel would run as the wrong identity."""
    _concurrency(monkeypatch, 4)
    connection = object()
    with sc.use_connection(connection, identity=("ALICE", "ANALYST")):
        dashboards.run_dashboard("r1")
        dashboards._validate_panels(_plan(8)["panels"])
    assert warehouse.identities == {("ALICE", "ANALYST")}
    assert warehouse.connections == {id(connection)}


def test_two_users_at_once_each_keep_their_own_connection(warehouse, monkeypatch):
    _concurrency(monkeypatch, 4)
    seen: dict[str, set] = {"ALICE": set(), "BOB": set()}
    original = warehouse.run_query

    def recording(sql, max_rows=None):
        user = sc.bound_identity()[0]
        seen[user].add(id(sc._request_connection.get()))
        return original(sql)

    monkeypatch.setattr(sc, "run_query", recording)
    connections = {"ALICE": object(), "BOB": object()}

    def load(user):
        with sc.use_connection(connections[user], identity=(user, "R")):
            dashboards.run_dashboard("r1")

    threads = [threading.Thread(target=load, args=(u,)) for u in ("ALICE", "BOB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"ALICE": {id(connections["ALICE"])}, "BOB": {id(connections["BOB"])}}


def test_many_dashboards_at_once_share_one_process_wide_cap(warehouse, monkeypatch):
    """Six people opening 12-panel dashboards together, 4 at a time each, would
    be 24 queries; the process cap holds it to its limit and the rest queue."""
    _concurrency(monkeypatch, 4)
    monkeypatch.setattr(dashboards, "_PROCESS_QUERY_SLOTS", threading.BoundedSemaphore(6))
    results = []
    threads = [threading.Thread(target=lambda: results.append(dashboards.run_dashboard("r1")))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert warehouse.most <= 6
    assert len(results) == 6
    assert all(len(r["panels"]) == 12 and not any("error" in p for p in r["panels"]) for r in results)


def test_parallel_is_actually_faster(warehouse, monkeypatch):
    _concurrency(monkeypatch, 1)
    start = time.monotonic()
    dashboards.run_dashboard("r1")
    serial = time.monotonic() - start
    _concurrency(monkeypatch, 4)
    start = time.monotonic()
    dashboards.run_dashboard("r1")
    assert time.monotonic() - start < serial / 2


# --- the setting itself cannot stop the backend ---------------------------------------
#
# Found in the local test: adding DASHBOARD_QUERY_CONCURRENCY to .env stopped the
# backend from starting, because Settings rejects any .env entry it does not
# declare. The same would have happened on a server whose .env carried it.


def test_every_setting_in_the_example_env_is_accepted_at_startup():
    import re
    from pathlib import Path

    from app.core.config import Settings

    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=", example, flags=re.M))
    undeclared = sorted(keys - set(Settings.model_fields))
    assert "DASHBOARD_QUERY_CONCURRENCY" in keys
    assert undeclared == [], f"in .env.example but not in Settings (startup would fail): {undeclared}"


@pytest.mark.parametrize("stack", ["docker-compose.prod.yml", "docker-compose.dev.yml"])
def test_both_hosted_stacks_pass_the_setting_through_defaulting_to_off(stack):
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[3] / stack).read_text(encoding="utf-8")
    assert "DASHBOARD_QUERY_CONCURRENCY: ${DASHBOARD_QUERY_CONCURRENCY:-1}" in compose


def test_an_unreadable_value_does_not_stop_startup(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("DASHBOARD_QUERY_CONCURRENCY", "four")
    assert Settings().DASHBOARD_QUERY_CONCURRENCY == "four"
    assert dashboards._query_concurrency() == 1

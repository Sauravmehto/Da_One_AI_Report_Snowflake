"""Connection-liveness tests for snowflake_client — no real Snowflake needed.

Guards the failure that hit us twice (webapp + scan_excel_template): a shared
connection goes stale while idle, is_closed() doesn't notice, and the next
statement hangs on a dead socket forever."""

import time

import pytest

from app.engine.connectors.snowflake import client as sc


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *args, **kwargs):
        self.conn.executed.append(sql)
        if self.conn.hang_seconds:
            time.sleep(self.conn.hang_seconds)      # simulate a dead socket
        if self.conn.dead:
            raise Exception("250002: connection is closed / socket dead")
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class FakeConn:
    """dead=True -> statements raise; hang_seconds -> statements block."""

    def __init__(self, closed=False, dead=False, hang_seconds=0):
        self._closed = closed
        self.dead = dead
        self.hang_seconds = hang_seconds
        self.executed = []
        self.close_calls = 0

    def is_closed(self):
        return self._closed

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.close_calls += 1
        self._closed = True


@pytest.fixture(autouse=True)
def _reset_conn_state():
    """Never let a test leak module-level connection state."""
    orig_conn, orig_last = sc._conn, sc._last_used
    sc._conn, sc._last_used = None, None
    yield
    sc._conn, sc._last_used = orig_conn, orig_last


def _idle():
    return time.monotonic() - (sc.HEALTHCHECK_IDLE_SECONDS + 10)


# --- 1. dead-on-idle -> reconnect -------------------------------------------

def test_stale_idle_connection_is_probed_closed_and_replaced(monkeypatch):
    dead, fresh = FakeConn(dead=True), FakeConn()
    monkeypatch.setattr(sc, "_new_connection", lambda: fresh)
    sc._conn, sc._last_used = dead, _idle()

    got = sc.get_connection()

    assert got is fresh                                   # reconnected
    assert any("SELECT 1" in s for s in dead.executed)    # probe ran
    assert dead.close_calls == 1                          # stale one closed


def test_probe_that_hangs_is_treated_as_dead_and_does_not_block(monkeypatch):
    """The regression that mattered: a probe on a dead socket must not hang —
    a hard deadline declares it dead and we reconnect."""
    monkeypatch.setattr(sc, "HEALTHCHECK_TIMEOUT_SECONDS", 0.2)
    hung, fresh = FakeConn(hang_seconds=5), FakeConn()
    monkeypatch.setattr(sc, "_new_connection", lambda: fresh)
    sc._conn, sc._last_used = hung, _idle()

    started = time.monotonic()
    got = sc.get_connection()
    elapsed = time.monotonic() - started

    assert got is fresh
    assert elapsed < 2.0        # bounded by the deadline, not the 5s "socket"


def test_idle_but_healthy_connection_is_kept(monkeypatch):
    live = FakeConn()
    monkeypatch.setattr(sc, "_new_connection",
                        lambda: pytest.fail("must not reconnect a healthy connection"))
    sc._conn, sc._last_used = live, _idle()

    assert sc.get_connection() is live
    assert any("SELECT 1" in s for s in live.executed)    # probed, then kept


def test_hot_connection_is_reused_without_probing(monkeypatch):
    live = FakeConn()
    monkeypatch.setattr(sc, "_new_connection",
                        lambda: pytest.fail("must not reconnect a hot connection"))
    sc._conn, sc._last_used = live, time.monotonic()      # just used

    assert sc.get_connection() is live
    assert live.executed == []                            # no probe overhead


# --- 2. is_closed -> reconnect ----------------------------------------------

def test_closed_connection_is_reopened_without_probing(monkeypatch):
    closed, fresh = FakeConn(closed=True), FakeConn()
    monkeypatch.setattr(sc, "_new_connection", lambda: fresh)
    sc._conn, sc._last_used = closed, _idle()

    assert sc.get_connection() is fresh
    assert closed.executed == []          # is_closed() short-circuits the probe


# --- 3. writes are never auto-retried ---------------------------------------

def test_write_is_attempted_exactly_once_and_error_propagates(monkeypatch):
    """A failed write must surface, not be silently retried — retrying a
    statement that may have partially executed risks duplicate rows."""
    conn = FakeConn(dead=True)
    monkeypatch.setattr(sc, "get_connection", lambda: conn)

    with pytest.raises(Exception):
        sc.write_audit({"tool_name": "t", "status": "ok"})

    inserts = [s for s in conn.executed if s.strip().upper().startswith("INSERT")]
    assert len(inserts) == 1

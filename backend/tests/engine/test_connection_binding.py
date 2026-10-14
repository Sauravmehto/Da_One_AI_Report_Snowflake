"""The per-request connection seam.

Why this exists: the engine's Snowflake module owns a single shared,
service-account-authenticated connection. That is right for the standalone MCP
server and wrong for a multi-user web app — every user's query would run with
the service account's grants, and one lock would serialise them all. OAuth on
the *service* layer does not fix it, because the engine authenticates itself
and never calls that layer.

These tests pin the seam's contract: a bound connection wins, identity follows
it, isolation holds across concurrent tasks and across the threadpool hop that
blocking Snowflake calls require, and with nothing bound the behaviour is
exactly as it was before.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

from app.engine.connectors.snowflake import client


class FakeCursor:
    def __init__(self, identity):
        self._identity = identity
        self.description = [("CURRENT_USER()",), ("CURRENT_ROLE()",)]

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self._identity

    def close(self):
        return None


class FakeConnection:
    """Stands in for a caller-owned connection (e.g. one SnowflakeService
    handed out for this request)."""

    def __init__(self, identity=("ALICE", "ANALYST")):
        self._identity = identity
        self.closed = False

    def cursor(self):
        return FakeCursor(self._identity)

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


def test_bound_connection_is_returned_verbatim():
    conn = FakeConnection()
    with client.use_connection(conn):
        assert client.get_connection() is conn


def test_binding_is_reset_on_exit():
    conn = FakeConnection()
    with client.use_connection(conn):
        pass
    # Nothing bound afterwards, so the shared-connection path is back in play.
    assert client._request_connection.get() is None


def test_binding_resets_even_when_body_raises():
    conn = FakeConnection()
    try:
        with client.use_connection(conn):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert client._request_connection.get() is None


def test_supplied_identity_is_used_without_a_round_trip():
    """The caller already knows who it authenticated as; spending a
    SELECT CURRENT_USER() per tool call would be pure waste."""
    conn = FakeConnection(identity=("SHOULD_NOT_BE_QUERIED", "NOR_THIS"))
    with client.use_connection(conn, identity=("ALICE", "ANALYST")):
        assert client.session_identity() == ("ALICE", "ANALYST")


def test_identity_is_queried_over_the_bound_connection_when_not_supplied():
    conn = FakeConnection(identity=("BOB", "READER"))
    with client.use_connection(conn):
        assert client.session_identity() == ("BOB", "READER")


def test_shared_identity_cache_is_not_leaked_to_a_bound_connection():
    """The regression this seam exists to prevent: the module cache holds the
    SERVICE ACCOUNT's identity. Returning it for a per-user request would put
    the wrong user in the audit trail while the query ran as someone else."""
    client._identity = ("SVC_USER", "READ_WRITE_LOAN_DB_ROLE")
    try:
        conn = FakeConnection(identity=("ALICE", "ANALYST"))
        with client.use_connection(conn):
            assert client.session_identity() == ("ALICE", "ANALYST")
        # Outside the binding the shared cache still applies.
        assert client.session_identity() == ("SVC_USER", "READ_WRITE_LOAN_DB_ROLE")
    finally:
        client._identity = None


def test_bound_connection_is_not_health_checked_or_replaced():
    """A caller-owned connection's lifecycle belongs to the caller. The engine
    must hand it back untouched rather than probing or swapping it, unlike the
    shared connection it manages itself."""
    conn = FakeConnection()
    conn.closed = True  # the shared path would replace a closed connection
    with client.use_connection(conn):
        assert client.get_connection() is conn
        assert conn.closed is True


async def test_concurrent_tasks_do_not_see_each_others_connection():
    """ContextVars are per-task: two users served concurrently must not cross
    connections. This is the property a thread-local would NOT give us."""
    seen: dict[str, object] = {}

    async def request(name: str, conn: FakeConnection, delay: float):
        with client.use_connection(conn, identity=(name, "R")):
            await asyncio.sleep(delay)  # interleave the two tasks
            seen[name] = (client.get_connection(), client.session_identity())

    a, b = FakeConnection(), FakeConnection()
    await asyncio.gather(
        request("ALICE", a, 0.02),
        request("BOB", b, 0.01),
    )

    assert seen["ALICE"] == (a, ("ALICE", "R"))
    assert seen["BOB"] == (b, ("BOB", "R"))


async def test_binding_survives_the_threadpool_hop():
    """Blocking Snowflake calls get dispatched via asyncio.to_thread. That
    copies the calling context, so a binding made in the async handler must
    still apply inside the worker thread — otherwise every engine query would
    silently fall back to the shared service-account connection."""
    conn = FakeConnection()

    def blocking_engine_work():
        return client.get_connection(), client.session_identity()

    with client.use_connection(conn, identity=("ALICE", "ANALYST")):
        got_conn, got_identity = await asyncio.to_thread(blocking_engine_work)

    assert got_conn is conn
    assert got_identity == ("ALICE", "ANALYST")

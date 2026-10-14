"""Snowflake connection + safe query execution.

TESTING-ONLY auth model: one shared identity via RSA key-pair, no MFA prompt,
no password in config. Every query runs as this single Snowflake user, so
per-user RBAC does NOT apply. Fine for internal testing; NOT the client-facing
auth model (that needs per-user OAuth, tracked separately for Phase 4).

Everything that touches the database goes through run_query(), so the safety
rules (SELECT-only, row cap, timeout) cannot be bypassed by a tool author.

There is exactly ONE intentional exception: write_audit(), a hardcoded,
parameterized INSERT into the audit table only. It deliberately does NOT go
through validate_sql — that guard exists to police user-supplied SQL, whereas
write_audit takes no user SQL as code (all user data is passed as bound
parameters, never concatenated). The user query path cannot reach it.

Change from the earlier version: the connection is now opened once and reused
(reconnecting only if it dropped), instead of a fresh connect/close on every
call. Interactive tool use feels much snappier, and it lets us cache the
session identity (CURRENT_USER/CURRENT_ROLE) cheaply for the audit log.

Connection liveness: a reused connection can go stale while idle (sleep, network
change, token expiry) and is_closed() does not detect that, so get_connection()
validates an idle connection with a hard-bounded probe and transparently replaces
it. Combined with client-side login/socket timeouts, a dead connection can no
longer hang a tool indefinitely — it either self-heals or fails loudly.
"""

import json
import logging
import os
import re
import threading
import time as _time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from datetime import date, datetime, time
from typing import Any

import snowflake.connector

log = logging.getLogger(__name__)
from cryptography.hazmat.primitives import serialization

QUERY_TIMEOUT_SECONDS = 30
# The cap for chat and the agent, where every row is read by the model.
MAX_ROWS = 100
# The cap for a saved dashboard's panels, where rows go to the page and no
# further: the chart draws them all and the table pages through them.
DASHBOARD_MAX_ROWS = int(os.environ.get("MCP_DASHBOARD_MAX_ROWS", "5000"))

# --- Connection liveness -----------------------------------------------------
# The shared connection can go stale after a period of inactivity (laptop sleep,
# network change, Snowflake session-token expiry). is_closed() does NOT detect a
# half-open socket, so without these guards a stale connection gets reused and the
# next statement blocks on a dead socket forever (observed twice: the webapp, and
# a scan_excel_template call that never returned).
LOGIN_TIMEOUT_SECONDS = 20        # bound re-authentication
# Per-socket-read backstop. Deliberately generous: a legitimately slow
# cold-warehouse resume must NOT be killed, but a dead socket must not hang
# forever. (network_timeout is intentionally NOT set — it bounds a request's
# total time including retries, which would kill valid cold-start queries.)
SOCKET_TIMEOUT_SECONDS = 120
HEALTHCHECK_IDLE_SECONDS = 90     # validate the connection if idle longer than this
HEALTHCHECK_TIMEOUT_SECONDS = 5   # the validation probe must fail fast

# Fully-qualified audit table. Reuses the existing LOAN.AUDIT schema by default;
# override with MCP_AUDIT_TABLE if it lives elsewhere. This is a config value,
# never user input — safe to interpolate into the INSERT's table name.
AUDIT_TABLE = os.environ.get("MCP_AUDIT_TABLE", "LOAN.AUDIT.MCP_PROMPT_LOG")

# Fully-qualified saved-dashboards table (see sql/saved_dashboards.sql). Config
# value, never user input — safe to interpolate into statement text.
SAVED_DASHBOARDS_TABLE = os.environ.get(
    "MCP_SAVED_DASHBOARDS_TABLE", "LOAN.PUBLIC.MCP_SAVED_DASHBOARDS"
)

_conn = None
_conn_lock = threading.Lock()
_identity: tuple[str | None, str | None] | None = None  # (user, role), cached
_last_used: float | None = None  # monotonic clock of the last hand-out, for idle checks


# --- Caller-supplied connection (per request/task) ----------------------------
# The module-level connection below is a single shared, service-account-
# authenticated socket. That is correct for the standalone MCP server (one
# process, one operator) but wrong for a multi-user web app on two counts:
# every user's query would run with the service account's grants regardless of
# who is signed in, and the _conn_lock would serialise all of them behind one
# connection.
#
# These ContextVars let a caller bind its OWN connection (and the identity that
# connection authenticates as) for the duration of a request. Engine code keeps
# calling get_connection() unchanged — no signature churn, and with nothing
# bound the behaviour is exactly as before, which is what the engine test suite
# exercises.
#
# ContextVars specifically (not thread-locals): they are per-async-task, and
# asyncio.to_thread copies the calling context into the worker thread, so a
# binding made in an async handler survives the threadpool hop that blocking
# Snowflake calls require.
_request_connection: ContextVar[Any | None] = ContextVar(
    "engine_request_connection", default=None
)
_request_identity: ContextVar[tuple[str | None, str | None] | None] = ContextVar(
    "engine_request_identity", default=None
)


@contextmanager
def use_connection(conn: Any,
                   identity: tuple[str | None, str | None] | None = None):
    """Bind `conn` as the connection all engine queries use in this context.

    `identity` is the (user, role) that connection authenticates as. Pass it
    when the caller already knows — a web request holds it on the session — so
    the audit trail records the real user without spending a round trip on
    SELECT CURRENT_USER().

    The bound connection's lifecycle belongs to the CALLER: this module will
    not health-check, replace, or close it, unlike the shared connection it
    owns itself.
    """
    tokens = [(_request_connection, _request_connection.set(conn))]
    if identity is not None:
        tokens.append((_request_identity, _request_identity.set(identity)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _load_private_key() -> bytes:
    """Load the RSA private key used for key-pair auth (bypasses MFA)."""
    key_path = _require_env("SNOWFLAKE_PRIVATE_KEY_PATH")
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")

    with open(key_path, "rb") as fh:
        key_bytes = fh.read()

    private_key = serialization.load_pem_private_key(
        key_bytes,
        password=passphrase.encode() if passphrase else None,
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _new_connection():
    """Open a fresh Snowflake connection using key-pair auth (no password, no MFA).

    login_timeout/socket_timeout are client-side bounds: STATEMENT_TIMEOUT_IN_SECONDS
    is enforced by the SERVER and is therefore useless over a dead socket."""
    return snowflake.connector.connect(
        account=_require_env("SNOWFLAKE_ACCOUNT"),
        user=_require_env("SNOWFLAKE_USER"),
        private_key=_load_private_key(),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE") or None,
        role=os.environ.get("SNOWFLAKE_ROLE") or None,
        client_session_keep_alive=True,
        login_timeout=LOGIN_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        session_parameters={
            "STATEMENT_TIMEOUT_IN_SECONDS": QUERY_TIMEOUT_SECONDS,
        },
    )


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - a dead connection often fails to close
        pass


def _is_alive(conn) -> bool:
    """Probe the connection with a trivial query, bounded by a HARD wall-clock
    deadline.

    The deadline is enforced in a worker thread on purpose: the connector's
    `timeout=` is a server-side *query* cancel, which needs a live socket to be
    delivered — on a dead socket the probe would otherwise stall until
    socket_timeout. If the probe doesn't answer within HEALTHCHECK_TIMEOUT_SECONDS
    we declare the connection dead and move on; the orphaned daemon thread dies on
    its own once the socket times out.
    """
    result: list[bool] = []

    def _probe() -> None:
        try:
            cur = conn.cursor()
        except Exception:  # noqa: BLE001
            result.append(False)
            return
        try:
            cur.execute("SELECT 1", timeout=HEALTHCHECK_TIMEOUT_SECONDS)
            cur.fetchone()
            result.append(True)
        except Exception:  # noqa: BLE001
            result.append(False)
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    th = threading.Thread(target=_probe, daemon=True)
    th.start()
    th.join(HEALTHCHECK_TIMEOUT_SECONDS)
    return bool(result) and result[0] is True


def get_connection():
    """Return the connection to use for engine queries.

    A caller-bound connection (see use_connection) always wins — it is
    per-request and its lifecycle is the caller's, so it is handed back
    untouched.

    Otherwise: the shared connection, (re)opened if needed. Thread-safe.
    If the connection has been idle longer than HEALTHCHECK_IDLE_SECONDS it is
    validated first with a cheap, hard-bounded probe. A stale/dead connection is
    closed and replaced BEFORE the caller's statement runs — so no statement (least
    of all a write) is ever sent over a dead socket, and no write is ever retried
    mid-flight. A hot connection is reused with no probe overhead.
    """
    bound = _request_connection.get()
    if bound is not None:
        return bound

    global _conn, _last_used
    with _conn_lock:
        if _conn is not None and not _conn.is_closed():
            idle_for = None if _last_used is None else (_time.monotonic() - _last_used)
            if (idle_for is None or idle_for > HEALTHCHECK_IDLE_SECONDS) and not _is_alive(_conn):
                _close_quietly(_conn)
                _conn = None
        if _conn is None or _conn.is_closed():
            _conn = _new_connection()
        _last_used = _time.monotonic()
        return _conn


def bound_identity() -> tuple[str | None, str | None]:
    """The (user, role) a caller bound with use_connection, or (None, None).

    Never queries: the audit log asks this on every row, including rows written
    where no connection is bound at all.
    """
    return _request_identity.get() or (None, None)


def session_identity() -> tuple[str | None, str | None]:
    """Return (CURRENT_USER, CURRENT_ROLE) for the live session, cached.

    Used by the audit log to record who a call ran as.

    Resolution order matters for audit correctness:
      1. an identity supplied alongside a caller-bound connection — the caller
         already knows who it authenticated as, so no round trip is spent;
      2. a query over a caller-bound connection, if no identity was supplied;
      3. the shared connection's cached identity (the service account).

    The module-level `_identity` cache is deliberately NOT consulted while a
    connection is bound: it belongs to the shared service-account connection,
    and returning it for a per-user request would record the wrong user in the
    audit trail — the exact failure this seam exists to prevent.
    """
    bound_identity = _request_identity.get()
    if bound_identity is not None:
        return bound_identity

    if _request_connection.get() is not None:
        return _query_identity(get_connection())

    global _identity
    if _identity is not None:
        return _identity
    resolved = _query_identity(get_connection_or_none())
    if resolved != (None, None):
        _identity = resolved
    # Do NOT cache a failed/empty lookup: a transient connection issue must not
    # poison the identity for the rest of the process. Retry on the next call.
    return resolved


def get_connection_or_none():
    """get_connection(), but a connection failure yields None instead of
    raising — identity lookup is best-effort and must never break a query."""
    try:
        return get_connection()
    except Exception:  # noqa: BLE001
        return None


def _query_identity(conn) -> tuple[str | None, str | None]:
    """SELECT CURRENT_USER(), CURRENT_ROLE() over `conn`. Never raises."""
    if conn is None:
        return (None, None)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
            row = cur.fetchone()
            if row:
                return (row[0], row[1])
        finally:
            cur.close()
    except Exception:  # noqa: BLE001
        pass
    return (None, None)


def _jsonify(value: Any) -> Any:
    """Make a Snowflake cell JSON-safe for the MCP response."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def run_query(sql: str, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Validate, cap, and execute a read-only query. Raises UnsafeSQLError if blocked."""
    from app.engine.sql_safety import enforce_row_limit, validate_sql  # local import avoids a cycle

    safe_sql = enforce_row_limit(validate_sql(sql), max_rows)
    return _execute_select(safe_sql)


def _execute_select(safe_sql: str) -> dict[str, Any]:
    """Execute an already-safe SELECT and shape the result. Internal — callers
    must pass SQL that has already been validated."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(safe_sql, timeout=QUERY_TIMEOUT_SECONDS)
        columns = [col[0] for col in cursor.description]
        rows = [[_jsonify(cell) for cell in row] for row in cursor.fetchall()]
    finally:
        cursor.close()

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "sql": safe_sql,
    }


# Column order for the audit INSERT. Fixed to match audit_table.sql. EVENT_ID
# (autoincrement) and EVENT_TS (DB default) are intentionally NOT written here.
_AUDIT_COLUMNS = (
    "AUDIT_USER", "AUTH_MODE", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE", "SESSION_ID",
    "TOOL_NAME", "USER_QUESTION", "GENERATED_SQL", "ROW_COUNT", "STATUS",
    "ERROR_MESSAGE", "DURATION_MS", "CLIENT_INFO",
)

#: What one model call cost, added by sql/audit_token_usage.sql. Optional for
#: the same reason the dashboard grouping columns are: a deployment that has not
#: run the migration must keep auditing, not stop. Naming a column that is not
#: there would fail every INSERT, and because auditing is best-effort the
#: failure would be silent — the audit trail would simply stop appearing.
_AUDIT_TOKEN_COLUMNS = (
    "MODEL", "INPUT_TOKENS", "OUTPUT_TOKENS", "CACHE_READ_TOKENS",
    "CACHE_WRITE_TOKENS", "CONVERSATION_ID", "MODEL_CALLS",
)

#: None until a write has told us. Optimistic: a migrated table — the expected
#: case — costs nothing to discover, and an unmigrated one wastes one rejected
#: statement per process and then stops trying.
_audit_token_columns: bool | None = None


def _missing_audit_token_column(exc: Exception) -> bool:
    """Whether `exc` is Snowflake objecting to the token columns."""
    message = str(exc).upper()
    return any(col in message for col in _AUDIT_TOKEN_COLUMNS)


def _audit_insert(row: dict[str, Any], with_tokens: bool) -> tuple[str, tuple]:
    """The audit INSERT and its bound values, with or without token usage."""
    columns = _AUDIT_COLUMNS + (_AUDIT_TOKEN_COLUMNS if with_tokens else ())
    placeholders = ", ".join(["%s"] * len(columns))
    stmt = (
        f"INSERT INTO {AUDIT_TABLE} ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    return stmt, tuple(row.get(col.lower()) for col in columns)


def write_audit(row: dict[str, Any]) -> None:
    """The ONE intentional write path — a parameterized INSERT into the audit
    table only.

    Safety: the statement is hardcoded here; the table name comes from config
    (AUDIT_TABLE), never from a user. All user-supplied values (question, SQL,
    etc.) are passed as BOUND PARAMETERS, so they are data, never executable
    SQL — this cannot be used to run arbitrary statements. It bypasses
    validate_sql by design, and no user-facing tool can invoke it with
    attacker-controlled SQL text.

    Token columns are optional: a deployment that has not run
    sql/audit_token_usage.sql must keep auditing rather than stop, and because
    auditing is best-effort it would stop SILENTLY. Unlike the dashboard insert
    — which probes first, because it runs inside a transaction it could not
    recover inside — this statement stands alone, so it can simply find out. A
    missing column is a COMPILE error: nothing executed, so the retry cannot
    duplicate a row, and the no-retry-on-write rule does not apply to it.
    Every other failure propagates on the first attempt, as before.
    """
    global _audit_token_columns
    with_tokens = _audit_token_columns is not False

    conn = get_connection()
    cursor = conn.cursor()
    try:
        stmt, values = _audit_insert(row, with_tokens)
        try:
            cursor.execute(stmt, values)
        except Exception as exc:  # noqa: BLE001 - the message is the diagnosis
            if not with_tokens or not _missing_audit_token_column(exc):
                raise
            log.warning(
                "%s has no token-usage columns; recording this and later calls "
                "without them. Tool calls are still audited, but what each "
                "model call cost is not. Run sql/audit_token_usage.sql.",
                AUDIT_TABLE,
            )
            _audit_token_columns = False
            stmt, values = _audit_insert(row, with_tokens=False)
            cursor.execute(stmt, values)
        else:
            if with_tokens:
                _audit_token_columns = True
    finally:
        cursor.close()


def read_audit(select_sql: str, max_rows: int) -> dict[str, Any]:
    """Read from the audit table. Same SELECT-only validation as run_query, but
    uses the audit-appropriate cap (max_rows) instead of the 100-row query cap,
    so requesting e.g. 300 audit rows actually returns up to 300 — the query
    cap exists to protect Claude's context on data queries, which doesn't apply
    to a review of the audit trail."""
    from app.engine.sql_safety import enforce_row_limit, validate_sql

    safe_sql = enforce_row_limit(validate_sql(select_sql), max_rows)
    return _execute_select(safe_sql)


def list_databases() -> list[str]:
    """Databases the connected role can see."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW DATABASES")
        name_idx = [c[0].lower() for c in cursor.description].index("name")
        return [row[name_idx] for row in cursor.fetchall()]
    finally:
        cursor.close()


def list_tables(database: str) -> list[dict[str, Any]]:
    """Live table list for a database, straight from information_schema."""
    if not re.fullmatch(r"[A-Za-z0-9_$]+", database):
        raise ValueError(f"Invalid database name: {database!r}")

    sql = (
        f'SELECT table_schema, table_name, row_count '
        f'FROM "{database.upper()}".information_schema.tables '
        f"WHERE table_schema <> 'INFORMATION_SCHEMA' "
        f"ORDER BY table_schema, table_name"
    )
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(sql, timeout=QUERY_TIMEOUT_SECONDS)
        return [
            {
                "full_name": f"{database.upper()}.{row[0]}.{row[1]}",
                "row_count": row[2],
            }
            for row in cursor.fetchall()
        ]
    finally:
        cursor.close()


# --- Saved dashboards --------------------------------------------------------
# Read path (dry_run_query) goes through validate_sql like every other query.
# The write paths (write_saved_dashboard, touch_saved_dashboard_run) are, like
# write_audit, intentional hardcoded/parameterized exceptions to the SELECT-only
# rule: the statement text is fixed here, the table name is config, and all user
# data is bound as parameters — never concatenated. No user-facing tool can
# invoke them with attacker-controlled SQL.

def dry_run_query(sql: str) -> list[str]:
    """Compile a panel's SELECT against Snowflake WITHOUT returning data, to
    prove its columns/joins resolve. Validates read-only first (raises
    UnsafeSQLError), then wraps the query so zero rows are fetched but the inner
    query is still compiled — so a bad column or table surfaces here, at plan
    time. Returns the result column names (useful to confirm x/y aliases exist).
    """
    from app.engine.sql_safety import validate_sql  # local import avoids a cycle

    cleaned = validate_sql(sql)
    probe = f"SELECT * FROM (\n{cleaned}\n) AS _dry_run LIMIT 0"
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(probe, timeout=QUERY_TIMEOUT_SECONDS)
        return [col[0] for col in cursor.description]
    finally:
        cursor.close()


# Column list for a saved-dashboard version INSERT. Every write (initial save or
# a later edit) creates a row with these; VERSION_ID is the unique row key while
# REPORT_ID is the stable identifier shared across a report's versions.
#: Whether this deployment's table has the grouping columns yet — see
#: sql/dashboard_database_name.sql. None until something has tried.
#:
#: Shared by reads AND writes. Reads alone used to carry it, which meant a
#: deployment that had not run the migration listed its dashboards fine and
#: then failed to SAVE one, because the INSERT named a column that was not
#: there. A feature that is optional to read is optional to write.
_group_columns: bool | None = None


def _missing_group_column(exc: Exception) -> bool:
    """Whether `exc` is Snowflake objecting to the grouping columns."""
    message = str(exc).upper()
    return "DATABASE_NAME" in message or "DATA_GROUP" in message


#: CREATED_AT is written explicitly as SYSDATE() -- UTC -- rather than left to
#: the column's DEFAULT CURRENT_TIMESTAMP(). That default is the SESSION's wall
#: clock stored without a zone, so the same moment was saved as a different time
#: depending on who saved it, and the app, which reads these columns as UTC,
#: showed some edits hours out. SYSDATE() is UTC whatever the session says.
_DASHBOARD_INSERT = (
    "(REPORT_ID, VERSION_ID, VERSION, REPORT_TITLE, DESCRIPTION, TAGS, "
    "DASHBOARD_PLAN, CREATED_BY, CREATED_AT, RUN_COUNT, IS_ACTIVE, IS_CURRENT, "
    "EDIT_NOTE, DATABASE_NAME) "
    "SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, SYSDATE(), "
    "0, TRUE, TRUE, %s, %s"
)

#: The same insert for a table that has not been migrated yet. Written out in
#: full rather than derived from the one above: two statements that must bind a
#: different number of values are clearer side by side than as a string edit.
_DASHBOARD_INSERT_LEGACY = (
    "(REPORT_ID, VERSION_ID, VERSION, REPORT_TITLE, DESCRIPTION, TAGS, "
    "DASHBOARD_PLAN, CREATED_BY, CREATED_AT, RUN_COUNT, IS_ACTIVE, IS_CURRENT, "
    "EDIT_NOTE) "
    "SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, SYSDATE(), "
    "0, TRUE, TRUE, %s"
)


def _dashboard_insert_values(row: dict[str, Any]) -> tuple:
    return (
        row["report_id"],
        row["version_id"],
        row.get("version", 1),
        row["report_title"],
        row.get("description"),
        json.dumps(row.get("tags") or []),
        json.dumps(row["dashboard_plan"]),
        row.get("created_by"),
        row.get("edit_note"),
        row.get("database_name"),
    )


def _has_group_columns() -> bool:
    """Whether the table has DATABASE_NAME / DATA_GROUP, asked once.

    Probed with a SELECT that matches no rows rather than by letting a real
    statement fail: the version insert runs inside a transaction, and finding
    out there would mean recovering mid-transaction.
    """
    global _group_columns
    if _group_columns is not None:
        return _group_columns

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT DATABASE_NAME, DATA_GROUP FROM {SAVED_DASHBOARDS_TABLE} "
            "WHERE FALSE"
        )
        _group_columns = True
    except Exception as exc:  # noqa: BLE001 - the message is the diagnosis
        if not _missing_group_column(exc):
            raise
        log.warning(
            "MCP_SAVED_DASHBOARDS has no DATABASE_NAME/DATA_GROUP column. "
            "Dashboards will save and list normally but will not be grouped by "
            "database. Run sql/dashboard_database_name.sql to enable it."
        )
        _group_columns = False
    finally:
        cursor.close()
    return _group_columns


def _dashboard_insert_statement(row: dict[str, Any]) -> tuple[str, tuple]:
    """The INSERT and its bound values, matching what the table actually has."""
    values = _dashboard_insert_values(row)
    if _has_group_columns():
        return f"INSERT INTO {SAVED_DASHBOARDS_TABLE} {_DASHBOARD_INSERT}", values
    # The grouping column is the last bound value, so it drops off the end.
    return (
        f"INSERT INTO {SAVED_DASHBOARDS_TABLE} {_DASHBOARD_INSERT_LEGACY}",
        values[:-1],
    )


def write_saved_dashboard(row: dict[str, Any]) -> None:
    """Persist the FIRST version of a saved dashboard (VERSION 1, current).
    Parameterized INSERT; TAGS and DASHBOARD_PLAN are bound as JSON strings and
    parsed by Snowflake via PARSE_JSON."""
    stmt, values = _dashboard_insert_statement(row)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(stmt, values)
    finally:
        cursor.close()


def insert_dashboard_version(row: dict[str, Any]) -> int:
    """Add a NEW version row for an existing REPORT_ID and demote the previous
    current row — atomically, in one transaction. Returns the version written.

    The old version's row is never overwritten or deleted: it stays as a
    historical record with IS_CURRENT = FALSE, so revert_dashboard_version can
    make it current again later with only a pointer change.

    The number written is one past the HIGHEST version this dashboard has, not
    one past the current one. After a revert those differ: reverting v3 to v2
    and then saving used to write a second "v3" beside the first, so the history
    showed two version 3s and a later revert to 3 made both current. Callers
    pass the number they expect and report the one returned.
    """
    demote = (
        f"UPDATE {SAVED_DASHBOARDS_TABLE} SET IS_CURRENT = FALSE "
        "WHERE REPORT_ID = %s AND IS_CURRENT = TRUE"
    )
    highest = (
        f"SELECT MAX(VERSION) FROM {SAVED_DASHBOARDS_TABLE} WHERE REPORT_ID = %s"
    )
    conn = get_connection()
    cursor = conn.cursor()
    version = int(row.get("version") or 1)
    try:
        cursor.execute("BEGIN")
        cursor.execute(highest, (row["report_id"],))
        found = cursor.fetchall()
        top = found[0][0] if found and found[0] and found[0][0] is not None else None
        if top is not None and int(top) >= version:
            version = int(top) + 1
        row = {**row, "version": version, "version_id": f"{row['report_id']}_v{version}"}
        insert, values = _dashboard_insert_statement(row)
        cursor.execute(demote, (row["report_id"],))
        cursor.execute(insert, values)
        cursor.execute("COMMIT")
    except Exception:
        try:
            cursor.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        cursor.close()
    return version


def revert_dashboard_version(report_id: str, version: int) -> bool:
    """Make an EXISTING version current: set that version's IS_CURRENT = TRUE and
    every other version of the same REPORT_ID to FALSE, atomically. Deletes
    nothing — a revert is only a pointer move. Returns False if the version does
    not exist for this report_id (no change made)."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT COUNT(*) FROM {SAVED_DASHBOARDS_TABLE} "
            "WHERE REPORT_ID = %s AND VERSION = %s",
            (report_id, version),
        )
        if int(cursor.fetchone()[0]) == 0:
            return False
        cursor.execute("BEGIN")
        cursor.execute(
            f"UPDATE {SAVED_DASHBOARDS_TABLE} SET IS_CURRENT = FALSE WHERE REPORT_ID = %s",
            (report_id,),
        )
        cursor.execute(
            f"UPDATE {SAVED_DASHBOARDS_TABLE} SET IS_CURRENT = TRUE "
            "WHERE REPORT_ID = %s AND VERSION = %s",
            (report_id, version),
        )
        cursor.execute("COMMIT")
        return True
    except Exception:
        try:
            cursor.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        cursor.close()


def deactivate_saved_dashboard(report_id: str) -> int:
    """Retire a saved dashboard: IS_ACTIVE = FALSE on EVERY version of it.

    A soft delete, not a DELETE, for two reasons. Every read path already
    filters on IS_ACTIVE, so flipping the flag removes it from the product
    completely — and the rows carry a report's whole version history, which a
    physical delete would destroy irrecoverably on one mis-click. Restoring is
    a single UPDATE by someone with warehouse access.

    Returns the number of version rows retired; 0 means there was nothing
    active under that id, which the caller reports as a 404.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE {SAVED_DASHBOARDS_TABLE} SET IS_ACTIVE = FALSE "
            "WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE",
            (report_id,),
        )
        return int(cursor.rowcount or 0)
    finally:
        cursor.close()


def resolve_report_ids(id_or_title: str) -> list[dict[str, Any]]:
    """Find CURRENT dashboards whose REPORT_ID or (case-insensitive) REPORT_TITLE
    matches. Only IS_CURRENT rows are considered, so a report resolves to at most
    one row per distinct report_id. Returns [{report_id, report_title, version}].
    The caller decides zero / one / many — this never guesses."""
    sql = (
        "SELECT REPORT_ID, REPORT_TITLE, VERSION "
        f"FROM {SAVED_DASHBOARDS_TABLE} "
        "WHERE IS_ACTIVE = TRUE AND IS_CURRENT = TRUE "
        "AND (UPPER(REPORT_ID) = UPPER(%s) OR UPPER(REPORT_TITLE) = UPPER(%s))"
    )
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (id_or_title, id_or_title))
        cols = [c[0] for c in cursor.description]
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [{c.lower(): _jsonify(v) for c, v in zip(cols, r)} for r in rows]


def list_dashboard_versions(report_id: str) -> list[dict[str, Any]]:
    """Every version of one report_id, oldest first, metadata only (no plan)."""
    sql = (
        "SELECT VERSION, VERSION_ID, EDIT_NOTE, CREATED_BY, CREATED_AT, IS_CURRENT "
        f"FROM {SAVED_DASHBOARDS_TABLE} WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE "
        "ORDER BY VERSION"
    )
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (report_id,))
        cols = [c[0] for c in cursor.description]
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [{c: _jsonify(v) for c, v in zip(cols, r)} for r in rows]


def read_saved_dashboard(report_id: str) -> dict[str, Any] | None:
    """Return one active saved dashboard as a dict (DASHBOARD_PLAN and TAGS
    parsed back into Python), or None if not found / inactive."""
    sql = (
        "SELECT REPORT_ID, VERSION_ID, VERSION, REPORT_TITLE, DESCRIPTION, TAGS, "
        "DASHBOARD_PLAN, CREATED_BY, CREATED_AT, LAST_RUN_AT, RUN_COUNT "
        f"FROM {SAVED_DASHBOARDS_TABLE} "
        "WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE AND IS_CURRENT = TRUE"
    )
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (report_id,))
        fetched = cursor.fetchone()
        if not fetched:
            return None
        cols = [c[0] for c in cursor.description]
    finally:
        cursor.close()

    rec = {col: _jsonify(val) for col, val in zip(cols, fetched)}
    # VARIANT/ARRAY come back as JSON text from the connector — parse to objects.
    plan = rec.get("DASHBOARD_PLAN")
    rec["DASHBOARD_PLAN"] = json.loads(plan) if isinstance(plan, str) else plan
    tags = rec.get("TAGS")
    rec["TAGS"] = json.loads(tags) if isinstance(tags, str) and tags else (tags or [])
    return rec


#: Whether this deployment's table has DATABASE_NAME yet.
#:
#: An edit inserts a NEW row, so CREATED_BY on the current row is the last
#: editor, not the owner. The owner and the creation time live on the FIRST
#: version, which is still there — hence the window functions rather than a
#: schema change. DATABASE_NAME is a real column (see
#: sql/dashboard_database_name.sql) and is the one thing here that needs the
#: migration to have run.
_LIST_WITH_DATABASE = (
    "SELECT REPORT_ID, REPORT_TITLE, VERSION, LAST_RUN_AT, RUN_COUNT, "
    "DATABASE_NAME, DATA_GROUP, "
    "CREATED_BY AS UPDATED_BY, CREATED_AT AS UPDATED_AT, "
    "FIRST_VALUE(CREATED_BY) OVER ("
    "  PARTITION BY REPORT_ID ORDER BY VERSION) AS OWNER, "
    "FIRST_VALUE(CREATED_AT) OVER ("
    "  PARTITION BY REPORT_ID ORDER BY VERSION) AS CREATED_AT "
    "FROM {table} WHERE IS_ACTIVE = TRUE "
    "QUALIFY IS_CURRENT = TRUE "
    "ORDER BY UPDATED_AT DESC LIMIT {limit}"
)

#: The same query without the column, for a deployment that has not migrated.
_LIST_WITHOUT_DATABASE = _LIST_WITH_DATABASE.replace("DATABASE_NAME, DATA_GROUP, ", "")

def set_dashboard_group(report_id: str, group: str | None) -> None:
    """File a dashboard under `group`, or clear it back to its database.

    Applied to EVERY version row, not just the current one: filing is a
    property of the dashboard, and a revert to an older version must not send
    it back to a folder somebody moved it out of.

    A hardcoded, parameterized UPDATE against the configured table — the same
    intentional-write pattern as write_audit. No user SQL reaches it.
    """
    if not _has_group_columns():
        raise RuntimeError(
            "This deployment cannot move dashboards between folders yet: "
            "MCP_SAVED_DASHBOARDS has no DATA_GROUP column. Run "
            "sql/dashboard_database_name.sql."
        )
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE {SAVED_DASHBOARDS_TABLE} SET DATA_GROUP = %s "
            "WHERE REPORT_ID = %s",
            (group, report_id),
        )
    finally:
        cursor.close()


def list_saved_dashboards(limit: int) -> list[dict[str, Any]]:
    """Active saved dashboards, most recently touched first — metadata only.

    Only the CURRENT version of each report_id is returned, but the OWNER and
    CREATED_AT come from that report's FIRST version: an edit writes a new row
    under the editor's name, and the person who edited a dashboard is not the
    person who owns it.
    """
    def _run(statement: str) -> list[dict[str, Any]]:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(statement, timeout=QUERY_TIMEOUT_SECONDS)
            cols = [c[0] for c in cursor.description]
            return [
                {col: _jsonify(val) for col, val in zip(cols, r)}
                for r in cursor.fetchall()
            ]
        finally:
            cursor.close()

    template = (
        _LIST_WITH_DATABASE if _has_group_columns() else _LIST_WITHOUT_DATABASE
    )
    return _run(template.format(table=SAVED_DASHBOARDS_TABLE, limit=int(limit)))


def touch_saved_dashboard_run(report_id: str) -> tuple[int, str | None]:
    """Increment RUN_COUNT and set LAST_RUN_AT=now for a saved dashboard, then
    return the updated (run_count, last_run_at). Returns (0, None) if the id
    doesn't exist / is inactive."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE {SAVED_DASHBOARDS_TABLE} "
            # SYSDATE(): UTC, like CREATED_AT -- see _DASHBOARD_INSERT.
            "SET RUN_COUNT = RUN_COUNT + 1, LAST_RUN_AT = SYSDATE() "
            "WHERE REPORT_ID = %s AND IS_ACTIVE = TRUE AND IS_CURRENT = TRUE",
            (report_id,),
        )
        cursor.execute(
            f"SELECT RUN_COUNT, LAST_RUN_AT FROM {SAVED_DASHBOARDS_TABLE} "
            "WHERE REPORT_ID = %s AND IS_CURRENT = TRUE",
            (report_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return (0, None)
    return (int(row[0]), _jsonify(row[1]))
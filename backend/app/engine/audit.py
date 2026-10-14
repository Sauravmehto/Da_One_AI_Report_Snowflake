"""Server-side audit log — WHO asked WHAT, written to Snowflake.

Every prompt that hits this MCP server is recorded in the audit table
(LOAN.AUDIT.MCP_PROMPT_LOG by default), independent of whatever the Claude
Desktop client does. The write is best-effort: if it fails, the failure is
logged to stderr but the user's actual query is never broken by it.

--- Two identities, kept separate (survives service-account -> OAuth) ---

audit_user   WHO asked. Today there is no real per-person login (shared
             service account), so this is a proxy: the OS username of the
             machine running the server. A convenience signal, NOT verified
             identity. Under per-user OAuth (Phase 4) the caller passes the
             real authenticated user here instead — no schema change needed.

auth_mode    How audit_user was established: 'service_account' now,
             'oauth_user' later. Lets anyone reading a historical row know
             whether audit_user was verified or just a proxy.

snowflake_user / snowflake_role
             What the query physically connected as (CURRENT_USER/ROLE).
             Same as the service account for everyone today; becomes the real
             per-user value once OAuth is live.

Set MCP_AUTH_MODE=oauth_user (and pass audit_user per call) when you cut over.
"""

import hashlib
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.engine.connectors.snowflake import client as snowflake_client

log = logging.getLogger(__name__)

# How the current deployment authenticates callers. Flip to 'oauth_user' when
# per-user OAuth is live.
AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "service_account")

# One id per server process: the fallback for a call made outside any signed-in
# request. A request records its own login instead -- see bind_session.
SESSION_ID = str(uuid.uuid4())

#: Who this request is: (session reference, how they signed in). Set once per
#: request by the session dependency, so every row it writes -- tool, chat turn
#: or AI call -- names the same login without each call site passing it along.
_session_scope: ContextVar[tuple[str, str] | None] = ContextVar(
    "audit_session_scope", default=None
)
#: The chat conversation a tool or AI call ran inside, when there is one.
_conversation: ContextVar[str | None] = ContextVar("audit_conversation", default=None)


def session_ref(session_id: str) -> str:
    """A stable, one-way reference to a login for the audit table.

    Never the id itself: it is the session cookie, and anyone able to read the
    audit table could sign in as that user with it.
    """
    return hashlib.sha256(f"audit-session:{session_id}".encode()).hexdigest()[:16]


def bind_session(session_id: str, auth_mode: str | None) -> None:
    """Record the signed-in login for the rest of this request."""
    _session_scope.set((session_ref(session_id), auth_mode or AUTH_MODE))


@contextmanager
def conversation(conversation_id: str | None):
    """Tag the audit rows written inside this block with a conversation."""
    token = _conversation.set(conversation_id)
    try:
        yield
    finally:
        _conversation.reset(token)


def local_identity() -> str:
    """OS login of the machine running this server — the proxy for 'who asked'
    while on the shared service account. NOT authentication; spoofable."""
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def log_event(
    tool: str,
    status: str,
    snowflake_user: str | None = None,
    snowflake_role: str | None = None,
    question: str | None = None,
    sql: str | None = None,
    row_count: int | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    audit_user: str | None = None,
    client_info: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    conversation_id: str | None = None,
    model_calls: int | None = None,
) -> None:
    """Record one tool call to the Snowflake audit table. Never raises —
    auditing must not break a request.

    audit_user defaults to the OS-username proxy; pass it explicitly (the real
    authenticated user) once per-user OAuth is in place.

    The token fields are for the calls that spend money: a chat turn, or a tool
    that runs a model of its own. They stay None for a tool that only runs SQL,
    and they are dropped entirely on a table that predates
    sql/audit_token_usage.sql — see write_audit.

    cache_write_tokens is recorded alongside cache_read_tokens because reads
    alone cannot show whether caching is paying for itself: a write costs 1.25x
    input and a read 0.1x, so a prompt that writes every call and never reads
    costs more than no caching at all.
    """
    # The signed-in user wherever one is known -- passed in, or bound to this
    # request's connection. The machine login is only the last resort: it named
    # the server ("unknown", "shete"), never the person.
    bound_user, bound_role = snowflake_client.bound_identity()
    snowflake_user = snowflake_user or bound_user
    snowflake_role = snowflake_role or bound_role
    scope = _session_scope.get()
    row = {
        "audit_user": audit_user or snowflake_user or local_identity(),
        "auth_mode": scope[1] if scope else AUTH_MODE,
        "snowflake_user": snowflake_user,
        "snowflake_role": snowflake_role,
        "session_id": scope[0] if scope else SESSION_ID,
        "tool_name": tool,
        "user_question": question,
        "generated_sql": sql,
        "row_count": row_count,
        "status": status,
        "error_message": error,
        "duration_ms": duration_ms,
        "client_info": client_info,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "conversation_id": conversation_id or _conversation.get(),
        "model_calls": model_calls,
    }
    try:
        snowflake_client.write_audit(row)
    except Exception as exc:  # noqa: BLE001 - auditing is best-effort
        # Name the role that tried. Under per-user OAuth the INSERT runs as the
        # SIGNED-IN user, not as a service account, so the usual cause is that
        # role lacking INSERT on the table — which "write failed" alone sends
        # nobody looking in the right place. The other cause is no Snowflake
        # connection bound at all: a conversation against an uploaded file
        # binds DuckDB, and a deployment with no service-account credentials
        # then has nothing to write the row with.
        log.error(
            "Audit write to %s failed for tool=%s as role=%s user=%s: %s "
            "(the role needs INSERT on that table; a turn against an uploaded "
            "file has no Snowflake connection to write it with at all).",
            snowflake_client.AUDIT_TABLE, tool, snowflake_role or "?",
            snowflake_user or "?", exc,
        )


def log_ai_call(
    what: str,
    model: str | None,
    response: Any = None,
    *,
    started: float,
    error: BaseException | str | None = None,
    question: str | None = None,
) -> None:
    """One audit row for one model call made outside the chat loop.

    The chat recorded its own spend; the planner, editor, summaries and every
    other direct call recorded nothing, so the audit showed a fraction of the
    bill. `started` is a time.monotonic() reading from just before the call.
    Never raises.
    """
    try:
        usage = getattr(response, "usage", None)

        def count(name: str) -> int | None:
            value = getattr(usage, name, None) if usage is not None else None
            return int(value) if isinstance(value, (int, float)) else None

        log_event(
            f"ai:{what}",
            "error" if error is not None else "ok",
            question=question,
            error=str(error)[:2000] if error is not None else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            client_info="webapp",
            model=model,
            input_tokens=count("input_tokens"),
            output_tokens=count("output_tokens"),
            cache_read_tokens=count("cache_read_input_tokens"),
            cache_write_tokens=count("cache_creation_input_tokens"),
            model_calls=1,
        )
    except Exception:  # noqa: BLE001 - auditing must never break the call it describes
        log.warning("could not record AI call %s", what, exc_info=True)


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent audit rows, newest first, as a list of dicts.

    Reads via the normal safe read path (run_query), so it's just a SELECT on
    the audit table — no special write handling involved.
    """
    limit = max(1, min(int(limit), 500))
    query = (
        "SELECT EVENT_ID, EVENT_TS, AUDIT_USER, AUTH_MODE, SNOWFLAKE_USER, "
        "SNOWFLAKE_ROLE, SESSION_ID, TOOL_NAME, USER_QUESTION, GENERATED_SQL, "
        "ROW_COUNT, STATUS, ERROR_MESSAGE, DURATION_MS "
        f"FROM {snowflake_client.AUDIT_TABLE} "
        # EVENT_TS, not EVENT_ID: the ids come from an autoincrement that does
        # not promise to follow insert time across concurrent writers.
        f"ORDER BY EVENT_TS DESC, EVENT_ID DESC LIMIT {limit}"
    )
    result = snowflake_client.read_audit(query, limit)
    cols = result["columns"]
    return [dict(zip(cols, r)) for r in result["rows"]]
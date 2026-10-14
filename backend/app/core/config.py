import json
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Populate os.environ from .env BEFORE Settings is constructed.
#
# pydantic-settings reads .env itself but does not export it to os.environ, and
# the ported engine (app/engine/*) reads os.environ directly — it was written
# for the standalone MCP server, which called load_dotenv() at startup. Without
# this the engine's Anthropic client finds no key and every LLM-backed engine
# path (plan_dashboard, edit_dashboard, ask_data_question) fails with a
# configuration error, while the app's own settings look perfectly fine.
#
# override=False so a real environment variable still beats the file.
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    PROJECT_NAME: str = "AI Reporting"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    SESSION_BACKEND: Literal["memory", "redis"] = "memory"
    REDIS_URL: str = "redis://localhost:6379/0"

    # With SESSION_BACKEND=memory, a dev-login session persists to disk so a
    # backend restart does not sign you out. OAuth sessions are never written
    # to disk regardless. "memory" opts out.
    SESSION_STORE: Literal["file", "memory"] = "file"
    SESSION_DIR: str = ".sessions"

    # Chat history outlives a login session on purpose (sessions are 24h and
    # are recreated on every sign-in; the history is the valuable part).
    CONVERSATION_TTL_SECONDS: int = 60 * 60 * 24 * 30  # 30 days
    # With SESSION_BACKEND=memory, conversations still persist to disk so a
    # backend restart does not wipe chat history. "memory" opts out.
    CONVERSATION_STORE: Literal["file", "memory"] = "file"
    CONVERSATION_DIR: str = ".conversations"

    SNOWFLAKE_ACCOUNT: str = ""
    SNOWFLAKE_WAREHOUSE: str = ""
    SNOWFLAKE_DATABASE: str = ""
    # Fallback role for the key-pair service account ONLY — it has no user to
    # inherit a role from. Never applied to an OAuth session: see the comment
    # in SnowflakeService._connect for why that mattered.
    SNOWFLAKE_DEFAULT_ROLE: str = ""

    # Roles shown in the in-app role picker. Comma-separated or JSON list.
    # Empty → picker hidden; sessions keep Snowflake's own default / probed role.
    # Switching still requires Snowflake to grant the chosen role to the user.
    # NoDecode: same reason as ADMIN_ROLES — avoid pre-validator JSON decode.
    SNOWFLAKE_AVAILABLE_ROLES: Annotated[list[str], NoDecode] = []

    # ── Admin surface ──────────────────────────────────────────────────────
    # Snowflake roles permitted to author semantic layers. Authorisation is
    # anchored to a Snowflake role rather than an application user list, for
    # the same reason data access is: there is no user store in this app, and
    # a second place to grant privileges is a second place for them to drift.
    #
    # Empty means NOBODY, which is the correct default for an authorisation
    # check — a deployment that forgets to configure this refuses admin access
    # rather than granting it to everyone. Set it explicitly per environment.
    # Accepts a JSON list or a plain comma-separated string.
    #
    # NoDecode is load-bearing: pydantic-settings JSON-decodes a list field
    # inside the env source, BEFORE any field validator runs, so
    # `ADMIN_ROLES=DATAALPHA_ADMIN` would fail at import with an opaque
    # "error parsing value" and take the whole app down. NoDecode hands the raw
    # string to the validator below instead.
    ADMIN_ROLES: Annotated[list[str], NoDecode] = []

    # ── Dev-only key-pair login ────────────────────────────────────────────
    # TEMPORARY: lets local development proceed before the Snowflake OAuth
    # SECURITY INTEGRATION exists. Authenticates to Snowflake for real (RSA
    # key-pair, the same service account the legacy MCP server uses) but
    # short-circuits the WEB APP's login, so anyone who can reach the port
    # gets a session. Both DEBUG and DEV_LOGIN_ENABLED must be true for the
    # endpoint to exist at all; it 404s otherwise. Delete this block, the
    # key-pair branch in SnowflakeService, and the /auth/dev-login route once
    # OAuth is live.
    DEV_LOGIN_ENABLED: bool = False
    SNOWFLAKE_USER: str = ""
    SNOWFLAKE_PRIVATE_KEY_PATH: str = ""
    SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: str = ""

    OAUTH_CLIENT_ID: str = ""
    OAUTH_CLIENT_SECRET: str = ""
    OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/callback"
    OAUTH_AUTHORIZE_URL: str = ""
    OAUTH_TOKEN_URL: str = ""

    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    SESSION_COOKIE_NAME: str = "session_id"
    SESSION_TTL_SECONDS: int = 86400
    OAUTH_STATE_TTL_SECONDS: int = 600
    FRONTEND_URL: str = "http://localhost:5173"
    TOKEN_ENCRYPTION_KEY: str = ""

    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"

    # ── AI query flow (Anthropic) ──────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    # Sonnet writes/repairs SQL (needs reasoning); Haiku does the cheap,
    # high-volume work (classification + answer summarising).
    ANTHROPIC_MODEL_SQL: str = "claude-sonnet-5"
    ANTHROPIC_MODEL_FAST: str = "claude-haiku-4-5-20251001"
    # Drives the agent loop: tool selection, multi-step reasoning, and the
    # user-facing prose. Sonnet balances instruction-following against the
    # latency an interactive stream can tolerate.
    ANTHROPIC_MODEL_AGENT: str = "claude-sonnet-5"
    # Ceiling on tool-call rounds per user turn. A malformed plan or a tool the
    # model keeps retrying must terminate rather than bill indefinitely.
    AGENT_MAX_ITERATIONS: int = 12
    #: Output ceiling for ONE agent turn. Thinking tokens are drawn from this
    #: same budget, so the old 4096 could be spent reasoning before a single
    #: word or tool call was emitted — the turn then ended on stop_reason
    #: "max_tokens" with nothing on screen, which read as the app hanging.
    AGENT_MAX_TOKENS: int = 16000
    #: Once a chat's history reaches about this many tokens, the turns before the
    #: most recent CHAT_SUMMARY_KEEP_TURNS are condensed into a summary for the
    #: model (app/agent/history.py). 0 turns condensing off.
    CHAT_SUMMARY_TRIGGER_TOKENS: int = 50000
    CHAT_SUMMARY_KEEP_TURNS: int = 4
    # Hard cap on rows returned to the UI from a generated query.
    QUERY_ROW_LIMIT: int = 100

    # Excel report extract cap (per query/block). Read by excel_reports via
    # os.environ; declared here so a .env value is not rejected as extra.
    MCP_EXCEL_MAX_ROWS: int = 50000
    # Rows a saved dashboard panel fetches. Read by the Snowflake client via
    # os.environ; declared here so a .env value is not rejected as extra.
    MCP_DASHBOARD_MAX_ROWS: int = 5000
    # Where Excel templates and reports are stored. Read by engine.storage via
    # os.environ; declared so copying .env.example to .env does not stop the
    # backend from starting.
    EXCEL_STORAGE_BACKEND: str = "local"
    EXCEL_S3_BUCKET: str = ""
    EXCEL_S3_PREFIX: str = ""

    # Base for the dashboard links handed back after a save. Read by
    # engine.dashboards via os.environ; declared here for the same reason, and
    # it must match where the frontend is actually served or the link 404s.
    WEBAPP_BASE_URL: str = "http://localhost:5173"

    # How many of one dashboard's panel queries run at once. Read by
    # engine.dashboards via os.environ; declared here so a .env value is not
    # rejected as extra -- which stops the backend from starting at all. A string
    # on purpose: an unreadable value falls back to 1 there instead of failing
    # validation here.
    DASHBOARD_QUERY_CONCURRENCY: str = "1"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY)

    @property
    def metadata_dir(self) -> Path:
        """Where the semantic layer TOMLs live.

        Both loaders and the admin editor must agree on this, so it is stated
        once here rather than derived independently in three places. Becomes
        the local implementation's root when the repository moves to S3.
        """
        return Path(__file__).resolve().parents[2] / "config" / "metadata"

    @property
    def dev_login_available(self) -> bool:
        """Dev key-pair login is usable. Requires DEBUG *and* the explicit
        opt-in flag *and* the key material — so it can never switch itself on
        in a deployment that merely forgot to set DEBUG=false."""
        return bool(
            self.DEBUG
            and self.DEV_LOGIN_ENABLED
            and self.SNOWFLAKE_USER
            and self.SNOWFLAKE_PRIVATE_KEY_PATH
        )

    @field_validator("ADMIN_ROLES", "SNOWFLAKE_AVAILABLE_ROLES", mode="before")
    @classmethod
    def _split_role_list(cls, value: Any) -> Any:
        """Accept comma-separated role lists as well as JSON.

        pydantic-settings parses a list field as JSON, so the obvious
        comma-separated spelling would otherwise fail at startup with a parse
        error rather than doing what it looks like it does.

        JSON is tried FIRST — splitting first would tear `["A", "B"]` into
        `['["A"', '"B"]'] and produce role names that can never match.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass  # fall through and treat it as a plain string
        return [part.strip() for part in text.split(",") if part.strip()]

    def available_roles(self) -> list[str]:
        """Allowlist for the role picker, uppercased and de-duplicated."""
        seen: set[str] = set()
        out: list[str] = []
        for role in self.SNOWFLAKE_AVAILABLE_ROLES:
            key = role.strip().upper()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    def is_available_role(self, role: str | None) -> bool:
        if not role:
            return False
        return role.strip().upper() in set(self.available_roles())

    def is_admin_role(self, role: str | None) -> bool:
        """Whether a session's active role may author semantic layers.

        Compared case-insensitively: Snowflake reports role names uppercased,
        and a lower-case entry in the environment that silently never matched
        would look exactly like a permissions problem.

        Only the session's ACTIVE role is considered, not every role the user
        holds. That keeps the rule legible — what you can edit matches what you
        are querying as — and means a user with an admin role that is not their
        default does not get admin privileges by surprise.
        """
        if not role:
            return False
        return role.strip().upper() in {r.strip().upper() for r in self.ADMIN_ROLES}

    @model_validator(mode="after")
    def validate_session_backend(self) -> "Settings":
        if self.SESSION_BACKEND == "redis" and not self.REDIS_URL:
            raise ValueError("REDIS_URL is required when SESSION_BACKEND=redis")
        return self

    @cached_property
    def oauth_authorize_url(self) -> str:
        if self.OAUTH_AUTHORIZE_URL:
            return self.OAUTH_AUTHORIZE_URL
        return (
            f"https://{self.SNOWFLAKE_ACCOUNT}.snowflakecomputing.com/oauth/authorize"
        )

    @cached_property
    def oauth_token_url(self) -> str:
        if self.OAUTH_TOKEN_URL:
            return self.OAUTH_TOKEN_URL
        return (
            f"https://{self.SNOWFLAKE_ACCOUNT}.snowflakecomputing.com/oauth/token-request"
        )


settings = Settings()

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import snowflake.connector

from app.core.config import Settings
from app.core.security import TokenEncryptor
from app.services.connection_cache import connection_cache
from app.services.oauth import OAuthService
from app.session.base import SessionData, SessionStore


class SnowflakeService:
    def __init__(
        self,
        settings: Settings,
        session_store: SessionStore,
        encryptor: TokenEncryptor,
    ) -> None:
        self._settings = settings
        self._session_store = session_store
        self._encryptor = encryptor
        self._oauth = OAuthService(settings)

    async def ensure_fresh_session(self, session: SessionData) -> SessionData:
        # Key-pair (dev-login) sessions have no OAuth token to refresh — the
        # private key is read at connect time and never expires mid-session.
        if session.auth_mode == "keypair":
            return session
        if session.token_expires_at > datetime.now(UTC) + timedelta(minutes=2):
            return session

        refresh_token = self._encryptor.decrypt(session.refresh_token_encrypted)
        tokens = await self._oauth.refresh_tokens(refresh_token)

        updated = session.model_copy(
            update={
                "access_token": tokens.access_token,
                "refresh_token_encrypted": self._encryptor.encrypt(
                    tokens.refresh_token or refresh_token
                ),
                "token_expires_at": OAuthService.token_expires_at(tokens.expires_in),
            }
        )
        await self._session_store.set(
            session.session_id,
            updated,
            self._settings.SESSION_TTL_SECONDS,
        )
        # Retire, not evict: other requests of this session may still be
        # mid-query on the old connection. See connection_cache.
        connection_cache.retire(session.session_id)
        return updated

    def _load_private_key(self) -> bytes:
        """DER-encoded private key for the TEMPORARY dev-login path. Read from
        disk on demand; the passphrase (if any) comes from settings."""
        from cryptography.hazmat.primitives import serialization

        path = self._settings.SNOWFLAKE_PRIVATE_KEY_PATH
        passphrase = self._settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE

        with open(path, "rb") as fh:
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

    def _connect(self, session: SessionData) -> snowflake.connector.SnowflakeConnection:
        if session.auth_mode == "keypair":
            # TEMPORARY dev-login path — shared service account, no per-user
            # identity. Removed once OAuth is live.
            connect_kwargs: dict[str, Any] = {
                "account": self._settings.SNOWFLAKE_ACCOUNT,
                "user": self._settings.SNOWFLAKE_USER,
                "private_key": self._load_private_key(),
                "warehouse": self._settings.SNOWFLAKE_WAREHOUSE,
            }
        else:
            connect_kwargs = {
                "account": self._settings.SNOWFLAKE_ACCOUNT,
                "authenticator": "oauth",
                "token": session.access_token,
                "warehouse": self._settings.SNOWFLAKE_WAREHOUSE,
            }

        # The session's own role wins, and SNOWFLAKE_DEFAULT_ROLE applies ONLY
        # to the shared key-pair service account, which has no user to inherit
        # a role from.
        #
        # It used to apply to every session. That flattened every OAuth user
        # onto one configured role: two people with entirely different grants
        # got identical access, and the app looked like it was enforcing
        # something it was not. Passing NO role is the right default for a real
        # user — Snowflake then resolves their own default role, which is
        # exactly the behaviour we want and needs no probe to have succeeded.
        role = session.role or (
            self._settings.SNOWFLAKE_DEFAULT_ROLE
            if session.auth_mode == "keypair"
            else ""
        )
        if role:
            connect_kwargs["role"] = role
        if self._settings.SNOWFLAKE_DATABASE:
            connect_kwargs["database"] = self._settings.SNOWFLAKE_DATABASE

        return snowflake.connector.connect(**connect_kwargs)

    async def get_connection(
        self, session: SessionData
    ) -> snowflake.connector.SnowflakeConnection:
        fresh_session = await self.ensure_fresh_session(session)

        def factory() -> snowflake.connector.SnowflakeConnection:
            return self._connect(fresh_session)

        return connection_cache.get_or_create(session.session_id, factory)

    async def resolve_identity(self, access_token: str) -> tuple[str, str | None]:
        """(CURRENT_USER, CURRENT_ROLE) for a freshly issued OAuth token.

        The probe connects with no role, so Snowflake resolves the user's own
        default role and reports it back. Recording it makes the session honest
        about which role its queries ran under — worth having for the audit
        trail and for showing the user.

        Access control does not depend on this succeeding: a session with no
        role connects without one, and Snowflake applies the user's default
        role anyway. The caller is free to treat a failure here as non-fatal.
        """
        temp_session = SessionData(
            session_id="temp",
            snowflake_user="unknown",
            access_token=access_token,
            refresh_token_encrypted="",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        connection = self._connect(temp_session)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
            row = cursor.fetchone()
            if row:
                user = str(row[0]) if row[0] else "unknown"
                role = str(row[1]) if row[1] else None
                return user, role
        finally:
            connection.close()

        return "unknown", None

    async def verify_role(self, session: SessionData, role: str) -> str:
        """Connect with ``role`` and return Snowflake's CURRENT_ROLE().

        Raises the underlying connector error if the user is not granted the
        role or the connection otherwise fails.
        """
        probe = session.model_copy(update={"role": role})
        fresh = await self.ensure_fresh_session(probe)
        connection = self._connect(fresh)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT CURRENT_ROLE()")
            row = cursor.fetchone()
            if not row or row[0] is None:
                raise RuntimeError(f"Snowflake did not confirm role '{role}'")
            return str(row[0])
        finally:
            connection.close()

    async def visible_databases(self, session: SessionData) -> set[str]:
        """Database names this session's role can actually see, uppercased.

        Snowflake already refuses the queries, so this is not what keeps the
        data safe. It exists to stop the app handing over things that sit
        BESIDE the data — most importantly a database's semantic layer, which
        is injected into the model's context and describes every table, column
        meaning, join rule and metric formula it has.
        """
        rows = await self.execute_query_dict(session, "SHOW DATABASES")
        names: set[str] = set()
        for row in rows:
            for key, value in row.items():
                if key.lower() == "name" and value:
                    names.add(str(value).upper())
                    break
        return names

    async def resolve_keypair_identity(self) -> tuple[str, str | None]:
        """(CURRENT_USER, CURRENT_ROLE) over a throwaway key-pair connection.

        TEMPORARY — supports the dev-login path only. Recording the real
        Snowflake identity here keeps the audit trail truthful about which
        account a dev session actually ran as.
        """
        probe_session = SessionData(
            session_id="dev-login-probe",
            snowflake_user="unknown",
            access_token="",
            refresh_token_encrypted="",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_mode="keypair",
        )
        connection = self._connect(probe_session)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT CURRENT_USER(), CURRENT_ROLE()")
            row = cursor.fetchone()
            if row:
                return str(row[0]), (str(row[1]) if row[1] is not None else None)
        finally:
            connection.close()

        return "unknown", None

    async def execute_query(
        self, session: SessionData, sql: str
    ) -> list[tuple[Any, ...]]:
        connection = await self.get_connection(session)
        cursor = connection.cursor()
        cursor.execute(sql)
        return list(cursor.fetchall())

    async def execute_query_dict(
        self,
        session: SessionData,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a query and key each row by column name instead of position.

        Useful for SHOW commands and information_schema lookups where column
        order isn't worth hardcoding against. `params` are bound safely by the
        connector (pyformat / %s placeholders) — never string-interpolated.
        """
        connection = await self.get_connection(session)
        cursor = connection.cursor()
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    async def execute_query_table(
        self,
        session: SessionData,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run a query and return (column_names, rows).

        Preferred for row previews: keeps column headers available even when
        zero rows come back, and preserves the original column order.
        """
        connection = await self.get_connection(session)
        cursor = connection.cursor()
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        rows = list(cursor.fetchall())
        return columns, rows

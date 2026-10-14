import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import get_current_session
from app.core.config import settings
from app.core.security import TokenEncryptor
from app.services.connection_cache import connection_cache
from app.services.oauth import OAuthService
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class UserResponse(BaseModel):
    snowflake_user: str
    role: str | None = None
    # Lets the UI hide what this user cannot use. NOT the enforcement point:
    # every admin route checks the role itself, because a client is free to
    # ignore this field.
    is_admin: bool = False


class RolesResponse(BaseModel):
    roles: list[str]
    current: str | None = None


class SetRoleRequest(BaseModel):
    role: str


def _get_encryptor(request: Request) -> TokenEncryptor:
    return request.app.state.token_encryptor


def _get_oauth_service() -> OAuthService:
    return OAuthService(settings)


def _get_snowflake_service(request: Request) -> SnowflakeService:
    return SnowflakeService(
        settings,
        request.app.state.session_store,
        request.app.state.token_encryptor,
    )


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        max_age=settings.SESSION_TTL_SECONDS,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
    )


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    if not settings.OAUTH_CLIENT_ID or not settings.SNOWFLAKE_ACCOUNT:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth is not configured",
        )

    state = secrets.token_urlsafe(32)
    await request.app.state.session_store.set_oauth_state(
        state,
        settings.OAUTH_STATE_TTL_SECONDS,
    )

    authorize_url = _get_oauth_service().build_authorize_url(state)
    return RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        redirect = RedirectResponse(
            url=f"{settings.FRONTEND_URL}/login?error={error}",
            status_code=status.HTTP_302_FOUND,
        )
        return redirect

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing code or state",
        )

    if not await request.app.state.session_store.consume_oauth_state(state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state",
        )

    oauth_service = _get_oauth_service()
    snowflake_service = _get_snowflake_service(request)
    encryptor = _get_encryptor(request)

    tokens = await oauth_service.exchange_code(code)

    # Ask Snowflake who this is and what role they hold. Non-fatal by design:
    # a session with no role connects without one and Snowflake applies the
    # user's own default role regardless, so a failed probe costs us an
    # accurate label in the audit trail, not correct access control. Failing
    # the login here would be trading a real outage for a cosmetic gap.
    resolved_user: str = ""
    resolved_role: str | None = None
    try:
        resolved_user, resolved_role = await snowflake_service.resolve_identity(
            tokens.access_token
        )
    except Exception:  # noqa: BLE001 - see above; login proceeds either way
        log.warning("Could not resolve Snowflake identity at login", exc_info=True)

    snowflake_user = tokens.username or resolved_user or "unknown"

    session_id = secrets.token_urlsafe(32)
    session_data = SessionData(
        session_id=session_id,
        snowflake_user=snowflake_user,
        # Deliberately NOT falling back to SNOWFLAKE_DEFAULT_ROLE: None means
        # "connect without a role", which lets Snowflake apply this user's own
        # default. Substituting the configured role here is precisely the bug
        # this change removes.
        role=resolved_role,
        access_token=tokens.access_token,
        refresh_token_encrypted=encryptor.encrypt(tokens.refresh_token),
        token_expires_at=OAuthService.token_expires_at(tokens.expires_in),
    )

    await request.app.state.session_store.set(
        session_id,
        session_data,
        settings.SESSION_TTL_SECONDS,
    )

    response = RedirectResponse(
        url=settings.FRONTEND_URL,
        status_code=status.HTTP_302_FOUND,
    )
    _set_session_cookie(response, session_id)
    return response


@router.get("/capabilities")
async def capabilities() -> dict[str, bool]:
    """What login methods this deployment actually offers, so the UI doesn't
    render a button that cannot work."""
    return {
        "oauth": bool(settings.OAUTH_CLIENT_ID and settings.SNOWFLAKE_ACCOUNT),
        "dev_login": settings.dev_login_available,
    }


@router.post("/dev-login")
async def dev_login(request: Request, response: Response) -> UserResponse:
    """TEMPORARY dev-only login — DELETE once Snowflake OAuth is live.

    Mints a session backed by the RSA key-pair service account instead of a
    per-user OAuth token. Snowflake authentication is still real; what is
    bypassed is the WEB APP's login, so anyone able to reach this port gets a
    session. It therefore 404s (rather than 403) unless DEBUG *and*
    DEV_LOGIN_ENABLED are both set and the key material is configured —
    absent in any normal deployment, the route does not exist at all.
    """
    if not settings.dev_login_available:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    snowflake_service = _get_snowflake_service(request)
    snowflake_user, role = await snowflake_service.resolve_keypair_identity()

    session_id = secrets.token_urlsafe(32)
    session_data = SessionData(
        session_id=session_id,
        snowflake_user=snowflake_user,
        role=role or settings.SNOWFLAKE_DEFAULT_ROLE or None,
        access_token="",
        refresh_token_encrypted="",
        # No OAuth token to expire; bounded by the session TTL instead.
        token_expires_at=datetime.now(UTC) + timedelta(seconds=settings.SESSION_TTL_SECONDS),
        auth_mode="keypair",
    )
    await request.app.state.session_store.set(
        session_id, session_data, settings.SESSION_TTL_SECONDS
    )

    _set_session_cookie(response, session_id)
    return UserResponse(
        snowflake_user=snowflake_user,
        role=session_data.role,
        is_admin=settings.is_admin_role(session_data.role),
    )


@router.get("/me", response_model=UserResponse)
async def me(session: SessionData = Depends(get_current_session)) -> UserResponse:
    return UserResponse(
        snowflake_user=session.snowflake_user,
        role=session.role,
        is_admin=settings.is_admin_role(session.role),
    )


@router.get("/roles", response_model=RolesResponse)
async def list_roles(
    session: SessionData = Depends(get_current_session),
) -> RolesResponse:
    """Allowlist for the in-app role picker (from SNOWFLAKE_AVAILABLE_ROLES)."""
    return RolesResponse(
        roles=settings.available_roles(),
        current=session.role,
    )


@router.post("/role", response_model=UserResponse)
async def set_role(
    body: SetRoleRequest,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> UserResponse:
    """Switch the session's active Snowflake role.

    The role must be on SNOWFLAKE_AVAILABLE_ROLES, and Snowflake must accept a
    connection under it for this user. On success the session is updated and
    any cached Snowflake connection for this session is dropped.
    """
    requested = body.role.strip().upper()
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role is required",
        )
    if not settings.is_available_role(requested):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role '{requested}' is not in SNOWFLAKE_AVAILABLE_ROLES",
        )

    snowflake_service = _get_snowflake_service(request)
    try:
        confirmed = await snowflake_service.verify_role(session, requested)
    except Exception as exc:  # noqa: BLE001 - surface connector errors to the client
        log.warning(
            "Role switch rejected by Snowflake user=%s role=%s",
            session.snowflake_user,
            requested,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Snowflake refused role '{requested}'. "
                "Confirm the role is granted to your user."
            ),
        ) from exc

    active_role = confirmed.strip().upper() or requested
    updated = session.model_copy(update={"role": active_role})
    await request.app.state.session_store.set(
        session.session_id,
        updated,
        settings.SESSION_TTL_SECONDS,
    )
    connection_cache.evict(session.session_id)

    return UserResponse(
        snowflake_user=updated.snowflake_user,
        role=updated.role,
        is_admin=settings.is_admin_role(updated.role),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_id:
        await request.app.state.session_store.delete(session_id)
        connection_cache.evict(session_id)

    _clear_session_cookie(response)

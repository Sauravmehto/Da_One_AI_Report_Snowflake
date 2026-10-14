from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.engine import audit
from app.session.base import SessionData


async def get_current_session(request: Request) -> SessionData:
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    session_store = request.app.state.session_store
    session = await session_store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )

    # Every audit row this request writes names this login and how it signed in.
    audit.bind_session(session.session_id, session.auth_mode)
    return session


async def require_admin(
    session: SessionData = Depends(get_current_session),
) -> SessionData:
    """Gate a route on the signed-in user's Snowflake role being an admin one.

    Authorisation is anchored to the same identity that governs data access,
    so there is one place to grant a person privileges rather than two that
    can disagree.

    403 rather than 404: the admin surface is not a secret, and telling
    somebody their role is insufficient is more useful than pretending the
    page does not exist. What IS hidden is anything about databases they
    cannot see — that filtering happens per-route.
    """
    if not settings.is_admin_role(session.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This action requires an administrator role. "
                f"Your active Snowflake role is '{session.role or 'none'}'."
            ),
        )
    return session

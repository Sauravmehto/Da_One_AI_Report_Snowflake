from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import get_current_session
from app.core.config import settings
from app.engine.connectors.snowflake import client as engine_client
from app.metadata.loader import metadata_service
from app.services.ai_query import AIQueryService
from app.services.explore import ExploreService, InvalidIdentifierError
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    database: str
    message: str


class ChatData(BaseModel):
    columns: list[str]
    rows: list[list[object]]


class ChatResponse(BaseModel):
    kind: str
    answer: str
    sql: str | None = None
    data: ChatData | None = None


def _get_ai_service(request: Request) -> AIQueryService:
    snowflake_service = SnowflakeService(
        settings,
        request.app.state.session_store,
        request.app.state.token_encryptor,
    )
    explore_service = ExploreService(snowflake_service, metadata_service)
    return AIQueryService(
        settings, snowflake_service, explore_service, metadata_service
    )


@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> ChatResponse:
    if not settings.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI is not configured (missing ANTHROPIC_API_KEY).",
        )

    message = body.message.strip()
    if not message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Message is empty."
        )

    # Same role check the streaming endpoint applies. This route also builds a
    # semantic-context block for whatever database it is handed, so leaving it
    # ungated would make it the way around that check rather than a legacy
    # path nobody uses.
    snowflake_service = SnowflakeService(
        settings, request.app.state.session_store, request.app.state.token_encryptor
    )
    visible = await snowflake_service.visible_databases(session)
    if body.database.upper() not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your Snowflake role does not have access to '{body.database}'.",
        )

    ai_service = _get_ai_service(request)
    try:
        # Bound so the audit rows for this answer's model calls are written as
        # the signed-in user, over their own connection.
        connection = await snowflake_service.get_connection(session)
        with engine_client.use_connection(
            connection, identity=(session.snowflake_user, session.role)
        ):
            result = await ai_service.answer(session, body.database, message)
    except InvalidIdentifierError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return ChatResponse.model_validate(result)

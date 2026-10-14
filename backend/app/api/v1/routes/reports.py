from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import get_current_session
from app.core.config import settings
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

router = APIRouter(prefix="/reports", tags=["reports"])


class SampleReportResponse(BaseModel):
    snowflake_user: str
    current_user: str
    current_role: str | None


def _get_snowflake_service(request: Request) -> SnowflakeService:
    return SnowflakeService(
        settings,
        request.app.state.session_store,
        request.app.state.token_encryptor,
    )


@router.get("/sample", response_model=SampleReportResponse)
async def sample_report(
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> SampleReportResponse:
    snowflake_service = _get_snowflake_service(request)

    rows = await snowflake_service.execute_query(
        session,
        "SELECT CURRENT_USER(), CURRENT_ROLE()",
    )

    current_user = str(rows[0][0]) if rows else session.snowflake_user
    current_role = str(rows[0][1]) if rows and rows[0][1] is not None else session.role

    return SampleReportResponse(
        snowflake_user=session.snowflake_user,
        current_user=current_user,
        current_role=current_role,
    )

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_current_session
from app.core.config import settings
from app.metadata.loader import metadata_service
from app.services.explore import (
    ExploreService,
    InvalidIdentifierError,
    TableNotFoundError,
)
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

router = APIRouter(prefix="/databases", tags=["databases"])

_SKIP_DATABASES = {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"}


# ── Response models ───────────────────────────────────────────────────────────


class DatabaseSummary(BaseModel):
    name: str
    display_name: str
    description: str = ""
    is_documented: bool = False


class TableSummary(BaseModel):
    # `schema` shadows a BaseModel attribute, so store as schema_name and
    # expose it as "schema" in the JSON via the alias.
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema")
    name: str
    full_name: str
    table_type: str = ""
    row_count: int | None = None
    description: str = ""
    is_documented: bool = False


class ColumnDetail(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    description: str = ""


class TableDetail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    database: str
    schema_name: str = Field(alias="schema")
    name: str
    full_name: str
    description: str = ""
    notes: str = ""
    is_documented: bool = False
    columns: list[ColumnDetail]


class TablePreview(BaseModel):
    columns: list[str]
    rows: list[list[object]]
    row_limit: int


# ── Dependencies ──────────────────────────────────────────────────────────────


def _get_snowflake_service(request: Request) -> SnowflakeService:
    return SnowflakeService(
        settings,
        request.app.state.session_store,
        request.app.state.token_encryptor,
    )


def _get_explore_service(request: Request) -> ExploreService:
    return ExploreService(_get_snowflake_service(request), metadata_service)


def _row_value(row: dict[str, object], key: str) -> object:
    """Case-insensitive value lookup (SHOW commands return lowercase keys)."""
    for existing_key, value in row.items():
        if existing_key.lower() == key.lower():
            return value
    return None


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[DatabaseSummary])
async def list_databases(
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> list[DatabaseSummary]:
    snowflake_service = _get_snowflake_service(request)
    rows = await snowflake_service.execute_query_dict(session, "SHOW DATABASES")

    databases: list[DatabaseSummary] = []
    for row in rows:
        name = str(_row_value(row, "name"))
        if name.upper() in _SKIP_DATABASES:
            continue
        described = metadata_service.describe_database(name)
        databases.append(
            DatabaseSummary(
                name=name,
                display_name=described["display_name"],
                description=described["description"],
                is_documented=described["is_documented"],
            )
        )

    # Documented databases first, then alphabetical within each group.
    databases.sort(key=lambda d: (not d.is_documented, d.name))
    return databases


@router.get("/{database}/tables", response_model=list[TableSummary])
async def list_tables(
    database: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> list[TableSummary]:
    explore = _get_explore_service(request)
    try:
        rows = await explore.list_tables(session, database)
    except InvalidIdentifierError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return [TableSummary.model_validate(row) for row in rows]


@router.get("/{database}/tables/{schema}/{table}", response_model=TableDetail)
async def get_table(
    database: str,
    schema: str,
    table: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
) -> TableDetail:
    explore = _get_explore_service(request)
    try:
        detail = await explore.get_table(session, database, schema, table)
    except InvalidIdentifierError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except TableNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Table not found: {exc}"
        ) from exc
    return TableDetail.model_validate(detail)


@router.get(
    "/{database}/tables/{schema}/{table}/preview", response_model=TablePreview
)
async def preview_table(
    database: str,
    schema: str,
    table: str,
    request: Request,
    session: SessionData = Depends(get_current_session),
    limit: int = 50,
) -> TablePreview:
    explore = _get_explore_service(request)
    try:
        preview = await explore.preview(session, database, schema, table, limit)
    except InvalidIdentifierError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except TableNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Table not found: {exc}"
        ) from exc
    return TablePreview.model_validate(preview)

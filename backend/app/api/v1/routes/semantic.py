"""Admin API for authoring semantic layers.

Separate router from /databases, which is the read-only view the chat and
Explore surfaces use. This one writes, so it is namespaced under /admin and is
the only place in the API that can change what the model believes a database
means.

The list endpoint deliberately includes databases with no layer at all: those
are exactly the ones an admin needs to find, and they are invisible in chat
today because the agent endpoint rejects any database that is not documented.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.core.config import settings
from app.semantic import (
    DocumentNotFoundError,
    Issue,
    LocalDirectoryRepository,
    SemanticDocument,
    SemanticLayerService,
    ValidationFailedError,
    validate_document,
)
from app.services.explore import ExploreService, InvalidIdentifierError
from app.services.snowflake import SnowflakeService
from app.session.base import SessionData

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/semantic", tags=["semantic-admin"])

_SKIP_DATABASES = {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"}


def _service() -> SemanticLayerService:
    return SemanticLayerService(LocalDirectoryRepository(settings.metadata_dir))


def _snowflake(request: Request) -> SnowflakeService:
    return SnowflakeService(
        settings,
        request.app.state.session_store,
        request.app.state.token_encryptor,
    )


def _explore(request: Request) -> ExploreService:
    return ExploreService(_snowflake(request))


async def _require_visible(
    request: Request, session: SessionData, database: str
) -> None:
    """Refuse a database this admin's role cannot see.

    Being an administrator grants the ability to AUTHOR, not wider reach into
    the warehouse: an admin scoped to private credit edits private-credit
    layers and nothing else. The list endpoint already only offers databases
    the role can see, so this closes the gap between what the UI shows and what
    the API accepts — a named database in a URL is not the same as one the user
    was offered.
    """
    visible = await _snowflake(request).visible_databases(session)
    if database.upper() not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your Snowflake role does not have access to '{database}'.",
        )


# ── Response models ───────────────────────────────────────────────────────


class IssueOut(BaseModel):
    severity: str
    field: str
    message: str

    @classmethod
    def of(cls, issue: Issue) -> "IssueOut":
        return cls(severity=issue.severity, field=issue.field, message=issue.message)


class LayerSummary(BaseModel):
    database: str
    display_name: str = ""
    is_published: bool = False
    has_draft: bool = False
    documented_tables: int = 0


class Coverage(BaseModel):
    """How much of the live schema the document actually describes.

    The number that tells an admin where to spend their time, and the reason
    the editor shows the schema beside the document rather than alone.
    """

    total_tables: int = 0
    documented_tables: int = 0
    total_columns: int = 0
    documented_columns: int = 0
    tables_with_date_roles: int = 0


class LayerDetail(BaseModel):
    database: str
    document: SemanticDocument | None
    is_published: bool
    has_draft: bool
    coverage: Coverage
    issues: list[IssueOut] = Field(default_factory=list)


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/databases", response_model=list[LayerSummary])
async def list_layers(
    request: Request,
    session: SessionData = Depends(require_admin),
) -> list[LayerSummary]:
    """Every visible database, documented or not."""
    service = _service()
    snowflake = SnowflakeService(
        settings, request.app.state.session_store, request.app.state.token_encryptor
    )

    rows = await snowflake.execute_query_dict(session, "SHOW DATABASES")
    names = sorted(
        str(row.get("name") or row.get("NAME") or "").upper()
        for row in rows
        if str(row.get("name") or row.get("NAME") or "").upper() not in _SKIP_DATABASES
    )

    out: list[LayerSummary] = []
    for name in names:
        if not name:
            continue
        state = service.get_state(name)
        doc = state.editable
        out.append(
            LayerSummary(
                database=name,
                display_name=(doc.display_name if doc else "") or name,
                is_published=state.is_published,
                has_draft=state.has_draft,
                documented_tables=len(doc.tables) if doc else 0,
            )
        )
    return out


@router.get("/{database}", response_model=LayerDetail)
async def get_layer(
    database: str,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> LayerDetail:
    """The editable document plus coverage against the live schema."""
    await _require_visible(request, session, database)

    service = _service()
    state = service.get_state(database)

    try:
        schema = await _explore(request).schema_map(session, database)
    except InvalidIdentifierError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc

    doc = state.editable
    return LayerDetail(
        database=database.upper(),
        document=doc,
        is_published=state.is_published,
        has_draft=state.has_draft,
        coverage=_coverage(doc, schema),
        issues=[
            IssueOut.of(i)
            for i in (validate_document(doc, schema=schema or None) if doc else [])
        ],
    )


@router.post("/{database}/draft", response_model=LayerDetail)
async def save_draft(
    database: str,
    document: SemanticDocument,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> LayerDetail:
    """Save work in progress. Invisible to the model until published."""
    await _require_visible(request, session, database)

    service = _service()
    issues = service.save_draft(database, document)

    try:
        schema = await _explore(request).schema_map(session, database)
    except InvalidIdentifierError:
        schema = {}

    return LayerDetail(
        database=database.upper(),
        document=document,
        is_published=service.get_state(database).is_published,
        has_draft=True,
        coverage=_coverage(document, schema),
        issues=[IssueOut.of(i) for i in issues],
    )


@router.post("/{database}/publish", response_model=LayerDetail)
async def publish(
    database: str,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> LayerDetail:
    """Make the draft authoritative, after validating against the live schema."""
    await _require_visible(request, session, database)

    service = _service()

    try:
        schema = await _explore(request).schema_map(session, database)
    except InvalidIdentifierError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc

    try:
        warnings = service.publish(database, schema=schema)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=str(exc)) from exc
    except ValidationFailedError as exc:
        # 422 with the full issue list: the editor highlights each offending
        # field rather than showing one opaque failure message.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Cannot publish: the document has errors.",
                "issues": [IssueOut.of(i).model_dump() for i in exc.issues],
            },
        ) from exc

    state = service.get_state(database)
    return LayerDetail(
        database=database.upper(),
        document=state.published,
        is_published=True,
        has_draft=False,
        coverage=_coverage(state.published, schema),
        issues=[IssueOut.of(i) for i in warnings],
    )


@router.delete("/{database}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def discard_draft(
    database: str,
    request: Request,
    session: SessionData = Depends(require_admin),
) -> None:
    """Throw away the draft, reverting the editor to the published document."""
    await _require_visible(request, session, database)
    _service().discard_draft(database)


# ── Helpers ───────────────────────────────────────────────────────────────


def _coverage(doc: SemanticDocument | None, schema: dict[str, dict[str, str]]) -> Coverage:
    total_tables = len(schema)
    total_columns = sum(len(cols) for cols in schema.values())
    if doc is None:
        return Coverage(total_tables=total_tables, total_columns=total_columns)

    documented_tables = 0
    documented_columns = 0
    tables_with_dates = 0
    for name, table in doc.tables.items():
        live = schema.get(name.upper())
        if live is None:
            continue  # documented but no longer exists; validation flags it
        documented_tables += 1
        documented_columns += sum(
            1 for col in table.key_columns if col.upper() in live
        )
        if table.date_roles:
            tables_with_dates += 1

    return Coverage(
        total_tables=total_tables,
        documented_tables=documented_tables,
        total_columns=total_columns,
        documented_columns=documented_columns,
        tables_with_date_roles=tables_with_dates,
    )

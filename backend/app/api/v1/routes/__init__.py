from fastapi import APIRouter

from .agent import router as agent_router
from .auth import router as auth_router
from .chat import router as chat_router
from .dashboards import router as dashboards_router
from .databases import router as databases_router
from .excel_reports import router as excel_reports_router
from .excel_sources import router as excel_sources_router
from .reports import router as reports_router
from .semantic import router as semantic_router

router = APIRouter()

router.include_router(auth_router)
router.include_router(databases_router)
# Legacy one-shot chat (services/ai_query.py). Nothing in the frontend calls it
# any more — the chat uses the streaming endpoint under /agent — and it is kept
# only because its chart-suggestion logic is still under test. Remove it with
# sendChatMessage in frontend/src/lib/api.ts, not separately.
router.include_router(chat_router)
router.include_router(agent_router)
router.include_router(dashboards_router)
router.include_router(reports_router)
# Excel template reports — upload, bind to SQL, run for a period.
router.include_router(excel_reports_router)
# Excel as a DATA SOURCE — profile a client's own workbook and map its fields.
# Separate from the report builder above; the two share no state.
router.include_router(excel_sources_router)
# Authoring the semantic layer — the only writer of the metadata configs.
router.include_router(semantic_router)

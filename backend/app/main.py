from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.routes import router as api_router
from app.conversation import get_conversation_store
from app.core.config import settings
from app.core.security import TokenEncryptor
from app.session import get_session_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    encryption_key = settings.TOKEN_ENCRYPTION_KEY
    if not encryption_key:
        encryption_key = Fernet.generate_key().decode()

    app.state.session_store = get_session_store(settings)
    app.state.conversation_store = get_conversation_store(settings)
    app.state.token_encryptor = TokenEncryptor(encryption_key)

    yield

    await app.state.session_store.close()
    await app.state.conversation_store.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    debug=settings.DEBUG,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "version": settings.VERSION}

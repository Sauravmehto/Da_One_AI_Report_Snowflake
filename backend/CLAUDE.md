# Backend — Agent Guide

## Commands
- `pipenv install` — install deps
- `pipenv run uvicorn app.main:app --reload` — dev server (port 8000)
- `pipenv run pytest` — run tests
- `pipenv install <pkg>` — add a package

## Structure
- `app/main.py` — FastAPI app, middleware, router registration
- `app/core/config.py` — all settings via pydantic-settings
- `app/api/v1/routes/` — route files; register in `routes/__init__.py`
- `app/models/` — ORM models (add when DB is wired)
- `app/schemas/` — Pydantic request/response schemas
- `app/services/` — business logic (called from routes)

## Adding a new resource
1. Schema in `app/schemas/<name>.py`
2. Service in `app/services/<name>.py`
3. Router in `app/api/v1/routes/<name>.py`
4. Register in `app/api/v1/routes/__init__.py`

## Config
All settings live in `app/core/config.py` using `pydantic-settings`.
Add new vars there and to `.env.example`.

# PrivateCredit — Agent Guide

## Stack
- **Frontend**: React 19 + Vite 7 + pnpm + TypeScript + Tailwind 4 + shadcn/ui → `frontend/`
- **Backend**: FastAPI + pipenv + Python 3.12 → `backend/`
- **Database**: PostgreSQL (not yet wired — see `backend/CLAUDE.md` when added)

## Running locally (no Docker)
```sh
# Terminal 1 — backend
cd backend
pipenv install
pipenv run uvicorn app.main:app --reload
# → http://localhost:8000

# Terminal 2 — frontend
cd frontend
pnpm install
pnpm dev
# → http://localhost:5173
```

## API conventions
- All routes versioned under `/api/v1/`
- Frontend proxies `/api` → `http://localhost:8000` in dev (vite.config.ts)
- Health check: `GET /health`

## Env vars
- `backend/.env` — copy from `backend/.env.example`
- `frontend/.env` — copy from `frontend/.env.example` (VITE_ prefix only)
- Never commit `.env` files

## Repo layout
```
PrivateCredit/
├── frontend/       # React app
├── backend/        # FastAPI app
├── docker/         # Dockerfiles
├── docker-compose.yml
└── docker-compose.prod.yml
```

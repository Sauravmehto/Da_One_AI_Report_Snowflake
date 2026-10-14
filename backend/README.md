# Backend

FastAPI API server for AI Reporting with Snowflake OAuth authentication.

## Prerequisites

- [Python 3.12](https://www.python.org/downloads/)
- [pipenv](https://pipenv.pypa.io/en/latest/installation.html)
- Snowflake account with OAuth security integration configured (see below)
- Redis (production only — local dev uses in-memory sessions by default)

## Install dependencies

From the `backend/` directory:

```sh
pipenv install
```

## Snowflake OAuth setup (admin)

A Snowflake admin must create a custom OAuth security integration before auth will work.
See [Snowflake OAuth documentation](https://docs.snowflake.com/en/user-guide/oauth).

```sql
CREATE SECURITY INTEGRATION ai_reporting_oauth
  TYPE = OAUTH
  ENABLED = TRUE
  OAUTH_CLIENT = CUSTOM
  OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
  OAUTH_REDIRECT_URI = 'http://localhost:8000/api/v1/auth/callback'
  OAUTH_ISSUE_REFRESH_TOKENS = TRUE
  OAUTH_REFRESH_TOKEN_VALIDITY = 86400;
```

Then obtain the client id and secret:

```sql
SELECT SYSTEM$SHOW_OAUTH_CLIENT_SECRETS('AI_REPORTING_OAUTH');
-- or describe the integration for OAUTH_CLIENT_ID
```

Register a separate production redirect URI when deploying
(e.g. `https://api.yourdomain.com/api/v1/auth/callback`).

**Important:** The OAuth callback URL must hit the backend directly on port `8000`.
The frontend dev server proxies `/api` for API calls, but Snowflake redirects to
`OAUTH_REDIRECT_URI` which must be the backend URL.

## Environment variables

Copy the example env file and adjust as needed:

```sh
cp .env.example .env
```

| Variable | Local dev | Production |
|----------|-----------|------------|
| `SESSION_BACKEND` | `memory` | `redis` |
| `REDIS_URL` | unused | `redis://...` |
| `SNOWFLAKE_ACCOUNT` | your account locator | same |
| `SNOWFLAKE_WAREHOUSE` | compute warehouse | same |
| `OAUTH_CLIENT_ID` | from Snowflake integration | same |
| `OAUTH_CLIENT_SECRET` | from Snowflake integration | same |
| `OAUTH_REDIRECT_URI` | `http://localhost:8000/api/v1/auth/callback` | `https://api.../api/v1/auth/callback` |
| `FRONTEND_URL` | `http://localhost:5173` | `https://app...` |
| `TOKEN_ENCRYPTION_KEY` | Fernet key (see `.env.example`) | strong random key |
| `JWT_SECRET` | any dev value | strong random secret |
| `COOKIE_SECURE` | `false` | `true` |

Generate a Fernet encryption key:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Start the app

```sh
pipenv run uvicorn app.main:app --reload
```

The API runs at [http://localhost:8000](http://localhost:8000).

- Health check: `GET /health`
- Auth: `GET /api/v1/auth/login`, `GET /api/v1/auth/callback`, `GET /api/v1/auth/me`, `POST /api/v1/auth/logout`
- Sample protected route: `GET /api/v1/reports/sample`

## Session storage

Sessions are stored via a pluggable backend controlled by `SESSION_BACKEND`:

- `memory` — in-process dict (default for local development)
- `redis` — Redis-backed store for production multi-worker deployments

Switch by setting `SESSION_BACKEND=redis` and `REDIS_URL` in production `.env`.

## Other commands

```sh
pipenv run pytest          # run tests
pipenv install <package>   # add a dependency
```

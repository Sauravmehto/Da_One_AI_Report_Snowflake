# Deployment notes

Written for whoever takes this from the repo to a server. It covers the things
that are **not** obvious from the code and that will break a deployment if
missed. Everything here was verified against the current tree, not assumed.

---

## 1. Redis is mandatory, not optional

`docker/backend.Dockerfile` starts the production target with:

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

Four worker processes. The default `SESSION_BACKEND=memory` keeps sessions in
one process's memory, so a session created by worker 2 does not exist for
workers 1, 3 and 4. Requests round-robin, so a signed-in user gets a 401 on
roughly three requests in four — login appears to succeed and then drops out at
random. It is not intermittent flakiness; it is the expected behaviour of that
combination.

Set `SESSION_BACKEND=redis` and provide `REDIS_URL`. The Redis store is already
implemented; `docker-compose.prod.yml` simply has no Redis service defined yet.

The same applies to `CONVERSATION_STORE` if the app ever runs as more than one
container: the file store is fine across workers on one host (they share a
filesystem) but not across hosts.

## 2. State that must survive a redeploy

`docker-compose.prod.yml` currently mounts no volumes, so everything the app
writes lives inside the container and is destroyed on every deploy:

| Path                       | Contents                               | Consequence if lost                                                                                                                                              |
| -------------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/config/metadata/` | published semantic layers              | reverts to whatever shipped in the image; admin edits gone                                                                                                       |
| `backend/excel_storage/`   | approved templates + generated reports | **saved reports break** — their metadata is in Snowflake and survives, but the template workbook does not, and runs fail with "stored template workbook missing" |
| `backend/.conversations/`  | chat history                           | users lose their threads                                                                                                                                         |

Mount volumes for all three, or move them to shared storage. `app/semantic/`
already has a `MetadataRepository` seam intended for S3; the Excel storage does
not have one yet.

## 3. Snowflake OAuth

Users sign in with their own Snowflake identity, so queries run as them and
their row-access policies apply. There is no application-level user store.

Create a SECURITY INTEGRATION as `ACCOUNTADMIN`:

```sql
CREATE SECURITY INTEGRATION DATAALPHAONE_OAUTH
  TYPE = OAUTH
  ENABLED = TRUE
  OAUTH_CLIENT = CUSTOM
  OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
  OAUTH_REDIRECT_URI = 'https://<host>/api/v1/auth/callback'
  OAUTH_ISSUE_REFRESH_TOKENS = TRUE
  OAUTH_REFRESH_TOKEN_VALIDITY = 86400
  BLOCKED_ROLES_LIST = ('ACCOUNTADMIN','SECURITYADMIN')
  COMMENT = 'AI Reporting web app';

SELECT SYSTEM$SHOW_OAUTH_CLIENT_SECRETS('DATAALPHAONE_OAUTH');
```

Then set `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` in the environment.

Four things that catch people out:

- **Primary vs alternate redirect URIs.** Snowflake custom OAuth has one
  `OAUTH_REDIRECT_URI` plus optional `OAUTH_ALTERNATE_REDIRECT_URIS`. Localhost,
  prod (`one.${DOMAIN}`), and hosted dev (`one-dev.${DOMAIN}`) can share one
  security integration if every callback is listed. Example:

  ```sql
  ALTER SECURITY INTEGRATION DATAALPHAONE_OAUTH SET
    OAUTH_ALTERNATE_REDIRECT_URIS = (
      'https://one-dev.<DOMAIN>/api/v1/auth/callback',
      'http://localhost:8000/api/v1/auth/callback'
    );
  ```

  Set each environment’s `OAUTH_REDIRECT_URI` to the URI that environment
  actually uses. Separate integrations are fine too, but not required when
  alternates are configured.
- **HTTPS is required.** Snowflake accepts `http://` only for `localhost`.
- **`COOKIE_SECURE=true`** wherever the app is served over https, or the browser
  discards the session cookie and login silently fails.
- **`BLOCKED_ROLES_LIST`.** Snowflake blocks `ACCOUNTADMIN` and `SECURITYADMIN`
  from OAuth by default. A user whose *default* role is one of those cannot log
  in, and the error does not say why.

If the Snowflake account has SAML SSO configured, the authorize step hands off
to that identity provider automatically — users get their normal company login.

## 4. Secrets

`TOKEN_ENCRYPTION_KEY` **must be set explicitly in production.** When it is
empty, `app/main.py` generates a Fernet key at startup — which with 4 workers
means four different keys, and a refresh token encrypted by one worker cannot be
decrypted by another. Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`JWT_SECRET` must likewise be set and stable across instances and restarts.

`.env` is gitignored (verified), as are `*.p8`, `*.pem`, `rsa_key*`, `*.key`,
`excel_storage/` and `.conversations/`. Only `.env.example` is committed.

## 5. Remove the development sign-in

`DEV_LOGIN_ENABLED=true` authenticates as a shared key-pair service account and
bypasses the web app's login entirely — anyone who can reach the port gets a
session. It requires `DEBUG` *and* the flag *and* the key material, so it cannot
enable itself accidentally, but it must be off in any deployed environment.

Once OAuth is confirmed working, the code can be deleted outright: the
`/auth/dev-login` route, the key-pair branch in `SnowflakeService`, and the
config block in `app/core/config.py` (which carries a comment marking all three).

## 6. Ports

The backend listens on **8000** inside its container. Local development uses
**8001** to avoid colliding with a legacy application on 8000 — hence
`OAUTH_REDIRECT_URI=http://localhost:8001/...` in the development example. A
deployed environment should use the public URL, and the container's internal
port is a detail of the compose file.

---

## 7. Hosted dev stack (parallel to prod)

Production stays on [docker-compose.prod.yml](../docker-compose.prod.yml)
(`one.${DOMAIN}`, ECR `:latest`). Hosted **dev** is a second Portainer stack
using [docker-compose.dev.yml](../docker-compose.dev.yml)
(`one-dev.${DOMAIN}`, ECR `:dev`). Same Traefik `web` network and `DOMAIN`
stack variable; unique Traefik router names (`one-dev` / `one-dev-api`).

Images use the **prod** Dockerfile targets (nginx + uvicorn workers), not local
`--reload` stages.

### Build and push `:dev` (does not overwrite `:latest`)

`VITE_SHOW_REPORT_BUILDER` is a **frontend build-time** flag (Vite inlines it).
Setting it in Portainer `environment:` does nothing — pass it when building,
then redeploy/pull the new `:dev` frontend image.

PowerShell:

```powershell
$env:VITE_SHOW_REPORT_BUILDER = "true"
docker compose -f docker-compose.yml build backend frontend

docker tag 629143592903.dkr.ecr.us-east-1.amazonaws.com/ai-reporting-be:latest 629143592903.dkr.ecr.us-east-1.amazonaws.com/ai-reporting-be:dev
docker tag 629143592903.dkr.ecr.us-east-1.amazonaws.com/ai-reporting-fe:latest 629143592903.dkr.ecr.us-east-1.amazonaws.com/ai-reporting-fe:dev

docker push 629143592903.dkr.ecr.us-east-1.amazonaws.com/ai-reporting-be:dev
docker push 629143592903.dkr.ecr.us-east-1.amazonaws.com/ai-reporting-fe:dev
```

Bash:

```bash
VITE_SHOW_REPORT_BUILDER=true docker compose -f docker-compose.yml build backend frontend
# then tag + push :dev as above
```

### Portainer

1. Create a **new** stack from `docker-compose.dev.yml` (do not replace the prod stack).
2. Set `DOMAIN` the same as prod (e.g. `dataalpha.in`).
3. Ensure containers join the external Docker network `web`.
4. Use a **separate** Environment from prod. Required differences:

| Variable                              | Dev value                                                   |
| ------------------------------------- | ----------------------------------------------------------- |
| `FRONTEND_URL` / `WEBAPP_BASE_URL`    | `https://one-dev.${DOMAIN}`                                 |
| `CORS_ORIGINS`                        | include that origin                                         |
| `OAUTH_REDIRECT_URI`                  | `https://one-dev.${DOMAIN}/api/v1/auth/callback`            |
| `JWT_SECRET` / `TOKEN_ENCRYPTION_KEY` | **new** values — do not reuse prod                          |
| `REDIS_URL`                           | separate Redis DB index (e.g. `/1`) or a dedicated instance |
| `DEV_LOGIN_ENABLED`                   | `false` on the shared host                                  |

### DNS and Snowflake

- Point `one-dev.${DOMAIN}` at the same Traefik host as prod (Let’s Encrypt via
  existing `certresolver=le`).
- Add the dev callback to `OAUTH_ALTERNATE_REDIRECT_URIS` (see §3).

### Verify after deploying

- [ ] `https://one-dev.${DOMAIN}/` serves the UI; `/api` reaches the backend
- [ ] OAuth login completes with the dev redirect URI
- [ ] Prod `https://one.${DOMAIN}` still works with both stacks up
- [ ] Dev and prod sessions do not cross (separate JWT / encryption keys)
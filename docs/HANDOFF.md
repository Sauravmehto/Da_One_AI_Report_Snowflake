# Handoff — current state and what is outstanding

Snapshot of the repository as handed over. Written so someone who has not seen
the development can tell what works, what is deliberately unfinished, and what
must happen before a deployment.

**Verified at handoff:** 249 backend tests pass (`pytest` from `backend/`),
frontend typechecks clean (`npx tsc --noEmit` from `frontend/`).

---

## What works

| Area | State |
|---|---|
| **Chat (Q&A)** | Streaming agent over 11 tools. Answers questions, runs read-only SQL, renders charts inline, plans and saves dashboards. History persists. |
| **Semantic layer** | Per-database TOMLs describing what columns mean, which tables may be joined, and which columns date filters may bind to. Injected into the model's context as a cached block. |
| **Semantic layer admin** | `/admin/semantic` — edit a layer, save a draft, publish. Publishing validates against the live schema and takes effect without a restart. |
| **Dashboards** | Saved, versioned, re-runnable against live data. |
| **Excel reports** | Upload a workbook, classify its sheets and formulas, approve as a template, bind to parameterised SQL, run for a period, download. Formulas re-extend to the new row count. |
| **Auth** | Snowflake OAuth implemented end to end — but not yet configured (see below). |

## Blocking before anyone can log in

**Snowflake OAuth is written and inert.** It needs two environment values that
come from a Snowflake SECURITY INTEGRATION, which requires `ACCOUNTADMIN` to
create. The exact SQL and the four things that catch people out are in
[DEPLOYMENT.md](./DEPLOYMENT.md#3-snowflake-oauth).

Until then the app uses `DEV_LOGIN_ENABLED=true`, a key-pair service-account
login that **bypasses authentication entirely** — anyone who can reach the port
gets a session. It is gated behind `DEBUG` plus an explicit flag plus the key
material, so it cannot switch itself on, but **it must be off in any deployed
environment**.

## Blocking before a deployment survives

Both are in [DEPLOYMENT.md](./DEPLOYMENT.md) with the reasoning:

1. **Redis is mandatory.** The production container runs `uvicorn --workers 4`
   while sessions default to in-process memory. Users appear to log in and are
   then logged out on roughly three requests in four. The Redis store is already
   implemented; the compose file has no Redis service.
2. **Volumes are mandatory.** Nothing is mounted, so published semantic layers,
   Excel templates and chat history are destroyed on every redeploy — and saved
   Excel reports break, because their metadata lives in Snowflake and survives
   while their template workbook does not.

## Known gaps, deliberately left

Not defects — decisions taken to ship the working path first.

- **No authorisation on `/admin`.** Any authenticated user can reach the
  semantic layer editor and report builder. Acceptable while everything runs as
  one service account; needs a role check before real users exist.
- **Excel templates from the previous MCP project were not migrated.** Two saved
  reports exist in Snowflake whose template workbooks are not in this repo, so
  running them fails with "stored template workbook missing". Either copy the
  old `excel_storage/templates/` across or re-upload the workbooks.
- **Report builder has no scan overrides.** Sheet roles and formula tiers are
  classified heuristically and shown read-only. A misclassified sheet cannot
  currently be corrected. The engine supports it; the UI does not.
- **Report builder takes SQL as free text.** It shows the data sheet's expected
  column names beside the editor, but there is no assistant in that screen yet.
- **Chat cannot answer questions about report figures — by design, for now.**
  A report's formula sheets hold business logic that exists nowhere else, so
  letting the assistant re-derive those numbers would produce a second,
  disagreeing answer. Nothing currently *stops* it doing so; a "do not derive"
  guardrail is the intended next step.
- **Semantic layer has no metrics editor.** Named metrics round-trip safely
  through save and publish but cannot be edited in the UI.
- **Comments in TOML files do not survive a publish through the admin UI.**
  `tomllib` reads them; `tomli_w` cannot write them. Swapping in `tomlkit` would
  fix it and is contained to `app/semantic/document.py`.
- **Single database vendor.** A connector interface and registry exist with
  Snowflake as the only implementation. Adding a second vendor should start with
  connection *instances* — semantic layers are keyed by bare database name today,
  so two vendors both having `PRIVATECREDIT` would collide.

## Things worth knowing before changing code

- **Every tool call is audit-logged**, including failures. Preserve that.
- **SQL is gated by an AST parser** (`app/engine/sql_safety.py`), not keyword
  matching. A regex gate was replaced because it allowed `SELECT ... INTO`
  (a write on SQL Server) while blocking legitimate queries containing words
  like `MERGE`. Do not reintroduce keyword filtering.
- **Date filters bind by declared role**, never by guessing a column name. A
  table with no `date_roles` cannot be date-filtered at all — that is deliberate.
- **The semantic layer's job is to refuse.** It declines undocumented joins and
  terms that are not in the glossary. Answers that look plausible but are
  unverifiable are the failure mode this product exists to prevent.
- **Unmodelled TOML sections are preserved server-side** in
  `SemanticLayerService._carry_unmodelled`. Without it, one save through the
  admin UI silently deletes any section the editor has no form for. This
  happened for real during development.
- **Generated Excel reports ship formulas, not values.** They recalculate when
  opened. A consequence: preview-only tools (Outlook, SharePoint, Quick Look)
  show formula cells blank until the file is opened in a spreadsheet
  application.

## Running locally

```bash
# backend  (http://localhost:8001)
cd backend && python -m venv .venv && .venv/Scripts/pip install -r <deps>
.venv/Scripts/python -m uvicorn app.main:app --port 8001

# frontend (http://localhost:5173)
cd frontend && npm install && npm run dev
```

Copy `backend/.env.example` to `backend/.env` and fill it in. Port 8001 is used
locally to avoid colliding with a legacy application on 8000; the container
listens on 8000.

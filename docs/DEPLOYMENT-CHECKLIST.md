# Deployment checklist — changes in this cycle

What the latest changes need that a previous deployment did not. For the
pre-existing blockers (Redis, volumes, secrets) see [DEPLOYMENT.md](./DEPLOYMENT.md);
they still apply and are not repeated here.

Snowflake-side objects are in [SNOWFLAKE-SETUP.md](./SNOWFLAKE-SETUP.md). Do those first —
two of the steps below depend on them.

---

## 1. New environment variable: `ADMIN_ROLES`

Snowflake roles allowed into the semantic layer editor.

```bash
ADMIN_ROLES=DATAALPHA_ADMIN
```

Comma-separated or a JSON list. Matched case-insensitively against the user's
**active** role.

- **Empty means nobody.** Deliberate: a deployment that forgets this locks the
  admin surface rather than opening it to everyone. There is no default.
- Admin grants the ability to **author**, not wider data access. An admin still
  only sees layers for databases their own role can reach — so the role named
  here must itself hold `USAGE` on those databases, or the editor shows an
  empty list. This is the most common way to get this wrong.

## 2. Changed meaning: `SNOWFLAKE_DEFAULT_ROLE`

**Check this value on any existing deployment before shipping.**

It used to be applied to every session. That flattened all users onto one role:
two people with different grants got identical access, and the app looked like
it was enforcing per-user permissions when it was not.

It now applies **only to the dev key-pair service account**, which has no user
to inherit a role from. A real signed-in user connects under their own Snowflake
role.

| Situation | Action |
|---|---|
| Set on a deployed environment | Safe to leave, but it no longer does anything for signed-in users. Clearing it avoids confusion. |
| You relied on it to scope access | It was not scoping anything. Grant per-role access in Snowflake instead. |
| Dev login (`DEV_LOGIN_ENABLED=true`) | Keep it — the service account needs it. |

## 3. Nothing else new

No other new variables. The dashboard chat reuses the existing
`ANTHROPIC_API_KEY` and conversation store.

One knock-on: dashboard chats are stored as conversations, so the
`.conversations/` volume item in [DEPLOYMENT.md §2](./DEPLOYMENT.md) now matters
more than it did — losing that volume loses dashboard threads as well as chats.

---

## Deploy

1. Apply [SNOWFLAKE-SETUP.md](./SNOWFLAKE-SETUP.md).
2. Set `ADMIN_ROLES` in the environment.
3. Review `SNOWFLAKE_DEFAULT_ROLE` per the table above.
4. Deploy and restart. `ADMIN_ROLES` is read once at startup.

## Verify after deploying

| Check | Expected |
|---|---|
| `GET /health` | `{"status":"ok"}` |
| Sign in, `GET /api/v1/auth/me` | `role` is **your own** Snowflake role, not the configured one |
| Same, as an admin | `is_admin: true`; **Configuration** appears in the sidebar |
| Same, as a non-admin | `is_admin: false`; no Configuration section; `/admin/semantic` shows "Administrator access required" |
| `GET /api/v1/databases` as a scoped user | Only their databases |
| Ask chat about a database they cannot see | `403`, and no schema details in the reply |

If `role` comes back as the value of `SNOWFLAKE_DEFAULT_ROLE` for every user,
the old build is still running.

## Rollback

Nothing here is a data migration — the only state change is an optional
`dashboard_id` field on stored conversations, which older builds ignore.
Rolling back is a redeploy.

---

## Dashboard grouping: `DATABASE_NAME` on `MCP_SAVED_DASHBOARDS`

The dashboard list is now a tree of data groups, and a dashboard's group is the
database it was built from. That database was never stored — it was inferred by
scanning each saved plan's SQL — so it becomes a column.

Run `backend/sql/dashboard_database_name.sql` as a role that **owns** the table.
`ALTER TABLE` needs ownership, not INSERT/SELECT, so the application role cannot
do it. Point the statements at whatever `MCP_SAVED_DASHBOARDS_TABLE` names in
that environment (the file is written against `LOAN.PUBLIC`).

The script adds the column, backfills existing rows by the same rule the old
inference used, and finishes with a count per database so you can see what it
attributed.

**The application does not require this to have run.** Without the column it
logs a warning once and lists every dashboard under "Ungrouped"; ownership and
the created/edited columns work either way. Run it to get the grouping, not to
avoid an outage.

---

## Token accounting: usage columns on `MCP_PROMPT_LOG`

The audit table records who asked what. It did not record what the answer cost,
so every question about spend was guesswork. Seven columns fix that: `MODEL`,
`INPUT_TOKENS`, `OUTPUT_TOKENS`, `CACHE_READ_TOKENS`, `CACHE_WRITE_TOKENS`,
`CONVERSATION_ID` and `MODEL_CALLS`.

Run `backend/sql/audit_token_usage.sql` as a role that **owns** the table, the
same ownership requirement as the dashboard migration above. Point it at
whatever `MCP_AUDIT_TABLE` names in that environment (the file is written
against `LOAN.AUDIT`). There is no backfill — the historical counts were never
captured.

Each chat turn now writes one row with `TOOL_NAME = 'agent_turn'`, aggregated
across the turn's tool rounds rather than one row per model call. `MODEL_CALLS`
keeps the round count. The per-tool rows are unchanged and do not carry
`CONVERSATION_ID`.

**The application does not require this to have run.** The first audit write on
an unmigrated table is rejected, logged once, and immediately retried without
the new columns; every later write skips them. Tool calls keep being audited
either way — only the cost columns are lost.

**Reading the columns.** `INPUT_TOKENS` is not the prompt size — the API reports
it as the *uncached remainder*, so with conversation caching working it collapses
to single digits and the volume appears in `CACHE_READ_TOKENS` /
`CACHE_WRITE_TOKENS` instead. The prompt is the sum of all three. `CACHE_READ_TOKENS`
is the check on conversation caching: a healthy multi-turn thread reads far more
than it writes, and reads near zero across many turns means something is
invalidating the prefix and the caching is costing 1.25x rather than saving.

`EVENT_TS` is `TIMESTAMP_TZ` and keeps the offset of the session that wrote each
row, so setting the account timezone changes new rows only and never the ones
already there. Read it through `CONVERT_TIMEZONE('UTC', EVENT_TS)` to get every
row on one clock. The file ends with two queries that do both of these.

# Snowflake setup

What must exist in Snowflake for the current build. All of it needs
`ACCOUNTADMIN`.

The app has **no user store**. Who someone is, which databases they see, and
whether they can edit semantic layers all come from their Snowflake identity and
role. Everything below is therefore a prerequisite, not a nice-to-have.

---

## 1. OAuth security integration — one per environment

Snowflake custom OAuth allows exactly **one** redirect URI per integration, so
production and local development each need their own. They cannot share one.

```sql
CREATE SECURITY INTEGRATION DATAALPHAONE_OAUTH
  TYPE = OAUTH
  ENABLED = TRUE
  OAUTH_CLIENT = CUSTOM
  OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
  OAUTH_REDIRECT_URI = 'https://one.dataalpha.in/api/v1/auth/callback'
  OAUTH_ISSUE_REFRESH_TOKENS = TRUE
  OAUTH_REFRESH_TOKEN_VALIDITY = 86400
  BLOCKED_ROLES_LIST = ('ACCOUNTADMIN','SECURITYADMIN')
  COMMENT = 'AI Reporting web app';

SELECT SYSTEM$SHOW_OAUTH_CLIENT_SECRETS('DATAALPHAONE_OAUTH');
```

For local development, the same again with a different name and:

```sql
  OAUTH_REDIRECT_URI = 'http://localhost:8001/api/v1/auth/callback'
```

`http://` is accepted **only** for localhost. Put the client id and secret into
that environment's `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET`.

## 2. Every user needs a DEFAULT_ROLE

**New requirement.** The app no longer forces a role onto sessions — it connects
under the user's own default role, which is what makes per-user access work.

```sql
ALTER USER analyst_priya SET DEFAULT_ROLE = PRIVATE_CREDIT_ANALYST;
```

A user with no usable default role signs in and sees nothing, with no error
explaining why.

`ACCOUNTADMIN` and `SECURITYADMIN` are blocked from OAuth by
`BLOCKED_ROLES_LIST`. A user whose **default** role is one of those cannot log
in at all, and the failure does not say so.

## 3. Roles for the databases people should see

One role per audience. `SHOW DATABASES` returns only what the role can see, and
that is what the app lists, queries and lets the assistant read.

```sql
CREATE ROLE PRIVATE_CREDIT_ANALYST;
GRANT USAGE ON DATABASE PRIVATECREDIT TO ROLE PRIVATE_CREDIT_ANALYST;
GRANT USAGE ON ALL SCHEMAS IN DATABASE PRIVATECREDIT TO ROLE PRIVATE_CREDIT_ANALYST;
GRANT SELECT ON ALL TABLES IN DATABASE PRIVATECREDIT TO ROLE PRIVATE_CREDIT_ANALYST;
GRANT SELECT ON FUTURE TABLES IN DATABASE PRIVATECREDIT TO ROLE PRIVATE_CREDIT_ANALYST;
GRANT USAGE ON WAREHOUSE <WH> TO ROLE PRIVATE_CREDIT_ANALYST;

GRANT ROLE PRIVATE_CREDIT_ANALYST TO USER analyst_priya;
ALTER USER analyst_priya SET DEFAULT_ROLE = PRIVATE_CREDIT_ANALYST;
```

Repeat per persona. A demo persona for a private credit fund gets this role and
sees only `PRIVATECREDIT`.

## 4. An admin role for semantic layer editing

```sql
CREATE ROLE DATAALPHA_ADMIN;

-- Must be able to READ what it administers, or the editor is empty.
GRANT USAGE ON DATABASE PRIVATECREDIT TO ROLE DATAALPHA_ADMIN;
GRANT USAGE ON ALL SCHEMAS IN DATABASE PRIVATECREDIT TO ROLE DATAALPHA_ADMIN;
GRANT SELECT ON ALL TABLES IN DATABASE PRIVATECREDIT TO ROLE DATAALPHA_ADMIN;
GRANT SELECT ON FUTURE TABLES IN DATABASE PRIVATECREDIT TO ROLE DATAALPHA_ADMIN;
GRANT USAGE ON WAREHOUSE <WH> TO ROLE DATAALPHA_ADMIN;

GRANT ROLE DATAALPHA_ADMIN TO USER admin_sam;
ALTER USER admin_sam SET DEFAULT_ROLE = DATAALPHA_ADMIN;
```

Then set `ADMIN_ROLES=DATAALPHA_ADMIN` in the app environment.

Two things that catch people out:

- **The app checks the ACTIVE role, not every role the user holds.** An admin
  role that is not the user's default grants nothing. Set it as their default.
- **The admin role needs its own database grants.** Admin means "may author",
  not "may see more" — the same role decides both. A `DATAALPHA_ADMIN` with no
  `USAGE` grants opens the editor to an empty database list.

To administer several databases, grant the admin role read access on each.

## 5. Saved dashboards table

Only if this is a fresh account — `backend/sql/saved_dashboards.sql` creates the
table saved dashboards are stored in. Existing deployments already have it.

---

## Verify

```sql
-- Does the persona see only what it should?
SHOW GRANTS TO ROLE PRIVATE_CREDIT_ANALYST;

-- Is the default role set, and not a blocked one?
DESC USER analyst_priya;   -- check DEFAULT_ROLE

-- Is the integration's redirect URI exactly the app's callback?
DESC SECURITY INTEGRATION DATAALPHAONE_OAUTH;
```

Then sign in as that user and confirm `GET /api/v1/auth/me` reports their own
role — not the value of `SNOWFLAKE_DEFAULT_ROLE`.

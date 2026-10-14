-- Saved-dashboard persistence for the DataAlpha MCP server (Phase: dashboards).
--
-- One row per saved dashboard. DASHBOARD_PLAN is the exact, approved plan the
-- user confirmed in Claude Desktop: a JSON object {title, panels:[...]} where
-- each panel carries title/sql/chart_type/x/y/color/orientation/display. Storing
-- the whole plan as VARIANT keeps the saved report a faithful record of what was
-- approved and lets run_saved_dashboard re-execute it with zero new LLM calls.
--
-- REPORT_ID is generated application-side (uuid4 hex) and is the STABLE
-- identifier shared across all versions of a dashboard. With version history it
-- is NO LONGER unique per row — VERSION_ID (report_id + '_vN') is the unique row
-- key. Each edit inserts a new row (VERSION incremented, IS_CURRENT=TRUE) and
-- demotes the previous current row (IS_CURRENT=FALSE); the old row is preserved,
-- never overwritten, so a revert is just a pointer change. Exactly one row per
-- REPORT_ID has IS_CURRENT=TRUE, and that is the row run_saved_dashboard, the
-- webapp, and list_saved_dashboards use. Run an admin session once:
--   (replace the warehouse/role if your service identity differs)

CREATE TABLE IF NOT EXISTS LOAN.PUBLIC.MCP_SAVED_DASHBOARDS (
    REPORT_ID      STRING         NOT NULL,           -- app-generated uuid4 hex; stable across versions
    VERSION_ID     STRING,                            -- unique row key: REPORT_ID || '_v' || VERSION
    VERSION        NUMBER(38,0)   DEFAULT 1,          -- 1, 2, 3, ... per REPORT_ID
    IS_CURRENT     BOOLEAN        DEFAULT TRUE,        -- exactly one TRUE row per REPORT_ID
    EDIT_NOTE      STRING,                            -- changes_description for this version (NULL for v1)
    REPORT_TITLE   STRING         NOT NULL,
    DESCRIPTION    STRING,
    TAGS           ARRAY,                             -- e.g. ["risk","monthly"]
    DASHBOARD_PLAN VARIANT        NOT NULL,           -- {title, panels:[{title,sql,chart_type,x,y,color,size,orientation,display}]}
    CREATED_BY     STRING,                            -- snowflake_client.session_identity()[0]
    CREATED_AT     TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP(),
    LAST_RUN_AT    TIMESTAMP_NTZ,
    RUN_COUNT      NUMBER(38,0)   DEFAULT 0,
    IS_ACTIVE      BOOLEAN        DEFAULT TRUE,
    CONSTRAINT PK_MCP_SAVED_DASHBOARDS PRIMARY KEY (VERSION_ID)
);

-- Version-history columns were added out-of-band to an existing table with:
--   ALTER TABLE LOAN.PUBLIC.MCP_SAVED_DASHBOARDS ADD COLUMN
--     VERSION_ID STRING, VERSION NUMBER(38,0) DEFAULT 1,
--     IS_CURRENT BOOLEAN DEFAULT TRUE, EDIT_NOTE STRING;
--   UPDATE LOAN.PUBLIC.MCP_SAVED_DASHBOARDS
--     SET VERSION_ID = REPORT_ID || '_v1', VERSION = 1, IS_CURRENT = TRUE
--     WHERE VERSION_ID IS NULL;   -- backfill existing rows as version 1, current

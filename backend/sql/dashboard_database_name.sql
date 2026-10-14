-- Group saved dashboards by the database they were built from.
--
-- The database was never stored: it was inferred on demand by scanning each
-- panel's SQL for a documented database name. That is fine for one dashboard
-- and wrong for a list, so it becomes a column.
--
-- Run as a role that OWNS the table. ALTER requires ownership, not just
-- INSERT/SELECT — the application role cannot do this.
--
--   Dev : DATAALPHAONE_DEV.PUBLIC.MCP_SAVED_DASHBOARDS
--   Prod: whatever MCP_SAVED_DASHBOARDS_TABLE points at
--
-- Safe to re-run: the ADD COLUMN is guarded, and the backfill only touches
-- rows that have no value yet.

ALTER TABLE LOAN.PUBLIC.MCP_SAVED_DASHBOARDS
    ADD COLUMN IF NOT EXISTS DATABASE_NAME STRING;

-- Where a dashboard is FILED, when that differs from where its data comes from.
--
-- DATABASE_NAME is a fact about the SQL and is never edited. DATA_GROUP is a
-- filing decision a person makes — moving a dashboard into another folder, or
-- into a named group that is not a database at all (the DataGroup idea from the
-- entitlement model). Empty for almost every row: the database is the default,
-- and this only records a deliberate departure from it.
ALTER TABLE LOAN.PUBLIC.MCP_SAVED_DASHBOARDS
    ADD COLUMN IF NOT EXISTS DATA_GROUP STRING;

-- Backfill every existing version row by the same rule the application used to
-- apply at read time: the first documented database whose name appears as a
-- table qualifier anywhere in the stored plan.
--
-- Ordered by specificity is not necessary here — the names do not overlap — but
-- a plan that references two databases takes the first match, exactly as the
-- old inference did.
UPDATE LOAN.PUBLIC.MCP_SAVED_DASHBOARDS
SET DATABASE_NAME = CASE
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%ECOMMERCE_DB.%' THEN 'ECOMMERCE_DB'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%FINTECH_DB.%' THEN 'FINTECH_DB'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%HOSPITALITY_DB.%' THEN 'HOSPITALITY_DB'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%LOAN.%' THEN 'LOAN'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%LOGISTIC.%' THEN 'LOGISTIC'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%LOGISTICS_DB.%' THEN 'LOGISTICS_DB'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%PAYMENTS_DB.%' THEN 'PAYMENTS_DB'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%PRIVATECREDIT.%' THEN 'PRIVATECREDIT'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%PRIVATEEQUITY.%' THEN 'PRIVATEEQUITY'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%REALESTATEFUND.%' THEN 'REALESTATEFUND'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%RETAIL_DB.%' THEN 'RETAIL_DB'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%SNOWFLAKE_PUBLIC_DATA_FREE.%' THEN 'SNOWFLAKE_PUBLIC_DATA_FREE'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%US_REAL_ESTATE_DATA.%' THEN 'US_REAL_ESTATE_DATA'
        WHEN UPPER(TO_VARCHAR(DASHBOARD_PLAN)) LIKE '%_DEFAULT.%' THEN '_DEFAULT'
        ELSE NULL
    END
WHERE DATABASE_NAME IS NULL;

-- How many are still unattributed? A plan that names no documented database
-- (hand-written SQL, or a database removed from the semantic layer) groups
-- under "Ungrouped" in the UI rather than disappearing.
SELECT
    COALESCE(DATA_GROUP, DATABASE_NAME, '(none)') AS FOLDER,
    COUNT(*)                          AS VERSION_ROWS,
    COUNT(DISTINCT REPORT_ID)         AS DASHBOARDS
FROM LOAN.PUBLIC.MCP_SAVED_DASHBOARDS
WHERE IS_ACTIVE = TRUE AND IS_CURRENT = TRUE
GROUP BY 1
ORDER BY 3 DESC;

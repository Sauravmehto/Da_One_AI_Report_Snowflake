-- Token accounting for the audit log.
--
-- Every model call is billed on input, output, cache-read and cache-write
-- tokens, and none of those were recorded anywhere. The agent loop already
-- computed them per iteration and logged them to stderr, where they were lost
-- on the next restart. These columns are where they go instead.
--
-- Run as a role that OWNS the table. ALTER requires ownership, not just
-- INSERT/SELECT — the application role cannot do this.
--
--   Default: LOAN.AUDIT.MCP_PROMPT_LOG
--   Override: whatever MCP_AUDIT_TABLE points at
--
-- Safe against a running deployment: write_audit() builds its INSERT from an
-- explicit column list, so code that predates this migration keeps working and
-- simply leaves these columns NULL. There is no SELECT * against this table.
--
-- Safe to re-run: every ADD COLUMN is guarded. There is no backfill — the
-- historical token counts were never captured and cannot be recovered.

ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS MODEL STRING;

ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS INPUT_TOKENS NUMBER(38,0);

ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS OUTPUT_TOKENS NUMBER(38,0);

ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS CACHE_READ_TOKENS NUMBER(38,0);

-- Cache WRITES, not just reads. A read is billed at ~0.1x input and a write at
-- 1.25x, so reads alone cannot tell you whether caching is paying for itself —
-- a conversation that writes every turn and never reads costs MORE than no
-- caching at all. This column is how the conversation-caching change gets
-- judged rather than assumed.
ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS CACHE_WRITE_TOKENS NUMBER(38,0);

-- SESSION_ID is one UUID per server PROCESS, so under four uvicorn workers it
-- identifies the worker, not a user's thread. Nothing previously grouped an
-- audit row to the conversation it belonged to, which made "what did this
-- thread cost" unanswerable. This is that key. Written on agent_turn rows,
-- and on the tool, failure and ai:* rows written inside a conversation.
ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS CONVERSATION_ID STRING;

-- How many model round-trips one user question took. A turn is logged once,
-- aggregated, rather than once per tool round — this keeps the round count the
-- aggregate would otherwise hide. It is also the number that decides whether
-- capping conversation history is worth doing: a long thread is only a cost
-- problem if turns re-send a large transcript many times over.
ALTER TABLE LOAN.AUDIT.MCP_PROMPT_LOG
    ADD COLUMN IF NOT EXISTS MODEL_CALLS NUMBER(38,0);

-- ── Verify ───────────────────────────────────────────────────────────────

DESCRIBE TABLE LOAN.AUDIT.MCP_PROMPT_LOG;

SELECT EVENT_ID, EVENT_TS, TOOL_NAME, STATUS, DURATION_MS,
       MODEL, INPUT_TOKENS, OUTPUT_TOKENS,
       CACHE_READ_TOKENS, CACHE_WRITE_TOKENS,
       CONVERSATION_ID, MODEL_CALLS
FROM LOAN.AUDIT.MCP_PROMPT_LOG
ORDER BY EVENT_ID DESC
LIMIT 10;

-- ── Reading these columns ────────────────────────────────────────────────
--
-- INPUT_TOKENS IS NOT THE PROMPT SIZE. The API reports it as the UNCACHED
-- REMAINDER — the tail after the last cache breakpoint — so once conversation
-- caching is working it collapses to single digits. That is the feature, not
-- the traffic disappearing. The prompt is:
--
--     INPUT_TOKENS + CACHE_READ_TOKENS + CACHE_WRITE_TOKENS
--
-- Priced (relative to one uncached input token): a cache READ is ~0.1x and a
-- cache WRITE is 1.25x. Caching pays for itself the second time a prefix is
-- used; a prompt that writes every turn and never reads costs MORE than no
-- caching at all, which is the failure the last column below is for.
--
-- EVENT_TS is TIMESTAMP_TZ and keeps the offset of the session that wrote it,
-- so rows written before the account moved to UTC still render in the old
-- zone. CONVERT_TIMEZONE puts every row on one clock.

-- Spend per day, and whether prompt caching is paying off.
--
-- SELECT DATE_TRUNC('day', CONVERT_TIMEZONE('UTC', EVENT_TS))  AS DAY_UTC,
--        MODEL,
--        COUNT(*)                                              AS TURNS,
--        SUM(MODEL_CALLS)                                      AS CALLS,
--        SUM(ZEROIFNULL(INPUT_TOKENS)
--            + ZEROIFNULL(CACHE_READ_TOKENS)
--            + ZEROIFNULL(CACHE_WRITE_TOKENS))                 AS PROMPT_TOKENS,
--        SUM(ZEROIFNULL(CACHE_READ_TOKENS))                    AS FROM_CACHE,
--        SUM(ZEROIFNULL(CACHE_WRITE_TOKENS))                   AS WRITTEN,
--        SUM(ZEROIFNULL(OUTPUT_TOKENS))                        AS OUTPUT_TOKENS,
--        -- Billed input units vs. what the same prompts would have cost with
--        -- no caching at all. Below 1.0 the caching is winning.
--        ROUND(
--            (SUM(ZEROIFNULL(INPUT_TOKENS))
--             + 0.1  * SUM(ZEROIFNULL(CACHE_READ_TOKENS))
--             + 1.25 * SUM(ZEROIFNULL(CACHE_WRITE_TOKENS)))
--            / NULLIF(SUM(ZEROIFNULL(INPUT_TOKENS)
--                         + ZEROIFNULL(CACHE_READ_TOKENS)
--                         + ZEROIFNULL(CACHE_WRITE_TOKENS)), 0)
--        , 3)                                                  AS COST_RATIO
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE TOOL_NAME = 'agent_turn'
-- GROUP BY 1, 2
-- ORDER BY 1 DESC, 2;

-- The most expensive conversations, and how much of each was re-sent history.
-- A healthy multi-turn thread has FROM_CACHE far larger than WRITTEN; if the
-- two are comparable, something is invalidating the prefix every turn.
--
-- SELECT CONVERSATION_ID,
--        COUNT(*)                            AS TURNS,
--        SUM(MODEL_CALLS)                    AS CALLS,
--        SUM(ZEROIFNULL(INPUT_TOKENS)
--            + ZEROIFNULL(CACHE_READ_TOKENS)
--            + ZEROIFNULL(CACHE_WRITE_TOKENS)) AS PROMPT_TOKENS,
--        SUM(ZEROIFNULL(CACHE_READ_TOKENS))  AS FROM_CACHE,
--        SUM(ZEROIFNULL(CACHE_WRITE_TOKENS)) AS WRITTEN
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE TOOL_NAME = 'agent_turn' AND CONVERSATION_ID IS NOT NULL
-- GROUP BY 1
-- ORDER BY 4 DESC
-- LIMIT 25;

-- ── Every AI call, not just the chat ─────────────────────────────────────
--
-- The queries above read agent_turn rows: the chat's own model calls. Every
-- other model call writes its own row, named ai:<what> -- ai:dashboard_plan,
-- ai:dashboard_edit, ai:dashboard_summary, ai:panel_description,
-- ai:generate_sql, ai:classify_question, ai:summarize_result,
-- ai:document_review, ai:excel_mapping, ai:ai_query, and the file_ variants of
-- the planner and editor. A planner call made during a chat turn is its own
-- row, not part of the turn's figures, so the two never double count.
--
-- Rows before this change: ai:* rows do not exist, AUDIT_USER is the server's
-- machine login, AUTH_MODE is 'service_account' and SESSION_ID is one value
-- per server process. From it on: AUDIT_USER is the signed-in Snowflake user,
-- AUTH_MODE is how they signed in ('oauth' in production) and SESSION_ID is a
-- one-way reference to their login -- never the session cookie itself.
--
-- Always sort by EVENT_TS, not EVENT_ID: the autoincrement does not promise to
-- follow insert time across concurrent writers.

-- Where the tokens go, by feature and model.
--
-- SELECT TOOL_NAME,
--        MODEL,
--        COUNT(*)                                              AS ROWS_,
--        SUM(ZEROIFNULL(MODEL_CALLS))                          AS CALLS,
--        SUM(ZEROIFNULL(INPUT_TOKENS)
--            + ZEROIFNULL(CACHE_READ_TOKENS)
--            + ZEROIFNULL(CACHE_WRITE_TOKENS))                 AS PROMPT_TOKENS,
--        SUM(ZEROIFNULL(OUTPUT_TOKENS))                        AS OUTPUT_TOKENS,
--        COUNT_IF(STATUS = 'error')                            AS FAILED
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE MODEL IS NOT NULL
--   AND EVENT_TS >= DATEADD('day', -30, CURRENT_TIMESTAMP())
-- GROUP BY 1, 2
-- ORDER BY PROMPT_TOKENS DESC;

-- Who is spending it.
--
-- SELECT AUDIT_USER,
--        COUNT(DISTINCT SESSION_ID)                            AS LOGINS,
--        SUM(ZEROIFNULL(INPUT_TOKENS)
--            + ZEROIFNULL(CACHE_READ_TOKENS)
--            + ZEROIFNULL(CACHE_WRITE_TOKENS))                 AS PROMPT_TOKENS,
--        SUM(ZEROIFNULL(OUTPUT_TOKENS))                        AS OUTPUT_TOKENS
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE MODEL IS NOT NULL AND AUTH_MODE <> 'service_account'
-- GROUP BY 1
-- ORDER BY PROMPT_TOKENS DESC;

-- What failed: tools that returned an error, and model calls that did.
--
-- SELECT CONVERT_TIMEZONE('UTC', EVENT_TS) AS EVENT_UTC,
--        AUDIT_USER, TOOL_NAME, CONVERSATION_ID, USER_QUESTION, ERROR_MESSAGE
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE STATUS = 'error'
-- ORDER BY EVENT_TS DESC
-- LIMIT 100;

-- One conversation end to end: its turns, the tools and AI calls inside it.
--
-- SELECT CONVERT_TIMEZONE('UTC', EVENT_TS) AS EVENT_UTC, TOOL_NAME, STATUS,
--        MODEL, INPUT_TOKENS, CACHE_READ_TOKENS, CACHE_WRITE_TOKENS,
--        OUTPUT_TOKENS, DURATION_MS, ERROR_MESSAGE
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE CONVERSATION_ID = '<conversation id>'
-- ORDER BY EVENT_TS;

-- How often planning drops panels, and why. One row per plan: STATUS 'ok' when
-- every panel survived its dry-run, 'partial' when some were dropped, with the
-- count and each panel's reason in ERROR_MESSAGE. This is the number that
-- decides whether a repair attempt per failing panel is worth its extra calls.
--
-- SELECT DATE_TRUNC('week', CONVERT_TIMEZONE('UTC', EVENT_TS)) AS WEEK_UTC,
--        COUNT(*)                                             AS PLANS,
--        COUNT_IF(STATUS = 'partial')                         AS PLANS_THAT_DROPPED,
--        ROUND(100 * COUNT_IF(STATUS = 'partial') / COUNT(*), 1) AS PCT_DROPPED,
--        SUM(ROW_COUNT)                                       AS PANELS_KEPT
-- FROM LOAN.AUDIT.MCP_PROMPT_LOG
-- WHERE TOOL_NAME = 'plan_dashboard' AND STATUS IN ('ok', 'partial')
-- GROUP BY 1
-- ORDER BY 1 DESC;

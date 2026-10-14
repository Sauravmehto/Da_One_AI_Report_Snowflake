"""Tool surface for the agent loop.

Ported from the MCP server's 20 tools, deliberately narrowed to 10. Two
principles carried over from that implementation, both load-bearing:

**Errors are returned, never raised.** A rejected query or a failed plan comes
back as {"error": "..."} so the model reads the reason and corrects itself on
the next turn. Raising would abort the loop and strand the user.

**Every call is audited, success or failure.** Including the ones that error
out. The audit row records the user's ORIGINAL question, not the SQL that
resulted from it, so the trail reflects what was actually asked.

Descriptions are trimmed relative to the MCP docstrings — every word here is
resent on every turn — but the behavioural rules they encode are kept, because
those were debugged the hard way (date-parameter binding, the confirm-before-
save flow, the parameter-value redirect on edits).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.engine import audit, dashboards, file_dashboards, glossary as glossary_mod, llm
from app.engine.connectors.snowflake import client as snowflake_client
from app.engine.metadata_loader import metadata_store
from app.engine.sql_safety import UnsafeSQLError


@dataclass
class ToolContext:
    """Per-request state a tool may need. `database` is locked by the UI for
    the whole conversation, so tools never have to choose one.

    `excel_source` is set when the conversation is against an uploaded
    spreadsheet rather than a warehouse database. When it is, queries go through
    the DuckDB connector and the business context comes from that workbook's
    agreed definitions — the tools themselves do not branch beyond asking which
    connector to use, which is what connectors/registry.py exists for.
    """

    database: str
    snowflake_user: str | None = None
    role: str | None = None
    excel_source: Any | None = None
    #: Set when the conversation is against a whole uploaded FOLDER rather than
    #: one dataset: its spreadsheet tables and its documents are both in scope,
    #: and a question may span them.
    collection: Any | None = None

    @property
    def connector_name(self) -> str:
        if self.collection is not None or self.excel_source is not None:
            return "duckdb"
        return "snowflake"

    @property
    def connector(self):
        from app.engine.connectors import registry

        return registry.get(self.connector_name)

    @property
    def semantic_context(self) -> str | None:
        """The layer to put in the system prompt, when it is not the curated one.

        None for a warehouse, so prompts.py keeps building it from the TOML as
        before. For an uploaded spreadsheet there is no TOML to build from — its
        meaning comes from the definitions agreed for that workbook's shape.
        """
        if self.collection is not None:
            return _collection_context(self.collection)
        if self.excel_source is None:
            return None
        from app.engine import excel_sources

        return excel_sources.schema_context(self.excel_source)

    @property
    def has_documents(self) -> bool:
        from app.engine.connectors.duckdb import client as duckdb_client

        return duckdb_client.bound_documents() is not None


def _collection_context(collection: Any) -> str:
    """What the model needs to know about a folder holding several sources."""
    from app.engine import collection_documents, excel_sources

    parts = [
        f"# {collection.name} (an uploaded folder)",
        f"It holds {len(collection.datasets)} spreadsheet dataset(s) and "
        f"{collection.file_count} file(s).",
        # Stated here and not only per dataset: a folder of documents alone has
        # no dataset to carry this, and without it the model writes
        # DATABASE.SCHEMA.TABLE and every query fails on a catalog that does
        # not exist.
        "\n## Writing SQL against this folder\n"
        "This is a DuckDB database holding everything uploaded together. Use "
        "BARE table names exactly as listed below. Do NOT qualify them with a "
        "database or schema — there is no DATABASE.SCHEMA.TABLE form here and "
        "a qualified name fails with 'Catalog does not exist'. Date literals "
        "are ISO ('2026-01-31'). Call list_tables to see what is queryable.",
    ]
    for dataset in collection.datasets:
        parts.append("\n" + excel_sources.schema_context(dataset))
    documents = collection_documents.list_documents(collection.collection_id)
    if documents:
        parts.append(
            collection_documents.schema_context(collection.collection_id, documents)
        )
    return "\n".join(parts)


# --- Tool schemas -------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_database_context",
        "description": (
            "The curated business context for the current database: what tables and "
            "columns MEAN, mandatory join rules, enum values, data gotchas, and the "
            "exact SQL for named business metrics.\n\n"
            "This context is already provided in your system prompt. Call this only to "
            "re-read it deliberately — for example deep into a long conversation, or "
            "before writing unusually intricate SQL. It executes nothing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_tables",
        "description": (
            "Live table list for the current database, straight from information_schema "
            "(names and row counts). Use for 'what tables exist' questions, or to confirm "
            "a table is real before querying it. This reflects what physically EXISTS; "
            "get_database_context tells you what it MEANS."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_safe_sql",
        "description": (
            "Execute a concrete read-only SELECT and return the rows. Use this when you "
            "are composing the analysis yourself — multi-step work, a follow-up that "
            "builds on earlier results, or a query you want to inspect.\n\n"
            "Write the SQL yourself, respecting the documented join rules and metric "
            "definitions. Use fully-qualified names: DATABASE.SCHEMA.TABLE.\n\n"
            "Only a single SELECT (or WITH … SELECT) is allowed — INSERT/UPDATE/DELETE/"
            "DROP, SELECT … INTO and multi-statement queries are rejected by a parser, "
            "not a keyword filter. Results cap at 100 rows; queries time out at 30s.\n\n"
            "ALWAYS pass user_question as the person's original wording, verbatim, even "
            "if you rephrased it internally — the audit trail records what was asked, "
            "not just the SQL. If re-running or inspecting rather than answering "
            "something new, say so there (e.g. 're-running previous query')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single read-only SELECT."},
                "user_question": {
                    "type": "string",
                    "description": "The user's original question, verbatim, for the audit trail.",
                },
            },
            "required": ["sql", "user_question"],
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Search the text of the PDFs, Word files and notes uploaded in this "
            "folder, and get back the passages that match with the file and page "
            "they came from.\n\n"
            "Use this when the answer is WORDING rather than a figure — what a "
            "clause says, what a term means, whether something is mentioned at "
            "all. For a number, prefer run_safe_sql against the `facts` table "
            "or a spreadsheet table, because those are computed rather than "
            "read out of prose.\n\n"
            "Search matches words, not meaning. If a query returns nothing, try "
            "the words the document itself would use rather than a paraphrase — "
            "'termination' before 'ending the contract'.\n\n"
            "ALWAYS quote or cite the file and page when you use a passage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words likely to appear in the passage sought.",
                },
                "source_file": {
                    "type": "string",
                    "description": (
                        "Optional. Restrict to one document, by its exact file name."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "show_chart",
        "description": (
            "Run a query and render the result as a chart INLINE in this conversation. "
            "Use whenever a picture communicates better than a number or a table — a "
            "trend over time, a comparison across categories, a share of a total, or "
            "two metrics plotted against each other.\n\n"
            "This is a one-off visual for the current answer. It saves nothing and "
            "cannot be refreshed later. If the user wants something to keep, re-open "
            "and re-run, use plan_dashboard instead — do not build a dashboard just to "
            "show one chart.\n\n"
            "`x` and `y` must exactly match column aliases in your SQL. Aim for a "
            "handful of rows: aggregate in SQL rather than charting hundreds of points. "
            "After it renders, comment on what it shows — do not restate every value, "
            "the user can see them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single read-only SELECT."},
                "user_question": {
                    "type": "string",
                    "description": "The user's original question, verbatim, for the audit trail.",
                },
                "title": {"type": "string", "description": "Short chart title."},
                "chart_type": {
                    "type": "string",
                    "enum": [
                        "bar",
                        "stacked_bar",
                        "line",
                        "area",
                        "pie",
                        "donut",
                        "scatter",
                        "bubble",
                        "heatmap",
                        "radar",
                        "treemap",
                        "funnel",
                        "combo",
                        "area_line",
                        "geo_us_states",
                        "geo_countries",
                    ],
                    "description": (
                        "Choose from the SHAPE of the data, not from the wording: a "
                        "date on x means the time family, one category means the "
                        "comparison family, two categories carrying a measure means "
                        "heatmap, a place means a map, ordered subset stages mean "
                        "funnel, a single row means a KPI. Where the user named a "
                        "type and it fits, use it. Where it would misrepresent the "
                        "data, chart what reads correctly and say in one line what "
                        "you drew instead — never silently substitute, and never "
                        "refuse to chart. The notes below are what each type needs to "
                        "read correctly, not hoops to make the user jump through. "
                        "bar=independent categories compared; stacked_bar=parts that "
                        "ADD UP to a meaningful total (y is a LIST); "
                        "line=rate or ratio over time; area=volume over time; "
                        "donut/pie=share of whole (≤8 slices, never over time); "
                        "scatter/bubble=two (or three) numeric metrics per entity; "
                        "radar=ONE entity scored across 3-8 named dimensions, never "
                        "over time; "
                        "treemap=part-to-whole with more parts than a donut carries "
                        "(~10-60), never over time and never with negatives; "
                        "funnel=ORDERED stages where each is a subset of the one "
                        "before, largest first — not unrelated categories that "
                        "merely differ in size; "
                        "area_line=a measure against a target or threshold in the "
                        "SAME unit, area plus a dashed line on one shared axis "
                        "(combo instead when the two are on different scales); "
                        "combo=bars plus a line on a second axis, for a count "
                        "and a rate sharing an x axis (line measure LAST in y); "
                        "geo_us_states=a US state choropleth, ONLY when x holds US "
                        "state codes or names; "
                        "geo_countries=a country choropleth that frames itself to "
                        "whatever region the data covers, when x holds countries, "
                        "ISO codes, ports or cities anywhere in the world; "
                        "both maps need y to be ONE numeric measure; "
                        "heatmap=two categoricals with numeric intensity "
                        "(long-form: x=column category, color=row group, y=metric; "
                        "or wide: color=row entity, y=list of bucket columns). "
                        "Prefer donut over pie; use orientation h for long bar labels."
                    ),
                },
                "x": {"type": "string", "description": "Column alias for the x axis / category."},
                "y": {
                    "type": "string",
                    "description": (
                        "Column alias for the value. When the chart draws several value "
                        "columns, a comma-separated list in drawing order: stacked_bar "
                        "segments, combo (line measure LAST), area_line (target LAST), "
                        "radar, a line or bar with one series per column, or a wide "
                        "heatmap. Example: \"NPL_PCT,TARGET_NPL_PCT\"."
                    ),
                },
                "color": {
                    "type": "string",
                    "description": "Optional low-cardinality column to split into series.",
                },
                "size": {
                    "type": "string",
                    "description": "Optional numeric column driving bubble radius (bubble only).",
                },
                "orientation": {
                    "type": "string",
                    "enum": ["vertical", "h"],
                    "description": (
                        "Use 'h' when category labels are long. Omit it to let "
                        "the app decide, which also lets it fall back to "
                        "horizontal if the labels do not fit. 'vertical' keeps "
                        "the bars upright and stops that fallback."
                    ),
                },
                "column_formats": {
                    "type": "object",
                    "description": (
                        "Optional. How columns should read, keyed by column alias, the "
                        "same as a dashboard panel: {\"BALANCE_AED\": {\"format\": "
                        "\"currency\", \"currency\": \"AED\", \"label\": \"Balance\"}, "
                        "\"NPL_PCT\": {\"format\": \"percent\", \"decimals\": 2}}. format "
                        "is one of currency|percent|number|ratio|text|date. Set currency "
                        "whenever money is not in the database's own currency, and a "
                        "format whenever the alias does not make its unit obvious."
                    ),
                },
            },
            "required": ["sql", "user_question", "title", "chart_type", "x", "y"],
        },
    },
    {
        "name": "ask_data_question",
        "description": (
            "Answer ONE self-contained plain-English data question end to end: it "
            "classifies, generates the SQL, runs it with a repair retry, and returns a "
            "plain-language answer plus the SQL and rows.\n\n"
            "Prefer this for a straightforward single question where you do not need to "
            "shape the SQL yourself ('what's the delinquency rate?', 'top 5 states by "
            "exposure'). Prefer run_safe_sql when you are composing multi-step analysis, "
            "building on previous results, or need control over the exact SQL — this "
            "tool cannot see the conversation, only the question you pass it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "A single self-contained question. Include any context it needs — this tool cannot see the conversation.",
                }
            },
            "required": ["question"],
        },
    },
    {
        "name": "explain_concept",
        "description": (
            "Define a financial term or metric from the curated glossary (DSCR, TVPI, "
            "MOIC, cap rate, mark-to-par, …).\n\n"
            "Use for 'what does X mean' questions about terminology — NOT for values from "
            "the data (that's a data question). This NEVER fabricates: an unknown term "
            "returns close matches and an explicit miss rather than a guess. If it "
            "reports a miss, say the term isn't in the glossary — do not define it "
            "yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
    },
    {
        "name": "plan_dashboard",
        "description": (
            "Design a multi-panel dashboard from a description and return a plan for the "
            "user to review. Saves nothing and renders nothing.\n\n"
            "Works for Snowflake warehouse databases AND uploaded spreadsheets/folders.\n\n"
            "Every panel's SQL is safety-validated AND dry-run before the plan comes back, "
            "so columns and joins are proven to resolve. Panels that fail are DROPPED and "
            "reported in skipped_panels — if that list is non-empty you MUST tell the user "
            "which panels were dropped and why. Never present a shortened dashboard as if it "
            "were complete. An empty skipped_panels means every panel survived.\n\n"
            "Panel SQL is abbreviated in this result to save context; the full plan is held "
            "server-side under plan_id and saves intact. Present the whole plan — a large "
            "one is complete, not something to re-plan in smaller pieces.\n\n"
            "If the dashboard will be re-run for different dates or periods, the planner "
            "may declare runtime parameters instead of hardcoding values.\n\n"
            "For warehouse databases, DATE BINDING IS METADATA-DRIVEN: which column a date "
            "parameter filters comes from the semantic layer. For uploaded files/folders, "
            "use the date columns described in the source context (e.g. as_of, period_start).\n\n"
            "If planning returns status='needs_clarification', put those questions to the "
            "user and only re-plan once they answer.\n\n"
            "Chart choice is decided by the planner from a fixed cookbook (KPI for "
            "headlines, bar for category compare, stacked_bar for parts of a total, "
            "line/area for trends, donut for share, scatter/bubble for two metrics). "
            "Do not ask the user which chart type to use unless they asked for one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the dashboard should show.",
                }
            },
            "required": ["description"],
        },
    },
    {
        "name": "save_dashboard",
        "description": (
            "Persist a dashboard that was designed with plan_dashboard, using its "
            "plan_id so the exact reviewed plan is what gets stored.\n\n"
            "Call ONLY after the user has explicitly confirmed they want it saved. Never "
            "speculatively, and never as an assumed next step after planning — wait for "
            "them to say yes. (The UI also offers a Save button; either path is fine, but "
            "do not pre-empt the user's decision.)\n\n"
            "Returns webapp_url, the link to the saved dashboard. Give it to the user — "
            "opening what they just made is the next thing they want."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "report_title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["plan_id", "report_title"],
        },
    },
    {
        "name": "list_saved_dashboards",
        "description": (
            "Saved dashboards, newest first — metadata only, runs no panel SQL. Returns "
            "report_id, title, version, created_by, last_run_at, run_count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Default 50."}
            },
        },
    },
    {
        "name": "run_saved_dashboard",
        "description": (
            "Re-execute a saved dashboard's panels live and return the data plus each "
            "panel's saved chart config. No new SQL is generated.\n\n"
            "If the dashboard declares parameters, pass parameter_values to run it for "
            "specific inputs; omitted ones fall back to their saved defaults. Running "
            "with different parameter values is NOT an edit and creates no new version."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_id": {"type": "string"},
                "parameter_values": {
                    "type": "object",
                    "description": "Optional {name: value} for declared parameters.",
                },
            },
            "required": ["report_id"],
        },
    },
    {
        "name": "edit_dashboard",
        "description": (
            "Change a saved dashboard, keeping full version history. TWO STEPS: call "
            "without confirm to get a diff, review it, then call again with confirm=true.\n\n"
            "The preview regenerates ONLY the change described and returns a structured "
            "diff. Before confirming, check the diff contains ONLY what was asked — flag "
            "to the user any panel or field that moved outside the request.\n\n"
            "One call can carry many changes: adding a batch of panels, removing some and "
            "reordering others is a single changes_description, not several edits.\n\n"
            "If it returns status 'use_parameter' or 'suggest_parameter', the request was "
            "really about running for a different VALUE (a date or period), not changing "
            "structure. Do NOT edit: either re-run with run_saved_dashboard using that "
            "parameter, or offer to add the value AS a parameter. Changing a value must "
            "never create a new version.\n\n"
            "If it returns 'not_found' or 'ambiguous', stop and ask which dashboard was "
            "meant — never guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_id_or_title": {"type": "string"},
                "changes_description": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Omit/false to preview the diff; true to commit it.",
                },
            },
            "required": ["report_id_or_title", "changes_description"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_DEFINITIONS]

# Tools that WRITE — they save a dashboard or cut a new version of one.
# Withheld from the dashboard-scoped chat, which explains a dashboard rather
# than changing it. Editing stays in the main chat, where the two-step
# confirm-the-diff flow has the room it needs to be reviewed properly.
MUTATING_TOOL_NAMES = frozenset({"plan_dashboard", "save_dashboard", "edit_dashboard"})


def tool_definitions(*, read_only: bool = False) -> list[dict[str, Any]]:
    """The tool schemas for a turn. `read_only` drops the mutating tools.

    Filtered rather than flagged: a tool the model cannot see is one it cannot
    be talked into calling, whereas a tool that exists and refuses invites a
    retry loop and burns the iteration ceiling.
    """
    if not read_only:
        return TOOL_DEFINITIONS
    return [t for t in TOOL_DEFINITIONS if t["name"] not in MUTATING_TOOL_NAMES]


# --- Implementations ----------------------------------------------------------
# All are blocking: the engine wraps snowflake-connector, which has no async
# driver. dispatch() runs them on a worker thread.

def _get_database_context(ctx: ToolContext) -> dict[str, Any]:
    """What the current source MEANS.

    For a warehouse that is the hand-curated semantic layer; for an uploaded
    spreadsheet it is the definitions agreed for that workbook's shape. Same
    question, same tool, different place the answer is kept.
    """
    if ctx.excel_source is not None:
        from app.engine import excel_sources

        return {
            "database": ctx.database,
            "context": excel_sources.schema_context(ctx.excel_source),
        }
    return {"database": ctx.database, "context": metadata_store.build_context(ctx.database)}


def _list_tables(ctx: ToolContext) -> dict[str, Any]:
    if ctx.collection is not None:
        # A folder has no single catalogue to ask: its tables come from several
        # databases attached together, so the bound connection is the only
        # thing that knows what is actually visible. Listing the views rather
        # than information_schema also hides the full-text index's own internal
        # tables, which are real but are not anybody's data.
        from app.engine.connectors.duckdb import client as duckdb_client

        conn = duckdb_client.get_connection()
        try:
            rows = conn.execute(
                "SELECT view_name AS name FROM duckdb_views() "
                "WHERE database_name = 'memory' "
                "UNION "
                "SELECT table_name AS name FROM duckdb_tables() "
                "WHERE database_name = 'memory'"
            ).fetchall()
            tables = []
            for (name,) in sorted(rows):
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                tables.append({"name": name, "rows": count})
        finally:
            conn.close()
        return {"database": ctx.database, "count": len(tables), "tables": tables}
    if ctx.excel_source is not None:
        # The source record already lists every table that was loaded, with its
        # row count — no need to query the file to find out what is in it.
        tables = [
            {"name": t.name, "rows": t.row_count, "columns": len(t.columns)}
            for t in ctx.excel_source.tables
        ]
        return {"database": ctx.database, "count": len(tables), "tables": tables}
    tables = snowflake_client.list_tables(ctx.database)
    return {"database": ctx.database, "count": len(tables), "tables": tables}


def _run_safe_sql(ctx: ToolContext, sql: str, user_question: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    user, role = ctx.connector.session_identity()
    try:
        result = ctx.connector.run_query(sql)
    except UnsafeSQLError as exc:
        audit.log_event("run_safe_sql", "blocked", user, role, question=user_question,
                        sql=sql, error=str(exc), client_info="webapp",
                        duration_ms=int((time.perf_counter() - t0) * 1000))
        return {"error": f"Query rejected: {exc}"}
    except Exception as exc:  # noqa: BLE001
        audit.log_event("run_safe_sql", "error", user, role, question=user_question,
                        sql=sql, error=str(exc), client_info="webapp",
                        duration_ms=int((time.perf_counter() - t0) * 1000))
        return {"error": f"Query failed: {exc}"}

    audit.log_event("run_safe_sql", "ok", user, role, question=user_question,
                    sql=result["sql"], row_count=result["row_count"],
                    client_info="webapp",
                    duration_ms=int((time.perf_counter() - t0) * 1000))
    return result


def _show_chart(ctx: ToolContext, sql: str, user_question: str, title: str,
                chart_type: str, x: str, y: str | list[str], color: str | None = None,
                size: str | None = None, orientation: str | None = None,
                column_formats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the query and shape the result as a renderable panel.

    Returns the SAME shape a saved dashboard panel does, built the same way —
    the dashboard's config normalisation, the database's currency beside the
    rows, and the chart-type checks — so the UI renders it through the
    identical component and it looks as it would on a dashboard.
    """
    t0 = time.perf_counter()
    user, role = ctx.connector.session_identity()
    try:
        result = ctx.connector.run_query(sql)
    except UnsafeSQLError as exc:
        audit.log_event("show_chart", "blocked", user, role, question=user_question,
                        sql=sql, error=str(exc), client_info="webapp",
                        duration_ms=int((time.perf_counter() - t0) * 1000))
        return {"error": f"Query rejected: {exc}"}
    except Exception as exc:  # noqa: BLE001
        audit.log_event("show_chart", "error", user, role, question=user_question,
                        sql=sql, error=str(exc), client_info="webapp",
                        duration_ms=int((time.perf_counter() - t0) * 1000))
        return {"error": f"Query failed: {exc}"}

    columns_upper = [str(c).upper() for c in result["columns"]]

    def _resolve(name: str | None) -> str | None:
        """Match an alias case-insensitively, returning the real column name."""
        if not name:
            return None
        try:
            return result["columns"][columns_upper.index(name.upper())]
        except ValueError:
            return None

    # Several value columns arrive comma-separated (or, from some callers, as
    # a list); one stays a plain string, the shape a dashboard panel stores.
    names = y if isinstance(y, list) else str(y or "").split(",")
    names = [str(name).strip() for name in names if str(name).strip()]
    resolved = [_resolve(name) for name in names]
    y_value: Any = resolved[0] if len(resolved) == 1 else resolved

    missing = [f"x ({x})"] if not _resolve(x) else []
    missing += [f"y ({name})" for name, column in zip(names, resolved) if not column]
    if not names:
        missing.append("y")
    for label, value in (("color", color), ("size", size)):
        if value and not _resolve(value):
            missing.append(f"{label} ({value})")
    if missing:
        # Naming a column the SQL does not return is the most common failure
        # here; say so precisely so the model can correct itself in one turn.
        return {"error": (
            f"Chart column(s) {missing} not found in the query result. "
            f"Available columns: {result['columns']}")}

    from app.engine import panel_capabilities

    config = dashboards._panel_config({
        "title": title,
        "chart_type": chart_type,
        "x": _resolve(x),
        "y": y_value,
        "color": _resolve(color),
        "size": _resolve(size),
        "orientation": orientation,
        "display": "chart",
        "column_formats": column_formats,
    })
    # Only what would leave the chart unable to draw what it names — too few
    # value columns for a combo, several for a pie — goes back to the model.
    unusable = [problem.message for problem in
                panel_capabilities.check_panel(config, result["columns"])
                if problem.kind in ("column", "shape")]
    if unusable:
        return {"error": "The chart cannot be drawn as asked: " + "; ".join(unusable)}

    audit.log_event("show_chart", "ok", user, role, question=user_question,
                    sql=result["sql"], row_count=result["row_count"],
                    client_info="webapp",
                    duration_ms=int((time.perf_counter() - t0) * 1000))

    return {
        # Panel shape — identical to a saved dashboard panel.
        "panel": {
            **config,
            "columns": result["columns"],
            "rows": result["rows"],
            "row_count": result["row_count"],
            # Beside the rows, as run_dashboard does: money with no declared
            # currency reads in the database's currency, not dollars.
            "default_currency": metadata_store.currency(ctx.database),
        },
        "sql": result["sql"],
        "row_count": result["row_count"],
    }


def _ask_data_question(ctx: ToolContext, question: str) -> dict[str, Any]:
    """End-to-end NL question: classify -> SQL -> run (one repair retry) ->
    summarise. Ported from the MCP tool of the same name; the repair retry and
    the non-fatal summarise are the parts worth preserving exactly."""
    t0 = time.perf_counter()
    user, role = ctx.connector.session_identity()

    def _elapsed() -> int:
        return int((time.perf_counter() - t0) * 1000)

    try:
        category = llm.classify(question)

        # Non-data branches: nothing to execute.
        if category == "concept_explanation":
            # The whole QUESTION arrives here ("What does DSCR mean?"), which is
            # not itself a glossary key — pull the term out first, falling back
            # to the raw question so a miss still yields close matches.
            term = glossary_mod.extract_term(question) or question
            explanation = glossary_mod.explain(term)
            audit.log_event("ask_data_question", "ok", user, role, question=question,
                            client_info="webapp", duration_ms=_elapsed())
            return {"classification": category, "concept": explanation}

        if category == "out_of_scope":
            audit.log_event("ask_data_question", "ok", user, role, question=question,
                            client_info="webapp", duration_ms=_elapsed())
            return {"classification": category,
                    "message": "This question doesn't appear answerable from this database."}

        context = metadata_store.build_context(ctx.database)
        try:
            gen = llm.generate_sql(question, context)
        except llm.LLMError as exc:
            audit.log_event("ask_data_question", "error", user, role, question=question,
                            error=str(exc), client_info="webapp", duration_ms=_elapsed())
            return {"error": str(exc)}

        # ONE repair retry: feed the failure back and regenerate once.
        attempt_sql = gen.sql
        last_error: str | None = None
        result = None
        for attempt in (1, 2):
            try:
                result = ctx.connector.run_query(attempt_sql)
                break
            except UnsafeSQLError as exc:
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if attempt == 1:
                try:
                    gen = llm.generate_sql(question, context, prior_error=last_error)
                    attempt_sql = gen.sql
                except llm.LLMError as gen_exc:
                    last_error = str(gen_exc)
                    break

        if result is None:
            audit.log_event("ask_data_question", "error", user, role, question=question,
                            sql=attempt_sql, error=last_error, client_info="webapp",
                            duration_ms=_elapsed())
            return {"error": f"Could not answer from data: {last_error}", "sql": attempt_sql}

        # Summary failure must never sink the call — the data and SQL are already good.
        try:
            answer = llm.summarize(question, result["columns"], result["rows"],
                                   result["row_count"])
        except Exception:  # noqa: BLE001
            answer = ""

        status = "ok" if last_error is None else "ok_after_repair"
        audit.log_event("ask_data_question", status, user, role, question=question,
                        sql=result["sql"], row_count=result["row_count"],
                        error=last_error or None, client_info="webapp",
                        duration_ms=_elapsed())

        return {
            "classification": category,
            "answer": answer,
            "sql": result["sql"],
            "columns": result["columns"],
            "rows": result["rows"],
            "row_count": result["row_count"],
            "repaired": last_error is not None,
        }
    except Exception as exc:  # noqa: BLE001
        # Catch-all so a raw model-API error (not an llm.LLMError) still lands
        # in the audit trail rather than vanishing.
        audit.log_event("ask_data_question", "error", user, role, question=question,
                        error=str(exc), client_info="webapp", duration_ms=_elapsed())
        return {"error": f"Could not answer question: {exc}"}


def _explain_concept(ctx: ToolContext, term: str) -> dict[str, Any]:
    return glossary_mod.explain(term)


def _plan_dashboard(ctx: ToolContext, description: str) -> dict[str, Any]:
    try:
        if ctx.connector_name == "duckdb":
            context = ctx.semantic_context or ""
            return file_dashboards.create_plan(
                description, ctx.database, context, ctx.connector
            )
        return dashboards.create_plan(description, ctx.database)
    except dashboards.DashboardError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Planning failed: {exc}"}


def _save_dashboard(ctx: ToolContext, plan_id: str, report_title: str,
                    description: str = "") -> dict[str, Any]:
    try:
        if file_dashboards.get_cached_plan(plan_id) is not None:
            user, _role = ctx.connector.session_identity()
            return file_dashboards.save_plan(
                plan_id, report_title, description, created_by=user
            )
        return dashboards.save_plan(plan_id, report_title, description)
    except dashboards.DashboardError as exc:
        return {"error": str(exc)}


def _list_saved_dashboards(ctx: ToolContext, limit: int = 50) -> dict[str, Any]:
    if ctx.connector_name == "duckdb":
        items = file_dashboards.list_dashboards(source_key=ctx.database, limit=limit)
    else:
        items = dashboards.list_dashboards(limit)
        for item in items:
            item.setdefault("source_type", "snowflake")
    return {"count": len(items), "dashboards": items}


def _run_saved_dashboard(ctx: ToolContext, report_id: str,
                         parameter_values: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if file_dashboards.exists(report_id):
            return file_dashboards.run_dashboard(
                report_id, ctx.connector, parameter_values
            )
        return dashboards.run_dashboard(report_id, parameter_values)
    except dashboards.DashboardError as exc:
        return {"error": str(exc)}


def _edit_dashboard(ctx: ToolContext, report_id_or_title: str,
                    changes_description: str, confirm: bool = False) -> dict[str, Any]:
    try:
        file_matches = file_dashboards.resolve_report_ids(report_id_or_title)
        if len(file_matches) > 1:
            return {
                "status": "ambiguous",
                "matches": file_matches,
                "message": (
                    f"'{report_id_or_title}' matches {len(file_matches)} file-source "
                    "dashboards. Re-run with the exact report_id of the one you mean — "
                    "I won't guess."
                ),
            }
        if len(file_matches) == 1:
            rec = file_dashboards.read_record(file_matches[0]["report_id"])
            source_key = (rec or {}).get("source_key") or ""
            if ctx.connector_name != "duckdb":
                return {
                    "error": (
                        f"This dashboard is saved from uploaded files ({source_key}). "
                        "Open that source in Files and request the edit from there."
                    )
                }
            if ctx.database != source_key:
                return {
                    "error": (
                        f"This dashboard belongs to {source_key}. Switch to that "
                        "folder or spreadsheet source to edit it."
                    )
                }
            context = ctx.semantic_context or ""
            if confirm:
                return file_dashboards.commit_edit(
                    report_id_or_title, changes_description
                )
            return file_dashboards.preview_edit(
                report_id_or_title,
                changes_description,
                ctx.connector,
                context,
            )
        if confirm:
            return dashboards.commit_edit(report_id_or_title, changes_description)
        return dashboards.preview_edit(report_id_or_title, changes_description)
    except dashboards.DashboardError as exc:
        return {"error": str(exc)}


def _search_documents(
    ctx: ToolContext, query: str, source_file: str | None = None
) -> dict[str, Any]:
    """Passages from this folder's documents, with the page each came from."""
    from app.engine import collection_query
    from app.engine.connectors.duckdb import client as duckdb_client

    documents = duckdb_client.bound_documents()
    if documents is None:
        return {
            "error": (
                "There are no documents in this source. It holds spreadsheet "
                "tables only — query them with run_safe_sql."
            )
        }

    passages = collection_query.search_documents(
        documents, query, limit=8, source_file=source_file or None
    )
    if not passages:
        return {
            "query": query,
            "passages": [],
            "note": (
                "Nothing matched those words. Search matches wording, not "
                "meaning — try the terms the document itself would use."
            ),
        }
    return {
        "query": query,
        "passages": [
            {
                "source_file": p.source_file,
                "source_page": p.source_page,
                "text": p.text,
                "score": round(p.score, 3),
            }
            for p in passages
        ],
    }


_IMPLEMENTATIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_database_context": _get_database_context,
    "list_tables": _list_tables,
    "run_safe_sql": _run_safe_sql,
    "search_documents": _search_documents,
    "show_chart": _show_chart,
    "ask_data_question": _ask_data_question,
    "explain_concept": _explain_concept,
    "plan_dashboard": _plan_dashboard,
    "save_dashboard": _save_dashboard,
    "list_saved_dashboards": _list_saved_dashboards,
    "run_saved_dashboard": _run_saved_dashboard,
    "edit_dashboard": _edit_dashboard,
}


#: Markers of SQL the warehouse or the safety gate refused. Worth a retry, but
#: only with different SQL.
_SQL_ERROR_MARKERS = (
    "sql compilation error",
    "query rejected",
    "rejected by safety layer",
    "failed to compile",
    "could not parse sql",
    "invalid identifier",
    "parameter error",
    "does not exist or not authorized",
)

_CONNECTION_LOST_REMEDY = (
    "The warehouse connection for this turn has closed, so every further query in "
    "this turn fails the same way. Do not call another data tool now: tell the user "
    "the connection dropped and ask them to send their message again."
)
_TOO_LARGE_REMEDY = (
    "The same request stops at the same point every time. Ask for fewer panels or "
    "shorter SQL, or save what fits and add the rest with one edit."
)
_SQL_INVALID_REMEDY = (
    "Change the SQL the error names, then try again. The same SQL fails the same way."
)


def classify_error(message: str) -> dict[str, Any] | None:
    """Whether sending a failed call again can help, and what to do instead.

    Read from the error's text because the failures arrive from layers that
    share no exception type by the time they get here: the connector, the SQL
    gate, the planner. Without it the assistant guessed — it retried a closed
    connection three times and blamed its own wording for a quoting bug.

    A closed connection is checked first. A plan whose every panel failed
    lists each panel's reason, and when the connection is gone those reasons
    read as SQL failures too; retrying the SQL would not bring it back.
    """
    text = message.lower()
    if "08003" in text or "250002" in text or "connection is closed" in text:
        return {"error_kind": "connection_lost", "retryable": False,
                "remedy": _CONNECTION_LOST_REMEDY}
    if "ran out of room" in text:
        return {"error_kind": "too_large", "retryable": False,
                "remedy": _TOO_LARGE_REMEDY}
    if any(marker in text for marker in _SQL_ERROR_MARKERS):
        return {"error_kind": "sql_invalid", "retryable": True,
                "remedy": _SQL_INVALID_REMEDY}
    return None


def _labelled(result: Any) -> Any:
    """A failed result with its error kind added, when the kind is known."""
    if not isinstance(result, dict) or "error" not in result or "error_kind" in result:
        return result
    label = classify_error(str(result["error"]))
    return {**result, **label} if label else result


async def dispatch(name: str, tool_input: dict[str, Any],
                   ctx: ToolContext) -> dict[str, Any]:
    """Run one tool call off the event loop, returning a result dict.

    Never raises: an unknown tool, a bad argument set, or an exception the
    implementation missed all come back as {"error": ...} so the model can
    read the reason and recover on the next turn. A failure whose kind is
    recognised also carries error_kind, retryable and remedy — see
    classify_error.
    """
    impl = _IMPLEMENTATIONS.get(name)
    if impl is None:
        return {"error": f"Unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}"}

    try:
        return _labelled(await asyncio.to_thread(impl, ctx, **(tool_input or {})))
    except TypeError as exc:
        # Wrong/missing arguments — tell the model precisely, so it retries correctly.
        return {"error": f"Invalid arguments for '{name}': {exc}"}
    except Exception as exc:  # noqa: BLE001
        return _labelled({"error": f"Tool '{name}' failed: {exc}"})

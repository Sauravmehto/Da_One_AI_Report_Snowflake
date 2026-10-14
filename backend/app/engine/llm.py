"""Server-side LLM layer for ask_data_question.

The server holds its OWN Anthropic API key (separate from whatever model the
user is running in Claude Desktop) and uses it to turn a natural-language
question into validated SQL. This centralizes SQL generation so it is
consistent, auditable, and independent of the user's client model — the design
your lead's plan calls for.

Model routing (cheap model for cheap work, strong model for the hard part):
  - classification  -> Haiku 4.5  (fast, cheap: which kind of question is this?)
  - SQL generation  -> Sonnet 5   (needs real reasoning over schema + join rules)
  - answer summary  -> Haiku 4.5  (turn result rows into a one-paragraph answer)

Model IDs are overridable via env so you can retune without code changes.

Prompt caching: the business-context block is large and re-sent on every SQL
generation (and again on a repair retry). It's marked cache_control so repeat
sends within the cache window are billed at the cheaper cached rate.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field

from anthropic import Anthropic

# The two planners target different engines with different row caps. Read each
# cap from the connector that enforces it: the file cap is environment-tunable
# (MCP_MAX_ROWS), so a number written into the prompt by hand can be wrong in a
# deployment nobody thought to re-check.
from app.engine.connectors.duckdb import client as _duckdb_client
from app.engine.connectors.snowflake import client as _snowflake_client

_SF_CAP = _snowflake_client.MAX_ROWS
_FILE_CAP = _duckdb_client.MAX_ROWS

# --- Model routing (env-overridable) ---
MODEL_CLASSIFY = os.environ.get("MCP_MODEL_CLASSIFY", "claude-haiku-4-5-20251001")
MODEL_SQL = os.environ.get("MCP_MODEL_SQL", "claude-sonnet-5")
MODEL_SUMMARY = os.environ.get("MCP_MODEL_SUMMARY", "claude-haiku-4-5-20251001")
MODEL_DASHBOARD = os.environ.get("MCP_MODEL_DASHBOARD", "claude-opus-4-8")
#: The Edit panel's assistant. Its own setting, so it can move to a lighter
#: model once the assistant eval (scripts/assist_eval.py --model) shows the
#: edits still come out right. Unset, it is the dashboard model, as before.
MODEL_DASHBOARD_ASSIST = os.environ.get("MCP_MODEL_DASHBOARD_ASSIST", MODEL_DASHBOARD)

# The four buckets a question can fall into.
CATEGORIES = {"data_query", "schema_question", "concept_explanation", "out_of_scope"}


class LLMError(RuntimeError):
    """Raised when the LLM layer can't be used (e.g. missing API key)."""


class LLMCancelled(LLMError):
    """The person stopped the request, and the model call was closed part-way."""


#: A stop signal for the model call running in this context. The Edit panel's
#: assistant sets one: when the person presses Stop the browser drops the
#: request, the route sets the event, and a streamed call closes its stream at
#: the next chunk — so the model stops generating, and stops being billed,
#: instead of running on to the end for a reply nobody is waiting for.
_cancel_signal: ContextVar[threading.Event | None] = ContextVar(
    "llm_cancel_signal", default=None
)


@contextlib.contextmanager
def cancellable(signal: threading.Event):
    """Let model calls made inside this block be stopped by setting `signal`."""
    token = _cancel_signal.set(signal)
    try:
        yield
    finally:
        _cancel_signal.reset(token)


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. ask_data_question needs the "
                "server's own Anthropic key — add it to your .env."
            )
        _client = Anthropic(api_key=api_key)
    return _client


def _create(what: str, *, stream: bool = False, **request):
    """messages.create, with one audit row for what the call cost.

    Every model call in this module goes through here, so none can be added
    that the audit log does not see. A call that fails is recorded too, then
    re-raised unchanged.

    `stream` receives the reply as a stream and returns the same final message.
    Used for whole dashboard plans: a long reply requested in one piece is held
    to the SDK's ten-minute request timeout and refused above a size limit,
    while a stream is bounded only by the gap between chunks.
    """
    import time

    from app.engine import audit

    started = time.monotonic()
    signal = _cancel_signal.get()
    try:
        if signal is not None and signal.is_set():
            raise LLMCancelled(f"{what} was stopped before it started.")
        client = _get_client()
        if stream:
            with client.messages.stream(**request) as reply:
                if signal is not None:
                    # Read chunk by chunk so a stop is noticed while the reply
                    # is still coming. Leaving the block closes the stream.
                    for _chunk in reply:
                        if signal.is_set():
                            raise LLMCancelled(f"{what} was stopped part-way.")
                resp = reply.get_final_message()
        else:
            resp = client.messages.create(**request)
    except Exception as exc:
        audit.log_ai_call(what, request.get("model"), started=started, error=exc)
        raise
    audit.log_ai_call(what, request.get("model"), resp, started=started)
    return resp


def _text(resp) -> str:
    """Concatenate the text blocks of a messages response."""
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


#: Output ceiling for every call that returns a whole dashboard plan. A plan is
#: one JSON object carrying every panel's SQL and chart config, so this caps how
#: many panels a dashboard can have at all. 8192 filled up around 15-20 panels;
#: 16000 left a 25-panel request with long SQL no margin. Only the tokens a plan
#: actually uses are billed, so the headroom costs nothing. Above 16000 the SDK
#: requires streaming, which is why these calls stream.
_PLAN_MAX_TOKENS = 32000

#: The most panels a planner builds when a user asks for a large dashboard. The
#: default stays 8-12; see _DASHBOARD_RULES.
MAX_REQUESTED_PANELS = 25


def _plan_text(resp, what: str) -> str:
    """The text of a plan response, refusing one the output ceiling cut short.

    A plan that hits max_tokens is valid JSON up to the cut and then simply
    stops, so it used to surface as "unparseable output" — which reads as a
    malformed answer worth retrying. It is not: the same request stops at the
    same place every time, and each retry bills another full ceiling of output.
    Say what actually happened, and what would get it through.
    """
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise LLMError(
            f"{what} ran out of room: the plan needed more than {_PLAN_MAX_TOKENS} "
            "tokens of output and was cut off mid-way. Retrying the same request "
            "will stop at the same point. Ask for fewer panels or shorter SQL."
        )
    return _text(resp)


def _strip_fences(s: str) -> str:
    """Remove ```json / ``` fences a model may wrap output in."""
    s = s.strip()
    s = re.sub(r"^```(?:json|sql)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    return s


def _unparseable(what: str, raw: str, exc: Exception) -> str:
    """The error for output that is not the JSON it should be.

    It used to quote the first 200 characters, which are nearly always a
    well-formed opening — so every failure looked as though it broke at
    '"bindings": [' whatever had actually gone wrong. The parser's own
    message names the line and column; the tail shows how the output ended.
    """
    text = _strip_fences(raw or "")
    if len(text) > 400:
        text = f"{text[:120]} … {text[-240:]}"
    return f"{what} returned unparseable output ({exc}): {text}"


def _json_object(raw: str) -> dict:
    """Parse the JSON object out of a model response, tolerating fences AND stray
    prose around it (e.g. a leading 'I cannot do X because …' before the object).
    Falls back to extracting the first balanced {...} block (string-aware) when a
    straight parse fails, so a well-formed object wrapped in explanation still
    loads. Raises json.JSONDecodeError if no object can be recovered."""
    text = _strip_fences(raw)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)


# --- Step 1: classify ------------------------------------------------------

def classify(question: str) -> str:
    """Return one of CATEGORIES. Cheap Haiku call, tiny output."""
    system = (
        "You classify a user's question about a financial database into exactly "
        "one category. Respond with ONLY the category word, nothing else.\n"
        "Categories:\n"
        "- data_query: wants specific numbers/rows from the data "
        "(e.g. 'what's the delinquency rate', 'top 5 states by exposure').\n"
        "- schema_question: asks what tables/columns exist or how data is "
        "structured (e.g. 'what tables are there', 'what columns does SERVICER1 have').\n"
        "- concept_explanation: asks what a term or metric MEANS, not its value "
        "(e.g. 'what is DSCR', 'explain mark-to-par').\n"
        "- out_of_scope: unrelated to the database or not answerable from it."
    )
    resp = _create(
        "classify_question",
        model=MODEL_CLASSIFY,
        max_tokens=16,
        system=system,
        messages=[{"role": "user", "content": question}],
        # No temperature — it is deprecated/rejected on current-generation
        # models (Sonnet 5 / Opus 4.8). Kept off here too so a model swap via
        # MCP_MODEL_CLASSIFY can't reintroduce the 400. Matches plan_dashboard.
    )
    label = _text(resp).lower().strip().strip(".")
    # Be forgiving if the model adds stray words; match the first known category.
    for cat in CATEGORIES:
        if cat in label:
            return cat
    return "data_query"  # safest default: try to answer from data


# --- Step 2: generate SQL --------------------------------------------------

@dataclass
class SQLGeneration:
    sql: str
    notes: str = ""


def generate_sql(question: str, business_context: str, prior_error: str | None = None) -> SQLGeneration:
    """Generate a single read-only Snowflake SELECT for the question.

    business_context is the metadata_store.build_context() output — table
    meanings, join rules, and exact metric SQL. If prior_error is given (a
    repair retry), it's included so the model can fix its previous attempt.
    """
    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are a Snowflake SQL generator for a financial database. "
                "Produce ONE read-only SQL statement (SELECT or WITH ... SELECT) "
                "that answers the user's question.\n"
                "Rules:\n"
                "- Follow the join rules in the context EXACTLY. Never join tables "
                "the context says must not be joined.\n"
                "- If the question matches a named metric, use that metric's EXACT "
                "SQL from the context; do not invent your own formula.\n"
                "- Use fully-qualified names: DATABASE.SCHEMA.TABLE.\n"
                "- For TEXT-encoded numeric columns, apply the parsing shown in the "
                "context before doing math.\n"
                "- Apply any scaling the context documents for a column: when a "
                "column is described as stored as a 0-1 FRACTION, multiply it by "
                "100 so it reads as a whole-number percent (e.g. 14 for 14%). "
                "Leave columns already stored as whole-number percents unchanged.\n"
                "- Output ONLY a JSON object: {\"sql\": \"...\", \"notes\": \"...\"}. "
                "No prose, no code fences. `notes` is a brief note on assumptions."
            ),
        },
        {
            "type": "text",
            "text": f"BUSINESS CONTEXT:\n{business_context}",
            # Cache the big context block — re-sent on repair retries and across
            # calls for the same database within the cache window.
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user = f"Question: {question}"
    if prior_error:
        user += (
            f"\n\nYour previous SQL failed with this error:\n{prior_error}\n"
            "Return a corrected single read-only SELECT."
        )

    resp = _create(
        "generate_sql",
        model=MODEL_SQL,
        max_tokens=1024,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
        # No temperature — MODEL_SQL is Sonnet 5, which REJECTS temperature with
        # a 400 ("temperature is deprecated for this model"). This was the call
        # that broke ask_data_question 100%. plan_dashboard already omits it.
    )
    raw = _strip_fences(_text(resp))
    try:
        data = json.loads(raw)
        return SQLGeneration(sql=data["sql"].strip(), notes=data.get("notes", ""))
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        raise LLMError(_unparseable("SQL generator", raw, exc)) from exc


# --- Step 3: summarize the result -----------------------------------------

def summarize(question: str, columns: list[str], rows: list[list], row_count: int) -> str:
    """One concise natural-language answer grounded in the returned rows, with
    chart guidance when appropriate."""
    # Keep the payload small — send at most the first 40 rows for phrasing.
    sample = rows[:40]
    payload = {"columns": columns, "rows": sample, "row_count": row_count}
    system = (
        "You state the answer to the user's question in 1-3 sentences, using ONLY "
        "the query results provided. Include the key number(s). Do not speculate "
        "beyond the data. If the result is empty, say so plainly.\n\n"
        "If the data is suitable for a visualization (e.g., ranked lists, geographic "
        "breakdowns, time series, comparisons across categories), suggest a chart type "
        "that would make the pattern clearest. For example: 'This would be clearer as "
        "a bar chart with [column] on the x-axis and [metric] on the y-axis.' Your "
        "suggestion helps render the data visually if appropriate."
    )
    resp = _create(
        "summarize_result",
        model=MODEL_SUMMARY,
        max_tokens=300,
        system=system,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nResults (JSON):\n{json.dumps(payload, default=str)}",
        }],
        # No temperature — deprecated/rejected on current-generation models; kept
        # off for consistency with the other calls and future model swaps.
    )
    return _text(resp)


_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+.*$", re.MULTILINE)


def _plain_prose(text: str) -> str:
    """The narrative is rendered verbatim, so strip any markdown the model emits."""
    text = _MD_HEADING.sub("", text)
    text = text.replace("**", "").replace("__", "")
    return " ".join(text.split())


#: The summary's instructions. Part of the summary cache key, so a change to
#: this wording produces a fresh summary rather than serving the old one.
SUMMARY_SYSTEM = (
    "You write a brief executive narrative for a business dashboard. "
    "Use ONLY the KPI values and panel figures provided. "
    "Every value is already written out in its unit and scale (M is million, "
    "B is billion): quote it exactly as given and never convert, recompute or "
    "rescale a number. For a chart over time, 'first' and 'latest' carry their "
    "periods and 'latest' is the most recent period shown; describe movement "
    "from first to latest using 'change'. For a breakdown, 'top' and 'bottom' "
    "are its largest and smallest items, and a share is given only where one "
    "is meaningful; 'top_share' is the combined share of its largest items -- "
    "quote it for concentration instead of adding shares together. "
    "Never calculate a figure that is not given: no shares, percentages, "
    "totals, sums, averages or differences of your own. If a point needs a "
    "number that is not in the payload, make it in words (for example "
    "\"the largest market by a wide margin\") rather than working one out. "
    "Every number you write keeps the unit it was given with (AED, %, pp, M) "
    "-- never a bare number. "
    "KPI values, marked (overall), are totals, averages or rates for the whole "
    "period the dashboard covers, not a latest reading: state them as the period's "
    "figures and never say a KPI rose, fell, declined or grew to its value. "
    "Movement comes only from a chart's 'change', described with that chart's "
    "own first and latest values and periods and kept apart from the KPI totals: "
    "never pair a chart's change with an (overall) value. A breakdown has no "
    "movement over time, so never say its items rose, fell or turned. "
    "2–4 sentences: lead with the headline position, then one risk or "
    "concentration signal, then optionally a trend or exception. "
    "Include concrete numbers from the payload. Do not invent metrics, "
    "dates, or causes. Do not mention chart types or SQL. "
    "A panel marked \"truncated\": true stopped at the query's row limit "
    "and its most recent rows are missing: never describe where such a "
    "series ends as where the trend ends, and never quote its last row as "
    "the current position. "
    "Plain prose only — no title, no heading, no bullets, no markdown."
)


def summarize_dashboard(
    title: str,
    kpi_facts: list[dict],
    panel_snapshots: list[dict],
) -> str:
    """2–4 sentence executive narrative for a saved dashboard run.

    Grounded only in the KPI facts and compact panel snapshots the caller
    already computed — no SQL, no speculation beyond those numbers.
    """
    payload = {
        "title": title,
        "kpis": kpi_facts[:12],
        # The route already caps panels; a second, lower cap here silently
        # dropped every chart after the tenth.
        "panels": panel_snapshots,
    }
    resp = _create(
        "dashboard_summary",
        model=MODEL_SUMMARY,
        max_tokens=280,
        system=SUMMARY_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                "Write the narrative for this dashboard run:\n"
                + json.dumps(payload, default=str)
            ),
        }],
    )
    return _plain_prose(_text(resp))


def describe_panel(
    title: str,
    chart_type: str,
    columns: list[str],
    x: str | None,
    y: list[str],
    dashboard_title: str | None = None,
) -> str:
    """Two or three sentences explaining what a chart shows, for someone who
    does not already know.

    NO DATA IS SENT. Only the panel's title, its chart type and its column
    NAMES — which is all the question "what am I looking at" needs, and it
    keeps this feature outside the row-sharing described to clients.
    """
    payload = {
        "dashboard": dashboard_title,
        "panel_title": title,
        "chart_type": chart_type,
        "x_axis": x,
        "values": y,
        "all_columns": columns[:25],
    }
    system = """You explain a single chart on a business dashboard to someone
who is not an analyst and may not know the chart type or the jargon in it.

This is read in a small popover beside the chart, so LENGTH IS THE HARD PART:
at most 3 sentences and under 60 words, as ONE paragraph. No preamble, no
markdown, no bullets, no line breaks. Do not open with "This chart" - start
with what it shows.

Cover, in that space:
1. What it plots and how to read it - name the chart type in ordinary words
   (a funnel shows how many drop out at each step; a treemap sizes each box by
   its share of the total).
2. What the measure MEANS in business terms.

Expand every abbreviation the first time it appears, with the full form in
brackets - CAC (customer acquisition cost), OTIF (on time in full), DSO (days
sales outstanding), AOV, ARR, MoM and so on. This is the single most useful
thing you do here.

You are given column names only, never the data, so never state or guess a
figure, a trend or a conclusion. Describe what the chart IS, not what it
currently says. If a column name is too cryptic to interpret honestly, say
what it appears to measure rather than inventing a definition."""
    resp = _create(
        "panel_description",
        model=MODEL_SUMMARY,
        max_tokens=160,
        system=system,
        messages=[{
            "role": "user",
            "content": "Explain this chart:\n" + json.dumps(payload, default=str),
        }],
    )
    return _text(resp).strip()


# --- Dashboard planning ----------------------------------------------------

@dataclass
class DashboardPlan:
    title: str
    # each: title, sql, chart_type, x, y, color, size, orientation, display,
    # plus the layout fields section, span and row_span
    panels: list[dict]
    # Optional runtime parameters: each {name, type, default, options?, bindings?,
    # panels?}. Panel SQL references them as {{name}} placeholders. Empty when the
    # dashboard has no parameters (the common case).
    parameters: list[dict] = field(default_factory=list)
    # Set by the planner ONLY when it cannot proceed without asking the user
    # (e.g. a date parameter can't be bound to any metadata date column). The
    # orchestration surfaces this instead of saving a guessed plan.
    needs_clarification: str = ""


_DOUBLED_QUOTE_RE = re.compile(r"''([^']*?)''")


def _fix_doubled_quotes(sql: str) -> str:
    """Un-double over-escaped single quotes the model sometimes emits (''x'' ->
    'x'). Applied to every panel's SQL right after JSON parse, so the SQL is
    correct before it is validated, dry-run, stored, or executed. Ported from the
    old Streamlit dashboard builder."""
    return _DOUBLED_QUOTE_RE.sub(r"'\1'", sql)


# The panel/SQL design rules for the Opus planner. Correctness/rendering-contract
# rules are stated as hard rules; chart *design* is left to the planner's judgment
# as goals rather than rigid pattern-matching (which was over-constraining layout).
_DASHBOARD_RULES = """RULES:

# --- SQL validity & data correctness (follow exactly) ---
- Each SQL must be a single valid Snowflake SELECT statement using fully qualified table names.
- Never nest window functions inside aggregate functions.
- Always use CTEs for complex queries.
- For derived fields such as LOAN_MONTH, RATE_BAND, or any CASE/DATE_TRUNC alias, create the field in one CTE and reference it only from a later CTE or final SELECT.
- Never reference a SELECT alias in the same SELECT's WHERE, GROUP BY, HAVING, QUALIFY, JOIN, or another expression.
- Every column referenced in a later CTE must be explicitly selected and aliased in the previous CTE.
- Never use TRY_CAST on NUMBER or DECIMAL columns, only on TEXT columns.
- Use DATE_TRUNC for month/quarter time-series grouping. For ANNUAL (per-year) grouping, do not DATE_TRUNC to a year — emit the year as text instead (see the year rules below).
- Use NULLIF(denominator, 0) to avoid division by zero.
- Money that is NOT US dollars MUST declare its currency in column_formats: {"AMOUNT_AED": {"format": "currency", "currency": "AED"}}. The code comes from the column name, the semantic layer, or a currency column beside it; use "" when the column mixes currencies. Left undeclared, dirhams and euros both print as dollars.
- Percentages must be emitted already scaled to the whole-number-percent convention the renderer expects (14.0 for 14%, NOT 0.14). The semantic layer is authoritative about scale, per column: when a column's description says it is stored as a 0-1 FRACTION, multiply it by 100 and keep the same alias (e.g. LTV_PCT * 100 AS LTV_PCT). When a column is already a whole-number percent, leave it exactly as-is — never multiply it a second time (this includes metric SQL that already contains 100.0 * ...). Do NOT infer scaling from the column name: a _PCT suffix alone does not mean a value needs scaling, and some _PCT columns are already whole-number percents. Apply this uniformly to KPI values, every chart series (including stacked_bar rate columns), and percentages shown in tables.
- Line chart SQL must return one row per time bucket per series. Aggregate to the month/quarter level before returning rows.
- Always ORDER BY the x-axis/time column in chart SQL — EXCEPT quarter-label dimensions, which must be ordered by their companion date column instead (see the quarter-label rule below).
- Every panel must return AT MOST <<ROW_CAP>> ROWS. The warehouse caps results there and drops the rest without a word, and because chart SQL is ordered oldest-first the rows lost are the most RECENT ones — the end of the trend a reader came to see. Aggregate to the grain the panel actually shows (a month rather than a day, a category rather than a row per record), restrict to a recent window, or rank and LIMIT deliberately. Watch breakdowns especially: one value per week over two years is already ~100 rows before any split by market or category, so a weekly series split five ways must move to months or to a shorter window.
- Year dimensions: when a column the schema identifies as a year (a whole-number year / fiscal year / vintage year, e.g. values like 2023) is used as a chart x-axis or grouping dimension, cast it to text with the SAME alias — e.g. TO_VARCHAR(FISCAL_YEAR) AS FISCAL_YEAR — and ORDER BY the year ascending, so it renders as a discrete year label rather than a continuous measure.
- Annual date buckets: never return DATE_TRUNC('year', <date>) (it comes back as a Jan-1 timestamp). Instead emit the year itself as text, e.g. TO_CHAR(<date>, 'YYYY') AS <alias> (or TO_VARCHAR(YEAR(<date>))), and GROUP BY / ORDER BY that year.
- Quarter-label dimensions: when a chart uses a quarter-label column (a TEXT column the schema describes with values like 'Q1 2025'), ORDER BY its companion quarter/period DATE column that the schema names (e.g. QUARTER_END_DATE, MARK_DATE, RATE_DATE) — never by the label text, which does not sort chronologically across years. Include that date in GROUP BY, or ORDER BY MIN(<date>), so rows come back in chronological order.
- When computing a subset metric (e.g., delinquent count), the SQL MUST include a WHERE clause that filters to the subset. Never return COUNT(*) or SUM() without the appropriate filter.
- CRITICAL: Use single quotes for SQL string literals — write 'DELQ%' not ''DELQ%'', write '%' not ''%''. Never double-escape single quotes inside SQL strings. The SQL will be executed directly, not embedded in another string.

# --- Parameters (runtime inputs) ---
- If the user's description implies the dashboard will be RE-RUN for different dates, periods, or filter values routinely (e.g. "as of a chosen date", "for a given quarter", "by a selected sector"), declare that value as a PARAMETER in the top-level "parameters" list instead of hardcoding a literal in the SQL, and reference it in the relevant panels' SQL as {{name}}.
- Every parameter MUST have a default that reproduces the normal/default view (e.g. the latest date), so the dashboard runs correctly with no value supplied. List the panels that use each parameter in its "panels".
- Write the placeholder exactly where a SQL literal would go, WITHOUT surrounding quotes: WHERE MARK_DATE <= {{as_of_date}} (correct), not '{{as_of_date}}' (wrong — date/string parameters are auto-quoted, so quoting again double-quotes them). For a number parameter the placeholder becomes a bare number.
- Do NOT parameterize a value that is intrinsic to a panel's meaning (a panel titled "Watch-list Exposure" keeps LOAN_STATUS = 'Watch-list' hardcoded). Parameterize only values a user would routinely change between runs; when unsure, prefer a hardcoded literal and no parameter.

# --- Date parameters: bind columns from metadata, NEVER guess ---
- A DATE parameter must bind to a specific column PER TABLE it filters, and that column MUST come from the table's "Date columns" metadata, chosen BY ROLE — never inferred from a column's name, and never defaulted to whichever date column happens to exist. For each date parameter, fill "bindings" with a {table, role} entry for every table it filters, and write that panel's SQL comparing the placeholder to exactly the column that role maps to (e.g. PC_MARKS snapshot_date → MARK_DATE: WHERE MARK_DATE <= {{as_of_date}}).
- The SAME date parameter can bind to DIFFERENT columns in different tables — use each table's own role (e.g. maturity_date on PRIVATE_CREDIT vs snapshot_date on PC_MARKS). Do NOT assume one column name applies everywhere.
- If a table you are asked to filter by a date has "Date columns: NONE" in its metadata (e.g. PC_RISK_LIMITS), it CANNOT support the parameter. Do NOT invent a filter column, and do NOT silently leave it unfiltered while presenting it as filtered — put a question naming that table in "needs_clarification".
- If more than one of a table's date columns could plausibly represent the requested date and the intent is unclear (e.g. a table with several event dates and no obvious as-of column), do NOT pick one — put a question in "needs_clarification" listing the candidate columns and asking which to use.
- Only bind a date parameter to a role that the table's metadata actually lists. If the role you need is not present, that is a clarification, not a guess.

# --- Period/label filters: prefer the real date column, never leave a constrained value as free text ---
- Before creating a STRING parameter that filters a period/quarter/month/label column (e.g. QUARTER_LABEL), check whether the SAME table also declares a date column for that grain in its "Date columns" metadata (e.g. MARK_DATE / snapshot_date). If it does, bind a proper DATE parameter to that column instead, per the date-parameter rules above — do NOT bind to the text label merely because matching it looks simpler. Metadata that flags a label column as "not a bindable date column" means exactly that; treat it as a hard rule, not a suggestion.
- A parameter that filters to one of a SMALL, KNOWN set of values (a period label, a status, a category) MUST be declared type "enum" with "options" set to the real distinct values from the table's metadata or documented value list — never type "string" free text for this. A free-text "string" parameter invites a value that matches nothing and silently returns unfiltered or empty results with no error; reserve "string" only for a genuine substring/name search, which is rare in a dashboard filter.
- When a requested date/period filter applies to a multi-panel dashboard, bind it to EVERY panel whose source table has a usable date dimension for it — not only the minimum panel the request mentioned — so the filter's coverage is as complete as the data allows. Panels whose tables have no date dimension at all stay unfiltered/static; do not fabricate one just to reach full coverage.

# --- Coverage ---
- Generate between 8 and 12 panels for comprehensive dashboard prompts, unless the user asks for a different number.
- When the user asks for a larger dashboard or names a count (for example 18 or 20 panels), build that many, up to 25. Never exceed 25 panels: past that, build the 25 that matter most.
- Cover every explicit section or numbered item in the user's prompt. Do not drop requested sections just because another panel is easier.

# --- Composition: design a dashboard, do not just list panels ---
Before writing any SQL, decide what QUESTION this dashboard answers and what a reader should conclude in the first ten seconds. Then build panels that serve that conclusion. A dashboard is an argument with an order, not an inventory of available measures.
- Lead with the answer. The headline KPIs come first and state the current position — totals, exposure, count, the one or two rates that matter. Everything after them explains or qualifies that position.
- Then show movement, then composition, then exceptions, then detail. A reader asks "where are we", "which way is it going", "what is it made of", "what needs attention", "show me the rows" — in that order. Order the panels to match.
- Every panel must earn its place by answering something the panels before it did not. Do not show the same measure three ways (a total as a KPI, again as a bar, again in a table) unless each cut genuinely reveals something different. Prefer 8 panels that each add something to 12 where four are restatements.
- Pair panels that are read together. A trend and the breakdown that explains it belong side by side, not separated by an unrelated chart.
- End with the exceptions and the detail: breaches, watch-list items, data-quality problems, and the row-level table a reader drills into. These are reference material and belong last, not in the middle where they cut the dashboard in half.
- Titles are read far more often than SQL. Write what the panel SHOWS, specific to this data ("Watch-list Exposure by Sector"), never a restatement of the chart type ("Bar Chart of Exposure") and never a bare column name.

# --- Sections (tabs) ---
- Give every panel a "section": the tab it belongs on. Group by the QUESTION a reader is asking, not by table, chart type, or the order you happened to write the SQL.
- Use ONE section (and give every panel that same section name) when the dashboard has fewer than 12 chart and table panels, or tells a single continuous story. Do not split a dashboard into tabs to look organised: a reader should not have to click to see something that fits on one scrolling page, and a tab of two charts reads as a dashboard with little in it.
- Split only a larger dashboard that genuinely covers separable concerns, into at most 4 sections, with the headline KPIs on the FIRST section so the landing view answers the main question without a click. Typical shapes: "Overview" then "Risk" then "Detail"; or "Portfolio" / "Performance" / "Exposure".
- Name sections in the domain's own words, 1-2 words, title case. Never "Tab 1", "Section A", "Charts", "Other", or "Miscellaneous" — a section that can only be named "Other" is a sign the grouping is wrong.
- A section needs at least 6 chart or table panels to be worth its own tab; aim for 6 to 8 on each. When the panels would not fill every tab that way, use fewer tabs rather than thinner ones — never add a panel only to fill a tab. If a few panels do not fit any group, put them on the section they are most often read with rather than giving them a tab of their own. These limits are enforced after planning: a smaller tab is folded into its neighbour.

# --- Panel size (the grid is 4 columns wide) ---
- Set "span" to how much width the panel NEEDS to be read, and "row_span" to how much height: both are about legibility and importance, never about how much data a query happens to return.
- span 4 (full width): row-level tables, long time series, and anything with many categories along the x axis. Also the right choice for the one panel that carries the dashboard's main point.
- span 2 (half): the DEFAULT for most charts — a bar with a handful of categories, a pie, a scatter. Prefer 2+2 rows over mixing widths.
- Do NOT use span 1 (quarter) for charts — they are illegible that narrow. Prefer span 2, or span 4 if the chart needs the full row.
- span 3 only when a chart truly needs more than half beside a narrow companion; never invent a span-1 panel just to fill the remaining column.
- row_span 2 is the NORMAL height and the right choice for most panels. Use 1 for a panel that reads better short and wide (a small bar chart, a two-row table); 3 for a table with many rows, a heatmap, or a scatter that needs vertical room to separate points; 4 only for a long detail table that is the point of its section.
- Make the spans on each section add up to full rows of 4 where you can (4, or 2+2). Avoid permanent holes; do not use 2+1+1 or 3+1 patterns that force quarter-width charts.
- KPI panels are exempt: they render in a band above the grid and their span and row_span are ignored, so set both to null for a panel whose display is "kpi".

# --- Chart cookbook: pick the chart the QUESTION asks for ---
Allowed chart_type values: bar, stacked_bar, line, area, pie, donut, scatter, bubble,
heatmap, radar, treemap, funnel, combo, area_line, geo_us_states,
geo_countries, kpi.
Do NOT emit histogram, or any type not on that list — it is not rendered and degrades to
a plain table.

Choose by what the reader is asking, never by what looks varied. An unusual chart on data
that does not suit it is worse than a bar chart. Pick the plainest chart that answers the
question, and reach for a specialised one only when its precondition below is met.

Decide from the SHAPE of the result you are about to SELECT. The user will rarely name a
chart type, and choosing it is your job, not theirs — never fall back to a bar chart
because it is the safe answer:
1. What is on x? A date or period → the time family (line, area, combo). One category →
   the comparison family (bar, stacked_bar, donut, treemap). TWO categories carrying a
   measure → heatmap. A state, country, port or city → a map. Ordered stages of one
   process, each a subset of the last → funnel.
2. How many measures on y? One → the simple form of the family. Several that ADD UP to a
   total → stacked_bar. Exactly two on different scales → combo — and the SAME unit at very
   different sizes counts as different scales: OTIF near 90% beside an exception rate near
   1% on one axis draws two flat lines, the smaller pressed against the floor. Two numerics per entity →
   scatter, plus a third as magnitude → bubble. One entity scored across 3-8 named
   dimensions → radar.
3. How many rows will the GROUP BY return? One → kpi. A handful (≤8) that are parts of one
   whole → donut. Roughly 10-60 parts → treemap. Many independent categories → bar, with
   orientation "h" once the labels get long, and null otherwise so the app can
   fall back to horizontal by itself if they turn out longer than you expected.
4. If the user DID name a chart type, use it wherever the shape above allows. When it does
   not — a pie over time, a donut of forty categories, two scales on one axis — plan the
   chart that reads correctly instead. A panel that misleads is worse than one that
   ignores a preference.

WHAT ARE THE HEADLINE NUMBERS?
- kpi — totals, rates, counts, MoM. span and row_span null. ONE kpi panel carrying several
  METRIC rows beats several single-number panels.

HOW DO CATEGORIES COMPARE?
- bar — independent categories ranked side by side. orientation "h" when labels are long
  (client, merchant, property names) or there are many categories.
- stacked_bar — the categories are PARTS THAT ADD UP to a meaningful total (cash + PIK,
  performing + delinquent). y is a LIST of the part columns. If the parts do not sum to
  something a reader would name, use plain bar.
- combo — a COUNT and a RATE share an x axis but not a scale (volume + approval rate,
  invoices + collection rate). The one case a stacked bar cannot cover, and the ONLY
  sanctioned way to put two scales on one panel. List the line measure LAST in y; exactly
  two measures, never three.
- area_line — a measure over time against a TARGET, THRESHOLD or PRIOR PERIOD in the
  SAME unit (OTIF % against target %, actual spend against budget). Filled area for the
  measure, dashed line for the reference, both on ONE axis — which is the difference
  from combo: combo has two scales because its measures are not comparable, this has one
  because they are, and a reader is meant to compare their heights directly. List the
  reference measure LAST in y. Never use it for two different units.

WHAT IS IT MADE OF?
- donut (preferred) or pie — share of a whole, at most 8 slices. Never over time, never a
  ranking with many categories.
- treemap — share of a whole with MORE parts than a donut can carry (roughly 10-60), or
  where the parts span orders of magnitude. Not for time, not for negative values.

HOW IS IT CHANGING?
- line — a rate, ratio or percentage over time; also 2-4 comparable series on ONE scale.
- area — a VOLUME or magnitude over time where the quantity under the curve matters
  (balance outstanding, processed volume, invoiced amount). Prefer area over two lines for
  "invoiced vs collected" style panels.
- Always ORDER BY the time column. Never a pie, donut, treemap or radar over time.

HOW DO TWO MEASURES RELATE?
- scatter — two numeric metrics per entity (DSCR vs LTV, take rate vs volume). color for a
  low-cardinality group when it adds something.
- bubble — the same, plus a third magnitude mapped to size.

WHERE IS IT CONCENTRATED?
- heatmap — TWO categorical dimensions with a numeric intensity (aging bucket × client,
  weekday × hour, segment × period). Prefer it over a crowded stacked_bar when the matrix
  itself is the answer.
- geo_us_states — x holds US states (two-letter codes or full names) and y is ONE numeric
  measure.
- geo_countries — x holds countries, ISO codes, ports or cities anywhere in the world and y
  is ONE numeric measure. The map frames itself to whatever region the data covers, so one
  country zooms in and several spread across continents draw the world.

WHERE ARE WE LOSING PEOPLE?
- funnel — ORDERED STAGES of one process where each stage is a subset of the one before
  (initiated → authorised → captured → settled; lead → qualified → won). Order the rows by
  stage, largest first. Never for unrelated categories that merely differ in size.

HOW DOES ONE ENTITY SCORE ACROSS DIMENSIONS?
- radar — 3 to 8 NAMED dimensions read as a single profile (a merchant scored on approval,
  fraud, chargeback, SLA and margin). More axes collide and stop meaning anything. Never
  over time, never for a single dimension.

WHAT NEEDS ATTENTION / SHOW ME THE ROWS?
- A row listing: display "table", and then chart_type, x, y, color and
  orientation are ALL null. Exceptions, watch-lists, data quality, row detail.

DISPLAY AND CHART_TYPE ARE ONE DECISION — set them together and make them agree:
- display "kpi"   → chart_type "kpi", span and row_span null. Headline numbers.
- display "chart" → a chart_type from the list above, with x and y set.
- display "both"  → the same, and the rows are shown under the chart. Prefer
  this over bare "chart" whenever the numbers are worth inspecting.
- display "table" → chart_type null, and x and y null with it.

A panel carrying x and y but no chart_type is a CONTRADICTION. It renders as a
grid of numbers, which is never what a panel with a category and a measure was
for. If you have chosen a column to group by and a column to measure, you have
already chosen a chart — name it. Most panels on a dashboard are charts; a table
is the exception you reach for deliberately, not the safe default.

Variety comes from asking different QUESTIONS, not from rotating chart types:
- A request that is a LIST OF METRICS will become a wall of KPI tiles and trend lines if
  you answer it literally. For each headline metric, also plan the CUT that explains it —
  the same measure broken down by merchant, channel, method, region, segment, stage or
  reason code. That breakdown is what bar, donut, treemap, heatmap, funnel and the maps are
  for, and it is where a dashboard stops being a report card and starts being an argument.
- Do NOT show the same measure three ways (KPI + bar + table) unless each cut reveals
  something different.
- Do NOT hand-build a dual-axis chart; combo is the only two-scale panel.
- Use color only for a low-cardinality categorical series; never color by a near-unique id.
# --- Rendering contracts (a panel of this type will NOT render unless you follow these) ---
- The x and y values must exactly match column aliases in the SQL.
- Add a "display" field to each panel with value "chart", "table", "both", or "kpi".
- If display is "table", set x, y, color, orientation and chart_type all to null.
- If display is "both", generate both a valid chart_type with x and y, and the table will also be shown.
- If the user explicitly says "as a table" for a panel, you MUST set "display" to "table" and set "chart_type" to null.
- For scatter/bubble panels: x and y are the two NUMERIC axis columns (one point per entity). For a bubble panel set chart_type to "bubble" and set "size" to a numeric column whose magnitude maps to the bubble radius (use "scatter" with size null when there is no third magnitude). Use "color" for a LOW-cardinality categorical column to split the points into named series; leave it null when there is no categorical grouping. The SQL must return one row per entity with the x, y, and (if used) size and color columns.
- For heatmap panels: prefer LONG-FORM SQL — one row per (row group × column category)
  with chart_type "heatmap", x = COLUMN-category (e.g. aging bucket), y = numeric intensity
  metric, color = ROW-grouping (e.g. client). Prefer ≤12 columns and ≤20 row groups.
  Alternatively WIDE form is allowed: color (or x) = row entity, y = a LIST of bucket/metric
  column aliases present in the SELECT (e.g. ["0-30","31-60","61-90","90+"]). Alias aging
  buckets with readable labels; never leave bucket labels blank.
- Exception / "worst overdue" / watch-list TABLES must filter to the risk population
  (e.g. PAYMENT_STATUS in overdue/disputed, or DAYS_OVERDUE > 0). Do not title a panel
  "overdue" and then include invoices that are not yet due.
- For stacked_bar panels: set chart_type to "stacked_bar", x to the category column, and y to a LIST of the numeric value columns that stack, e.g. ["CASH_RATE","PIK_RATE"]. Each listed column becomes one stacked series with its own color and legend entry, so the SQL MUST return exactly one row per x category with every listed value column present as its own aliased column. Do not use a color grouping column for stacked_bar — the series come from the y list, not from rows.
- For KPI panels, return metric rows with aliases METRIC, VALUE, and FORMAT (all three are REQUIRED), plus optional MOM_CHANGE_PCT or STATUS; set display to "kpi".
- KPI panels: FORMAT declares how each row's VALUE is rendered and MUST be one of: 'currency:<ISO code>' (money in a named currency — 'currency:AED' renders AED 5.2M), 'currency' (money in US dollars ONLY), 'currency:' (money whose currency cannot be named, e.g. a total that mixes currencies — renders with no symbol), 'percent' (a whole-number percent, e.g. 14 renders as 14%), 'count' (an integer tally), or 'ratio' (a plain multiple, e.g. 1.85). A KPI row has no column_formats, so FORMAT is the ONLY place its currency can be stated: when the semantic layer puts the money in any currency other than US dollars — a "Reporting currency" line, column names ending _AED or _EUR, descriptions saying "in AED" — every money row MUST name that code, e.g. 'currency:AED'. Bare 'currency' on dirhams prints a dollar sign over them. Author FORMAT per UNION ALL branch to match what THAT metric actually is — you already know each branch's meaning as you write it: a COUNT(*) is 'count', a SUM/AVG of money is 'currency:<its code>', a rate/share is 'percent', an average DSCR/TVPI/multiple is 'ratio'. Do NOT omit FORMAT and do NOT infer it from the metric name — the renderer formats each tile from FORMAT, not from the label text. Two metrics whose names share a word (e.g. "Debt Count" vs "Debt Outstanding") must each carry their own correct FORMAT ('count' vs 'currency:<code>').
- KPI panels: for a 'percent' FORMAT, VALUE must still be emitted already scaled to whole-number percent (multiply a 0-1 fraction column by 100, per the percentage rule above) — FORMAT only controls the % suffix, it does not rescale.
- KPI panels: MOM_CHANGE_PCT must be a percentage value (e.g., 5.2, -3.1), NOT a dollar amount. If month-over-month change is not calculable, omit the MOM_CHANGE_PCT column entirely. Never reuse the VALUE column's number as MOM_CHANGE_PCT.
- KPI panels: Each METRIC row must represent a DIFFERENT measure. Never have two rows with the same VALUE — this indicates a missing filter in the SQL.
- KPI panels: you MAY add an optional SEVERITY column to flag a row as a risk/alert signal for highlighted rendering. Allowed values: 'normal' (default look), 'warning' (amber), 'critical' (red). Set warning/critical ONLY for genuine risk metrics and, where possible, from the DATA itself rather than a constant — e.g. SELECT ..., CASE WHEN COUNT(*) > 0 THEN 'critical' ELSE 'normal' END AS SEVERITY for a breach/watch-list count, so a zero count is not highlighted. Do NOT infer risk from the metric's NAME (the renderer never reads the label to decide highlighting — same rule as FORMAT). Omit SEVERITY entirely for ordinary metrics; when omitted, tiles render in the default style.

# --- Output format ---
- Return only the raw JSON, no markdown, no explanation.

# --- Display choice ---
- Choose display to fit the panel: a chart whenever the panel groups by something and measures something, which is most of them; a table for detail, listing, and data-quality/alert panels; "both" when the rows are worth inspecting alongside the chart. Do NOT default to "table" — a breakdown returning four or five rows is a four-bar chart, not a listing. A result of exactly one row is a KPI tile, not a table.
- When in doubt between 'chart' and 'both' for a panel with more than a handful of rows, prefer 'both' — the drill-down table costs little and is often what users actually want to inspect. Reserve bare 'chart' for panels where the underlying data is large, high-cardinality, or not meaningfully inspectable as a table (e.g. a fine-grained time series).
- Add a KPI panel ("kpi") when the user wants headline numbers, totals, rates, or MoM/summary metrics; use a table panel for anomaly, data-quality, or detail listings. Use judgment on when these help rather than adding them mechanically.""".replace("<<ROW_CAP>>", str(_SF_CAP))

_DASHBOARD_SCHEMA = """Return ONLY a valid JSON object with this exact structure, no markdown fences, no explanation:
{
  "dashboard_title": "string",
  "parameters": [
    {
      "name": "snake_case identifier, referenced in panel SQL as {{name}}",
      "type": "date|string|number|enum — a filter over a small known set of values (period label, status, category) MUST be enum, never string; string is only for genuine free-text search",
      "default": "value used when no value is supplied at run time",
      "options": ["allowed values — REQUIRED for type enum (the real distinct values), otherwise omit"],
      "bindings": [
        {
          "table": "DB.SCHEMA.TABLE this date parameter filters",
          "role": "the date_role from that table's 'Date columns' metadata to bind to (DATE parameters only)"
        }
      ],
      "panels": ["titles of the panels whose SQL uses this parameter"]
    }
  ],
  "needs_clarification": "OMIT normally. Set to a question ONLY when you cannot bind a requested date parameter because a table has no date column, or the choice is genuinely ambiguous — see the date-parameter rules.",
  "panels": [
    {
      "title": "string",
      "sql": "single valid Snowflake SELECT statement",
      "chart_type": "bar|stacked_bar|line|area|pie|donut|scatter|bubble|heatmap|radar|treemap|funnel|combo|area_line|geo_us_states|geo_countries|kpi — ONLY these; do not invent other types. geo_us_states is a US state choropleth: use it ONLY when x is a US state column (two-letter codes or full state names). geo_countries is a country choropleth for anywhere else: use it when x holds countries, ISO codes, ports or cities — it frames itself to whatever region the data covers, so one country zooms in and several spread across continents draw the world. Both maps need y to be ONE numeric measure — a map of anything else is unreadable. radar compares 3-8 named dimensions as a profile (more axes collide and stop meaning anything). treemap is part-to-whole when there are too many slices for a pie. funnel is stages of a process, largest first. combo is bars plus a line on a SECOND axis — the one case a stacked bar cannot cover, where a count and a rate share an x axis but not a scale; list the line measure LAST in y. area_line is a filled area plus a dashed reference line on ONE shared axis — a measure against a target, threshold or prior period in the SAME unit; list the reference measure LAST in y.",
      "x": "column name for x axis or labels",
      "y": "column name for y axis or values (for stacked_bar: a LIST of the value column names that stack, e.g. [\"CASH_RATE\",\"PIK_RATE\"])",
      "color": "column name for color grouping or null",
      "size": "column name mapping to bubble radius (bubble only) or null",
      "orientation": "\"h\" for horizontal bars, \"vertical\" to keep them upright, or null to let the app decide. null is the safe default: it draws upright and turns the bars on their side by itself if the labels turn out too long to fit. Use \"vertical\" only when upright is the point — a time-ordered or ranked-by-size series that must read left to right. Do NOT emit \"v\"; it is an older spelling that means null.",
      "display": "chart|table|both|kpi — how to PRESENT the panel, and it MUST agree with chart_type. 'chart' and 'both' require a chart_type plus x and y; 'kpi' requires chart_type kpi; 'table' requires chart_type, x and y to be null. Default to showing a chart — a table is for row-level detail and exception listings, not for any result that happens to be small.",
      "section": "the tab this panel belongs on — see the composition rules",
      "span": "how many of the 4 grid columns this panel occupies: 2 or 4 for charts (prefer 2), 4 for tables; never 1 for a chart; null for KPI panels",
      "row_span": "how tall the panel is: 1 (short), 2 (normal — the usual choice), 3 (tall), 4 (tallest); null for KPI panels",
      "column_formats": "OPTIONAL. How this panel's columns should READ, keyed by column name: {\"BALANCE_DUE\": {\"format\": \"currency\"}, \"SHARE\": {\"format\": \"percent\", \"decimals\": 1}, \"CLIENT_NM\": {\"label\": \"Client\"}}. format is one of currency|percent|number|ratio|text|date; decimals overrides the default precision; label renames the header WITHOUT changing the SQL alias; currency is an ISO 4217 code and applies only alongside format currency. ALWAYS set currency when the money is not US dollars — take the code from the column name (AMOUNT_AED, EUR_BALANCE), from the semantic layer's description of the column, or from a currency column in the same table. Set it to the EMPTY STRING "" when the column mixes currencies or you genuinely cannot tell: omitting it means US dollars, and printing dollars over dirhams states a wrong fact about the amount that nothing on screen lets a reader catch. Example: {"FRAUD_LOSS_AED": {"format": "currency", "currency": "AED"}}. Declare a format whenever the column name does not make its unit obvious — TOTAL, AMT, OPENING_BAL and the like otherwise render as bare numbers. Omit entirely when every column reads correctly from its name."
    }
  ]
}
Omit "parameters" (or use []) when the dashboard has no runtime inputs — that is the common case. A panel's "sql" may contain {{parameter_name}} placeholders for any declared parameter."""


def generate_dashboard_plan(description: str, business_context: str,
                            heuristics: str = "") -> DashboardPlan:
    """Design a multi-panel dashboard from a natural-language description.

    business_context is metadata_store.build_context() output (table meanings,
    join rules, metric SQL); heuristics is the config's domain-specific dashboard
    guidance. Returns a DashboardPlan; raises LLMError on unparseable output.
    """
    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are a senior data analyst and dashboard designer for a "
                "Snowflake financial database. The user wants a multi-panel "
                "dashboard; design it.\n\n" + _DASHBOARD_SCHEMA + "\n\n"
                + _DASHBOARD_RULES
            ),
        },
        {
            "type": "text",
            "text": (
                "SCHEMA / BUSINESS CONTEXT (use these exact tables, columns, "
                "join rules, and metric SQL):\n" + business_context
                + (f"\n\nDOMAIN-SPECIFIC RULES:\n{heuristics}" if heuristics else "")
            ),
            # Cache the big context block — re-sent across dashboard requests for
            # the same database within the cache window.
            "cache_control": {"type": "ephemeral"},
        },
    ]

    resp = _create(
        "dashboard_plan",
        model=MODEL_DASHBOARD,
        max_tokens=_PLAN_MAX_TOKENS,
        stream=True,
        system=system_blocks,
        messages=[{"role": "user", "content": description}],
        # NB: no temperature — it is deprecated/rejected for Opus 4.8.
    )
    raw = _plan_text(resp, "Dashboard planner")
    try:
        data = _json_object(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(_unparseable("Dashboard planner", raw, exc)) from exc

    clarify = (data.get("needs_clarification") or "").strip()
    panels = data.get("panels") or []   # a clarification response may carry no panels
    for panel in panels:
        if panel.get("sql"):
            panel["sql"] = _fix_doubled_quotes(panel["sql"])

    title = data.get("dashboard_title") or "Dashboard"
    parameters = data.get("parameters") or []
    return DashboardPlan(title=title, panels=panels, parameters=parameters,
                         needs_clarification=clarify)


# Lighter rules for uploaded spreadsheets and document folders (DuckDB). The
# user drives content through conversation, so these focus on SQL validity and
# rendering contracts rather than warehouse-specific heuristics.
_FILE_DASHBOARD_RULES = """RULES:

# --- SQL validity (DuckDB, uploaded sources) ---
- Each SQL must be a single valid DuckDB SELECT (or WITH … SELECT).
- Use BARE table names exactly as listed in the schema context below. Do NOT qualify with DATABASE.SCHEMA.TABLE — there is no such form here and qualified names fail.
- Date literals are ISO strings: '2026-01-31'.
- Use NULLIF(denominator, 0) to avoid division by zero.
- Money that is NOT US dollars MUST declare its currency in column_formats:
  {"AMOUNT_AED": {"format": "currency", "currency": "AED"}} — an ISO 4217
  code taken from the column name or the source's own definitions, or "" when
  the column mixes currencies. Left undeclared, dirhams print as dollars.
- A KPI FORMAT for money MUST name its currency after a colon — "currency:AED"
  — whenever the money is not US dollars. Bare "currency" means US dollars
  ONLY; "currency:" with nothing after it means the currency cannot be named.
- Percentages must be whole numbers for the renderer (14.0 for 14%, NOT 0.14).
- Always ORDER BY the x-axis/time column in chart SQL.
- Every panel must return AT MOST <<ROW_CAP>> ROWS. The file engine caps results there and drops the rest without a word, and because chart SQL is ordered oldest-first the rows lost are the most RECENT ones — the end of the trend a reader came to see. Aggregate to the grain the panel actually shows (a month rather than a day, a category rather than a row per record), restrict to a recent window, or rank and LIMIT deliberately.
- When computing a subset metric, include the appropriate WHERE filter.

# --- Document folders (facts / entities tables) ---
- Query extracted figures from `facts`; join `entities` for names: facts.entity_id = entities.entity_id.
- facts holds one row per extracted figure — filter by metric and aggregate (GROUP BY entity) to avoid duplicate rows per property.
- Use entity_aliases when matching spreadsheet names to document names (see schema context).
- Cite source_file and source_page in table panels when showing document-sourced figures.

# --- Spreadsheet uploads ---
- Tables include provenance columns: as_of, source_file, source_sheet, source_row. Use as_of for period trends (GROUP BY as_of).
- Multiple files of the same shape are stacked — a plain query over the table includes all of them unless filtered.

# --- Parameters (optional) ---
- Declare runtime parameters only when the user wants to re-run for different dates or filters. Reference as {{name}} without extra quotes for dates.
- Omit parameters when not needed.

# --- Rendering contracts (same as warehouse dashboards) ---
- The x and y values must exactly match column aliases in the SQL.
- Add "display": "chart", "table", "both", or "kpi" to each panel.
- If display is "table", set chart_type, x, y, color, orientation to null.
- For KPI panels return METRIC, VALUE, FORMAT (currency:<ISO code>|currency|currency:|percent|count|ratio — the currency rule above decides which) and optional MOM_CHANGE_PCT or SEVERITY.
- For stacked_bar set y to a LIST of value column names.
- Generate 3–8 panels unless the user asks for fewer — match what they described, do not pad with unrelated panels.

# --- Chart cookbook: pick the chart the QUESTION asks for ---
Allowed chart_type values: bar, stacked_bar, line, area, pie, donut, scatter, bubble,
heatmap, radar, treemap, funnel, combo, area_line, geo_us_states,
geo_countries, kpi.
Do NOT emit histogram or any type not on that list — it is not rendered.
Pick the plainest chart that answers the question; reach for a specialised one only when
its precondition is met. Decide from the SHAPE of the result you are about to SELECT — the
user will rarely name a chart type, and choosing it is your job: a date on x → the time
family; one category → the comparison family; two categories carrying a measure → heatmap;
a place → a map; ordered subset stages → funnel; one row → kpi. Where the user did name a
type, use it unless the shape makes it misleading, and then chart what reads correctly.
- Headline totals / rates → kpi (span and row_span null).
- Category comparison → bar (orientation "h" when labels are long).
- Parts of a total → stacked_bar (y is a LIST of part columns).
- Share of a whole, ≤8 slices → donut (preferred) or pie; never over time.
- Share of a whole, 10-60 parts or magnitudes spanning orders → treemap.
- Over time → line for rates and ratios; area when volume matters (prefer area for
  invoiced vs collected); ORDER BY the time column.
- A count and a rate on one x axis but different scales → combo (line measure LAST in y).
  The same unit at very different sizes is also different scales: OTIF near 90% beside an
  exception rate near 1% on one axis draws two flat lines. Use combo, or two panels.
- A measure against a target or threshold in the SAME unit → area_line (reference
  measure LAST in y). Different units instead → combo.
- Two numeric metrics per entity → scatter; add size for a third → bubble.
- Two categoricals + numeric intensity (aging × client, period × segment) → heatmap
  (long-form: x = column category, y = metric, color = row group; or wide: color = row,
  y = LIST of bucket columns with readable aliases).
- Ordered stages where each is a subset of the one before → funnel, largest first.
- One entity scored across 3-8 named dimensions → radar.
- US states → geo_us_states; countries, ISO codes, ports or cities → geo_countries. Both
  need y to be ONE numeric measure.
- Exceptions and row detail → display "table" with chart_type, x and y ALL null;
  "overdue"/"worst" tables MUST filter to overdue rows.
- display and chart_type are ONE decision and must agree: "chart"/"both" need a
  chart_type with x and y, "kpi" needs chart_type kpi, "table" needs all three
  null. A panel with x and y but no chart_type is a contradiction and renders as
  a grid of numbers — if you picked a group-by column and a measure, name the
  chart. Most panels are charts; a table is a deliberate exception.
- Variety comes from asking different questions, not from rotating chart types. For each
  headline metric, plan the breakdown that explains it rather than another trend line.
- No hand-built dual-axis charts; combo is the only two-scale panel.
# --- Composition: design a dashboard, do not just list panels ---
Decide what QUESTION this dashboard answers before writing any SQL, then order the panels to answer it: current position first (KPIs), then movement over as_of periods, then composition, then exceptions, then the row-level detail table. Every panel must add something the panels before it did not — do not show one measure as a KPI, again as a bar, and again as a table unless each cut reveals something different. Write titles that say what the panel shows in the data's own terms, never the chart type or a bare column name.

# --- Sections (tabs) ---
- Give every panel a "section": the tab it belongs on, grouped by the question a reader is asking rather than by table or chart type.
- Most file dashboards are small: use ONE section (the same name on every panel) when there are fewer than 12 chart and table panels, or no genuinely separable concerns. When you do split, use at most 4 sections, put the headline KPIs on the first one, and name them in the domain's own words (1–2 words, title case) — never "Tab 1", "Charts", or "Other". A section needs at least 6 chart or table panels to deserve a tab, and 6 to 8 is the aim; use fewer tabs rather than thinner ones, and never add a panel only to fill one. A smaller tab is folded into its neighbour after planning.

# --- Panel size (the grid is 4 columns wide) ---
- "span" is how much width a panel needs to be legible, "row_span" how much height — never a function of how many rows the query returns.
- span 4 for row-level tables, long as_of time series, and the one panel carrying the main point; span 2 (half) as the DEFAULT for most charts. Prefer 2+2 rows.
- Do NOT use span 1 for charts — they are illegible that narrow. Prefer span 2 or 4.
- row_span 2 is the NORMAL height; use 1 for a panel that reads better short, 3 for a many-row table, heatmap, or scatter needing vertical room, and 4 only for a long detail table that is the point of its section.
- Make each section's spans add up to full rows of 4 where you can (4, or 2+2) so no row is left with a permanent hole. Avoid 2+1+1 and 3+1 patterns.
- KPI panels render in a band above the grid, so set span and row_span to null when display is "kpi".

# --- Output ---
- Return only the raw JSON, no markdown, no explanation.
- Set needs_clarification ONLY when you truly cannot write valid SQL from the available tables.""".replace("<<ROW_CAP>>", str(_FILE_CAP))


def generate_file_dashboard_plan(description: str, business_context: str) -> DashboardPlan:
    """Design a dashboard for an uploaded spreadsheet or document folder."""
    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are a senior data analyst designing a multi-panel dashboard over "
                "uploaded spreadsheets and/or extracted document figures (DuckDB). "
                "The user describes what they want; design panels that answer that.\n\n"
                + _DASHBOARD_SCHEMA
                + "\n\n"
                + _FILE_DASHBOARD_RULES
            ),
        },
        {
            "type": "text",
            "text": (
                "SCHEMA / SOURCE CONTEXT (use these exact table and column names):\n"
                + business_context
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    resp = _create(
        "file_dashboard_plan",
        model=MODEL_DASHBOARD,
        max_tokens=_PLAN_MAX_TOKENS,
        stream=True,
        system=system_blocks,
        messages=[{"role": "user", "content": description}],
    )
    raw = _plan_text(resp, "File dashboard planner")
    try:
        data = _json_object(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(_unparseable("File dashboard planner", raw, exc)) from exc

    clarify = (data.get("needs_clarification") or "").strip()
    panels = data.get("panels") or []
    for panel in panels:
        if panel.get("sql"):
            panel["sql"] = _fix_doubled_quotes(panel["sql"])

    title = data.get("dashboard_title") or "Dashboard"
    parameters = data.get("parameters") or []
    return DashboardPlan(
        title=title,
        panels=panels,
        parameters=parameters,
        needs_clarification=clarify,
    )


# --- Dashboard edits: the model returns the CHANGE, not the dashboard ---------
#
# An edit used to have the model re-write the whole plan with the change applied.
# Output grew with the dashboard, not with the request: at ten panels of heavy
# SQL, adding ONE panel no longer fit the output ceiling, and every attempt
# failed at the same place. It was also the most expensive way to change a
# title. Now the model names operations against panel indexes and this module
# applies them, so an edit costs what it changes, and a panel the request does
# not mention never passes through the model at all.

_EDIT_RESPONSE_FORMAT = """EDIT RESPONSE FORMAT — THIS REPLACES THE TOP-LEVEL STRUCTURE ABOVE.
The dashboard structure above tells you what a panel CONTAINS. For this edit do NOT return a dashboard: return ONLY this JSON object, no markdown fences, no explanation:
{
  "operations": [
    {"op": "update", "index": 2, "fields": {"chart_type": "line", "title": "Monthly GMV"}},
    {"op": "add", "after": 4, "panel": {"title": "...", "sql": "...", "chart_type": "bar", "x": "...", "y": "...", "display": "chart", "section": "...", "span": 2, "row_span": 2}},
    {"op": "remove", "index": 7},
    {"op": "move", "index": 3, "after": -1}
  ],
  "dashboard_title": "OMIT unless the request renames the dashboard",
  "parameters": "OMIT unless the request changes parameters; when present, the COMPLETE new parameters list",
  "needs_clarification": "OMIT normally; a question, with operations [], only when the change cannot be made"
}
- Each current panel carries an "index". Every "index" and "after" refers to that ORIGINAL index, never to a position after your other operations. "after": -1 means before the first panel; omit "after" on an add to append at the end.
- update: "fields" holds ONLY the fields that change. Every field you leave out stays exactly as it is, so never repeat unchanged fields and never re-send unchanged SQL. Setting a field to null clears it.
- add: a complete new panel. Put it on a section that ALREADY EXISTS unless the request explicitly asks for a new tab.
- Emit operations only for what the request asks. Panels you do not name are left untouched.
- One response can hold many operations: "add these seven panels" is seven add operations in a single reply, never a reason to split the request."""


def _plan_for_editor(current_plan: dict) -> str:
    """The saved plan as the editor sees it: each panel numbered, empty fields
    dropped.

    Numbered because operations address panels by index — titles repeat. Empty
    fields dropped because a stored panel carries every key whether used or not,
    and the model read "palette": null once per panel for nothing.
    """
    panels = [
        {"index": i, **{k: v for k, v in panel.items() if v is not None}}
        for i, panel in enumerate(current_plan.get("panels") or [])
    ]
    shown: dict = {"title": current_plan.get("title"), "panels": panels}
    if current_plan.get("parameters"):
        shown["parameters"] = current_plan["parameters"]
    return json.dumps(shown)


def _panel_ref(op: dict, key: str, count: int, what: str, *, allow_start: bool = False) -> int:
    value = op.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LLMError(f"{what} gave {key}={value!r}, which is not a panel index.")
    low = -1 if allow_start else 0
    if not low <= value < count:
        raise LLMError(
            f"{what} referred to panel {value}, but the dashboard's panels are "
            f"numbered 0 to {count - 1}."
        )
    return value


def _apply_edit_operations(panels: list[dict], operations: list, what: str) -> list[dict]:
    """The saved panels with the model's operations applied.

    Positions are resolved against the ORIGINAL indexes, so the operations can
    arrive in any order: each panel gets a sort key — its original slot, or the
    slot it was added or moved after — and the list is sorted once at the end.
    Anything the operations do not name comes back as it was stored.
    """
    count = len(panels)
    entries = [{"key": (i, 0, 0), "panel": dict(panel), "removed": False}
               for i, panel in enumerate(panels)]
    added: list[dict] = []
    for seq, op in enumerate(operations, start=1):
        if not isinstance(op, dict):
            raise LLMError(f"{what} returned an operation that is not an object: {op!r}")
        kind = op.get("op")
        if kind == "update":
            fields = op.get("fields")
            if not isinstance(fields, dict):
                raise LLMError(f"{what} returned an update with no fields.")
            entry = entries[_panel_ref(op, "index", count, what)]
            entry["panel"].update({k: v for k, v in fields.items() if k != "index"})
            if "sql" in fields and entry["panel"].get("sql"):
                entry["panel"]["sql"] = _fix_doubled_quotes(entry["panel"]["sql"])
        elif kind == "remove":
            entries[_panel_ref(op, "index", count, what)]["removed"] = True
        elif kind == "move":
            index = _panel_ref(op, "index", count, what)
            entries[index]["key"] = (_panel_ref(op, "after", count, what, allow_start=True), 1, seq)
        elif kind == "add":
            panel = op.get("panel")
            if not isinstance(panel, dict) or not panel:
                raise LLMError(f"{what} returned an add with no panel.")
            panel = {k: v for k, v in panel.items() if k != "index"}
            if panel.get("sql"):
                panel["sql"] = _fix_doubled_quotes(panel["sql"])
            after = (count - 1 if op.get("after") is None
                     else _panel_ref(op, "after", count, what, allow_start=True))
            added.append({"key": (after, 1, seq), "panel": panel, "removed": False})
        else:
            raise LLMError(f"{what} returned an unknown operation {kind!r}.")

    live = [entry for entry in entries + added if not entry["removed"]]
    live.sort(key=lambda entry: entry["key"])
    return [entry["panel"] for entry in live]


def _edited_plan(current_plan: dict, data: dict, what: str) -> "DashboardPlan":
    """Turn the editor's reply into the COMPLETE updated plan callers expect.

    A reply without "operations" is a model that returned a whole dashboard
    anyway. It is taken as the full plan, which is exactly what this function
    accepted before operations existed — and the caller still diffs it against
    the saved version, so nothing it changed goes unseen.
    """
    clarify = (data.get("needs_clarification") or "").strip()
    operations = data.get("operations")
    if operations is None:
        panels = data.get("panels") or []
        for panel in panels:
            if panel.get("sql"):
                panel["sql"] = _fix_doubled_quotes(panel["sql"])
    elif clarify and not operations:
        panels = []
    else:
        if not isinstance(operations, list):
            raise LLMError(f"{what} returned operations that are not a list.")
        panels = _apply_edit_operations(current_plan.get("panels") or [], operations, what)

    title = data.get("dashboard_title") or current_plan.get("title") or "Dashboard"
    parameters = data.get("parameters")
    if parameters is None:
        parameters = current_plan.get("parameters") or []
    return DashboardPlan(title=title, panels=panels, parameters=parameters,
                         needs_clarification=clarify)


def edit_dashboard_plan(current_plan: dict, changes_description: str,
                        business_context: str, heuristics: str = "") -> DashboardPlan:
    """Apply ONE requested change to an existing dashboard plan and return the
    full updated plan.

    This is a NARROW edit, not a re-plan: everything the change does not require
    must come back byte-identical to `current_plan` (same panels, same order,
    same titles, same SQL, same chart config). The caller diffs the result
    against the original and surfaces anything outside the requested change, so
    the model is instructed hard against incidental edits.

    current_plan is the stored {title, panels:[...]} object. Returns a
    DashboardPlan; raises LLMError on unparseable output.
    """
    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are EDITING an existing Snowflake dashboard plan, not "
                "designing a new one. Apply ONLY the change the user asks for and "
                "change NOTHING else: do not re-generate, re-alias, reorder, "
                "restyle, add, or remove any panel the request did not ask you to "
                "touch. You describe the change as operations; panels you do not "
                "name are kept exactly as saved.\n\n"
                + _DASHBOARD_SCHEMA + "\n\n" + _DASHBOARD_RULES
                + "\n\n" + _EDIT_RESPONSE_FORMAT
            ),
        },
        {
            "type": "text",
            "text": (
                "SCHEMA / BUSINESS CONTEXT (use these exact tables, columns, "
                "join rules, and metric SQL):\n" + business_context
                + (f"\n\nDOMAIN-SPECIFIC RULES:\n{heuristics}" if heuristics else "")
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user = (
        "CURRENT DASHBOARD PLAN (JSON, each panel numbered by index):\n"
        + _plan_for_editor(current_plan)
        + "\n\nREQUESTED CHANGE (apply only this):\n" + changes_description
        + "\n\nReturn the operations JSON described in EDIT RESPONSE FORMAT."
    )

    resp = _create(
        "dashboard_edit",
        model=MODEL_DASHBOARD,
        max_tokens=_PLAN_MAX_TOKENS,
        stream=True,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
    )
    raw = _plan_text(resp, "Dashboard editor")
    try:
        data = _json_object(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(_unparseable("Dashboard editor", raw, exc)) from exc
    return _edited_plan(current_plan, data, "Dashboard editor")


def edit_file_dashboard_plan(
    current_plan: dict,
    changes_description: str,
    business_context: str,
) -> DashboardPlan:
    """Apply ONE requested change to an existing file-source dashboard plan."""
    system_blocks = [
        {
            "type": "text",
            "text": (
                "You are EDITING an existing dashboard over uploaded spreadsheets "
                "and/or extracted document figures (DuckDB), not designing a new one. "
                "Apply ONLY the change the user asks for and change NOTHING else: "
                "do not re-generate, re-alias, reorder, restyle, add, or remove any "
                "panel the request did not ask you to touch. You describe the change "
                "as operations; panels you do not name are kept exactly as saved.\n\n"
                + _DASHBOARD_SCHEMA
                + "\n\n"
                + _FILE_DASHBOARD_RULES
                + "\n\n"
                + _EDIT_RESPONSE_FORMAT
            ),
        },
        {
            "type": "text",
            "text": (
                "SCHEMA / SOURCE CONTEXT (use these exact table and column names):\n"
                + business_context
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user = (
        "CURRENT DASHBOARD PLAN (JSON, each panel numbered by index):\n"
        + _plan_for_editor(current_plan)
        + "\n\nREQUESTED CHANGE (apply only this):\n"
        + changes_description
        + "\n\nReturn the operations JSON described in EDIT RESPONSE FORMAT."
    )

    resp = _create(
        "file_dashboard_edit",
        model=MODEL_DASHBOARD,
        max_tokens=_PLAN_MAX_TOKENS,
        stream=True,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
    )
    raw = _plan_text(resp, "File dashboard editor")
    try:
        data = _json_object(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(_unparseable("File dashboard editor", raw, exc)) from exc
    return _edited_plan(current_plan, data, "File dashboard editor")


# --- Edit-panel assistant ------------------------------------------------------
#
# The same operations format as edit_dashboard_plan, used from inside a
# dashboard's Edit panel. Two differences from the main-chat edit: it works on
# the person's unsaved DRAFT rather than the saved version, and it talks back —
# every reply carries a short message, and a question gets advice with no
# change at all.

#: Settings the Edit panel exposes beyond SQL, chart and layout. The planner
#: never sets these, so the planner's schema does not describe them; the
#: assistant has to, or "hide the legend" has nothing to act on. The values
#: mirror dashboards.FORMAT_CHOICES and PALETTE_NAMES, which drop anything else.
_PRESENTATION_FIELDS = """PRESENTATION FIELDS — further panel settings the Edit panel exposes. All optional; absent means the chart standard applies, so set only what the person asks for.
- "format": an object, every key optional:
  "x_title" / "y_title": axis title text; "" hides that title.
  "unit": "none" | "K" | "M" | "B" — the scale value-axis numbers are written in.
  "data_labels": "none" | "max" | "min" | "extremes" | "all" — values written on the chart.
  "legend": "bottom" | "right" | "none".
  "label_position": "auto" | "inside_end" | "outside_end" — bar, grouped_bar and combo only.
  "markers": "auto" | "show" | "hide" — line, area, combo and area_line only.
  "stack_totals": true | false — stacked_bar only.
  "funnel_percent": "first" | "previous" | "none" — funnel only.
  "trend_line": true | false; "x_min", "x_max", "y_min", "y_max": numbers; "x_reference" / "y_reference": {"kind": "mean"} | {"kind": "median"} | {"kind": "value", "value": <number>} — scatter and bubble only.
  "series": {"<VALUE COLUMN>": {"data_labels": ..., "label_position": ..., "markers": ...}} — per-series overrides.
  In an update, "format" is MERGED into the panel's current format: send only the keys that change, and a key set to null goes back to automatic. "series" merges the same way, column by column. Sending "format": null clears every format setting.
  Which of these a panel actually uses depends on its chart type: see WHAT EACH PANEL CAN USE with the draft.
- "palette": "ocean" | "ember" | "meadow" | "slate", or null for the default colours.
- "zero_baseline": false lets the value axis fit the data instead of starting at zero; null anchors it at zero. Not for stacked_bar, pie, donut, treemap or heatmap."""

_ASSIST_ROLE = """You are the assistant inside a dashboard's Edit panel. The person is editing an UNSAVED DRAFT of the dashboard. Your changes are applied to that draft on their screen; nothing is saved until they press "Save and refresh".

Two kinds of message reach you:
- A change ("make the trend a line chart", "add approvals by product"): make exactly that change as operations, and nothing else.
- A question or a request for help deciding ("which chart suits this panel?", "what else could this dashboard show?"): answer it and recommend, with "operations": []. Offer to make the change; do not make it until they ask.

When a request could mean more than one panel, ask which one, with "operations": [], rather than guessing.

You cannot see the data. You see the draft's SQL and the schema context, nothing else. Never state a value, a count, a total or a category name (a region, a band, a product) unless it is written in the SQL or the context. Say what a panel's query does instead, and when a recommendation depends on the data, say what to check.

The draft in the last message is the CURRENT state, including changes made by hand and in earlier turns. Earlier messages in this conversation record what was asked and changed before; lines such as "Undid the last change", "was not applied" and "Saved as version N" say what became of a change."""

#: How the app draws charts, beyond what each panel's settings list says, and
#: how to work from the draft. The chart facts mirror sortHeatLabels in
#: frontend/src/lib/dashboard-format.ts and the axis plan in dashboard-panel.tsx;
#: which settings a chart type reads, and their defaults, are listed per panel
#: from panel_capabilities instead of being written out here.
_ASSIST_GUIDANCE = """HOW THIS APP DRAWS CHARTS. You cannot see the rendered chart; reason from these facts and from WHAT EACH PANEL CAN USE.
- Heatmap COLUMNS are ordered by their label text, with numbers inside a label compared as numbers; the query's ORDER BY does not change that. Heatmap rows keep the query's order. For time columns, return labels that sort as text, such as '2025-01' or '2025-Q1'.
- A date or timestamp on the x axis is drawn as a sequence: when its labels do not all fit, every Nth one is shown, and no setting forces all of them. To show every period, return a text label column instead (for example '2025-Q1'); text categories are never dropped. A full-width panel (span 4) also fits more labels.
- Other charts keep the order the query returns.
- x, y, color and size must be column names exactly as the query returns them.

WORKING FROM THE DRAFT
- Before you change, promise or explain anything about a panel, read that panel in the draft and its line in WHAT EACH PANEL CAN USE. The draft is the truth: earlier messages may describe changes that were undone, reverted or never saved. If the draft does not show a setting, it is not set; an unset setting means what the list says it means.
- NOT SAVED YET lists what the draft has that the dashboard page does not show until the person saves.
- To make something visible, set every setting it depends on, as the list describes. When the person says they cannot see something, believe them, find the reason in the draft and the list, and fix it.
- Only the settings in the list exist for a panel. If a request cannot be met with them, say so plainly and offer the nearest thing that can; never tell the person to set something themselves.
- Act on the visible problem. Ask at most one question in a row; if the answer is vague, make the most reasonable change, say what you assumed, and mention that Undo reverses it.
- Every change you return is checked against the list and against the columns the query returns before it reaches the draft; a change that fails is not applied. Say a change is done only if it is in your operations."""


_ASSIST_REPLY_FORMAT = """REPLY — also include in the same JSON object:
  "reply": "REQUIRED. 1-4 short plain sentences to the person. After a change, say what you changed, naming panels by title. After a question, give your recommendation and the reason in a sentence. Never claim a change that is not in operations. Do not mention indexes, JSON or operations."
"""

#: Marks each draft panel with its position while operations are applied, so
#: the caller can tell which result panel came from which draft panel. Never
#: shown to the model: it is added after the prompt is built.
_ORIGIN_KEY = "__draft_index"

#: Earlier messages of the conversation sent with each request. Enough to know
#: what was changed a few turns ago; not a whole afternoon of edits.
_ASSIST_HISTORY_MESSAGES = 12


def _assist_history(history: list[dict]) -> list[dict]:
    """Earlier turns as a Messages-API list: alternating, starting with the user.

    The stored thread can hold two assistant messages in a row — a reply and a
    "saved as version N" note — and the API refuses that, so neighbours with
    the same role are joined.
    """
    turns: list[dict] = []
    for item in history[-_ASSIST_HISTORY_MESSAGES:]:
        role = item.get("role")
        text = str(item.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    # The request itself is the next user message, so history must end on the
    # assistant.
    if turns and turns[-1]["role"] == "user":
        turns.pop()
    return turns


def assist_dashboard_edit(current_plan: dict, message: str, history: list[dict],
                          business_context: str, heuristics: str = "",
                          *, source: str = "snowflake", grounding: str = "") -> dict:
    """The Edit-panel assistant's answer to one message: the raw JSON object,
    holding "reply" and, for a change, "operations".

    `current_plan` is the draft on screen. `history` is earlier turns as
    [{"role", "text"}]. `source` is "snowflake" or "file", which decides the
    SQL rules the model follows. `grounding` is sent after the draft: what each
    panel can use, and what is not saved yet. Applying the operations is
    apply_assist_operations.
    Raises LLMError when the model's output cannot be read.
    """
    if source == "file":
        rules = _FILE_DASHBOARD_RULES
        reads = "uploaded spreadsheets and/or extracted document figures (DuckDB)"
    else:
        rules = _DASHBOARD_RULES
        reads = "a Snowflake database"

    system_blocks = [
        {
            "type": "text",
            "text": (
                _ASSIST_ROLE
                + f"\n\nThis dashboard reads {reads}.\n\n"
                + _DASHBOARD_SCHEMA + "\n\n" + rules + "\n\n"
                + _PRESENTATION_FIELDS + "\n\n"
                + _ASSIST_GUIDANCE + "\n\n"
                + _EDIT_RESPONSE_FORMAT + "\n\n" + _ASSIST_REPLY_FORMAT
            ),
        },
        {
            "type": "text",
            "text": (
                "SCHEMA / BUSINESS CONTEXT (use these exact tables, columns, "
                "join rules, and metric SQL):\n" + (business_context or "(none available)")
                + (f"\n\nDOMAIN-SPECIFIC RULES:\n{heuristics}" if heuristics else "")
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user = (
        "CURRENT DRAFT (JSON, each panel numbered by index):\n"
        + _plan_for_editor(current_plan)
        + (f"\n\n{grounding}" if grounding else "")
        + "\n\nMESSAGE:\n" + message
        + "\n\nReturn the JSON described in EDIT RESPONSE FORMAT, including \"reply\"."
    )

    resp = _create(
        "dashboard_edit_assist",
        model=MODEL_DASHBOARD_ASSIST,
        max_tokens=_PLAN_MAX_TOKENS,
        stream=True,
        system=system_blocks,
        messages=[*_assist_history(history), {"role": "user", "content": user}],
    )
    raw = _plan_text(resp, "Edit assistant")
    try:
        return _json_object(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(_unparseable("Edit assistant", raw, exc)) from exc


def merge_format(current: dict | None, change: dict | None) -> dict | None:
    """A panel's format with an update merged in: a key set to null goes back
    to automatic, and per-series settings merge column by column."""
    if not isinstance(change, dict):
        return change
    merged = dict(current) if isinstance(current, dict) else {}
    for key, value in change.items():
        if key == "series" and isinstance(value, dict):
            series = dict(merged.get("series") or {}) if isinstance(merged.get("series"), dict) else {}
            for column, settings in value.items():
                if settings is None:
                    series.pop(column, None)
                elif isinstance(settings, dict) and isinstance(series.get(column), dict):
                    entry = {k: v for k, v in {**series[column], **settings}.items() if v is not None}
                    if entry:
                        series[column] = entry
                    else:
                        series.pop(column, None)
                else:
                    series[column] = settings
            merged["series"] = series or None
        else:
            merged[key] = value
    merged = {key: value for key, value in merged.items() if value is not None}
    return merged or None


def _merged_format_update(op: object, draft_panels: list[dict]) -> object:
    """An update operation whose "format" is merged into the draft panel's."""
    if not isinstance(op, dict) or op.get("op") != "update":
        return op
    fields, index = op.get("fields"), op.get("index")
    if (not isinstance(fields, dict) or not isinstance(fields.get("format"), dict)
            or isinstance(index, bool) or not isinstance(index, int)
            or not 0 <= index < len(draft_panels)):
        return op
    merged = merge_format(draft_panels[index].get("format"), fields["format"])
    return {**op, "fields": {**fields, "format": merged}}


def apply_assist_operations(current_plan: dict, data: dict
                            ) -> tuple[DashboardPlan, list[int | None]]:
    """The draft with the assistant's operations applied, and for each resulting
    panel the index of the draft panel it came from (None for an added panel).

    A reply with neither operations nor panels is advice, and changes nothing.
    """
    if "operations" not in data and "panels" not in data:
        data = {**data, "operations": []}
    draft_panels = current_plan.get("panels") or []
    if isinstance(data.get("operations"), list):
        data = {**data, "operations": [_merged_format_update(op, draft_panels)
                                       for op in data["operations"]]}
    tagged = [{**panel, _ORIGIN_KEY: index}
              for index, panel in enumerate(draft_panels)]
    plan = _edited_plan({**current_plan, "panels": tagged}, data, "Edit assistant")
    origins: list[int | None] = []
    for panel in plan.panels:
        origin = panel.pop(_ORIGIN_KEY, None)
        origins.append(origin if isinstance(origin, int) else None)
    return plan, origins

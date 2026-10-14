"""AI query flow: natural language question -> answer (with data where relevant).

Ported and streamlined from the old Streamlit app's `chat_utils.py` and
`ai_utils.py`. The flow:

  1. Classify the question (cheap model): data_query / schema_info / explanation / out_of_scope.
  2. For data questions: generate a SELECT (reasoning model), validate it, run it,
     repair once on error, then summarise the rows (cheap model).
  3. For schema/explanation questions: answer from the schema + semantic layer.

The schema context comes live from Snowflake; the semantic layer (table/column
meanings, join rules) comes from the curated TOML metadata.
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import asyncio
import json

from anthropic import AsyncAnthropic

from app.core.config import Settings
from app.metadata.loader import MetadataService
from app.services.explore import ExploreService
from app.services.snowflake import SnowflakeService
# The AST gate, not the old regex one: this path runs LLM-written SQL, and the
# regex gate both let `SELECT ... INTO` through (a write on SQL Server) and
# false-positive blocked legitimate queries containing words like MERGE or COPY.
from app.engine.sql_safety import UnsafeSQLError, enforce_row_limit, validate_sql
from app.session.base import SessionData


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _strip_fences(text: str) -> str:
    return text.replace("```sql", "").replace("```json", "").replace("```", "").strip()


class AIQueryService:
    def __init__(
        self,
        settings: Settings,
        snowflake: SnowflakeService,
        explore: ExploreService,
        metadata: MetadataService,
    ) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._sf = snowflake
        self._explore = explore
        self._meta = metadata
        self._model_sql = settings.ANTHROPIC_MODEL_SQL
        self._model_fast = settings.ANTHROPIC_MODEL_FAST

    # ── public entrypoint ──────────────────────────────────────────────────

    async def answer(
        self, session: SessionData, database: str, message: str
    ) -> dict[str, Any]:
        semantic = self._meta.build_semantic_context(database)
        display = self._meta.describe_database(database)["display_name"]

        kind = await self._classify(message, display, semantic)

        if kind == "out_of_scope":
            return {
                "kind": "out_of_scope",
                "answer": (
                    f"I can only help with questions about {display} — its tables, "
                    "metrics, and the data inside them."
                ),
                "sql": None,
                "data": None,
            }

        if kind in ("schema_info", "explanation"):
            schema_context = await self._explore.build_schema_context(session, database)
            answer = await self._explain(message, display, schema_context, semantic, kind)
            return {"kind": kind, "answer": answer, "sql": None, "data": None}

        # data_query
        schema_context = await self._explore.build_schema_context(session, database)
        return await self._answer_data(
            session, database, message, schema_context, semantic
        )

    # ── steps ──────────────────────────────────────────────────────────────

    async def _classify(self, message: str, display: str, semantic: str) -> str:
        system = (
            "Classify the user's message into exactly one category. "
            "Reply with ONLY the category word.\n\n"
            f"Database: {display}\n\n"
            "Categories:\n"
            "- data_query: needs specific values/counts/aggregates/listings from the data.\n"
            "- schema_info: asks what tables/columns exist or how they relate.\n"
            "- explanation: asks to explain a term, metric, or concept.\n"
            "- out_of_scope: unrelated to this database or its data."
        )
        text = await self._complete(self._model_fast, system, message, max_tokens=16)
        result = text.strip().lower()
        if "schema" in result:
            return "schema_info"
        if "out" in result or "scope" in result:
            return "out_of_scope"
        if "expla" in result:
            return "explanation"
        return "data_query"

    async def _explain(
        self, message: str, display: str, schema_context: str, semantic: str, kind: str
    ) -> str:
        system = (
            f"You are a helpful data analyst assistant working with {display}.\n"
            f"{'Answer questions about the database structure.' if kind == 'schema_info' else 'Explain the financial term or concept asked about.'}\n\n"
            f"Schema:\n{schema_context}\n\n"
            f"Semantic layer:\n{semantic}\n\n"
            "RULES:\n"
            "- Plain, conversational English. Keep it concise.\n"
            "- Do NOT generate SQL.\n"
            "- NEVER invent data values, row counts, or entity names. You do not "
            "have the data in front of you in this mode; only describe structure "
            "and meaning."
        )
        return await self._complete(self._model_fast, system, message, max_tokens=700)

    async def _answer_data(
        self,
        session: SessionData,
        database: str,
        message: str,
        schema_context: str,
        semantic: str,
    ) -> dict[str, Any]:
        join_rules = self._meta.get_join_rules(database)
        metrics = self._meta.build_metrics_context(database)
        schema_block = (
            f"{schema_context}\n\n## Semantic Layer (column meanings)\n{semantic}"
        )
        if metrics:
            schema_block = f"{schema_block}\n\n{metrics}"

        sql = await self._generate_sql(message, schema_block, join_rules)

        try:
            safe_sql = enforce_row_limit(
                validate_sql(sql), self._settings.QUERY_ROW_LIMIT
            )
            columns, rows = await self._sf.execute_query_table(session, safe_sql)
        except Exception as first_error:  # noqa: BLE001
            # One repair attempt, exactly like the old flow.
            try:
                fixed = await self._fix_sql(
                    sql, str(first_error), schema_block, join_rules, message
                )
                safe_sql = enforce_row_limit(
                    validate_sql(fixed), self._settings.QUERY_ROW_LIMIT
                )
                columns, rows = await self._sf.execute_query_table(session, safe_sql)
                sql = fixed
            except (UnsafeSQLError, Exception):  # noqa: BLE001
                return {
                    "kind": "data_query",
                    "answer": (
                        "I couldn't turn that into a query that runs against this "
                        "database. Try rephrasing, or naming the table you mean."
                    ),
                    "sql": sql,
                    "data": None,
                }

        json_rows = [[_jsonify(cell) for cell in row] for row in rows]
        # Summary and chart suggestion are independent, so run them together —
        # this keeps the chart "free" in latency terms (no extra round-trip).
        answer, chart = await asyncio.gather(
            self._summarise(message, columns, json_rows),
            self._suggest_chart(message, columns, json_rows),
        )
        return {
            "kind": "data_query",
            "answer": answer,
            "sql": sql,
            "data": {"columns": columns, "rows": json_rows},
            "chart": chart,
        }

    async def _generate_sql(
        self, message: str, schema_block: str, join_rules: str
    ) -> str:
        system = (
            "You are a senior Snowflake SQL expert. Write ONE read-only SELECT "
            "statement that answers the user's question.\n\n"
            f"Schema:\n{schema_block}\n\n"
            f"JOIN RULES:\n{join_rules}\n\n"
            "RULES:\n"
            "- Return ONLY raw SQL — no explanation, no markdown fences.\n"
            "- A single SELECT (or WITH ... SELECT). Never DDL or DML.\n"
            "- Fully-qualified names: DATABASE.SCHEMA.TABLE. Alias tables.\n"
            "- Never SELECT * — name the columns you need.\n"
            "- For TEXT columns holding numbers, use TRY_CAST after REPLACE.\n"
            "- Use NULLIF(denominator, 0) to avoid divide-by-zero.\n"
            "- Use CTEs for multi-step logic; never nest window functions.\n"
            f"- Keep results small (<= {self._settings.QUERY_ROW_LIMIT} rows) unless asked for everything."
        )
        text = await self._complete(self._model_sql, system, message, max_tokens=1500)
        return _strip_fences(text)

    async def _fix_sql(
        self,
        bad_sql: str,
        error: str,
        schema_block: str,
        join_rules: str,
        original_message: str,
    ) -> str:
        system = (
            "You are a senior Snowflake SQL expert. A query failed. Fix it.\n\n"
            f"ERROR:\n{error}\n\n"
            f"FAILING SQL:\n{bad_sql}\n\n"
            f"ORIGINAL QUESTION:\n{original_message}\n\n"
            f"JOIN RULES:\n{join_rules}\n\n"
            f"Schema:\n{schema_block}\n\n"
            "Return ONLY the corrected raw SQL — a single read-only SELECT, no fences."
        )
        text = await self._complete(
            self._model_sql, system, "Fix the SQL query.", max_tokens=1500
        )
        return _strip_fences(text)

    async def _summarise(
        self, message: str, columns: list[str], rows: list[list[Any]]
    ) -> str:
        if not rows:
            return "No results found for that. The filters might be too narrow."

        preview_rows = rows[:30]
        data_text = f"Columns: {columns}\nRows ({len(rows)} total, showing {len(preview_rows)}):\n"
        data_text += "\n".join(str(row) for row in preview_rows)

        system = (
            "You are a data analyst. Answer the user's question using ONLY the "
            f"query results below. The query returned {len(rows)} row(s).\n\n"
            "RULES:\n"
            "- Reference ONLY the rows below. Never invent numbers, names, or rows.\n"
            "- Plain sentences. No markdown headers or bullet lists.\n"
            "- Format currency with $ and commas, percentages with %.\n"
            "- Be concise (2-4 sentences for simple answers)."
        )
        user = f"Question: {message}\n\nResults:\n{data_text}"
        return await self._complete(self._model_fast, system, user, max_tokens=700)

    async def _suggest_chart(
        self, message: str, columns: list[str], rows: list[list[Any]]
    ) -> dict[str, Any] | None:
        """Suggest the best chart for a result, or None when a table is better.

        Returns a small spec the frontend renders with one shared chart
        component: {"type", "x", "y": [...], "title"}. Fully guarded — any
        failure here returns None and never affects the answer.
        """
        # A chart needs at least a couple of rows and more than one column.
        if not rows or len(rows) < 2 or len(columns) < 2:
            return None

        preview_rows = rows[:10]
        data_text = f"Columns: {columns}\nRows:\n" + "\n".join(
            str(row) for row in preview_rows
        )
        catalog = self._meta.build_chart_catalog_context(v1_only=True)
        qa_types = self._meta.get_qa_chart_types()
        type_list = "|".join(qa_types) if qa_types else "bar|line|area|pie|none"
        system = (
            "You pick the best chart for a SQL result using the catalog below.\n\n"
            f"{catalog}\n\n"
            "RULES:\n"
            f"- type must be one of: {type_list}\n"
            "- Use ONLY column names exactly as given in the result.\n"
            "- x is the category or time axis; y are numeric value columns.\n"
            "- Omit modifiers object entirely when defaults apply.\n"
            "- Reply with ONLY the JSON object described in the catalog."
        )
        user = f"Question: {message}\n\n{data_text}"
        try:
            text = await self._complete(self._model_fast, system, user, max_tokens=300)
            spec = json.loads(_strip_fences(text))
        except Exception:  # noqa: BLE001 - charts are best-effort, never fatal
            return None
        return self._validate_chart(spec, columns)

    def _validate_chart_modifiers(
        self, chart_type: str, raw: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep only modifiers allowed for this chart type with valid values."""
        type_meta = self._meta.get_chart_type(chart_type)
        if not type_meta:
            return {}

        allowed_mods = type_meta.get("modifiers") or {}
        if not isinstance(allowed_mods, dict):
            return {}

        result: dict[str, Any] = {}
        for key, value in raw.items():
            mod_meta = allowed_mods.get(key)
            if not isinstance(mod_meta, dict):
                continue
            if mod_meta.get("optional") and key in {"trend", "centerLabel"}:
                if value is not None and value != "":
                    result[key] = value
                continue
            if key == "negative":
                if isinstance(value, bool):
                    result[key] = value
                elif str(value).lower() in {"true", "false"}:
                    result[key] = str(value).lower() == "true"
                continue
            allowed = mod_meta.get("allowed")
            if not isinstance(allowed, list):
                continue
            str_val = str(value).lower()
            allowed_lower = [str(a).lower() for a in allowed]
            if str_val in allowed_lower:
                idx = allowed_lower.index(str_val)
                result[key] = allowed[idx]

        return result

    def _validate_chart(
        self, spec: Any, columns: list[str]
    ) -> dict[str, Any] | None:
        """Trust nothing from the model: validate every field against real columns."""
        if not isinstance(spec, dict):
            return None
        ctype = str(spec.get("type", "none")).lower()
        if ctype == "none":
            return None
        if ctype not in {"bar", "line", "area", "pie"}:
            return None

        colset = set(columns)
        x = spec.get("x")
        if x is None or str(x) not in colset:
            return None
        x = str(x)

        y_raw = spec.get("y")
        if not isinstance(y_raw, list):
            return None
        y = [str(c) for c in y_raw if str(c) in colset and str(c) != x]
        if not y:
            return None
        if ctype == "pie":
            y = y[:1]

        title = spec.get("title")
        title = str(title) if title else None

        modifiers_raw = spec.get("modifiers")
        modifiers: dict[str, Any] = {}
        if isinstance(modifiers_raw, dict):
            modifiers = self._validate_chart_modifiers(ctype, modifiers_raw)

        result: dict[str, Any] = {"type": ctype, "x": x, "y": y, "title": title}
        if modifiers:
            result["modifiers"] = modifiers
        return result

    # ── low-level ────────────────────────────────────────────────────────────

    async def _complete(
        self, model: str, system: str, user: str, max_tokens: int
    ) -> str:
        import asyncio
        import time

        from app.engine import audit

        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            await asyncio.to_thread(audit.log_ai_call, "ai_query", model,
                                    started=started, error=exc)
            raise
        # Off the event loop: the audit write is a blocking Snowflake INSERT.
        await asyncio.to_thread(audit.log_ai_call, "ai_query", model, response,
                                started=started)
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
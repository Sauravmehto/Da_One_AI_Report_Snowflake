"""Shared parameterized-SQL substitution + validation.

One implementation of {{name}} placeholder rendering (typed, single-quote
escaped) and of representative-value generation for a validation dry-run — used
by BOTH saved Excel reports (excel_reports.py) and parameterized dashboards
(dashboards.py), so the two features can never drift apart.

Security boundary note: the SQL this module produces is ALWAYS still passed
through sql_safety.validate_sql by the caller before it executes (dashboards via
run_query/dry_run_query, Excel via _run_report_query). So this layer is typed
convenience + defense-in-depth (values are escaped/typed here too), never the
security gate itself — a malicious parameter value cannot turn a SELECT into
DDL/DML, because the rendered statement is re-validated as read-only downstream.
"""

from __future__ import annotations

import re
from typing import Any

# Placeholder syntax in a parameterized query: {{param_name}}.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")


class ParameterError(ValueError):
    """A parameter problem: missing value with no default, wrong type, or an
    unfilled/undeclared placeholder. Callers translate this into their own
    error type (ExcelReportError / DashboardError) as appropriate."""


def has_placeholders(sql: str | None) -> bool:
    """Cheap check for whether a query needs substitution at all."""
    return bool(sql) and "{{" in sql


def render_literal(ptype: str, value: Any) -> str:
    """Render a parameter value as a typed, escaped SQL literal. Defense-in-depth
    only — the final SQL is still validate_sql-gated — but it keeps well-formed
    values from silently corrupting the query."""
    if ptype == "number":
        if isinstance(value, bool):
            raise ParameterError("numeric parameter got a boolean")
        if isinstance(value, (int, float)):
            return repr(value)
        text = str(value).strip()
        if not _NUMERIC_RE.fullmatch(text):
            raise ParameterError(f"parameter expected a number, got {value!r}")
        return text
    # string / date / enum / anything else -> a quoted, single-quote-escaped literal
    return "'" + str(value).replace("'", "''") + "'"


#: A placeholder with a single quote hard against each side: '{{name}}'.
_QUOTED_PLACEHOLDER_RE = re.compile(r"'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}'")


def _unquote_placeholders(sql_template: str, declared: dict[str, dict[str, Any]]) -> str:
    """Drop the quotes a template put around a quoted-type placeholder.

    String, date and enum values are quoted by render_literal, so '{{name}}'
    rendered as ''All'' — an empty string, the word All, another empty string —
    and the query failed to parse. The planner is told not to write the quotes
    and writes them anyway, most often in an optional filter:
    ('{{trade_lane}}' = 'All' OR TRADE_LANE = '{{trade_lane}}').

    Number parameters are left alone: '{{n}}' renders as '5', which is valid
    SQL, and changing it would change what the query compares against.
    Undeclared names are left for render_sql to report.
    """
    def _strip(match: re.Match) -> str:
        spec = declared.get(match.group(1))
        if spec is None or spec.get("type", "string") == "number":
            return match.group(0)
        return "{{" + match.group(1) + "}}"

    return _QUOTED_PLACEHOLDER_RE.sub(_strip, sql_template)


def render_sql(sql_template: str, parameters: list[dict[str, Any]],
               values: dict[str, Any] | None) -> str:
    """Substitute {{name}} placeholders in a parameterized query.

    Uses the supplied value, else the parameter's declared default (a value of
    None is treated as "not supplied", so it falls back to the default). Raises
    ParameterError if a placeholder has neither, or if an undeclared/unfilled
    placeholder remains after substitution.
    """
    values = values or {}
    declared = {p["name"]: p for p in (parameters or [])}
    resolved: dict[str, str] = {}
    for name, spec in declared.items():
        if name in values and values[name] is not None:
            raw = values[name]
        elif spec.get("default") is not None:
            raw = spec["default"]
        else:
            raise ParameterError(f"missing value for parameter '{name}' (no default)")
        resolved[name] = render_literal(spec.get("type", "string"), raw)

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in resolved:
            raise ParameterError(f"query references undeclared parameter '{name}'")
        return resolved[name]

    rendered = PLACEHOLDER_RE.sub(_sub, _unquote_placeholders(sql_template, declared))
    if "{{" in rendered:
        raise ParameterError("query still has an unfilled {{placeholder}} after substitution")
    return rendered


def sample_values(parameters: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Representative values (matching each declared type) for a validation
    dry-run of a parameterized query: the declared default when present, else a
    safe, type-appropriate sample. Lets a parameterized query be compiled against
    the database before any real value exists."""
    out: dict[str, Any] = {}
    for p in (parameters or []):
        if p.get("default") is not None:
            out[p["name"]] = p["default"]
            continue
        t = p.get("type", "string")
        if t == "number":
            out[p["name"]] = 0
        elif t == "date":
            out[p["name"]] = "2024-01-01"
        elif t == "enum":
            opts = p.get("options") or p.get("enum") or []
            out[p["name"]] = opts[0] if opts else "sample"
        else:
            out[p["name"]] = "sample"
    return out

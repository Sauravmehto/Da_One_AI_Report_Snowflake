"""Validation for semantic layer documents.

Why this is strict on publish: a saved layer that breaks the loaders takes a
database offline, and a layer that is merely *wrong* is worse — it stays
online and quietly makes every answer for that database wrong. Neither failure
announces itself, so both have to be caught here.

Errors block publishing. Warnings do not: an undocumented column is a normal
intermediate state, and refusing to publish until a layer is perfect would
mean nobody ever publishes.

Pure functions over a document plus (optionally) the live schema, so this is
testable without a warehouse connection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.semantic.document import SemanticDocument

# Roles are open vocabulary, not a fixed enum. The planner never carries a
# hard-coded list: it is shown the roles a table declares ("MARK_DATE:
# role=snapshot_date") and asks for one of those by name. So a domain is free
# to invent `acquisition_date` or `warrant_expiry_date` — and the shipped
# configs do exactly that, across real estate, PE and loan servicing.
#
# These are the roles offered as suggestions in the editor. Anything outside
# the list is allowed; it only warns, in case it is a typo.
SUGGESTED_DATE_ROLES = {
    "snapshot_date",       # the as-of role — special, and worth spelling consistently
    "acquisition_date",
    "closing_date",
    "effective_date",
    "entry_date",
    "inception_date",
    "last_payment_date",
    "maturity_date",
    "next_payment_date",
    "origination_date",
    "payment_date",
    "payment_received_date",
    "reporting_date",
    "transaction_date",
    "valuation_date",
}

# Roles are matched by exact string, so whitespace or case drift silently fails
# to resolve. The shape is enforced; the vocabulary is not.
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_DATE_TYPES = ("DATE", "TIMESTAMP", "DATETIME")


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    field: str          # dotted path, so the UI can focus the offending control
    message: str


# A live-schema snapshot: {FULL_TABLE_NAME: {COLUMN: DATA_TYPE}}, all uppercase.
LiveSchema = dict[str, dict[str, str]]


def validate_document(
    doc: SemanticDocument,
    *,
    schema: LiveSchema | None = None,
) -> list[Issue]:
    """Check a document, optionally against the live schema.

    Without `schema` only structural rules run — enough for autosaving a draft.
    Publishing should always pass the live schema, because the rules that
    matter most are the ones that compare claims against reality.
    """
    issues: list[Issue] = []

    if not doc.database.strip():
        issues.append(Issue("error", "database", "Database name is required."))
    if not doc.display_name.strip():
        issues.append(Issue("warning", "display_name",
                            "No display name; the raw database name will be shown."))

    # An unrecognised code is worse than none: build_context would drop it
    # silently and every money KPI would go back to printing dollars, with
    # nothing on screen to say the declaration was ignored.
    raw_currency = doc.extra.get("currency")
    if raw_currency is not None:
        code = raw_currency.strip().upper() if isinstance(raw_currency, str) else ""
        if not (len(code) == 3 and code.isascii() and code.isalpha()):
            issues.append(Issue(
                "error", "currency",
                "Reporting currency must be a three-letter ISO 4217 code such "
                f"as AED; got {raw_currency!r}.",
            ))

    for name, table in doc.tables.items():
        field = f"tables.{name}"

        # Both loaders index tables by three-part name. A two-part key silently
        # never matches, so the table's documentation is simply never applied.
        if name.count(".") != 2:
            issues.append(Issue(
                "error", field,
                f"Table key must be a three-part name (DATABASE.SCHEMA.TABLE); got '{name}'.",
            ))
            continue

        db_part = name.split(".")[0].upper()
        if doc.database and db_part != doc.database.upper():
            issues.append(Issue(
                "error", field,
                f"Table '{name}' belongs to '{db_part}', not '{doc.database.upper()}'.",
            ))

        if not table.description.strip():
            issues.append(Issue("warning", f"{field}.description",
                                "No description; the model sees only the table name."))

        live_columns = (schema or {}).get(name.upper())
        if schema is not None and live_columns is None:
            issues.append(Issue(
                "error", field,
                f"Table '{name}' does not exist in the database.",
            ))
            continue

        for column in table.key_columns:
            if live_columns is not None and column.upper() not in live_columns:
                issues.append(Issue(
                    "error", f"{field}.key_columns.{column}",
                    f"Column '{column}' does not exist on {name}.",
                ))

        seen_roles: dict[str, str] = {}
        for column, role in table.date_roles.items():
            path = f"{field}.date_roles.{column}"

            if not _ROLE_RE.match(role):
                issues.append(Issue(
                    "error", path,
                    f"Role '{role}' must be lower-case snake_case (letters, "
                    "digits and underscores). Roles are matched exactly, so "
                    "spacing or capitalisation would never resolve.",
                ))
            elif role not in SUGGESTED_DATE_ROLES:
                # Not an error: domains legitimately need their own roles.
                issues.append(Issue(
                    "warning", path,
                    f"'{role}' is not one of the common roles — fine if "
                    "deliberate, worth a check if it was meant to be one.",
                ))

            # Two columns sharing a role make resolve_date_column() return
            # "ambiguous", so the planner stops and asks every single time.
            if role in seen_roles:
                issues.append(Issue(
                    "error", path,
                    f"Role '{role}' is already used by '{seen_roles[role]}'. "
                    "Two columns with the same role can never be resolved.",
                ))
            else:
                seen_roles[role] = column

            if live_columns is None:
                continue

            actual = live_columns.get(column.upper())
            if actual is None:
                issues.append(Issue(
                    "error", path,
                    f"Column '{column}' does not exist on {name}, so no date "
                    "parameter can ever bind to it.",
                ))
            elif not any(t in actual.upper() for t in _DATE_TYPES):
                # The classic one: QUARTER_LABEL is TEXT like 'Q1 2025'.
                # Binding a date range to it produces no rows, silently.
                issues.append(Issue(
                    "error", path,
                    f"Column '{column}' is {actual}, not a date type. "
                    "A date parameter bound to it would match nothing.",
                ))

    for name, metric in doc.metrics.items():
        if not metric.sql.strip():
            issues.append(Issue("error", f"metrics.{name}.sql",
                                "A metric without SQL cannot be used; remove it or add SQL."))
        if not metric.definition.strip():
            issues.append(Issue("warning", f"metrics.{name}.definition",
                                "No plain-English definition for this metric."))

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)

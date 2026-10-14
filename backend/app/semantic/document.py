"""The semantic layer document — the editable shape behind the TOML files.

A typed model rather than raw dicts, because this is what the admin UI edits
and what validation runs against. The TOML on disk stays the storage format
(portable, diffable, and what both loaders already read); this is its in-memory
form.

One deliberate loss to know about: TOML comments do not survive a round trip.
The hand-written files use comments to explain things — notably why a table's
`date_roles` exclude an as-of date — and that reasoning has to move into the
`notes` fields or into UI help text, because a save will drop it. Comments were
never part of the data model; they were a workaround for not having an editor.
"""

from __future__ import annotations

from typing import Any

import tomli_w
from pydantic import BaseModel, Field


# Top-level keys the editor models. Anything else is preserved via `extra`.
# Module-level rather than a class attribute: pydantic turns a leading-
# underscore class attribute into a private attr, not a plain set.
_KNOWN_KEYS = {
    "database", "display_name", "description", "join_rules",
    "dashboard_heuristics", "tables", "metrics",
}


class TableDoc(BaseModel):
    """One table's meaning. Keyed in the document by three-part name."""

    description: str = ""
    notes: str = ""
    # {COLUMN: plain-English meaning}
    key_columns: dict[str, str] = Field(default_factory=dict)
    # {COLUMN: role} — the binding target for date parameters. Never guessed:
    # a table with no entry here cannot be filtered by date at all.
    date_roles: dict[str, str] = Field(default_factory=dict)


class MetricDoc(BaseModel):
    """A named business metric with the exact SQL to use for it."""

    definition: str = ""
    sql: str = ""


class SemanticDocument(BaseModel):
    database: str
    display_name: str = ""
    description: str = ""
    join_rules: str = ""
    dashboard_heuristics: str = ""
    tables: dict[str, TableDoc] = Field(default_factory=dict)
    metrics: dict[str, MetricDoc] = Field(default_factory=dict)
    # Anything the editor does not model yet is carried through untouched, so
    # saving a hand-written file cannot silently delete sections we have not
    # built UI for (e.g. [briefing]).
    extra: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @classmethod
    def from_toml_dict(cls, raw: dict[str, Any]) -> SemanticDocument:
        tables = {
            str(name): TableDoc(
                description=str(meta.get("description") or ""),
                notes=str(meta.get("notes") or ""),
                key_columns={str(k): str(v) for k, v in (meta.get("key_columns") or {}).items()},
                date_roles={str(k): str(v) for k, v in (meta.get("date_roles") or {}).items()},
            )
            for name, meta in (raw.get("tables") or {}).items()
            if isinstance(meta, dict)
        }
        metrics = {
            str(name): MetricDoc(
                definition=str(meta.get("definition") or ""),
                sql=str(meta.get("sql") or ""),
            )
            for name, meta in (raw.get("metrics") or {}).items()
            if isinstance(meta, dict)
        }
        return cls(
            database=str(raw.get("database") or "").upper(),
            display_name=str(raw.get("display_name") or ""),
            description=str(raw.get("description") or ""),
            join_rules=str(raw.get("join_rules") or ""),
            dashboard_heuristics=str(raw.get("dashboard_heuristics") or ""),
            tables=tables,
            metrics=metrics,
            extra={k: v for k, v in raw.items() if k not in _KNOWN_KEYS},
        )

    def to_toml_dict(self) -> dict[str, Any]:
        """Plain dict in the on-disk key order, empty fields omitted.

        Omitting empties matters: an empty `date_roles` table would read as
        "declared, with nothing in it" to a human, when the truth is that the
        table has no date dimension.
        """
        out: dict[str, Any] = {"database": self.database}
        if self.display_name:
            out["display_name"] = self.display_name
        if self.description:
            out["description"] = self.description
        if self.join_rules:
            out["join_rules"] = self.join_rules
        if self.dashboard_heuristics:
            out["dashboard_heuristics"] = self.dashboard_heuristics

        # Carry unmodelled sections through untouched.
        out.update(self.extra)

        tables: dict[str, Any] = {}
        for name, table in self.tables.items():
            meta: dict[str, Any] = {}
            if table.description:
                meta["description"] = table.description
            if table.notes:
                meta["notes"] = table.notes
            if table.key_columns:
                meta["key_columns"] = dict(table.key_columns)
            if table.date_roles:
                meta["date_roles"] = dict(table.date_roles)
            tables[name] = meta
        if tables:
            out["tables"] = tables

        metrics: dict[str, Any] = {}
        for name, metric in self.metrics.items():
            meta = {}
            if metric.definition:
                meta["definition"] = metric.definition
            if metric.sql:
                meta["sql"] = metric.sql
            metrics[name] = meta
        if metrics:
            out["metrics"] = metrics

        return out

    def to_toml(self) -> bytes:
        # multiline_strings keeps join_rules and heuristics as readable """
        # blocks instead of one enormous \n-escaped line. These files stay
        # diffable and hand-editable, and a rule-per-line diff is the whole
        # point when reviewing a change to what the model believes.
        return tomli_w.dumps(
            self.to_toml_dict(), multiline_strings=True
        ).encode("utf-8")

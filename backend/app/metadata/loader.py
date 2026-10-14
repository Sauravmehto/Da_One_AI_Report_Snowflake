"""Business-metadata loader.

Ported from the previous Streamlit app's `config_loader.py`. Loads the
hand-curated TOML files that describe each database in plain English:
table descriptions, column meanings, and the gotchas ("notes") that
Snowflake's schema cannot tell you on its own.

This metadata is deliberately separate from the live schema. The live
schema (table list, column types) is always read fresh from Snowflake and
is authoritative for *what exists*. These configs only add *meaning* on top.
Tables/columns without an entry here simply show their raw Snowflake names.
"""

import tomllib
from pathlib import Path
from typing import Any

from app.metadata.watch import Fingerprint, directory_fingerprint

# Per-database semantic-layer TOMLs are client-editable content and live
# outside the code tree, in backend/config/metadata/ (shared with the engine's
# own loader — one source of truth, no duplicate configs to drift apart).
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "metadata"
# The chart catalog is system config (which chart types the renderer supports),
# not per-client content, so it stays alongside the code that reads it.
_CHART_CATALOG_PATH = Path(__file__).parent / "chart_catalog.toml"


class MetadataService:
    def __init__(self, config_dir: Path | None = None) -> None:
        self._config_dir = config_dir or _CONFIG_DIR
        self._configs: dict[str, dict[str, Any]] = {}
        self._default: dict[str, Any] = {}
        # database -> {UPPERCASE three-part table name -> table metadata}
        self._table_index: dict[str, dict[str, dict[str, Any]]] = {}
        self._chart_catalog: dict[str, Any] = {}
        self._loaded = False
        self._fingerprint: Fingerprint = ()

    def invalidate(self) -> None:
        """Drop the cache so the next read reloads the configs.

        Both this loader and the engine's MetadataStore read the same files, so
        publishing invalidates both or the two disagree about what a database
        means — the UI showing one thing and the model told another. That call
        only reaches THIS process, though; _ensure_loaded re-reads on a changed
        directory fingerprint, which is what makes a publish visible to the
        other workers.
        """
        self._loaded = False
        self._fingerprint = ()
        self._configs.clear()
        self._table_index.clear()
        self._default = {}

    def _ensure_loaded(self) -> None:
        fingerprint = directory_fingerprint(self._config_dir)
        if self._loaded and fingerprint == self._fingerprint:
            return

        # Rebuilt from scratch: a reload must drop a database whose file was
        # deleted, not merge the new view over the old one.
        self._configs = {}
        self._table_index = {}
        self._default = {}

        for path in sorted(self._config_dir.glob("*.toml")):
            with open(path, "rb") as fh:  # tomllib requires binary mode
                cfg = tomllib.load(fh)

            db_name = str(cfg.get("database") or "").upper()
            if not db_name:
                # Not a database metadata file (e.g. a chatbot guardrails file).
                continue

            if db_name == "_DEFAULT":
                self._default = cfg
                continue

            self._configs[db_name] = cfg
            self._table_index[db_name] = {
                str(key).upper(): value
                for key, value in (cfg.get("tables") or {}).items()
            }

        if not self._default:
            self._default = {
                "database": "_DEFAULT",
                "display_name": "",
                "description": "",
                "tables": {},
            }

        if _CHART_CATALOG_PATH.is_file():
            with open(_CHART_CATALOG_PATH, "rb") as fh:
                self._chart_catalog = tomllib.load(fh)
        else:
            self._chart_catalog = {}

        self._fingerprint = fingerprint
        self._loaded = True

    def get_database_config(self, database: str) -> dict[str, Any]:
        self._ensure_loaded()
        return self._configs.get(database.upper(), self._default)

    def is_documented(self, database: str) -> bool:
        self._ensure_loaded()
        return database.upper() in self._configs

    def describe_database(self, database: str) -> dict[str, Any]:
        """Return display_name / description / is_documented for a database."""
        self._ensure_loaded()
        documented = self.is_documented(database)
        if not documented:
            return {"display_name": database, "description": "", "is_documented": False}

        cfg = self.get_database_config(database)
        return {
            "display_name": cfg.get("display_name") or database,
            "description": cfg.get("description", ""),
            "is_documented": True,
        }

    def get_table_meta(
        self, database: str, schema: str, table: str
    ) -> dict[str, Any] | None:
        """Look up one table's metadata by its three-part name (case-insensitive)."""
        self._ensure_loaded()
        index = self._table_index.get(database.upper())
        if not index:
            return None
        full_name = f"{database}.{schema}.{table}".upper()
        return index.get(full_name)

    def get_column_meta(
        self, database: str, schema: str, table: str
    ) -> dict[str, str]:
        """Return {UPPERCASE column name -> plain-English meaning} for a table."""
        meta = self.get_table_meta(database, schema, table)
        if not meta:
            return {}
        return {
            str(col).upper(): str(desc)
            for col, desc in (meta.get("key_columns") or {}).items()
        }

    def get_join_rules(self, database: str) -> str:
        self._ensure_loaded()
        return str(self.get_database_config(database).get("join_rules", ""))

    def get_metrics(self, database: str) -> dict[str, dict[str, str]]:
        """Return the curated business-metric definitions for a database.

        Each metric is keyed by its display name and carries a plain-English
        ``definition`` plus the exact ``sql`` Claude should use. This is the
        one idea borrowed from Snowflake Semantic Views: a single authoritative
        definition for things like "delinquency rate" so the model never has to
        guess the formula.
        """
        self._ensure_loaded()
        metrics = self.get_database_config(database).get("metrics") or {}
        # Normalise to a predictable shape: {name: {"definition": str, "sql": str}}
        result: dict[str, dict[str, str]] = {}
        for name, meta in metrics.items():
            if isinstance(meta, dict):
                result[str(name)] = {
                    "definition": str(meta.get("definition", "")),
                    "sql": str(meta.get("sql", "")),
                }
        return result

    def build_metrics_context(self, database: str) -> str:
        """Render metric definitions as a text block for the SQL-generation prompt."""
        metrics = self.get_metrics(database)
        if not metrics:
            return ""
        lines: list[str] = ["## Business Metrics (use these exact definitions)"]
        for name, meta in metrics.items():
            lines.append(f"\n### {name}")
            if meta["definition"]:
                lines.append(f"Definition: {meta['definition']}")
            if meta["sql"]:
                lines.append(f"SQL: {meta['sql']}")
        return "\n".join(lines)

    def build_semantic_context(
        self, database: str, filter_tables: set[str] | None = None
    ) -> str:
        """Render the curated table/column meanings as a text block for the LLM.

        Ported from the old `build_semantic_context_from_config`.
        """
        self._ensure_loaded()
        tables = self.get_database_config(database).get("tables") or {}
        if not tables:
            return ""

        include = filter_tables if filter_tables is not None else set(tables.keys())
        lines: list[str] = []

        # Said once, up front: the model cannot infer a reporting currency from
        # column names reliably, and a KPI row has nowhere else to carry one.
        raw = self.get_database_config(database).get("currency")
        code = raw.strip().upper() if isinstance(raw, str) else ""
        if len(code) == 3 and code.isascii() and code.isalpha():
            lines.append(
                f"Reporting currency: {code} - every monetary amount here is in "
                f"{code}, never US dollars."
            )
        for table, meta in tables.items():
            if table not in include:
                continue
            lines.append(f"\n### {table}")
            lines.append(f"Description: {meta.get('description', '')}")
            if meta.get("notes"):
                lines.append(f"IMPORTANT: {meta['notes']}")
            key_cols = meta.get("key_columns") or {}
            if key_cols:
                lines.append("Key columns:")
                for col, desc in key_cols.items():
                    lines.append(f"  - {col}: {desc}")
        return "\n".join(lines)

    def get_chart_catalog(self) -> dict[str, Any]:
        """Return the parsed chart catalog TOML."""
        self._ensure_loaded()
        return self._chart_catalog

    def get_chart_type(self, type_id: str) -> dict[str, Any] | None:
        """Look up one chart type definition (e.g. bar, line, stat)."""
        self._ensure_loaded()
        types = self._chart_catalog.get("types") or {}
        meta = types.get(type_id)
        return meta if isinstance(meta, dict) else None

    def get_qa_chart_types(self) -> list[str]:
        """Chart type ids enabled for the Q&A chart picker (excludes stat, radar)."""
        self._ensure_loaded()
        types = self._chart_catalog.get("types") or {}
        result: list[str] = []
        for type_id, meta in types.items():
            if not isinstance(meta, dict):
                continue
            if not meta.get("v1_enabled", True):
                continue
            if not meta.get("qa_picker", False):
                continue
            alias = meta.get("qa_type_alias")
            result.append(str(alias if alias else type_id))
        return sorted(set(result))

    def get_chart_modifier_allowed(
        self, type_id: str, modifier: str
    ) -> list[str] | None:
        """Return allowed values for a modifier, or None if not defined."""
        meta = self.get_chart_type(type_id)
        if not meta:
            return None
        modifiers = meta.get("modifiers") or {}
        mod_meta = modifiers.get(modifier)
        if not isinstance(mod_meta, dict):
            return None
        allowed = mod_meta.get("allowed")
        if not isinstance(allowed, list):
            return None
        return [str(v) for v in allowed]

    def build_chart_catalog_context(self, *, v1_only: bool = True) -> str:
        """Render chart catalog as a text block for LLM chart-selection prompts."""
        self._ensure_loaded()
        if not self._chart_catalog:
            return ""

        lines: list[str] = ["## Chart Catalog (v1)"]

        rules = self._chart_catalog.get("selection_rules") or {}
        if rules.get("priority"):
            lines.append("\n### Global selection rules")
            lines.append(str(rules["priority"]).strip())
        if rules.get("json_output_hint"):
            lines.append("\n### JSON output format")
            lines.append(str(rules["json_output_hint"]).strip())

        types = self._chart_catalog.get("types") or {}
        for type_id in sorted(types.keys()):
            meta = types[type_id]
            if not isinstance(meta, dict):
                continue
            if v1_only and not meta.get("v1_enabled", True):
                continue
            if v1_only and not meta.get("qa_picker", False):
                continue

            lines.append(f"\n### {type_id}")
            if meta.get("summary"):
                lines.append(f"Summary: {str(meta['summary']).strip()}")
            if meta.get("use_when"):
                lines.append(f"Use when: {str(meta['use_when']).strip()}")
            if meta.get("avoid_when"):
                lines.append(f"Avoid when: {str(meta['avoid_when']).strip()}")
            if meta.get("data_shape"):
                lines.append(f"Data shape: {str(meta['data_shape']).strip()}")
            if meta.get("encoding"):
                lines.append(f"Encoding: {str(meta['encoding']).strip()}")

            modifiers = meta.get("modifiers") or {}
            if isinstance(modifiers, dict) and modifiers:
                mod_parts: list[str] = []
                for mod_name, mod_meta in sorted(modifiers.items()):
                    if not isinstance(mod_meta, dict):
                        continue
                    if mod_meta.get("optional"):
                        mod_parts.append(f"{mod_name}=optional")
                        continue
                    allowed = mod_meta.get("allowed")
                    if isinstance(allowed, list):
                        mod_parts.append(
                            f"{mod_name}=[{'|'.join(str(v) for v in allowed)}]"
                        )
                if mod_parts:
                    lines.append(f"Modifiers: {', '.join(mod_parts)}")

        cross = self._chart_catalog.get("cross_modifiers") or {}
        if isinstance(cross, dict) and cross:
            lines.append("\n### Cross-type modifiers (dashboard UI)")
            for name, meta in sorted(cross.items()):
                if isinstance(meta, dict) and meta.get("description"):
                    lines.append(f"- {name}: {str(meta['description']).strip()}")
                elif isinstance(meta, dict) and meta.get("note"):
                    lines.append(f"- {name}: {str(meta['note']).strip()}")

        return "\n".join(lines)


# Single shared instance — configs are static files, loaded once on first use.
metadata_service = MetadataService()
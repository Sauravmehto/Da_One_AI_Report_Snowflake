# `config/` — client-editable content

Everything in this folder is **content, not code**. It is what changes when
onboarding a new client or a new database; nothing here requires touching the
application.

```
config/
  glossary/    business term definitions
  metadata/    per-database semantic layer
```

## `metadata/` — one TOML per database

Snowflake tells the system what *exists* (tables, column types). These files
tell it what things *mean*: what a column represents, which tables must never
be joined, and the exact SQL behind a named business metric. Without them the
AI writes SQL that is syntactically valid and semantically wrong.

Add a database by dropping in `<name>.toml`:

```toml
database = "ACME_FUND"
display_name = "Acme Credit Fund"
description = "..."

join_rules = """
- FACT joins DIM on ID.
- Always LEFT JOIN when the right side may not match.
"""

dashboard_heuristics = """
- For portfolio overviews, include exposure by sector and covenant status.
"""

[tables."ACME_FUND.PUBLIC.DEALS"]
description = "One row per deal."
notes = "Central fact table. STATUS values: Active, Watch-list."

[tables."ACME_FUND.PUBLIC.DEALS".key_columns]
DEAL_ID = "Unique deal identifier (TEXT). Primary key."
PRINCIPAL = "Original principal amount (NUMBER)"
LTV_PCT = "Loan-to-value as a 0-1 FRACTION (0.55 = 55%). Multiply by 100."

# Date columns a date/as-of PARAMETER may bind to, by role. A date filter can
# only ever bind to a column declared here — never one inferred from a name.
# Omit this table entirely if it has no date dimension.
[tables."ACME_FUND.PUBLIC.DEALS".date_roles]
ORIGINATION_DATE = "origination_date"
MATURITY_DATE = "maturity_date"
```

Notes that matter:

- **`date_roles` is the contract for date filtering.** A dashboard date
  parameter binds only to a column declared here. A table with no `date_roles`
  cannot be filtered by date, and the planner is required to say so rather
  than guess a column.
- **State the scale of percentage columns explicitly** (`0-1 FRACTION` vs
  already-whole-number). The AI is instructed to trust this over the column
  name, because a `_PCT` suffix means both things in practice.
- Each file is parsed independently — one malformed file degrades to "no
  context for that database" instead of taking the whole semantic layer down.
- `default.toml` supplies fallback context for undocumented databases.

## `glossary/` — business term definitions

Backs the "what does DSCR mean?" path. The guarantee is that it **never
fabricates**: an unknown term returns close matches and an explicit miss, not
a plausible-sounding guess.

**Every `*.toml` in this folder is loaded and merged**, so a client adds their
own house terms without editing the shipped baseline:

```toml
[[term]]
name = "Debt Service Coverage Ratio (DSCR)"
category = "Credit"
definition = "Net operating income divided by total debt service."
formula = "NOI / (Principal + Interest)"
aliases = ["debt service coverage", "coverage ratio"]
see_also = ["Loan-to-Value (LTV)"]
```

- Files load in **filename order**; a later file redefining a term
  **replaces** it, so you can override a shipped definition with your own.
- Lookup is case-, space- and hyphen-insensitive, and matches the acronym in
  parentheses plus every alias — `DSCR`, `dscr`, and
  `debt-service coverage ratio` all resolve to the same entry.

## What is *not* here

`app/metadata/chart_catalog.toml` stays with the code: it declares which chart
types the renderer can produce, which is a property of the application rather
than of a client's data.

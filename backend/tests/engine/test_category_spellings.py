"""The CATEGORY clean-up rule the dashboard planner is given.

A Snowflake check of every text column found these two spellings per category,
and nothing else duplicated. Grouped as stored, one category is two bars, each
half its real size. The rule hands the planner a CASE expression; this runs that
exact expression -- as written in the config -- over the spellings found.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("SNOWFLAKE_ACCOUNT", "dummy")
os.environ.setdefault("SNOWFLAKE_USER", "dummy")

import duckdb

from app.engine.metadata_loader import MetadataStore

CONFIG = Path(__file__).resolve().parents[2] / "config" / "metadata"

FOUND = {
    "ECOMMERCE_DB": {
        "Baby & Kids": "Baby & Kids", "Baby/Kids": "Baby & Kids",
        "Fashion & Apparel": "Fashion & Apparel", "Fashion/Apparel": "Fashion & Apparel",
        "Health & Wellness": "Health & Wellness", "Health Wellness": "Health & Wellness",
        "Sports & Fitness": "Sports & Fitness", "Sports/Fitness": "Sports & Fitness",
        "Electronics": "Electronics",
    },
    "RETAIL_DB": {
        "Baby & Kids": "Baby & Kids", "Baby/Kids": "Baby & Kids",
        "Fashion & Apparel": "Fashion & Apparel", "Fashion/Apparel": "Fashion & Apparel",
        "Grocery & FMCG": "Grocery & FMCG", "Grocery/FMCG": "Grocery & FMCG",
        "Health & Wellness": "Health & Wellness", "Health Wellness": "Health & Wellness",
        "Luxury": "Luxury",
    },
}


def _case_expression(database: str) -> str:
    heuristics = MetadataStore(CONFIG).dashboard_heuristics(database)
    match = re.search(r"(CASE CATEGORY .*? END) AS CATEGORY", heuristics)
    assert match, f"{database}: no CATEGORY rule in dashboard_heuristics"
    return match.group(1)


def test_every_spelling_found_maps_to_one_category():
    for database, expected in FOUND.items():
        expression = _case_expression(database)
        con = duckdb.connect()
        con.execute("CREATE TABLE t (CATEGORY VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?)", [[v] for v in expected])
        mapped = dict(con.execute(f"SELECT CATEGORY, {expression} FROM t").fetchall())
        assert mapped == expected, database


def test_grouping_by_the_rule_merges_the_duplicates():
    for database, expected in FOUND.items():
        con = duckdb.connect()
        con.execute("CREATE TABLE t (CATEGORY VARCHAR, GMV DOUBLE)")
        con.executemany("INSERT INTO t VALUES (?, 1)", [[v] for v in expected])
        groups = con.execute(
            f"SELECT {_case_expression(database)} AS CATEGORY, SUM(GMV) FROM t GROUP BY 1"
        ).fetchall()
        assert len(groups) == len(set(expected.values())), database

"""The country table and the boundary file must agree.

`frontend/src/lib/geo-countries.ts` hand-carries ISO 3166-1 codes because the
world-atlas TopoJSON identifies countries by numeric id alone — no alpha codes,
and abbreviated names. That table is the only bridge between a warehouse column
holding "AE" or "United Arab Emirates" and a shape on the map.

Drift between the two is silent in the worst way. A country in the geometry but
missing from the table can never be matched, so it renders permanently grey and
its rows land in the "not matched" footer; a country in the table but not the
geometry matches happily and then shades nothing at all. Neither throws. Both
look like "we have no business there".

These tests live on the backend for the same reason test_chart_vocabulary.py
does: it is the only suite that runs, and it already reads frontend sources.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend"
TABLE_TS = FRONTEND / "src" / "lib" / "geo-countries.ts"
ATLAS = FRONTEND / "node_modules" / "world-atlas" / "countries-50m.json"

#: Natural Earth carries a handful of shapes with no ISO numeric id — disputed
#: or unrecognised territories. They are drawn as context and never matched, so
#: they are expected to be absent from the table.
UNCODED = {
    "Somaliland",
    "Kosovo",
    "N. Cyprus",
    "Indian Ocean Ter.",
    "Siachen Glacier",
}


def table_rows() -> list[tuple[str, str, str, str]]:
    text = TABLE_TS.read_text(encoding="utf-8")
    block = re.search(
        r"const TABLE: Array<\[string, string, string, string\]> = \[(.*?)\n\]",
        text,
        re.S,
    )
    assert block, "could not find the country TABLE"
    rows = re.findall(
        r'\["(\d{3})",\s*"([A-Z]{2})",\s*"([A-Z]{2,3})",\s*"([^"]+)"\]',
        block.group(1),
    )
    assert rows, "the country TABLE parsed as empty"
    return rows


def atlas_geometries() -> list[dict]:
    topology = json.loads(ATLAS.read_text(encoding="utf-8"))
    return topology["objects"]["countries"]["geometries"]


requires_atlas = pytest.mark.skipif(
    not ATLAS.is_file(),
    reason="frontend node_modules not installed; run pnpm install in frontend/",
)


@requires_atlas
def test_every_country_in_the_boundary_file_has_a_table_entry():
    """Otherwise that country is unmatchable and always renders as no data."""
    atlas_ids = {g["id"] for g in atlas_geometries() if g.get("id")}
    table_ids = {row[0] for row in table_rows()}

    missing = atlas_ids - table_ids
    assert not missing, (
        f"{sorted(missing)} are in the boundary file but not the country table "
        "— data naming those countries can never be matched"
    )


@requires_atlas
def test_every_table_entry_exists_in_the_boundary_file():
    """A code that matches but shades nothing is the same failure, reversed."""
    atlas_ids = {g["id"] for g in atlas_geometries() if g.get("id")}
    table_ids = {row[0] for row in table_rows()}

    extra = table_ids - atlas_ids
    assert not extra, (
        f"{sorted(extra)} are in the country table but not the boundary file "
        "— those values would match and then draw nothing"
    )


@requires_atlas
def test_the_uncoded_shapes_are_the_ones_we_expect():
    """If Natural Earth starts or stops issuing ids, we want to know."""
    uncoded = {
        g["properties"]["name"] for g in atlas_geometries() if not g.get("id")
    }
    assert uncoded == UNCODED, (
        f"the boundary file's id-less shapes changed: {sorted(uncoded)}"
    )


def test_the_alpha_codes_are_unique_and_well_formed():
    rows = table_rows()
    alpha2 = [row[1] for row in rows]
    alpha3 = [row[2] for row in rows]

    assert all(len(code) == 2 for code in alpha2)
    assert all(len(code) == 3 for code in alpha3), (
        "an alpha-3 code of the wrong length, most likely a typo in the table"
    )

    dup2 = {c for c in alpha2 if alpha2.count(c) > 1}
    dup3 = {c for c in alpha3 if alpha3.count(c) > 1}
    assert not dup2, f"duplicate alpha-2 codes: {sorted(dup2)}"
    assert not dup3, f"duplicate alpha-3 codes: {sorted(dup3)}"


def test_the_display_names_are_unique():
    """Two countries sharing a name makes a tooltip a lie."""
    names = [row[3] for row in table_rows()]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate country names: {sorted(dupes)}"


def test_aliases_and_cities_point_at_codes_that_exist():
    """A typo here fails open: the alias silently matches nothing.

    `ALIASES` carries the spellings warehouse columns actually use (UAE, UK) and
    `CITIES` rolls ports and financial centres up to their country. Both are
    keyed to alpha-2, and a wrong code is invisible at runtime.
    """
    text = TABLE_TS.read_text(encoding="utf-8")
    known = {row[1] for row in table_rows()}

    for name in ("ALIASES", "CITIES"):
        block = re.search(
            rf"const {name}: Record<string, string> = \{{(.*?)\n\}}", text, re.S
        )
        assert block, f"could not find {name}"
        codes = set(re.findall(r':\s*"([A-Z]{2})"', block.group(1)))
        assert codes, f"{name} parsed as empty"
        unknown = codes - known
        assert not unknown, f"{name} references unknown country codes: {sorted(unknown)}"

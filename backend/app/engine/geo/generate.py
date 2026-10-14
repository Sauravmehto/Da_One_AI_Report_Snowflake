"""Write `tables.py` from the frontend's TypeScript.

Run after changing `frontend/src/lib/geo-countries.ts` or `us-states.ts`:

    pipenv run python -m app.engine.geo.generate

The generated module is committed because the backend's Docker build context is
`./backend` — the frontend tree is not there at runtime, so the tables cannot be
parsed then. `tests/engine/test_geo_tables.py` re-parses the TypeScript and
fails if the committed file has fallen behind, which is what stops the two
drifting apart in silence.
"""

from __future__ import annotations

from pathlib import Path

from app.engine.geo import ts_source

OUT = Path(__file__).with_name("tables.py")

HEADER = '''"""Country and US state tables — GENERATED, do not edit by hand.

Written from `frontend/src/lib/geo-countries.ts` and `us-states.ts` by
`app.engine.geo.generate`, so the PDF places exactly the regions the screen
does, from exactly the same spellings. Edit the TypeScript and re-run the
generator; `tests/engine/test_geo_tables.py` fails if this file falls behind.
"""

from __future__ import annotations

'''


def render() -> str:
    countries = ts_source.parse_countries()
    aliases = ts_source.parse_aliases()
    cities = ts_source.parse_cities()
    states = ts_source.parse_states()
    state_aliases = ts_source.parse_state_aliases()

    lines = [HEADER]
    lines.append("#: (ISO numeric, alpha-2, alpha-3, display name).\n")
    lines.append("COUNTRIES: list[tuple[str, str, str, str]] = [\n")
    for row in countries:
        lines.append(f"    ({row[0]!r}, {row[1]!r}, {row[2]!r}, {row[3]!r}),\n")
    lines.append("]\n\n")

    lines.append("#: Spellings warehouse columns actually use -> alpha-2.\n")
    lines.append("ALIASES: dict[str, str] = {\n")
    for key in sorted(aliases):
        lines.append(f"    {key!r}: {aliases[key]!r},\n")
    lines.append("}\n\n")

    lines.append("#: Cities and ports that stand in for their country.\n")
    lines.append("CITIES: dict[str, str] = {\n")
    for key in sorted(cities):
        lines.append(f"    {key!r}: {cities[key]!r},\n")
    lines.append("}\n\n")

    lines.append("#: (FIPS, USPS code, name).\n")
    lines.append("US_STATES: list[tuple[str, str, str]] = [\n")
    for row in states:
        lines.append(f"    ({row[0]!r}, {row[1]!r}, {row[2]!r}),\n")
    lines.append("]\n\n")

    lines.append("#: Extra state spellings -> FIPS (DC is written several ways).\n")
    lines.append("US_STATE_ALIASES: dict[str, str] = {\n")
    for key in sorted(state_aliases):
        lines.append(f"    {key!r}: {state_aliases[key]!r},\n")
    lines.append("}\n")
    return "".join(lines)


def main() -> None:
    if not ts_source.available():
        raise SystemExit(
            "the frontend tree is not present; run this from a full checkout"
        )
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

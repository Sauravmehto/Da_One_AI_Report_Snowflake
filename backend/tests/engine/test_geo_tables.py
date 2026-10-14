"""The backend's country and state tables are generated — keep them generated.

`app/engine/geo/tables.py` is written from `frontend/src/lib/geo-countries.ts`
and `us-states.ts`, because the backend's Docker build context is `./backend`
and the frontend tree does not exist at runtime. Committing a generated file
invites it to fall behind the source, and the drift would be silent: a country
added on the frontend would shade on screen and be reported "not matched to a
country" on paper, in the same export, for the same value.

So this re-parses the TypeScript and fails the moment the two disagree. Run
`pipenv run python -m app.engine.geo.generate` to fix a failure — never edit
tables.py by hand.
"""

from __future__ import annotations

import pytest

from app.engine.geo import matching, ts_source
from app.engine.geo import tables

needs_frontend = pytest.mark.skipif(
    not ts_source.available(),
    reason="frontend tree not present (deployed backend); nothing to compare against",
)


@needs_frontend
def test_the_country_table_matches_the_typescript():
    assert [tuple(row) for row in tables.COUNTRIES] == ts_source.parse_countries(), (
        "tables.py has drifted from geo-countries.ts — re-run "
        "`python -m app.engine.geo.generate`"
    )


@needs_frontend
def test_the_alias_and_city_tables_match_the_typescript():
    assert tables.ALIASES == ts_source.parse_aliases()
    assert tables.CITIES == ts_source.parse_cities()


@needs_frontend
def test_the_state_table_matches_the_typescript():
    assert [tuple(row) for row in tables.US_STATES] == ts_source.parse_states()
    assert tables.US_STATE_ALIASES == ts_source.parse_state_aliases()


@needs_frontend
def test_normalisation_agrees_with_the_frontends():
    """Both sides must key on the same string or the tables are useless."""
    for value in ["U.A.E.", "Cote d'Ivoire", "Korea, South", "  Holland  ",
                  "St. Kitts and Nevis", "Côte d’Ivoire"]:
        assert matching.normalize(value) == ts_source.normalize(value), value


def test_the_spellings_real_data_uses_all_resolve():
    """ISO numeric, alpha-2, alpha-3 and the names columns actually contain."""
    cases = {
        "United Arab Emirates": "United Arab Emirates",
        "UAE": "United Arab Emirates",
        "AE": "United Arab Emirates",
        "ARE": "United Arab Emirates",
        "UK": "United Kingdom",
        "Holland": "Netherlands",
        "Ivory Coast": "Cote d'Ivoire",
        "USA": "United States",
        "Dubai": "United Arab Emirates",
        "Jebel Ali, Dubai (AEJEA)": "United Arab Emirates",
    }
    for value, expected in cases.items():
        found = matching.match_country(value)
        assert found is not None, f"{value!r} matched nothing"
        assert matching.country_name(found) == expected, value


def test_values_that_are_not_countries_are_reported_not_guessed():
    """An unshaded region is indistinguishable from one with no data."""
    for value in ["Europe", "EMEA", "APAC", "Alpha", "LATAM", "Chicago, IL"]:
        assert matching.match_country(value) is None, value
    assert matching.unmatched(
        ["Germany", "Europe", "UK", "EMEA", "Europe"], matching.match_country
    ) == ["Europe", "EMEA"]


def test_states_resolve_by_code_name_and_fips():
    for value, expected in [("CA", "California"), ("California", "California"),
                            ("06", "California"), ("6", "California"),
                            ("Washington DC", "District of Columbia")]:
        found = matching.match_state(value)
        assert found is not None, value
        assert matching.state_name(found) == expected, value

"""Match a data value to a country or a US state, exactly as the screen does.

Mirrors `frontend/src/lib/geo-countries.ts` and `us-states.ts`, from the tables
those files generate. The two matchers normalise DIFFERENTLY, and that is not an
oversight: the country matcher strips accents and punctuation so "U.A.E." and
"Cote d'Ivoire" resolve, while the state matcher only trims and lowercases,
which is what the TypeScript does. Making them uniform here would silently
change which values match on paper versus on screen.
"""

from __future__ import annotations

import re
import unicodedata

from app.engine.geo.tables import (
    ALIASES,
    CITIES,
    COUNTRIES,
    US_STATE_ALIASES,
    US_STATES,
)


def normalize(value: object) -> str:
    """The country matcher's key. Dots and apostrophes are deleted, not spaced."""
    text = unicodedata.normalize("NFD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"['’.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


_BY_ID = {c[0]: c for c in COUNTRIES}
_BY_ALPHA2 = {c[1]: c for c in COUNTRIES}

_BY_VALUE: dict[str, tuple[str, str, str, str]] = {}
for _c in COUNTRIES:
    _BY_VALUE[normalize(_c[1])] = _c
    _BY_VALUE[normalize(_c[2])] = _c
    _BY_VALUE[normalize(_c[3])] = _c
for _alias, _code in ALIASES.items():
    if _code in _BY_ALPHA2:
        _BY_VALUE[normalize(_alias)] = _BY_ALPHA2[_code]
for _city, _code in CITIES.items():
    # A city never overrides a country of the same name — Singapore, Panama and
    # Hong Kong are both, and the country is what a choropleth means.
    if _code in _BY_ALPHA2 and normalize(_city) not in _BY_VALUE:
        _BY_VALUE[normalize(_city)] = _BY_ALPHA2[_code]

_LOCODE = re.compile(r"\(\s*([A-Z]{2})[A-Z0-9]{3}\s*\)")


def country_name(country_id: str) -> str | None:
    row = _BY_ID.get(str(country_id).zfill(3))
    return row[3] if row else None


def match_country(value: object) -> str | None:
    """The ISO numeric id a value names, or None.

    Only the BRACKETED UN/LOCODE form counts. Reading a bare five-character
    token as a LOCODE looks harmless and is not: "Alpha" becomes Albania,
    "Cargo" Canada and "LATAM" Laos, each shading a country nobody has data on.
    """
    if value is None:
        return None
    raw = str(value)

    direct = _BY_VALUE.get(normalize(raw))
    if direct:
        return direct[0]

    locode = _LOCODE.search(raw)
    if locode:
        row = _BY_ALPHA2.get(locode.group(1))
        if row:
            return row[0]

    # "Mumbai, India" — the last comma-separated part is conventionally the
    # broadest. Two-letter tails are refused: "Chicago, IL" would otherwise
    # resolve to Israel and put every US city column in the Middle East.
    parts = raw.split(",")
    if len(parts) > 1:
        tail = normalize(parts[-1])
        if len(tail) > 2:
            row = _BY_VALUE.get(tail)
            if row:
                return row[0]
    return None


_STATE_BY_FIPS = {s[0]: s for s in US_STATES}
_STATE_BY_VALUE: dict[str, tuple[str, str, str]] = {}
for _s in US_STATES:
    _STATE_BY_VALUE[_s[1].lower()] = _s
    _STATE_BY_VALUE[_s[2].lower()] = _s
    _STATE_BY_VALUE[_s[0]] = _s
    # A FIPS code arriving as a number has usually lost its leading zero.
    _STATE_BY_VALUE[str(int(_s[0]))] = _s
for _alias, _fips in US_STATE_ALIASES.items():
    if _fips in _STATE_BY_FIPS:
        _STATE_BY_VALUE[_alias.strip().lower()] = _STATE_BY_FIPS[_fips]


def state_name(fips: str) -> str | None:
    row = _STATE_BY_FIPS.get(str(fips).zfill(2))
    return row[2] if row else None


def match_state(value: object) -> str | None:
    """The FIPS code a value names, or None. Trim and lowercase only, as the TS."""
    if value is None:
        return None
    row = _STATE_BY_VALUE.get(str(value).strip().lower())
    return row[0] if row else None


def unmatched(values, matcher) -> list[str]:
    """Distinct values the matcher could not place, in first-seen order.

    An unshaded region is indistinguishable from one with no data, so these are
    named on the chart rather than dropped.
    """
    missed: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if matcher(value) is None and text not in missed:
            missed.append(text)
    return missed

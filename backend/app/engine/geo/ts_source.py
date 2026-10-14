"""Read the frontend's geography tables out of their TypeScript.

The PDF has to place the same countries and states the screen does, from the
same spellings. Two hand-maintained tables would drift the moment either side
gained a country — and the drift would be silent, showing as a region that
shades on screen and is reported "not matched" on paper.

So there is one source, `frontend/src/lib/geo-countries.ts` and
`frontend/src/lib/us-states.ts`, and this parses it. The result is written out
as `tables.py` by `generate.py`, because the backend's Docker build context is
`./backend` — the frontend directory does not exist at runtime and cannot be
read then. `tests/engine/test_geo_tables.py` re-parses the TypeScript and fails
if the committed tables no longer match it.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[4] / "frontend" / "src" / "lib"
COUNTRIES_TS = FRONTEND / "geo-countries.ts"
STATES_TS = FRONTEND / "us-states.ts"


def available() -> bool:
    """False in a deployed backend, where the frontend tree is not present."""
    return COUNTRIES_TS.is_file() and STATES_TS.is_file()


def normalize(value: object) -> str:
    """The frontend's `normalize`, character for character.

    Dots and apostrophes are DELETED rather than turned into separators, so
    "U.A.E." collapses to "uae"; every other separator becomes one space.
    """
    text = unicodedata.normalize("NFD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"['’.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _block(text: str, header: str, opener: str, closer: str) -> str:
    """The balanced `opener`..`closer` run of the declaration named by `header`.

    Scanning starts after the `=`, not after the header: a type annotation
    carries brackets of its own, and `Array<[string, string, string, string]>`
    would otherwise be returned as the table.
    """
    start = text.index(header)
    open_at = text.index(opener, text.index("=", start))
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    raise ValueError(f"unterminated block for {header!r}")


def parse_countries() -> list[tuple[str, str, str, str]]:
    """(numeric id, alpha-2, alpha-3, display name) for every country."""
    text = COUNTRIES_TS.read_text(encoding="utf-8")
    body = _block(text, "const TABLE: Array<[string, string, string, string]>", "[", "]")
    rows = re.findall(
        r'\[\s*"(\d{3})"\s*,\s*"([A-Z]{2})"\s*,\s*"([A-Z]{3})"\s*,\s*"([^"]+)"\s*\]',
        body,
    )
    if not rows:
        raise ValueError("the country TABLE parsed as empty")
    return [(a, b, c, d) for a, b, c, d in rows]


def parse_aliases() -> dict[str, str]:
    """Alternate spellings -> alpha-2 (UK, Holland, Ivory Coast, ...)."""
    text = COUNTRIES_TS.read_text(encoding="utf-8")
    return _named_pairs(text, "const ALIASES: Record<string, string>")


def parse_cities() -> dict[str, str]:
    """Cities and ports -> alpha-2, so "Dubai" reaches the UAE."""
    text = COUNTRIES_TS.read_text(encoding="utf-8")
    return _named_pairs(text, "const CITIES: Record<string, string>")


def _named_pairs(text: str, header: str) -> dict[str, str]:
    body = _block(text, header, "{", "}")
    out: dict[str, str] = {}
    for match in re.finditer(
        r'(?:"(?P<q>[^"]+)"|(?P<b>[A-Za-z_][\w]*))\s*:\s*"(?P<v>[A-Z]{2})"', body
    ):
        key = match.group("q") if match.group("q") is not None else match.group("b")
        out[key] = match.group("v")
    if not out:
        raise ValueError(f"{header} parsed as empty")
    return out


def parse_states() -> list[tuple[str, str, str]]:
    """(FIPS, USPS code, name) for the 50 states plus DC."""
    text = STATES_TS.read_text(encoding="utf-8")
    body = _block(text, "export const US_STATES: UsState[]", "[", "]")
    rows = re.findall(
        r'\{\s*fips:\s*"(\d{2})"\s*,\s*code:\s*"([A-Z]{2})"\s*,\s*name:\s*"([^"]+)"\s*\}',
        body,
    )
    if not rows:
        raise ValueError("US_STATES parsed as empty")
    return rows


def parse_state_aliases() -> dict[str, str]:
    """The handful set imperatively — DC is written half a dozen ways."""
    text = STATES_TS.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for key, fips in re.findall(
        r'BY_VALUE\.set\(\s*"([^"]+)"\s*,\s*BY_FIPS\.get\("(\d{2})"\)', text
    ):
        out[key] = fips
    return out

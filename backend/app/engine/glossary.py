"""Glossary loader + lookup for explain_concept.

The guardrail that matters: this NEVER fabricates a definition. It answers
only from the curated glossary files. If a term isn't found, it says so and
offers close matches / available terms — it does not guess. That
non-fabrication behavior is the whole reason the tool exists instead of
letting the model define terms freely.

Terms live in config/glossary/ as one or more TOML files, ALL of which are
loaded and merged. That is deliberate: a deployment can drop in its own
house-terms file (e.g. `acme_terms.toml`) next to the shipped baseline
without editing a vendored file, and can override a shipped definition by
redefining the same term. Files load in filename order, so a later file wins
on a collision.

Matching is forgiving: users type "DSCR", "dscr", "debt service coverage",
"debt-service coverage ratio" for the same thing, so lookup is normalized
(case-, space-, and hyphen-insensitive) and alias-aware.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Client-editable content lives outside the code tree: backend/config/glossary/
_GLOSSARY_DIR = Path(__file__).resolve().parents[2] / "config" / "glossary"


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace.

    So 'Debt-Service Coverage Ratio', 'debt service coverage ratio', and
    'DEBT_SERVICE_COVERAGE' all normalize to the same key.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class Term:
    name: str
    category: str
    definition: str
    aliases: tuple[str, ...] = ()
    formula: str = ""
    see_also: tuple[str, ...] = ()


@dataclass
class Glossary:
    terms: list[Term] = field(default_factory=list)
    _index: dict[str, Term] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Glossary":
        """Load terms from a glossary directory (every *.toml, merged) or a
        single glossary file. Defaults to config/glossary/.

        Each file is parsed in its own try/except so one malformed client file
        degrades to "those terms are missing" rather than emptying the whole
        glossary — same resilience rule the semantic layer uses.
        """
        target = path or _GLOSSARY_DIR
        g = cls()

        if target.is_dir():
            files = sorted(target.glob("*.toml"))
            if not files:
                log.error("No glossary TOML files found in %s", target)
        elif target.exists():
            files = [target]
        else:
            log.error("Glossary path not found: %s", target)
            return g

        for file in files:
            g._load_file(file)
        return g

    def _load_file(self, path: Path) -> None:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            log.error("Glossary file %s failed to parse: %s", path.name, exc)
            return

        for entry in data.get("term", []):
            term = Term(
                name=entry["name"],
                category=entry.get("category", "General"),
                definition=entry.get("definition", ""),
                aliases=tuple(entry.get("aliases", [])),
                formula=entry.get("formula", ""),
                see_also=tuple(entry.get("see_also", [])),
            )
            # Index the canonical name, the acronym inside parentheses, and
            # every alias — all normalized.
            keys = {term.name, *term.aliases}
            acro = re.search(r"\(([^)]+)\)", term.name)
            if acro:
                keys.add(acro.group(1))

            # A later file redefining an existing term REPLACES it rather than
            # shadowing it: leaving the superseded copy in `terms` would let it
            # keep surfacing in suggest()/list output while lookup() returned
            # the new one — two different answers for the same term.
            superseded = {self._index[k] for k in map(_normalize, keys) if k in self._index}
            if superseded:
                self.terms = [t for t in self.terms if t not in superseded]
                log.debug("Glossary file %s overrides term(s): %s",
                          path.name, ", ".join(sorted(t.name for t in superseded)))

            self.terms.append(term)
            for k in keys:
                self._index[_normalize(k)] = term

    def lookup(self, query: str) -> Term | None:
        """Exact (normalized) match on name/acronym/alias, else None."""
        return self._index.get(_normalize(query))

    def find_term(self, text: str) -> Term | None:
        """Find a glossary term *mentioned inside* a free-text question.

        lookup() requires the query to BE a term key ("DSCR"); this instead
        scans a whole question ("What does DSCR mean?", "Define TVPI") for any
        indexed name/alias/acronym and returns the most specific (longest)
        match, or None. Matching is whole-word on the normalized text, so a key
        is never matched inside a larger word (e.g. 'irr' won't hit 'mirror').
        When several terms appear, the longest key wins ('coverage ratio' over
        a bare 'coverage').
        """
        norm = _normalize(text)
        if not norm:
            return None
        padded = f" {norm} "
        best: Term | None = None
        best_len = 0
        for key, term in self._index.items():
            if key and len(key) > best_len and f" {key} " in padded:
                best, best_len = term, len(key)
        return best

    def suggest(self, query: str, limit: int = 5) -> list[str]:
        """Close matches for a miss: terms whose name/alias contains — or is
        contained by — the query token. Purely to help the user retry; never a
        substitute for a real definition."""
        q = _normalize(query)
        if not q:
            return []
        hits: list[str] = []
        for term in self.terms:
            hay = [_normalize(term.name), *(_normalize(a) for a in term.aliases)]
            if any(q in h or h in q for h in hay):
                hits.append(term.name)
        return hits[:limit]

    def by_category(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for term in self.terms:
            out.setdefault(term.category, []).append(term.name)
        return out


# Loaded once at import.
glossary = Glossary.load()


def extract_term(text: str) -> str | None:
    """Return the canonical name of a glossary term mentioned in a free-text
    question, or None if none is found.

    ask_data_question routes a WHOLE question ("What does DSCR mean?") to the
    concept path, but that phrase is not itself a lookup key — so it must pull
    the term out before calling explain(). explain_concept doesn't need this: it
    already receives a bare, model-extracted term.
    """
    term = glossary.find_term(text)
    return term.name if term else None


def explain(query: str) -> dict:
    """Return a structured explanation, or a non-fabricated 'not found' result.

    Shape on hit:  {found, term, category, definition, formula?, see_also?}
    Shape on miss: {found: False, message, suggestions, categories}
    """
    if not query or not query.strip():
        return {
            "found": False,
            "message": "No term provided.",
            "suggestions": [],
        }

    term = glossary.lookup(query)
    if term is None:
        suggestions = glossary.suggest(query)
        msg = (
            f"No glossary entry for '{query.strip()}'. This tool returns only "
            f"curated definitions and does not invent them."
        )
        if suggestions:
            msg += " Did you mean one of the suggestions?"
        return {
            "found": False,
            "message": msg,
            "suggestions": suggestions,
            "categories": glossary.by_category(),
        }

    result = {
        "found": True,
        "term": term.name,
        "category": term.category,
        "definition": term.definition,
    }
    if term.formula:
        result["formula"] = term.formula
    if term.see_also:
        result["see_also"] = list(term.see_also)
    return result

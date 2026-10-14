"""Tests for the glossary — especially the non-fabrication guarantee.

The critical contract: explain() must NEVER return a definition for a term
that isn't in glossary.toml. If that ever breaks, the tool is fabricating,
which is the one thing it must not do.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.engine import glossary


def test_glossary_loads():
    assert len(glossary.glossary.terms) > 0, "glossary.toml failed to load"


def test_forgiving_matching_resolves_same_term():
    variants = ["DSCR", "dscr", "Debt Service Coverage",
                "debt-service coverage ratio", "DEBT_SERVICE_COVERAGE"]
    results = [glossary.explain(v) for v in variants]
    assert all(r["found"] for r in results)
    # All resolve to the same canonical term
    assert len({r["term"] for r in results}) == 1


def test_acronym_and_full_name_both_resolve():
    for q in ["TVPI", "total value to paid-in", "MOIC", "cap rate", "NOI"]:
        assert glossary.explain(q)["found"], f"missed {q!r}"


def test_formula_and_see_also_surface_when_present():
    r = glossary.explain("net leverage")
    assert r["found"]
    assert r.get("formula")
    assert r.get("see_also")


def test_unknown_term_never_fabricates():
    """The core guarantee: no definition is returned for an unknown term."""
    r = glossary.explain("flux capacitor ratio")
    assert r["found"] is False
    assert "definition" not in r          # NOTHING resembling a definition
    assert "formula" not in r
    assert "does not invent" in r["message"].lower() or "curated" in r["message"].lower()


def test_near_miss_offers_suggestions():
    r = glossary.explain("coverage")
    assert r["found"] is False
    assert r["suggestions"]  # should point toward DSCR


def test_empty_query_is_handled():
    r = glossary.explain("")
    assert r["found"] is False
    assert "definition" not in r


# --- term extraction from a phrased question (ask_data_question path) --------
# ask_data_question routes a whole question to the concept path, so the term
# must be pulled out of natural phrasings before the glossary lookup. Bare
# glossary.explain(question) does NOT do this (it needs a key), which is the
# bug this covers.

def test_extract_term_from_common_phrasings():
    cases = {
        "What does DSCR mean?": "Debt Service Coverage Ratio (DSCR)",
        "Define TVPI": "Total Value to Paid-In (TVPI)",
        "what is MOIC": "Multiple on Invested Capital (MOIC)",
        "Explain net leverage": "Net Leverage",
        "can you tell me what the internal rate of return is?":
            "Internal Rate of Return (IRR)",
        "what is the coverage ratio": "Debt Service Coverage Ratio (DSCR)",
    }
    for question, expected in cases.items():
        assert glossary.extract_term(question) == expected, f"missed {question!r}"


def test_extracted_term_resolves_to_a_definition():
    """The end-to-end shape ask_data_question relies on: extract, then explain
    the extracted term, yields found=True with a real definition."""
    for question in ["What does DSCR mean?", "Define TVPI", "what is MOIC",
                     "explain delinquency", "what is PIK interest"]:
        term = glossary.extract_term(question)
        assert term is not None, f"no term extracted from {question!r}"
        r = glossary.explain(term)
        assert r["found"], f"{question!r} -> {term!r} did not resolve"
        assert r.get("definition")


def test_extract_term_returns_none_for_no_known_term():
    """A concept-ish question naming nothing in the glossary extracts nothing —
    ask_data_question then falls back to the raw question (graceful miss)."""
    assert glossary.extract_term("what does flux capacitor ratio mean") is None
    assert glossary.extract_term("") is None


def test_extract_does_not_match_key_inside_a_word():
    """Whole-word matching: an acronym must not match as a substring of a
    larger word (e.g. 'IRR' inside 'mirror')."""
    assert glossary.extract_term("how does a mirror work") is None


def test_direct_bare_term_lookup_unaffected():
    """explain_concept passes an already-extracted bare term straight to
    explain(); that path must keep working without extraction."""
    r = glossary.explain("DSCR")
    assert r["found"] and r["term"] == "Debt Service Coverage Ratio (DSCR)"

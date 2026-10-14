"""Work out what a parsed document is about.

The semantic half of document ingestion. The parser said what is on the page;
this decides what kind of document it is, who it concerns, and which of its
label/value pairs are facts worth being able to query.

DRIVEN BY CONTENT, NOT BY FILE NAME
-----------------------------------
A file name is what somebody typed when saving. On a client's shared drive it
carries no convention worth trusting, so nothing here depends on one: the
document type comes from the words on the first pages, and the entity from the
document's own title block. The file name is a last resort, used only when the
content says nothing, and marked as the weaker evidence it is.

WHAT THE MODEL ADDS
-------------------
The rule-based pass alone produces usable facts, because a label/value grid is
already a label and a value. The model is asked only the questions rules cannot
answer — is this a mortgage or a lease, which of these names is the subject —
and when it is unavailable the rules still stand. That is the same arrangement
the spreadsheet mapper has: no model means worse names, never lost data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engine.document_store import Edge, Fact

log = logging.getLogger(__name__)

#: What each document type is called, and the words that corroborate it.
#:
#: A type needs at least one NAMING signal — a phrase that only appears because
#: the document is that thing. The supporting words then raise confidence but
#: can never assert a type on their own: site notes mentioning "unit" and
#: "occupancy" are notes about a building, not a rent roll, and calling them one
#: puts a wrong label on a real document.
DOC_TYPE_SIGNALS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "mortgage_agreement",
        ("mortgage", "deed of trust", "promissory note"),
        ("lender", "borrower", "principal", "maturity"),
    ),
    (
        "loan_agreement",
        ("loan agreement", "credit agreement", "facility agreement"),
        ("lender", "borrower", "drawdown", "covenant"),
    ),
    (
        "lease",
        ("lease agreement", "demised premises", "this lease"),
        ("landlord", "lessee", "lessor", "rent commencement"),
    ),
    (
        "purchase_agreement",
        ("purchase and sale", "purchase agreement", "agreement of sale"),
        ("buyer", "seller", "closing date", "earnest money"),
    ),
    (
        "rent_roll",
        ("rent roll", "rent schedule", "lease expiration schedule"),
        ("unit", "tenant", "occupancy", "square feet"),
    ),
    (
        "appraisal",
        ("appraisal report", "appraised value", "opinion of value"),
        ("comparable sales", "valuation", "highest and best use"),
    ),
    (
        "income_statement",
        ("income statement", "operating statement", "net operating income"),
        ("revenue", "operating expenses", "noi"),
    ),
    (
        "insurance",
        ("insurance policy", "certificate of insurance", "policy number"),
        ("coverage", "insured", "premium", "deductible"),
    ),
    (
        "title_report",
        ("title report", "title commitment", "preliminary title"),
        ("easement", "encumbrance", "vesting", "exception"),
    ),
]

#: Labels naming another party, and what that party is to the subject. Each
#: becomes an edge, which is what lets "who lends against this building" be a
#: query rather than a search.
PARTY_RELATIONS: dict[str, str] = {
    "borrower": "borrows_from_inverse",
    "lender": "borrows_from",
    "mortgagee": "borrows_from",
    "mortgagor": "borrows_from_inverse",
    "landlord": "leases_from",
    "lessor": "leases_from",
    "tenant": "leases_to",
    "lessee": "leases_to",
    "buyer": "sold_to",
    "purchaser": "sold_to",
    "seller": "bought_from",
    "guarantor": "guaranteed_by",
    "property manager": "managed_by",
    "manager": "managed_by",
    "servicer": "serviced_by",
    "trustee": "trustee_is",
    "insurer": "insured_by",
}

#: Labels that are not facts about anything — page furniture and formatting.
_NON_FACT_LABELS = {
    "page", "date printed", "printed", "prepared by", "confidential",
    "notes", "note", "comments", "total", "subtotal", "",
}

_MONEY = re.compile(r"^\(?\s*[-+]?\$?\s*[\d,]+(?:\.\d+)?\s*\)?$")
_PERCENT = re.compile(r"^[-+]?[\d,]*\.?\d+\s*%$")
_MULTIPLE = re.compile(r"^[-+]?[\d,]*\.?\d+\s*x$", re.I)


def to_number(value: Any) -> float | None:
    """The number a cell holds, if it holds one.

    Handles the ways money and ratios are written in documents: `$1,234`,
    `(1,234)` for negative, `5.85%`, `1.15x`.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[()$,%\s]", "", text)
    cleaned = re.sub(r"[xX]$", "", cleaned)
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def unit_of(value: Any) -> str:
    text = str(value or "").strip()
    if _PERCENT.match(text):
        return "pct"
    if _MULTIPLE.match(text):
        return "multiple"
    if "$" in text or _MONEY.match(text):
        return "usd"
    return "number" if to_number(text) is not None else "text"


def metric_name(label: str) -> str:
    """A label reduced to a queryable metric name."""
    cleaned = re.sub(r"[^\w\s]", " ", (label or "").lower())
    return "_".join(cleaned.split())[:80]


# ── label/value blocks that are not ruled tables ──────────────────────────────

#: A label and its value on one line. Documents lay these out three ways: with
#: a colon, with a run of spaces, or with a tab. Only ruled tables reach the
#: parser's table detection, and most summary blocks are not ruled — so without
#: this the majority of a document's stated figures are simply never seen.
_LABELLED_LINE = re.compile(
    r"^\s*(?P<label>[A-Za-z][A-Za-z0-9 /&'()\.\-]{1,38}?)"
    r"\s*(?::\s+|\t+|\s{2,})"
    r"(?P<value>\S.*?)\s*$"
)

#: A line ending like prose is a sentence that happens to contain a colon, not
#: a label and a value.
_PROSE_TAIL = re.compile(r"[.;,]$")


def pairs_from_text(text: str) -> list[tuple[str, str]]:
    """Label/value pairs stated as lines rather than as a table.

    Deliberately strict about what counts as a label: short, starting with a
    letter, and not a sentence. A summary block gives up its figures this way;
    a paragraph gives up nothing, which is the point.
    """
    pairs: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if len(line) > 200:
            continue
        match = _LABELLED_LINE.match(line)
        if not match:
            continue
        label = match.group("label").strip()
        value = match.group("value").strip()
        if not label or not value:
            continue
        if _PROSE_TAIL.search(value) and len(value.split()) > 6:
            continue  # a sentence, not a value
        if len(label.split()) > 5:
            continue
        pairs.append((label, value))
    return pairs


# ── what kind of document is this ─────────────────────────────────────────────

#: Only the opening pages decide the type. A mortgage agreement mentions
#: "tenant" somewhere in its forty pages without being a rent roll.
_TYPE_SCAN_PAGES = 3


def guess_doc_type(parsed: Any) -> tuple[str, float]:
    """The kind of document this is, from its own words.

    Scored by how many of a type's signals appear, so a document naming both a
    borrower and a lender reads as a mortgage rather than whichever rule was
    written first.
    """
    text = " ".join(p.text for p in parsed.pages[:_TYPE_SCAN_PAGES]).lower()
    if not text.strip():
        return "", 0.0

    best_type, best_score = "", 0
    for doc_type, naming, supporting in DOC_TYPE_SIGNALS:
        named = sum(1 for signal in naming if signal in text)
        if not named:
            continue  # nothing calls it this; supporting words cannot elect it
        score = 2 * named + sum(1 for signal in supporting if signal in text)
        if score > best_score:
            best_type, best_score = doc_type, score
    if not best_type:
        # Better to say nothing than to label a document wrongly: an unknown
        # type still searches, still extracts, and still cites.
        return "", 0.0
    return best_type, min(0.95, 0.4 + 0.15 * best_score)


# ── who is it about ───────────────────────────────────────────────────────────

#: Labels whose value names the document's subject outright.
_SUBJECT_LABELS = ("property name", "property", "subject property", "premises", "project", "asset")

_TITLE_NOISE = re.compile(
    r"\b(agreement|statement|report|schedule|roll|summary|analysis|"
    r"draft|final|confidential|page|exhibit|appendix)\b",
    re.I,
)


def page_pairs(page: Any) -> list[tuple[str, str | None]]:
    """Every label/value pair on a page, ruled or not."""
    pairs: list[tuple[str, str | None]] = []
    for table in page.tables:
        if table.shape == "kv_grid":
            pairs.extend(table.pairs)
    pairs.extend(pairs_from_text(page.text))
    return pairs


def guess_entity(parsed: Any, filename: str) -> tuple[str, str]:
    """The property or party this document is about, and where that came from.

    Tried in order of how much the document itself is asserting: a labelled
    subject, then the title block, then the file name. The source is returned
    because a name read off a file name is a guess and should be shown as one.
    """
    for page in parsed.pages[:2]:
        stated = list(page_pairs(page))
        for label, value in stated:
            if label.strip().lower().rstrip(":") in _SUBJECT_LABELS and value:
                return value.strip(), "stated"

    for page in parsed.pages[:1]:
        for line in page.text.split("\n"):
            candidate = line.strip()
            # A title line: substantial, not a sentence, not document furniture.
            if (
                8 <= len(candidate) <= 70
                and not candidate.endswith((".", ";", ":"))
                and not _TITLE_NOISE.search(candidate)
                and sum(c.isdigit() for c in candidate) < len(candidate) / 3
                and len(candidate.split()) >= 2
            ):
                return candidate, "title"

    stem = Path(filename).stem
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\b\d{1,4}\b", " ", stem)
    stem = _TITLE_NOISE.sub(" ", stem)
    return " ".join(stem.split()).strip() or Path(filename).stem, "filename"


# ── facts and edges ───────────────────────────────────────────────────────────


def facts_from_pairs(
    pairs: list[tuple[str, str | None]], entity: str, page: int
) -> list[Fact]:
    """Every label/value pair that says something about the subject."""
    facts: list[Fact] = []
    for label, value in pairs:
        clean_label = (label or "").strip().rstrip(":")
        if not clean_label or clean_label.lower() in _NON_FACT_LABELS:
            continue
        if value is None or not str(value).strip():
            continue
        if clean_label.lower() in PARTY_RELATIONS:
            continue  # a party, recorded as an edge instead
        facts.append(
            Fact(
                entity_name=entity,
                metric=metric_name(clean_label),
                value_text=str(value).strip(),
                value_num=to_number(value),
                unit=unit_of(value),
                source_page=page,
                source_label=clean_label,
                extractor="kv_grid",
                confidence=0.85,
            )
        )
    return facts


def edges_from_pairs(
    pairs: list[tuple[str, str | None]], entity: str, page: int
) -> list[Edge]:
    """Relationships stated as a named party in a summary block."""
    edges: list[Edge] = []
    for label, value in pairs:
        relation = PARTY_RELATIONS.get((label or "").strip().lower().rstrip(":"))
        if not relation or not value or not str(value).strip():
            continue
        # `_inverse` marks a label naming the SUBJECT rather than the other
        # party — "Borrower: Cherry Creek" says who this is, not who it deals
        # with, so the edge is not drawn from it.
        if relation.endswith("_inverse"):
            continue
        edges.append(
            Edge(
                src_name=entity,
                relation=relation,
                dst_name=str(value).strip(),
                source_page=page,
                confidence=0.8,
            )
        )
    return edges


def aliases_from_pairs(pairs: list[tuple[str, str | None]]) -> list[str]:
    """Other names the document gives its own subject.

    A mortgage naming its borrower is naming the same entity the rent roll
    calls the property, and recording that is what joins the two.
    """
    out: list[str] = []
    for label, value in pairs:
        key = (label or "").strip().lower().rstrip(":")
        if key in ("borrower", "mortgagor", "owner", "grantor") and value:
            out.append(str(value).strip())
    return out


# ── the whole extraction ──────────────────────────────────────────────────────


@dataclass
class Extraction:
    doc_type: str = ""
    doc_type_confidence: float = 0.0
    entity: str = ""
    entity_source: str = ""
    aliases: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def extract(parsed: Any, filename: str = "") -> Extraction:
    """Read a parsed document into facts, edges and the names involved."""
    filename = filename or parsed.filename
    doc_type, confidence = guess_doc_type(parsed)
    entity, entity_source = guess_entity(parsed, filename)

    result = Extraction(
        doc_type=doc_type,
        doc_type_confidence=confidence,
        entity=entity,
        entity_source=entity_source,
    )
    for page in parsed.pages:
        pairs = page_pairs(page)
        if not pairs:
            continue
        result.facts.extend(facts_from_pairs(pairs, entity, page.page))
        result.edges.extend(edges_from_pairs(pairs, entity, page.page))
        result.aliases.extend(aliases_from_pairs(pairs))
    return result


# ── the model's part ──────────────────────────────────────────────────────────

_REVIEW_PROMPT = """You are cataloguing a document for a real estate firm.

Below is what was read from it deterministically: the opening text, and the
label/value pairs found in its summary blocks.

Answer ONLY these questions, as JSON:

  doc_type   A short snake_case kind, e.g. mortgage_agreement, rent_roll,
             lease, appraisal, income_statement. Use "" if genuinely unclear.
  entity     The property or company this document is ABOUT — its subject, not
             its author and not the other party to it. Use the name as the
             document writes it.
  aliases    Other names in the document for that same subject. [] if none.

Return nothing but the JSON object.

--- opening text ---
{text}

--- label/value pairs ---
{pairs}
"""


def review(parsed: Any, extraction: Extraction) -> Extraction:
    """Ask the model the questions the rules cannot answer.

    Only the type and the subject. The facts are already correct — a label and
    a value need no interpretation — and sending them would spend a large
    prompt to be told back what was already known.

    Returns the extraction unchanged when the model is unavailable, so an
    outage costs naming quality rather than the document.
    """
    from app.engine.llm import MODEL_SQL, _create, _json_object, _text

    text = " ".join(p.text for p in parsed.pages[:2])[:4000]
    pairs = [
        f"{label}: {value}"
        for page in parsed.pages[:3]
        for label, value in page_pairs(page)[:40]
    ][:60]
    prompt = _REVIEW_PROMPT.format(text=text, pairs="\n".join(pairs))

    try:
        response = _create(
            "document_review",
            model=MODEL_SQL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = _json_object(_text(response))
    except Exception as exc:  # noqa: BLE001 - ingestion must never fail on this
        log.info("Document review unavailable for %s: %s", parsed.filename, exc)
        return extraction

    if not isinstance(answer, dict):
        return extraction

    doc_type = str(answer.get("doc_type") or "").strip()
    if doc_type:
        extraction.doc_type = doc_type
        extraction.doc_type_confidence = 0.9
    entity = str(answer.get("entity") or "").strip()
    if entity:
        # The rules' answer becomes an alias rather than being discarded: it is
        # a real name from the document either way, and one more way to join.
        if extraction.entity and extraction.entity != entity:
            extraction.aliases.append(extraction.entity)
        extraction.entity = entity
        extraction.entity_source = "model"
        for fact in extraction.facts:
            fact.entity_name = entity
        for edge in extraction.edges:
            if edge.src_name != entity:
                edge.src_name = entity
    aliases = answer.get("aliases")
    if isinstance(aliases, list):
        extraction.aliases.extend(str(a).strip() for a in aliases if str(a).strip())
    return extraction


def as_dict(extraction: Extraction) -> dict[str, Any]:
    return {
        "doc_type": extraction.doc_type,
        "doc_type_confidence": extraction.doc_type_confidence,
        "entity": extraction.entity,
        "entity_source": extraction.entity_source,
        "aliases": sorted(set(extraction.aliases)),
        "facts": len(extraction.facts),
        "edges": len(extraction.edges),
    }


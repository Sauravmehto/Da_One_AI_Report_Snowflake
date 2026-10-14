"""The semantic half of Excel ingestion: what do these fields MEAN?

excel_profiler.py extracts what is provably true about a workbook — where the
regions are, which cells are derived, what unit each carries. It deliberately
stops short of meaning, because meaning is judgement and no rule set survives
contact with real client spreadsheets.

This module asks a model that question, and only that question. It never sees
the workbook and never touches a number: it receives the profiler's evidence
packet and returns a mapping from each detected column or field onto a named
concept. The data itself is extracted deterministically afterwards, and every
figure a user eventually sees is computed by a query engine over those rows.

WHY A CONCEPT DICTIONARY RATHER THAN A TEMPLATE
-----------------------------------------------
Freezing a layout ("header on row 4, rent in column G") produces one stored
template per file shape, and every one of them rots the moment a property
manager renames a column or inserts a row. Freezing MEANING does not: "monthly
rent" is monthly rent whether the header says Rent, Monthly Rent, Mo. Rent or
Rent/Mo, and whether it sits in column G or column J.

So the durable artefact is a vocabulary of concepts, and mapping is the act of
attaching this file's columns to it. Measured across the three sample
workbooks, 20 property tabs of an operating statement produced 360 field
instances but only 24 distinct concepts, and most sheets after the eighth
introduced none at all. The vocabulary converges; layouts do not.

`known_concepts` is what makes that compounding real — pass the concepts already
in the dictionary and the model reuses them instead of coining synonyms. An
empty list is the cold-start case and is perfectly valid; it simply means every
concept in this file is new.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.engine import audit
from app.engine.llm import MODEL_SQL, LLMError, _get_client, _json_object, _text

log = logging.getLogger(__name__)

#: Mapping is judgement over a small amount of text, not arithmetic, so it uses
#: the same model tier as SQL generation rather than the cheap classifier.
MODEL_MAPPING = MODEL_SQL

#: One shape at a time, so the response is bounded by the widest shape rather
#: than by the whole workbook. Mapping all five shapes of a rent roll in one
#: call overran the limit and truncated mid-object.
MAX_TOKENS = 8192

#: Below this, the caller must ask a human before trusting the mapping.
CONFIDENCE_FLOOR = 0.7

#: Shapes after the first are independent, so they run concurrently. Capped
#: because a wide workbook should not open a connection per shape.
MAX_PARALLEL_SHAPES = 4


def _has_mappable_fields(shape: dict[str, Any]) -> bool:
    """Whether a shape contains anything worth asking a model about.

    A shape made only of prose regions — a title block, a footnote — has no
    columns and no fields. Sending it costs a round trip and comes back with an
    empty region that clutters the result.
    """
    for region in shape.get("regions") or []:
        if region.get("columns") or region.get("fields"):
            return True
    return False

_SLUG = re.compile(r"[^a-z0-9]+")


def to_slug(name: str) -> str:
    """A concept name reduced to a stable dictionary key."""
    return _SLUG.sub("_", (name or "").strip().lower()).strip("_")


# ── result types ──────────────────────────────────────────────────────────────


@dataclass
class FieldMapping:
    """One column or form field, attached to a concept."""

    source: str  #: the header or label exactly as it appears in the workbook
    concept: str  #: dictionary key, reused across files and document types
    display_name: str
    data_type: str  #: "text" | "number" | "currency" | "percent" | "date" | "bool"
    #: ISO 4217 code, meaningful only alongside data_type "currency".
    #:
    #: Three states, and the last two are NOT the same. None is "not stated",
    #: which renders as dollars -- correct for a plain "$" column and for every
    #: workbook mapped before this field existed. "" is "this column mixes
    #: currencies", which must render with no symbol at all. Dropping "" as
    #: falsy restores the dollar default to a column that has just said it is
    #: not dollars, which is the original bug wearing a new hat.
    currency: str | None = None
    role: str = "dimension"  #: "identifier"|"measure"|"dimension"|"date"|"derived"
    include: bool = True  #: False for derived subtotals and decorative columns
    confidence: float = 0.0
    reason: str = ""
    is_new_concept: bool = False

    @property
    def needs_review(self) -> bool:
        return self.confidence < CONFIDENCE_FLOOR


@dataclass
class Question:
    """One thing the model could not settle, addressed to a specific field.

    Carries `about` and `table` so the UI can render it beside the field it
    concerns rather than in a list at the top of the page, and `options` so it
    can be answered with a click instead of a support ticket. A question nobody
    can act on is worse than no question at all.
    """

    question: str
    about: str = ""
    table: str = ""
    options: list[str] = field(default_factory=list)


@dataclass
class RegionMapping:
    """One region of one shape, described in business terms."""

    sheets: list[str]
    region_rows: str
    purpose: str  #: e.g. "unit-level rent roll for a single property"
    grain: str  #: what one row represents, e.g. "one leased unit"
    suggested_table: str
    fields: list[FieldMapping] = field(default_factory=list)


@dataclass
class MappingProposal:
    """Everything the model concluded, plus what it could not settle."""

    regions: list[RegionMapping] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    new_concepts: list[str] = field(default_factory=list)
    model: str = MODEL_MAPPING

    @property
    def needs_review(self) -> list[FieldMapping]:
        return [f for r in self.regions for f in r.fields if f.needs_review]

    def as_dict(self) -> dict[str, Any]:
        """A plain-JSON view.

        Questions are converted too — leaving dataclasses in here worked only
        because pydantic coerced them on the way out of the API, and broke the
        moment anything tried to json.dumps a proposal in order to store it.
        """
        return {
            "regions": [asdict(r) for r in self.regions],
            "questions": [asdict(q) for q in self.questions],
            "new_concepts": self.new_concepts,
            "model": self.model,
            "needs_review": [asdict(f) for f in self.needs_review],
        }


# ── prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You map spreadsheet columns onto business concepts for a commercial real-estate \
investment firm. Their data covers multifamily, industrial, retail, mixed-use, \
hotel and life-science properties: rent rolls, operating statements, loan tapes, \
budgets and deal models.

You are given a structural profile of a workbook that has ALREADY been analysed. \
Every structural fact in it was computed, not guessed: which rows are headers, \
which values are derived from formulas, what unit each cell's number format \
declares. Do not second-guess those facts. Your job is the part a machine cannot \
do — decide what each field MEANS.

Rules:

1. REUSE CONCEPTS. You are given the concepts already in this firm's dictionary. \
If a field means the same thing as an existing concept, reuse that exact slug \
even when the wording differs ("Rent/Mo", "Monthly Rent" and "Lease Rent" are all \
the same concept). Only coin a new slug when nothing existing fits, and set \
is_new_concept true when you do. The dictionary compounding is the whole point.

2. IDENTIFIERS ARE NOT MEASURES. A column can be full of digits and still be an \
identity: unit numbers, suite numbers, loan numbers, account codes. Never mark \
those as measures — summing them is meaningless. Use role "identifier" and \
data_type "text" even when the profile suggests a numeric cast.

3. EXCLUDE ONLY DERIVED FIELDS. The profile marks fields computed from other \
rows — subtotals, totals, ratios like NOI or debt yield. Those get include=false \
and role="derived", so they are never aggregated alongside their own inputs. \
They are still worth mapping, because the concept is real. Nothing else is \
excluded: identifiers, dimensions and measures all get include=true, even when \
they repeat across regions.

3a. SKIP REGIONS WITH NO FIELDS. A region carrying only a title or a footnote \
has nothing to map. Leave it out of your "regions" array entirely rather than \
emitting an entry with an empty field list.

4. TEXT THAT IS REALLY TYPED. The profile flags columns stored as text that hold \
dates or numbers ('12/30/2023', '$113,990', '100%'). Give those the true \
data_type — unless rule 2 applies.

4a. CARRY THE CURRENCY THROUGH. Where the profile gives a column a \
"currency", COPY IT VERBATIM into that field's "currency". It was read from \
the cell's own number format, which is the only authoritative statement of \
the currency anywhere in the pipeline -- do not translate it, second-guess it \
or replace it with what the header suggests. A "currency" of "" means the \
column mixes currencies; copy the empty string, do not drop it. Where the \
profile states no currency, omit the field: that means US dollars. NEVER \
invent a code from a column name or a country mentioned on the sheet; an \
amount labelled with the wrong currency is worse than one labelled with none.

5. STATE THE GRAIN. For each region say what exactly one row (or one form) \
represents. "One leased unit at one property" is useful; "rent data" is not.

6. ADMIT DOUBT, ANSWERABLY. Set confidence below 0.7 for anything you are \
unsure of. For each of those, add a question that names the exact field it is \
about and offers the two or three concrete answers a person would pick between. \
Never guess silently at units, scale ("$ in 000s"), or an ambiguous label. \
A flagged uncertainty is cheap; a wrong number is not.

Ask about ONE field per question, phrase it in the user's own vocabulary rather \
than ours, and keep each option to a few words. Do not ask a question whose \
answer is already in the structural profile.

Return ONLY a JSON object, no prose:

{
  "regions": [
    {
      "sheets": ["<sheet names this shape covers>"],
      "region_rows": "<the rows string from the profile>",
      "purpose": "<what this region is, in one line>",
      "grain": "<what one row or one form represents>",
      "suggested_table": "<snake_case table name>",
      "fields": [
        {
          "source": "<header or label verbatim>",
          "concept": "<snake_case slug>",
          "display_name": "<human label>",
          "data_type": "text|number|currency|percent|date|bool",
          "currency": "<ISO 4217 code, only when data_type is currency>",
          "role": "identifier|measure|dimension|date|derived",
          "include": true,
          "confidence": 0.0,
          "reason": "<short justification>",
          "is_new_concept": false
        }
      ]
    }
  ],
  "questions": [
    {
      "about": "<the `source` of the field this is about, verbatim>",
      "table": "<the suggested_table it belongs to>",
      "question": "<one sentence, in the user's vocabulary>",
      "options": ["<answer>", "<answer>"]
    }
  ]
}
"""


def _build_user_prompt(
    shape: dict[str, Any], file_meta: dict[str, Any], known_concepts: list[str]
) -> str:
    known = (
        ", ".join(sorted(known_concepts))
        if known_concepts
        else "(none yet — this is the first shape seen)"
    )
    return (
        f"Concepts already in the dictionary:\n{known}\n\n"
        f"Workbook: {file_meta.get('file')} "
        f"({file_meta.get('sheet_count')} sheets, "
        f"{file_meta.get('distinct_shapes')} distinct shapes)\n\n"
        f"This shape covers {len(shape.get('sheets') or [])} sheet(s). "
        "Structural profile:\n"
        f"{json.dumps(shape, indent=2)}\n\n"
        "Map every column and field in every region of THIS shape. Reuse "
        "existing concepts wherever the meaning matches."
    )


# ── entry point ───────────────────────────────────────────────────────────────


def propose_mapping(
    packet: dict[str, Any],
    known_concepts: list[str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> MappingProposal:
    """Attach every detected field to a business concept, one shape at a time.

    Shapes are mapped sequentially rather than in one call for two reasons. The
    response for a whole workbook overruns the token limit and truncates
    mid-object. And threading the concepts found so far into the next shape's
    prompt is the dictionary compounding in miniature: by the third shape of a
    rent roll, most of its columns already have concepts to attach to.

    Raises LLMError only when NO shape could be mapped. A workbook where four
    shapes resolved and one failed is far more useful than an exception.
    """
    known = list(known_concepts or [])
    shapes = [s for s in (packet.get("shapes") or []) if _has_mappable_fields(s)]
    if not shapes:
        return MappingProposal()

    client = _get_client()
    file_meta = {k: packet.get(k) for k in ("file", "sheet_count", "distinct_shapes")}

    proposal = MappingProposal()
    failures: list[str] = []
    total = len(shapes)
    completed = 0

    def report() -> None:
        """Tell the caller how many shapes are done.

        Reported from here rather than estimated by a timer, so a progress bar
        built on it moves when work actually finishes.
        """
        if on_progress is not None:
            on_progress(completed, total)

    # Recorded afterwards on this thread, not inside run(): the pool threads do
    # not carry the request's connection or login, so a row written there would
    # name nobody -- or fail for want of a connection.
    calls: list[tuple[float, Any, BaseException | None]] = []

    def run(index: int, shape: dict[str, Any], vocabulary: list[str]):
        started = time.monotonic()
        response = None
        try:
            response = client.messages.create(
                model=MODEL_MAPPING,
                max_tokens=MAX_TOKENS,
                system=_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_prompt(shape, file_meta, vocabulary),
                    }
                ],
            )
            calls.append((started, response, None))
            return index, _json_object(_text(response)), None
        except json.JSONDecodeError:
            return index, None, f"shape {index + 1}: model returned unparseable output"
        except Exception as exc:  # anthropic raises a family of transport errors
            if response is None:
                calls.append((started, None, exc))
            return index, None, f"shape {index + 1}: {exc}"

    def absorb(
        payload: dict[str, Any] | None, error: str | None, count: bool = True
    ) -> None:
        """Fold one shape's result in. `count` is False when the shape was
        already counted as it landed, so parallel results are ordered on the
        way in without being double-counted."""
        nonlocal completed
        if count:
            completed += 1
        if error:
            failures.append(error)
            if count:
                report()
            return
        partial = _parse(payload or {}, known)
        proposal.regions.extend(partial.regions)
        proposal.questions.extend(partial.questions)
        known.extend(c for c in concepts_from(partial) if c not in known)
        if count:
            report()

    # The first shape runs alone so that the concepts it establishes are in the
    # dictionary before anything else is mapped — that is what makes the later
    # shapes reuse "monthly_rent" instead of coining a synonym for it. Once the
    # vocabulary exists, the remaining shapes are independent of each other and
    # run concurrently, which is the difference between one round trip and five.
    landed: list[tuple[int, dict[str, Any] | None, str | None]] = []

    first, payload, error = run(0, shapes[0], known)
    absorb(payload, error)

    if len(shapes) > 1:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SHAPES) as pool:
            futures = [
                pool.submit(run, index, shape, list(known))
                for index, shape in enumerate(shapes[1:], start=1)
            ]
            # Count each shape the moment it lands, but absorb them in index
            # order afterwards. Sorting the results eagerly would drain the
            # whole generator before the first progress report, freezing the
            # caller's progress bar through the slowest phase — while ordering
            # the regions still matters, because the output should not depend
            # on which shape happened to finish first.
            for future in as_completed(futures):
                landed.append(future.result())
                completed += 1
                report()

    for _, payload, error in sorted(landed, key=lambda r: r[0]):
        absorb(payload, error, count=False)

    for started, response, exc in calls:
        audit.log_ai_call("excel_mapping", MODEL_MAPPING, response, started=started, error=exc)

    if not proposal.regions:
        raise LLMError(
            "No shape in this workbook could be mapped. " + "; ".join(failures)
        )
    if failures:
        proposal.questions.append(
            Question(
                question="Some shapes could not be mapped automatically: "
                + "; ".join(failures)
            )
        )

    proposal.new_concepts = sorted(
        {f.concept for r in proposal.regions for f in r.fields if f.is_new_concept}
    )
    return proposal


def _parse(payload: dict[str, Any], known: list[str]) -> MappingProposal:
    """Turn the model's object into typed results, defensively.

    Every field is coerced rather than trusted: a mapping that arrives with a
    confidence of "high" instead of 0.9, or a missing `include`, should degrade
    to something reviewable rather than raise on the user's upload.
    """
    known_set = {to_slug(c) for c in known}
    proposal = MappingProposal()

    for raw_region in payload.get("regions") or []:
        if not isinstance(raw_region, dict):
            continue
        region = RegionMapping(
            sheets=[str(s) for s in (raw_region.get("sheets") or [])],
            region_rows=str(raw_region.get("region_rows") or ""),
            purpose=str(raw_region.get("purpose") or "").strip(),
            grain=str(raw_region.get("grain") or "").strip(),
            suggested_table=to_slug(str(raw_region.get("suggested_table") or "")),
        )

        for raw_field in raw_region.get("fields") or []:
            if not isinstance(raw_field, dict):
                continue
            concept = to_slug(str(raw_field.get("concept") or ""))
            if not concept:
                continue
            region.fields.append(
                FieldMapping(
                    source=str(raw_field.get("source") or ""),
                    concept=concept,
                    display_name=str(raw_field.get("display_name") or "").strip(),
                    data_type=str(raw_field.get("data_type") or "text"),
                    currency=_currency_code(raw_field.get("currency")),
                    role=str(raw_field.get("role") or "dimension"),
                    include=bool(raw_field.get("include", True)),
                    confidence=_confidence(raw_field.get("confidence")),
                    reason=str(raw_field.get("reason") or "").strip(),
                    # Trust the dictionary over the model's own claim: a concept
                    # is new if and only if we had not seen it before.
                    is_new_concept=concept not in known_set,
                )
            )
        proposal.regions.append(region)

    for raw_question in payload.get("questions") or []:
        # Tolerate a bare string: earlier prompt revisions returned those, and a
        # question the UI cannot anchor is still worth showing.
        if isinstance(raw_question, str):
            if raw_question.strip():
                proposal.questions.append(Question(question=raw_question.strip()))
            continue
        if not isinstance(raw_question, dict):
            continue
        text = str(raw_question.get("question") or "").strip()
        if not text:
            continue
        proposal.questions.append(
            Question(
                question=text,
                about=str(raw_question.get("about") or "").strip(),
                table=to_slug(str(raw_question.get("table") or "")),
                options=[
                    str(o).strip()
                    for o in (raw_question.get("options") or [])
                    if str(o).strip()
                ],
            )
        )
    proposal.new_concepts = sorted(
        {f.concept for r in proposal.regions for f in r.fields if f.is_new_concept}
    )
    return proposal


def _currency_code(raw: object) -> str | None:
    """An ISO 4217 code from the model's reply, or None.

    Deliberately not a truthiness test: "" is a real answer ("this column
    mixes currencies") and has to survive, while None means the model said
    nothing and the dollar default applies. Anything that is not three ASCII
    letters is discarded rather than passed on -- printed beside a number, a
    malformed code reads as a unit that does not exist ("US 1,200"), and a
    reader has no way to tell it is nonsense.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper()
    if code == "":
        return ""
    if len(code) == 3 and code.isascii() and code.isalpha():
        return code
    return None


def _confidence(value: Any) -> float:
    """Coerce a confidence into 0.0-1.0, defaulting to "needs review".

    An unparseable confidence must land BELOW the floor, not above it: the
    failure mode of a too-low score is a question nobody needed to answer, and
    the failure mode of a too-high one is a wrong number nobody checked.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:  # a model answering 85 rather than 0.85
        number = number / 100.0
    return max(0.0, min(1.0, number))


def concepts_from(proposal: MappingProposal) -> list[str]:
    """Every concept this proposal touched, for folding back into the
    dictionary once a human has confirmed it."""
    return sorted({f.concept for r in proposal.regions for f in r.fields})

"""Read a document into text, tables and pages.

The deterministic half of document ingestion, matching the split the
spreadsheet side already uses: this establishes what is provably on the page,
and nothing here decides what any of it means.

PDF handling is adapted from the parser proven against real property records.
Two things it does that a plain `find_tables()` call does not:

  1. Drops decoration BEFORE reading cells. Rotated spans (watermarks, stamps)
     and spans far larger than the page's own modal body size (headings) are
     removed first, because a rotated watermark otherwise bleeds stray
     characters into table values.
  2. Classifies each table as a label/value grid or a record table, so a
     summary block is not read as a table of records.

Both thresholds are computed per page rather than fixed: body text runs about
6.5pt in dense financial reports and 10-11pt in ordinary contracts, so any
hardcoded size is wrong for one of them.

NO OCR. A scanned page has no text layer, and this reports that as a fact about
the page rather than returning an empty string that reads like an empty
document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: A line whose direction vector has any vertical component is rotated.
ROTATION_EPS = 0.01
#: A span this many times the page's modal size is a heading, not body text.
OVERSIZE_RATIO = 1.8
#: Below this many characters, a page has no usable text layer.
NATIVE_TEXT_CHARS = 50

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}


@dataclass
class ParsedTable:
    page: int
    shape: str  # "kv_grid" | "record_table"
    header: list[str] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    #: Label/value pairs, for a kv_grid only.
    pairs: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class ParsedPage:
    page: int
    text: str
    tables: list[ParsedTable] = field(default_factory=list)
    #: False when the page carries no text layer, which means it is scanned.
    has_text_layer: bool = True


@dataclass
class ParsedDocument:
    filename: str
    file_format: str
    pages: list[ParsedPage] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def scanned_pages(self) -> list[int]:
        """Pages with no text layer.

        Surfaced rather than swallowed: a scanned PDF extracts to nothing and
        would otherwise be indexed as an empty document, which looks identical
        to a document that genuinely says nothing.
        """
        return [p.page for p in self.pages if not p.has_text_layer]

    @property
    def is_scanned(self) -> bool:
        return bool(self.pages) and len(self.scanned_pages) == len(self.pages)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages if p.text.strip())


# ── PDF ───────────────────────────────────────────────────────────────────────


def _modal_body_size(page) -> float:
    """The font size covering the most characters on this page.

    Weighted by characters rather than by span, so one paragraph of body text
    outweighs several short titles. That is the assumption: body text dominates
    a page. It holds for any real document and fails only on a page that is
    almost entirely heading, where there is no body text to protect anyway.
    """
    weight: dict[float, int] = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                size = round(span["size"], 1)
                weight[size] = weight.get(size, 0) + len(span["text"])
    return max(weight, key=weight.__getitem__) if weight else 10.0


def _split_spans(page) -> tuple[list[str], list[str]]:
    """Body text and decoration, separated."""
    ceiling = _modal_body_size(page) * OVERSIZE_RATIO
    kept: list[str] = []
    dropped: list[str] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            rotated = abs(line["dir"][1]) > ROTATION_EPS
            for span in line["spans"]:
                target = dropped if rotated or span["size"] > ceiling else kept
                target.append(span["text"])
    return kept, dropped


def _noise_characters(dropped: list[str]) -> set[str]:
    """Characters appearing in decoration, to scrub from table cells."""
    return {ch for text in dropped for ch in text.replace(" ", "")}


def _classify(rows: list[list[str | None]]) -> str:
    """A label/value grid, or a table of records."""
    if not rows:
        return "record_table"
    columns = max(len(row) for row in rows)
    if columns % 2 == 0 and columns <= 6 and len(rows) >= 2:
        # In a label/value grid the even-index cells are short repeated labels.
        label_like = sum(
            1
            for row in rows
            for index in range(0, len(row) - 1, 2)
            if (cell := (row[index] or "").strip())
            and len(cell) < 40
            and not cell.replace(".", "").replace("-", "").isdigit()
        )
        if label_like >= len(rows) * 0.6:
            return "kv_grid"
    return "record_table"


def _unroll(rows: list[list[str | None]]) -> list[tuple[str, str | None]]:
    pairs: list[tuple[str, str | None]] = []
    for row in rows:
        for index in range(0, len(row) - 1, 2):
            label, value = row[index], row[index + 1]
            if label and str(label).strip():
                pairs.append(
                    (str(label).strip(), None if value is None else str(value).strip())
                )
    return pairs


def _tables_on(page, noise: set[str]) -> list[ParsedTable]:
    tables: list[ParsedTable] = []
    for found in page.find_tables().tables:
        rows: list[list[str | None]] = []
        for row in found.extract():
            cells: list[str | None] = []
            for cell in row:
                if cell is None:
                    cells.append(None)
                    continue
                # A single character that only ever appears in decoration is a
                # watermark fragment that landed inside the cell's box.
                lines = [
                    line
                    for line in cell.split("\n")
                    if not (len(line.strip()) == 1 and line.strip() in noise)
                ]
                cells.append(" ".join(part.strip() for part in lines).strip())
            rows.append(cells)

        shape = _classify(rows)
        # find_tables() always calls the first row a header. True for a record
        # table, meaningless for a grid where every row is a real pair.
        if shape == "record_table" and rows:
            header = [str(c or "") for c in rows[0]]
            body = rows[1:]
        else:
            header, body = [], rows
        tables.append(
            ParsedTable(
                page=page.number + 1,
                shape=shape,
                header=header,
                rows=body,
                pairs=_unroll(rows) if shape == "kv_grid" else [],
            )
        )
    return tables


def parse_pdf(path: str | Path) -> ParsedDocument:
    import pymupdf

    path = Path(path)
    document = pymupdf.open(str(path))
    try:
        pages: list[ParsedPage] = []
        for page in document:
            kept, dropped = _split_spans(page)
            text = "\n".join(part for part in kept if part.strip())
            pages.append(
                ParsedPage(
                    page=page.number + 1,
                    text=text,
                    tables=_tables_on(page, _noise_characters(dropped)),
                    has_text_layer=len(page.get_text()) > NATIVE_TEXT_CHARS,
                )
            )
    finally:
        document.close()
    return ParsedDocument(filename=path.name, file_format="pdf", pages=pages)


# ── Word and plain text ───────────────────────────────────────────────────────

#: A Word document has no pages until it is laid out, so its content is split
#: into blocks of roughly this many characters and each is numbered like one.
#: Citations stay useful — "block 3 of the agreement" — without claiming a page
#: number the file does not actually have.
_BLOCK_CHARS = 3000


def _blocks(paragraphs: list[str]) -> list[str]:
    """Group paragraphs into page-sized pieces, never splitting one."""
    out: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if current and size + len(paragraph) > _BLOCK_CHARS:
            out.append("\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 1
    if current:
        out.append("\n".join(current))
    return out


def parse_docx(path: str | Path) -> ParsedDocument:
    import docx

    path = Path(path)
    document = docx.Document(str(path))
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]

    tables: list[ParsedTable] = []
    for table in document.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        shape = _classify(rows)
        if shape == "record_table" and rows:
            header, body = [str(c or "") for c in rows[0]], rows[1:]
        else:
            header, body = [], rows
        tables.append(
            ParsedTable(
                page=1,
                shape=shape,
                header=header,
                rows=body,
                pairs=_unroll(rows) if shape == "kv_grid" else [],
            )
        )

    pages = [
        ParsedPage(page=index + 1, text=block)
        for index, block in enumerate(_blocks(paragraphs))
    ]
    if not pages:
        pages = [ParsedPage(page=1, text="", has_text_layer=bool(tables))]
    # Word tables are not bound to a page, so they hang off the first block.
    pages[0].tables = tables
    return ParsedDocument(filename=path.name, file_format="docx", pages=pages)


def parse_text(path: str | Path) -> ParsedDocument:
    path = Path(path)
    raw = path.read_bytes().decode("utf-8", errors="replace")
    paragraphs = [line for line in re.split(r"\n\s*\n", raw) if line.strip()]
    pages = [
        ParsedPage(page=index + 1, text=block)
        for index, block in enumerate(_blocks(paragraphs))
    ]
    if not pages:
        pages = [ParsedPage(page=1, text="", has_text_layer=False)]
    return ParsedDocument(
        filename=path.name,
        file_format=path.suffix.lower().lstrip("."),
        pages=pages,
    )


def parse_document(path: str | Path) -> ParsedDocument:
    """Read any supported document into pages, text and tables."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix in (".txt", ".md"):
        return parse_text(path)
    raise ValueError(f"Cannot read {path.name}: expected one of {sorted(SUPPORTED_SUFFIXES)}.")

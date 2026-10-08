"""DOCX extractor: .docx file -> ContractDoc (see docs/schemas.md).

Public contract of this module: ``parse_docx(path: str) -> ContractDoc``.

Body order
    The document body is walked in physical order by iterating the children
    of ``w:body`` (``doc.paragraphs`` alone loses table positions).

Classification (T1.1 brief)
    heading     paragraph style name contains "Heading" or "Заголовок"
    list_item   paragraph style is a built-in Word list style
                ("List Paragraph", "List Number", "List Bullet",
                "List Continue") OR the text starts with a bullet/numbering
                prefix (``1.`` / ``1)`` / ``а)`` / ``•``)
    table_cell  one block per physical table cell; ``meta.table``,
                ``meta.row`` (0-based) and ``meta.col`` (0-based grid column)
                are filled; empty cells are kept with ``text: ""`` so tables
                can be rebuilt later
    paragraph   everything else

Tables are flattened: every table (including nested ones) gets a sequential
name ``table_N`` in document reading order and its cells are emitted as
regular blocks. A merged cell (gridSpan) produces a single block with the
starting grid column.

Text cleaning
    Only visible text is kept: the extractor reads ``w:t`` nodes anywhere
    inside the paragraph (hyperlinks and field results included) and drops
    field instructions (``w:instrText``) and tracked deletions (``w:delText``).
    Non-breaking / zero-width spaces are normalized, all whitespace runs are
    collapsed to a single space, and the result is stripped. Empty paragraphs
    (spacing artifacts) are dropped; empty table cells are kept.

Identifiers
    ``block_id`` is sequential (``b_0001``, ``b_0002``, ...); ``meta.order``
    is the 0-based position of the block in the document; ``doc_id`` is
    deterministic: ``d_`` + first 8 hex chars of the file bytes' SHA-256.

Errors (GLOBAl_RULES.md, working rule 7)
    EncryptedDocxError  the file is password-protected (OLE2 container)
    CorruptDocxError    the file is missing or not a valid .docx package
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Iterator, Literal

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from lxml import etree
from pydantic import BaseModel, Field

BlockType = Literal["heading", "paragraph", "list_item", "table_cell"]

# OLE2 compound file magic — encrypted OOXML documents are OLE2 containers,
# a readable .docx is always a zip archive starting with "PK".
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Text starting with these prefixes is treated as a list item.
_LIST_PREFIX_RE = re.compile(r"^(?:\d+[.)]\s|•\s*|[а-яёА-ЯЁ]\)\s*)")

# Built-in Word list styles, as reported by ``paragraph.style.name``.
_LIST_STYLES = frozenset(
    {"ListParagraph", "List Paragraph", "List Number", "List Bullet", "List Continue"}
)

_NBSP_RE = re.compile(r"[\u00a0\u2007\u202f\u2009\u200a]")
_WS_RE = re.compile(r"\s+")


class ExtractionError(Exception):
    """Base class for DOCX extraction failures."""


class EncryptedDocxError(ExtractionError):
    """The .docx file is password-protected (OLE2 compound file)."""


class CorruptDocxError(ExtractionError):
    """The file is missing or is not a readable .docx package."""


# --- schemas (docs/schemas.md) ---------------------------------------------


class BlockMeta(BaseModel):
    style: str | None = None
    table: str | None = None
    row: int | None = None
    col: int | None = None
    order: int = 0


class Block(BaseModel):
    block_id: str
    type: BlockType
    text: str
    meta: BlockMeta


class Stats(BaseModel):
    total_blocks: int = 0
    words: int = 0


class ContractDoc(BaseModel):
    doc_id: str
    filename: str
    domain: str | None = None
    blocks: list[Block] = Field(default_factory=list)
    stats: Stats = Field(default_factory=Stats)


# --- text handling -----------------------------------------------------------


def _clean_text(raw: str) -> str:
    """Normalize spaces and collapse all whitespace runs to single spaces."""
    text = _NBSP_RE.sub(" ", raw)
    text = text.replace("\u200b", "")
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _visible_text(element) -> str:
    """Visible text of a ``w:p`` / ``w:tc`` element.

    Reads ``w:t`` nodes anywhere inside the element (so hyperlink text and
    field results survive) while dropping field instructions
    (``w:instrText``) and tracked deletions (``w:delText``), which have no
    ``w:t`` nodes of their own. ``w:tab`` / ``w:br`` / ``w:cr`` count as
    spaces.
    """
    parts: list[str] = []
    for node in element.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag in (qn("w:tab"), qn("w:br"), qn("w:cr")):
            parts.append(" ")
    return _clean_text("".join(parts))


def _paragraph_text(paragraph: Paragraph) -> str:
    return _visible_text(paragraph._p)


def _cell_text(cell: _Cell) -> str:
    # Only the cell's own paragraphs; nested tables are emitted separately.
    return _clean_text(" ".join(_visible_text(p._p) for p in cell.paragraphs))


# --- classification ----------------------------------------------------------


def _classify_paragraph(paragraph: Paragraph, text: str) -> BlockType:
    style = paragraph.style.name if paragraph.style is not None else ""
    if "Heading" in style or "Заголовок" in style:
        return "heading"
    if style in _LIST_STYLES or _LIST_PREFIX_RE.match(text):
        return "list_item"
    return "paragraph"


# --- body / table walking ----------------------------------------------------


def _iter_body_items(document: Document) -> Iterator[tuple[str, Paragraph | Table]]:
    """Yield ("paragraph", Paragraph) and ("table", Table) in body order."""
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield "paragraph", Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield "table", Table(child, document)


def _grid_span(tc) -> int:
    tc_pr = tc.find(qn("w:tcPr"))
    if tc_pr is not None:
        span = tc_pr.get(qn("w:gridSpan"))
        if span:
            try:
                return int(span)
            except ValueError:
                return 1
    return 1


def _table_cells(table: Table) -> Iterator[tuple[int, int, _Cell]]:
    """Yield (row, grid_col, cell) for every physical cell of the table.

    Merged cells (gridSpan > 1) appear once, at their starting grid column.
    """
    for row_idx, tr in enumerate(table._tbl.iterchildren(qn("w:tr"))):
        col = 0
        for tc in tr.iterchildren(qn("w:tc")):
            yield row_idx, col, _Cell(tc, table)
            col += _grid_span(tc)


# --- public API ---------------------------------------------------------------


def parse_docx(path: str) -> ContractDoc:
    """Parse a .docx file into a ContractDoc (docs/schemas.md).

    Args:
        path: path to the .docx file.

    Returns:
        ContractDoc with sequential block ids and document-order blocks.

    Raises:
        EncryptedDocxError: the file is password-protected.
        CorruptDocxError: the file is missing or is not a valid .docx package.
    """
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except FileNotFoundError as exc:
        raise CorruptDocxError(f"file not found: {path}") from exc
    except OSError as exc:
        raise CorruptDocxError(f"cannot read {path}: {exc}") from exc

    if data.startswith(_OLE2_MAGIC):
        raise EncryptedDocxError(
            f"{file_path.name}: file is password-encrypted (OLE2 compound file); "
            "LexAudit does not support encrypted documents"
        )

    try:
        document = Document(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptDocxError(
            f"{file_path.name}: corrupt file, not a valid zip package ({exc})"
        ) from exc
    except PackageNotFoundError:
        raise CorruptDocxError(
            f"{file_path.name}: corrupt file, not a valid Office Open XML package"
        ) from None
    except (KeyError, etree.XMLSyntaxError, ValueError) as exc:
        raise CorruptDocxError(
            f"{file_path.name}: corrupt file, cannot read document parts ({exc})"
        ) from exc

    doc_id = "d_" + hashlib.sha256(data).hexdigest()[:8]

    raw_blocks: list[tuple[BlockType, str, BlockMeta]] = []
    table_counter = 0

    def process_table(table: Table) -> None:
        nonlocal table_counter
        table_counter += 1
        table_name = f"table_{table_counter}"
        for row_idx, col_idx, cell in _table_cells(table):
            raw_blocks.append(
                (
                    "table_cell",
                    _cell_text(cell),
                    BlockMeta(table=table_name, row=row_idx, col=col_idx),
                )
            )
            for nested in cell.tables:
                process_table(nested)

    for kind, item in _iter_body_items(document):
        if kind == "table":
            process_table(item)
            continue
        text = _paragraph_text(item)
        if not text:
            continue  # drop empty spacing paragraphs; empty table cells are kept
        raw_blocks.append(
            (
                _classify_paragraph(item, text),
                text,
                BlockMeta(style=item.style.name if item.style is not None else None),
            )
        )

    blocks = [
        Block(
            block_id=f"b_{index + 1:04d}",
            type=block_type,
            text=text,
            meta=meta.model_copy(update={"order": index}),
        )
        for index, (block_type, text, meta) in enumerate(raw_blocks)
    ]
    stats = Stats(
        total_blocks=len(blocks),
        words=sum(len(block.text.split()) for block in blocks),
    )
    return ContractDoc(
        doc_id=doc_id,
        filename=file_path.name,
        domain=None,
        blocks=blocks,
        stats=stats,
    )

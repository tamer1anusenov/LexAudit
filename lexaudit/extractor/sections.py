"""Pack original document blocks into bounded legal sections."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

from lexaudit.config.settings import get_settings
from lexaudit.extractor.parse import Block, ContractDoc


class SkippedBlock(BaseModel):
    block_id: str
    reason: Literal["heading", "empty", "short", "requisites"]


class Section(BaseModel):
    section_id: str
    title: str | None
    block_ids: list[str]
    text_llm: str
    skipped: list[SkippedBlock] = Field(default_factory=list)
    status: Literal["pending", "drafted", "needs_review"] = "pending"
    note: str | None = None
    _block_texts: dict[str, str] = PrivateAttr(default_factory=dict)


_REQUISITES = re.compile(r"\b(?:БИН|ИИН|БИК|IBAN)\b|подпис|____________", re.IGNORECASE)
_HEADING_LEVEL = re.compile(r"(?:Heading|Заголовок)\s*(\d+)", re.IGNORECASE)


def _text(block: Block) -> str:
    return getattr(block, "text_llm", block.text)


def _skip_reason(block: Block) -> Literal["heading", "empty", "short", "requisites"] | None:
    if block.type == "heading":
        return "heading"
    text = _text(block).strip()
    if not text:
        return "empty"
    if _REQUISITES.search(text):
        return "requisites"
    if len(text) < 40:
        return "short"
    return None


def build_sections(doc: ContractDoc) -> list[Section]:
    """Keep nested headings inside their parent; split only between blocks.

    An indivisible block longer than the limit is retained intact. Skipped
    blocks belong to the first chunk of a split section, without duplication.
    """
    limit = get_settings().SECTION_MAX_CHARS
    if limit < 1:
        raise ValueError("SECTION_MAX_CHARS must be positive")
    target = max(1, int(limit * 0.8))
    groups: list[tuple[str, list[Block]]] = []
    title = "Преамбула"
    blocks: list[Block] = []
    level: int | None = None
    for block in doc.blocks:
        if block.type == "heading":
            match = _HEADING_LEVEL.search(block.meta.style or "")
            heading_level = int(match.group(1)) if match else 1
            if level is None or heading_level <= level:
                if blocks:
                    groups.append((title, blocks))
                title, blocks, level = block.text, [], heading_level
        blocks.append(block)
    if blocks:
        groups.append((title, blocks))

    result: list[Section] = []
    for title, blocks in groups:
        skipped: list[SkippedBlock] = []
        included: list[Block] = []
        for block in blocks:
            reason = _skip_reason(block)
            if reason:
                skipped.append(SkippedBlock(block_id=block.block_id, reason=reason))
            else:
                included.append(block)
        chunks: list[list[Block]] = []
        if len("\n\n".join(_text(b) for b in included)) <= limit:
            chunks.append(included)
        else:
            start = 0
            while start < len(included):
                end = start
                size = 0
                while end < len(included):
                    addition = len(_text(included[end])) + (2 if end > start else 0)
                    bound = limit if start > 0 and end == start + 1 else target
                    if end > start and size + addition > bound:
                        break
                    size += addition
                    end += 1
                chunks.append(included[start:end])
                if end == len(included):
                    break
                # Overlap only when it leaves room for a new block and advances.
                pair_size = len(_text(included[end - 1])) + 2 + len(_text(included[end]))
                start = end - 1 if end - start > 1 and pair_size <= limit else end
        for index, chunk in enumerate(chunks):
            result.append(Section(
                section_id=f"s_{len(result) + 1:02d}", title=title,
                block_ids=[b.block_id for b in chunk],
                text_llm="\n\n".join(_text(b) for b in chunk),
                skipped=skipped if index == 0 else [],
            ))
            result[-1]._block_texts = {b.block_id: _text(b) for b in chunk}
    return result

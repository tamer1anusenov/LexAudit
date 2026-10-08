"""DOCX extractor: .docx -> ContractDoc (docs/schemas.md)."""
from lexaudit.extractor.parse import (
    Block,
    BlockMeta,
    BlockType,
    ContractDoc,
    CorruptDocxError,
    EncryptedDocxError,
    ExtractionError,
    Stats,
    parse_docx,
)
from lexaudit.extractor.sections import Section, SkippedBlock, build_sections

__all__ = [
    "Block",
    "BlockMeta",
    "BlockType",
    "ContractDoc",
    "CorruptDocxError",
    "EncryptedDocxError",
    "ExtractionError",
    "Stats",
    "parse_docx",
    "Section",
    "SkippedBlock",
    "build_sections",
]

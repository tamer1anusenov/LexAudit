"""Standalone entry point for the DOCX extractor (T1.1).

Usage:
    python -m lexaudit.extractor <file.docx>   # print ContractDoc JSON
    python -m lexaudit.extractor --help
"""
from __future__ import annotations

import argparse
import json
import sys

from lexaudit.extractor.parse import ExtractionError, parse_docx
from lexaudit.extractor.sections import build_sections


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lexaudit.extractor",
        description="Parse a .docx contract into a ContractDoc JSON (docs/schemas.md).",
    )
    parser.add_argument("docx", help="path to the .docx file, or 'sections'")
    parser.add_argument("sections_docx", nargs="?", help="DOCX path for the sections command")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    section_mode = args.docx == "sections"
    if section_mode and not args.sections_docx:
        build_parser().error("sections requires a DOCX path")
    if not section_mode and args.sections_docx:
        build_parser().error("unexpected extra DOCX path")
    try:
        contract = parse_docx(args.sections_docx if section_mode else args.docx)
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if section_mode:
        sections = build_sections(contract)
        for section in sections:
            print(f"{section.section_id} — {section.title}: блоков {len(section.block_ids)}, "
                  f"символов {len(section.text_llm)}, пропущено {len(section.skipped)}")
        print(json.dumps([section.model_dump() for section in sections], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(contract.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

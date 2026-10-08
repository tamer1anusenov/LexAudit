"""Standalone help for the legal agent module."""
from __future__ import annotations

import argparse
import json
import sys

from lexaudit.pipeline.analyze import analyze_contract
from lexaudit.config.settings import get_settings
from lexaudit.extractor.parse import ExtractionError, parse_docx
from lexaudit.logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lexaudit.agent",
        description="Analyze all blocks or selected blocks in a DOCX contract.",
    )
    parser.add_argument("--doc", required=True, help="path to the DOCX contract")
    parser.add_argument(
        "--blocks",
        nargs="+",
        help="block IDs to analyze (space-separated or comma-separated); defaults to all blocks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(
        level=settings.LOG_LEVEL,
        log_dir=settings.LOG_DIR,
        secrets=[settings.ALEM_API_KEY.get_secret_value()],
    )
    try:
        contract = parse_docx(args.doc)
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.blocks:
        requested = {block_id for value in args.blocks for block_id in value.split(",") if block_id}
        known = {block.block_id for block in contract.blocks}
        unknown = sorted(requested - known)
        if unknown:
            print(f"unknown block ID(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        contract = contract.model_copy(
            update={"blocks": [block for block in contract.blocks if block.block_id in requested]}
        )
    result = analyze_contract(contract)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Standalone entry point for the Adilet client (T1.2).

Usage:
    python -m lexaudit.adilet "зарплата" [--limit 10] [--language rus]
    python -m lexaudit.adilet --help
"""
from __future__ import annotations

import argparse
import json
import sys

from lexaudit.adilet.client import AdiletError, get_full_text, search_laws
from lexaudit.config.settings import get_settings
from lexaudit.logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lexaudit.adilet",
        description="Search Adilet or read a mirrored act's full text (T1.3).",
    )
    parser.add_argument("command_or_query", help="'act' or a search query")
    parser.add_argument("value", nargs="?", help="act ID when command is 'act'")
    parser.add_argument("--limit", type=int, default=10, help="max results (default 10)")
    parser.add_argument(
        "--language",
        default="rus",
        choices=("rus", "kaz", "eng"),
        help="portal language (default rus)",
    )
    parser.add_argument("--article", help="print only this article (act command)")
    parser.add_argument("--force", action="store_true", help="refresh cached act text")
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
        if args.command_or_query == "act":
            if not args.value:
                raise ValueError("act command requires an act ID")
            act = get_full_text(args.value, language=args.language, force=args.force)
            if args.article:
                article = next((a for a in act.articles if a.number == args.article.rstrip(".")), None)
                if article is None:
                    print(f"article not found: {args.article}", file=sys.stderr)
                    return 2
                print(json.dumps(article.model_dump(), ensure_ascii=False, indent=2))
                return 0
            print(json.dumps(act.model_dump(), ensure_ascii=False, indent=2))
            return 0
        if args.value:
            raise ValueError("search accepts one query argument")
        hits = search_laws(args.command_or_query, limit=args.limit, language=args.language)
    except AdiletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps([h.model_dump() for h in hits], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

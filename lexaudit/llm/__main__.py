"""Standalone help for the LLM module."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lexaudit.llm",
        description="Alem.ai chat client. Run `python -m lexaudit.llm.ping` to probe provider modes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

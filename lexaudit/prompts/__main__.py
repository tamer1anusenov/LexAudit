"""Standalone prompt-version listing command."""
from __future__ import annotations

import argparse

from lexaudit.prompts.registry import list_versions


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m lexaudit.prompts",
        description="List available versioned prompts.",
    )


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    versions = list_versions()
    if not versions:
        print("No prompt versions found.")
        return 1
    print("Prompt versions: " + ", ".join(versions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

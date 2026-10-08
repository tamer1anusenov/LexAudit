"""LexAudit `report` module.

Purpose: HTML report builder + templates.
This is a scaffold stub (T1.1); functionality lands in a later task.
"""
from __future__ import annotations


def build_parser():
    import argparse

    return argparse.ArgumentParser(
        prog=f"python -m lexaudit.report",
        description=f"LexAudit report module (scaffold stub — not yet implemented).",
    )


def main() -> None:
    build_parser().parse_args()
    raise SystemExit(f"lexaudit.report: not yet implemented (scaffold stub, T1.1)")


if __name__ == "__main__":
    main()

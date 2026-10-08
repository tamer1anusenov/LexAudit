"""Standalone entry point for lexaudit.config: show the resolved settings.

Usage: python -m lexaudit.config --help
"""
from __future__ import annotations

import argparse

from rich.console import Console

from lexaudit.config.settings import get_settings


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m lexaudit.config",
        description="Show the resolved LexAudit settings (secrets are masked).",
    )


def main() -> None:
    build_parser().parse_args()
    settings = get_settings()
    key = settings.ALEM_API_KEY.get_secret_value()
    masked = f"<set, {len(key)} chars>" if key else "<empty>"
    console = Console()
    console.print(
        f"ALEM_API_KEY={masked}\n"
        f"ALEM_BASE_URL={settings.ALEM_BASE_URL}\n"
        f"ALEM_MODEL={settings.ALEM_MODEL}\n"
        f"ADILET_BASE_URL={settings.ADILET_BASE_URL}\n"
        f"DATABASE_PATH={settings.DATABASE_PATH}\n"
        f"LOG_DIR={settings.LOG_DIR}\n"
        f"PROMPTS_VERSION={settings.PROMPTS_VERSION}\n"
        f"LOG_LEVEL={settings.LOG_LEVEL}"
    )


if __name__ == "__main__":
    main()

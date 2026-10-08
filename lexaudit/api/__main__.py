"""Run the LexAudit HTTP service."""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m lexaudit.api")
    parser.parse_args(argv)
    uvicorn.run("lexaudit.api.app:app")


if __name__ == "__main__":
    main()

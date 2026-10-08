"""Central logging setup for LexAudit.

Two handlers on the root logger:

* human-readable console output via rich;
* JSON lines appended to ``logs/app.jsonl`` (one JSON object per line).

GLOBAL RULES: every external HTTP call and every LLM call must be logged as
JSONL with (ts, endpoint/model, latency, status, token usage); secrets are
never logged. Structured fields (endpoint, latency_ms, status, tokens, ...)
are passed via ``logger.info("...", extra={...})`` and are copied into the
JSON line verbatim. A redaction filter scrubs configured secret values from
every record.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

REDACTED = "REDACTED"

# Attribute names set by logging itself; anything else in ``record.__dict__``
# is treated as structured extra data and copied into the JSON line.
_RESERVED_ATTRS = frozenset(vars(logging.makeLogRecord({})))


class JsonlHandler(logging.Handler):
    """Append log records to a file as JSON lines (one object per line)."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.format(record)
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            entry[key] = value
        try:
            self._stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:  # pragma: no cover - logging must never crash the app
            self.handleError(record)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()


class SecretRedactionFilter(logging.Filter):
    """Replace configured secret values with ``REDACTED`` in every record."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            for secret in self._secrets:
                if secret in message:
                    message = message.replace(secret, REDACTED)
                    break
            record.msg = message
            record.args = ()
        return True


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str = "logs",
    *,
    secrets: Iterable[str] = (),
    force: bool = False,
) -> None:
    """Configure the root logger: rich console + JSON lines to logs/app.jsonl.

    Idempotent unless ``force=True`` (re-entry does not duplicate handlers).
    """
    root = logging.getLogger()
    log_dir = Path(log_dir)

    if force:
        root.handlers.clear()
    elif root.handlers:
        # Already configured earlier in this process.
        root.setLevel(level.upper())
        return

    redaction = SecretRedactionFilter(secrets)

    console_handler = RichHandler(console=Console(), show_path=False, rich_tracebacks=True)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.setLevel(level.upper())
    console_handler.addFilter(redaction)

    jsonl_handler = JsonlHandler(log_dir / "app.jsonl")
    jsonl_handler.setLevel(level.upper())
    jsonl_handler.addFilter(redaction)

    root.setLevel(level.upper())
    root.addHandler(console_handler)
    root.addHandler(jsonl_handler)

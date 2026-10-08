"""JSONL trace writer for legal-agent steps."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lexaudit.config.settings import get_settings


class AgentTrace:
    """Append one structured record per model/tool step to the analysis trace."""

    def __init__(self, doc_id: str) -> None:
        settings = get_settings()
        settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = settings.LOG_DIR / f"analysis_{doc_id}.jsonl"
        self._summary_limit = min(max(0, settings.AGENT_TRACE_SUMMARY_LIMIT), 300)
        self.doc_id = doc_id

    def write(
        self,
        *,
        block_id: str,
        step: int,
        tool_name: str | None = None,
        args: dict[str, Any] | None = None,
        result: Any = None,
        tokens: int = 0,
    ) -> None:
        summary = json.dumps(result, ensure_ascii=False, default=str)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "doc_id": self.doc_id,
            "block_id": block_id,
            "step": step,
            "tool_name": tool_name,
            "args": args,
            "result_summary": summary[: self._summary_limit],
            "tokens": tokens,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

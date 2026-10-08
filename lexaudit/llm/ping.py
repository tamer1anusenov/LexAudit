"""Probe Alem.ai plain chat and the native/JSON tool-call protocols."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from lexaudit.config.settings import get_settings
from lexaudit.llm.client import LLMClient, LLMError
from lexaudit.logging_setup import setup_logging
from lexaudit.prompts.registry import load as load_prompt

_ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Return the text argument unchanged.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}
_TEST_TEXT = "проверка инструментов LexAudit"


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m lexaudit.llm.ping",
        description="Probe Alem.ai completion and tool-calling modes, then update docs/alem-api.md.",
    )


def _probe(mode: str, messages: list[dict[str, str]], *, tools: bool) -> tuple[bool | None, str]:
    try:
        with LLMClient(tool_mode=mode) as client:
            result = client.chat(
                messages,
                tools=[_ECHO_TOOL] if tools else None,
                temperature=0.0,
                max_tokens=128,
            )
        if not tools:
            if not result.content.strip():
                return False, "empty completion"
            return True, "completion returned"
        success = (
            len(result.tool_calls) == 1
            and result.tool_calls[0].name == "echo"
            and result.tool_calls[0].arguments.get("text") == _TEST_TEXT
        )
        return success, "echo tool call returned" if success else "expected echo tool call was not returned"
    except LLMError as exc:
        if getattr(exc, "status_code", None) in {401, 403}:
            return None, f"HTTP {exc.status_code}: authentication/permission denied; protocol unverified"
        return False, f"{type(exc).__name__}: {exc}"


def _probe_prompt(section: str) -> str:
    prompt = load_prompt(None, "probe")
    marker = f"## {section}\n"
    if marker not in prompt:
        raise ValueError(f"missing {section!r} section in versioned probe prompt")
    return prompt.split(marker, 1)[1].split("\n## ", 1)[0].strip()


def _write_conclusion(results: dict[str, tuple[bool, str]]) -> Path:
    settings = get_settings()
    destination = Path(__file__).resolve().parents[2] / "docs" / "alem-api.md"
    native = results["native tools"][0]
    json_mode = results["JSON protocol"][0]
    lines = [
        "# Alem.ai API — T1.4 verification",
        "",
        "## Provider documentation",
        "",
        "Alem Plus documents its OpenAI-compatible API at `https://llm.alem.ai/v1`;",
        "its chat completion route is `/v1/chat/completions`. The site also",
        "describes its LLM catalog as OpenAI-compatible. The configured default",
        "model identifier in this project is `qwen3-8`; the exact model name and",
        "native function-calling behavior are account/deployment dependent and",
        "must be established by the authenticated probe below.",
        "",
        "Sources: [Alem Plus Gemma 4 API example](https://doc.alem.ai/services/gemma4.html),",
        "[Alem Plus GPT OSS API example](https://doc.alem.ai/services/gptoos.html),",
        "[Alem Plus service catalog](https://plus.alem.ai/services).",
        "",
        "## Authenticated probe",
        "",
        f"- Timestamp (UTC): `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`",
        f"- Base URL: `{settings.ALEM_BASE_URL}`",
        f"- Model: `{settings.ALEM_MODEL}`",
    ]
    for label, (success, detail) in results.items():
        outcome = "works" if success is True else "failed" if success is False else "inconclusive"
        lines.append(f"- {label}: **{outcome}** — {detail}")
    lines.extend(
        [
            "",
            f"Conclusion for this configured model: native tools {'worked' if native is True else 'were not confirmed' if native is None else 'did not work'}; "
            f"the JSON protocol {'worked' if json_mode is True else 'was not confirmed' if json_mode is None else 'did not work'}. "
            "Authentication/permission failures do not establish whether a protocol is supported. "
            "Set `LLM_TOOL_MODE=auto` "
            "to prefer native function calling and fall back to the JSON protocol when the server rejects it, "
            "or select `native` / `json` explicitly.",
            "",
        ]
    )
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(
        level=settings.LOG_LEVEL,
        log_dir=settings.LOG_DIR,
        secrets=[settings.ALEM_API_KEY.get_secret_value()],
    )
    plain = _probe("native", [{"role": "user", "content": _probe_prompt("plain")}], tools=False)
    tool_prompt = _probe_prompt("echo").replace("{{TEST_TEXT}}", _TEST_TEXT)
    prompt = [{"role": "user", "content": tool_prompt}]
    native = _probe("native", prompt, tools=True)
    json_mode = _probe("json", prompt, tools=True)
    results = {"plain completion": plain, "native tools": native, "JSON protocol": json_mode}
    for label, (success, detail) in results.items():
        status = "OK" if success is True else "FAILED" if success is False else "INCONCLUSIVE"
        print(f"{label}: {status} ({detail})")
    path = _write_conclusion(results)
    print(f"Wrote {path}")
    return 0 if all(success is True for success, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

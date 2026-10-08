"""OpenAI function schemas and dispatch for the Adilet tools."""
from __future__ import annotations

from typing import Any

from lexaudit.adilet.client import AdiletError, get_full_text, search_laws

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_laws",
            "description": "Ищет нормативные правовые акты в Adilet по теме или формулировке.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Тема или текст нормы для поиска."},
                    "limit": {"type": "integer", "minimum": 1},
                    "language": {"type": "string", "enum": ["rus", "kaz", "eng"]},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_text",
            "description": "Получает полный текст акта из локального зеркала Adilet или обновляет его.",
            "parameters": {
                "type": "object",
                "properties": {
                    "act_id": {"type": "string", "description": "Идентификатор акта из результата поиска Adilet."},
                    "language": {"type": "string", "enum": ["rus", "kaz", "eng"]},
                    "force": {"type": "boolean"},
                },
                "required": ["act_id"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolDispatchError(ValueError):
    """Raised when an LLM tool call has an unknown name or invalid arguments."""


def dispatch(name: str, args: dict[str, Any]) -> Any:
    """Run one Adilet tool and return its documented schema as plain data."""
    if not isinstance(args, dict):
        raise ToolDispatchError("tool arguments must be a JSON object")
    if name == "search_laws":
        if set(args) - {"query", "limit", "language"} or not isinstance(args.get("query"), str):
            raise ToolDispatchError("search_laws requires query and accepts only limit/language options")
        if not args["query"].strip():
            raise ToolDispatchError("search_laws query must not be empty")
        if "limit" in args and (not isinstance(args["limit"], int) or isinstance(args["limit"], bool)):
            raise ToolDispatchError("search_laws limit must be an integer")
        if "language" in args and args["language"] not in {"rus", "kaz", "eng"}:
            raise ToolDispatchError("search_laws language must be rus, kaz, or eng")
        return [hit.model_dump() for hit in search_laws(**args)]
    if name == "get_full_text":
        if set(args) - {"act_id", "language", "force"} or not isinstance(args.get("act_id"), str):
            raise ToolDispatchError("get_full_text requires act_id and accepts only language/force options")
        if not args["act_id"].strip():
            raise ToolDispatchError("get_full_text act_id must not be empty")
        if "language" in args and args["language"] not in {"rus", "kaz", "eng"}:
            raise ToolDispatchError("get_full_text language must be rus, kaz, or eng")
        if "force" in args and not isinstance(args["force"], bool):
            raise ToolDispatchError("get_full_text force must be a boolean")
        return get_full_text(**args).model_dump()
    raise ToolDispatchError(f"unknown Adilet tool: {name}")


__all__ = ["TOOLS", "ToolDispatchError", "dispatch", "AdiletError"]

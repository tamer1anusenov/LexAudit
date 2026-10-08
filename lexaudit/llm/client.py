"""Alem.ai chat-completion client and protocol adapter."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, ConfigDict

from lexaudit.config.settings import get_settings
from lexaudit.prompts.registry import load as load_prompt

logger = logging.getLogger("lexaudit.llm")


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    arguments: dict[str, Any]


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    content: str
    tool_calls: list[ToolCall]
    usage: TokenUsage
    latency_ms: float


class LLMError(RuntimeError):
    """Base class for LLM client failures."""


class LLMUnavailable(LLMError):
    """Raised when the provider remains unavailable after retries."""


class LLMClientError(LLMError):
    """Raised for rejected requests or invalid provider responses."""


class LLMResponseError(LLMClientError):
    """Raised when a response does not match the requested protocol."""


class LLMClient:
    """Thin OpenAI SDK wrapper; all Alem.ai chat calls pass through this class."""

    def __init__(self, *, tool_mode: str | None = None, client: OpenAI | None = None) -> None:
        settings = get_settings()
        mode = (tool_mode or settings.LLM_TOOL_MODE).lower()
        if mode not in {"auto", "native", "json"}:
            raise ValueError("LLM_TOOL_MODE must be auto, native, or json")
        if settings.LLM_RETRIES < 1:
            raise ValueError("LLM_RETRIES must be at least 1")
        api_key = settings.ALEM_API_KEY.get_secret_value()
        if client is None and not api_key:
            raise LLMClientError("ALEM_API_KEY is not configured")
        self.model = settings.ALEM_MODEL
        self.endpoint = f"{settings.ALEM_BASE_URL.rstrip('/')}/chat/completions"
        self.tool_mode = mode
        self.retries = settings.LLM_RETRIES
        self.backoff = settings.LLM_RETRY_BACKOFF_SECONDS
        self.log_content_limit = min(max(0, settings.LLM_LOG_CONTENT_LIMIT), 2000)
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=settings.ALEM_BASE_URL,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Create a chat completion in native or JSON-protocol tool mode."""
        started = time.perf_counter()
        if tools and self.tool_mode == "json":
            response = self._create(self._json_messages(messages, tools), None, temperature, max_tokens)
            return self._decode_json_tools(response, tools, (time.perf_counter() - started) * 1000)
        if tools and self.tool_mode == "auto":
            try:
                response = self._create(messages, tools, temperature, max_tokens)
                return self._decode_native(response, (time.perf_counter() - started) * 1000)
            except LLMClientError as exc:
                if not getattr(exc, "tool_unsupported", False):
                    raise
                logger.info("native tool calling rejected; trying JSON protocol", extra={"model": self.model})
                response = self._create(self._json_messages(messages, tools), None, temperature, max_tokens)
                return self._decode_json_tools(response, tools, (time.perf_counter() - started) * 1000)
        response = self._create(
            messages, tools if tools and self.tool_mode == "native" else None, temperature, max_tokens
        )
        return self._decode_native(response, (time.perf_counter() - started) * 1000)

    def _create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int | None,
    ) -> Any:
        request: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            request["tools"] = tools
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        log_content = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, default=str)
        log_content = log_content[: self.log_content_limit]

        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                response = self._client.chat.completions.create(**request)
                latency = round((time.perf_counter() - started) * 1000, 1)
                usage = getattr(response, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                content = self._response_text(response)
                logger.info(
                    "Alem.ai chat completion succeeded",
                    extra={
                        "endpoint": self.endpoint, "model": self.model,
                        "latency_ms": latency, "status": 200,
                        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "tokens": prompt_tokens + completion_tokens,
                        "content": (log_content + content)[: self.log_content_limit], "attempt": attempt + 1,
                    },
                )
                return response
            except (APITimeoutError, APIConnectionError) as exc:
                self._log_failure(exc, started, 0, attempt, log_content)
                if attempt + 1 == self.retries:
                    raise LLMUnavailable(f"Alem.ai unavailable after {self.retries} attempts") from exc
            except APIStatusError as exc:
                self._log_failure(exc, started, exc.status_code, attempt, log_content)
                if tools and self.tool_mode == "auto" and exc.status_code in {400, 404, 422}:
                    error = LLMClientError(f"Alem.ai rejected native tools with HTTP {exc.status_code}")
                    error.tool_unsupported = True
                    error.status_code = exc.status_code
                    raise error from exc
                if exc.status_code == 429 or exc.status_code >= 500:
                    if attempt + 1 == self.retries:
                        raise LLMUnavailable(
                            f"Alem.ai unavailable: HTTP {exc.status_code} after {self.retries} attempts"
                        ) from exc
                else:
                    error = LLMClientError(f"Alem.ai rejected the request with HTTP {exc.status_code}")
                    error.status_code = exc.status_code
                    raise error from exc
            if attempt + 1 < self.retries:
                time.sleep(self.backoff * (2**attempt))
        raise LLMUnavailable(f"Alem.ai unavailable after {self.retries} attempts")

    def _log_failure(self, exc: Exception, started: float, status: int, attempt: int, content: str) -> None:
        logger.warning(
            "Alem.ai chat completion failed: %s", exc.__class__.__name__,
            extra={
                "endpoint": self.endpoint, "model": self.model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "status": status, "prompt_tokens": 0, "completion_tokens": 0, "tokens": None,
                "content": content[: self.log_content_limit], "attempt": attempt + 1,
            },
        )

    @staticmethod
    def _response_text(response: Any) -> str:
        try:
            return str(response.choices[0].message.content or "")
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMResponseError("Alem.ai returned no chat choice") from exc

    @staticmethod
    def _decode_native(response: Any, latency_ms: float) -> ChatResult:
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMResponseError("Alem.ai returned no chat choice") from exc
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be an object")
                calls.append(ToolCall(name=call.function.name, arguments=arguments))
            except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise LLMResponseError("Alem.ai returned invalid native tool arguments") from exc
        usage = getattr(response, "usage", None)
        return ChatResult(
            content=message.content or "", tool_calls=calls,
            usage=TokenUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            latency_ms=round(latency_ms, 1),
        )

    @staticmethod
    def _json_messages(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        available = []
        for item in tools:
            function = item.get("function", item)
            available.append({
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object"}),
            })
        protocol = load_prompt(None, "tool_protocol").replace(
            "{{TOOLS_JSON}}", json.dumps(available, ensure_ascii=False)
        )
        result = [dict(message) for message in messages]
        for message in reversed(result):
            if message.get("role") == "system" and isinstance(message.get("content"), str):
                message["content"] += protocol
                break
        else:
            result.insert(0, {"role": "system", "content": protocol.strip()})
        return result

    @staticmethod
    def _decode_json_tools(response: Any, tools: list[dict[str, Any]], latency_ms: float) -> ChatResult:
        raw = LLMClient._response_text(response).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMResponseError("Alem.ai JSON tool protocol response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMResponseError("Alem.ai JSON tool protocol response must be an object")
        usage = getattr(response, "usage", None)
        token_usage = TokenUsage(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
        if payload.get("action") == "final" and isinstance(payload.get("answer"), str):
            return ChatResult(
                content=payload["answer"], tool_calls=[], usage=token_usage, latency_ms=round(latency_ms, 1)
            )
        if payload.get("action") != "tool" or not isinstance(payload.get("tool"), str):
            raise LLMResponseError("Alem.ai JSON tool protocol action must be tool or final")
        arguments = payload.get("args")
        if not isinstance(arguments, dict):
            raise LLMResponseError("Alem.ai JSON tool protocol args must be an object")
        names = {item.get("function", item).get("name") for item in tools if isinstance(item, dict)}
        if payload["tool"] not in names:
            raise LLMResponseError(f"Alem.ai returned unknown tool name: {payload['tool']}")
        return ChatResult(
            content="", tool_calls=[ToolCall(name=payload["tool"], arguments=arguments)],
            usage=token_usage, latency_ms=round(latency_ms, 1),
        )

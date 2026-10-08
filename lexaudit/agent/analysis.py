"""Per-block legal analysis loop."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from lexaudit.adilet.client import AdiletError, AdiletUnavailable
from lexaudit.adilet.tools import TOOLS, ToolDispatchError, dispatch
from lexaudit.config.settings import get_settings
from lexaudit.extractor.parse import ContractDoc
from lexaudit.llm.client import ChatResult, LLMClient, LLMError
from lexaudit.prompts.registry import load as load_prompt
from lexaudit.agent.trace import AgentTrace
from lexaudit.agent.validate import (
    AnalysisResult,
    AnalysisStats,
    BlockAnalysis,
    BlockVerdict,
    Finding,
    OutputValidationError,
    validate_block_output,
)

logger = logging.getLogger("lexaudit.agent")
_WORD_RE = re.compile(r"[а-яёa-z0-9]{4,}", re.IGNORECASE)


def analyze_contract(
    contract_doc: ContractDoc,
    search_hints: list[str] | None = None,
    *,
    llm_client: LLMClient | None = None,
) -> AnalysisResult:
    """Analyze every document block and return the documented AnalysisResult."""
    settings = get_settings()
    if settings.AGENT_MODE != "per_block":
        raise ValueError("AGENT_MODE currently supports only 'per_block'")
    if settings.AGENT_MAX_STEPS < 1:
        raise ValueError("AGENT_MAX_STEPS must be at least 1")
    summary_limit = min(max(0, settings.AGENT_CONTEXT_SUMMARY_LIMIT), 300)
    started = time.perf_counter()
    trace = AgentTrace(contract_doc.doc_id)
    findings: list[Finding] = []
    verdicts: list[BlockVerdict] = []
    llm_calls = tool_calls = tokens = 0
    owned_client = llm_client is None
    active_client = llm_client

    try:
        system_prompt = load_prompt(None, "system") + "\n\n" + load_prompt(None, "output_contract")
        for block in contract_doc.blocks:
            block_search_hits: dict[str, dict[str, Any]] = {}
            block_full_acts: dict[str, dict[str, Any]] = {}
            block_search_queries: list[str] = []
            if active_client is None:
                try:
                    active_client = LLMClient()
                except LLMError as exc:
                    verdicts.append(BlockVerdict(block_id=block.block_id, verdict="needs_review", note="llm_error"))
                    trace.write(
                        block_id=block.block_id, step=0,
                        result={"status": "llm_error", "error": str(exc)},
                    )
                    continue

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _block_context(contract_doc, block, search_hints, findings)},
            ]
            block_output: BlockAnalysis | None = None
            block_verdict_count = len(verdicts)
            for step in range(1, settings.AGENT_MAX_STEPS + 1):
                llm_calls += 1
                try:
                    response = active_client.chat(messages, tools=TOOLS)
                except LLMError as exc:
                    verdicts.append(BlockVerdict(block_id=block.block_id, verdict="needs_review", note="llm_error"))
                    trace.write(
                        block_id=block.block_id, step=step,
                        result={"status": "llm_error", "error": str(exc)},
                    )
                    break
                tokens += _tokens(response)

                if response.tool_calls:
                    tool_calls += len(response.tool_calls)
                    try:
                        tool_results = _run_tools(
                            response, block_search_hits, block_full_acts,
                            block_id=block.block_id, step=step, trace=trace,
                            token_count=_tokens(response),
                            context_text=block.text,
                            search_queries=block_search_queries,
                        )
                    except AdiletUnavailable:
                        verdicts.append(
                            BlockVerdict(block_id=block.block_id, verdict="needs_review", note="adilet_unavailable")
                        )
                        break
                    messages.append(
                        {
                            "role": "assistant",
                            "content": json.dumps(
                                {"tool_calls": [call.model_dump() for call in response.tool_calls]},
                                ensure_ascii=False,
                            ),
                        }
                    )
                    messages.append({"role": "user", "content": json.dumps({"tool_results": tool_results}, ensure_ascii=False)})
                    continue

                trace.write(
                    block_id=block.block_id, step=step, result=response.content,
                    tokens=_tokens(response),
                )
                try:
                    block_output = validate_block_output(
                        response.content, block, block_search_hits, block_full_acts
                    )
                except OutputValidationError as exc:
                    repair_prompt = load_prompt(None, "repair").replace("{{VALIDATION_ERROR}}", str(exc))
                    repair_messages = messages + [
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": repair_prompt},
                    ]
                    llm_calls += 1
                    try:
                        repaired = active_client.chat(repair_messages)
                    except LLMError as llm_exc:
                        verdicts.append(
                            BlockVerdict(block_id=block.block_id, verdict="needs_review", note="llm_error")
                        )
                        trace.write(
                            block_id=block.block_id, step=step + 1,
                            result={"status": "llm_error", "error": str(llm_exc)},
                        )
                        break
                    tokens += _tokens(repaired)
                    trace.write(
                        block_id=block.block_id, step=step + 1,
                        result=repaired.content, tokens=_tokens(repaired),
                    )
                    try:
                        block_output = validate_block_output(
                            repaired.content, block, block_search_hits, block_full_acts
                        )
                    except OutputValidationError:
                        verdicts.append(
                            BlockVerdict(block_id=block.block_id, verdict="needs_review", note="validation_failed")
                        )
                        break
                verdicts.append(BlockVerdict(block_id=block.block_id, verdict=block_output.verdict))
                findings.extend(block_output.findings)
                break
            if block_output is None and len(verdicts) == block_verdict_count:
                verdicts.append(
                    BlockVerdict(block_id=block.block_id, verdict="needs_review", note="max_steps_exceeded")
                )
    finally:
        if owned_client and active_client is not None:
            active_client.close()

    return AnalysisResult(
        doc_id=contract_doc.doc_id,
        findings=findings,
        verdicts=verdicts,
        stats=AnalysisStats(
            blocks=len(contract_doc.blocks),
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            tokens=tokens,
            duration_s=round(time.perf_counter() - started, 3),
        ),
    )


def _block_context(
    contract_doc: ContractDoc,
    block: Any,
    search_hints: list[str] | None,
    findings: list[Finding],
) -> str:
    settings = get_settings()
    limit = min(max(0, settings.AGENT_CONTEXT_SUMMARY_LIMIT), 300)
    summary = " | ".join(finding.risk_explanation.replace("\n", " ") for finding in findings)
    payload = {
        "domain": contract_doc.domain,
        "search_hints": search_hints or [],
        "findings_so_far_summary": summary[:limit],
        "block": {"block_id": block.block_id, "type": block.type, "text": block.text},
    }
    return json.dumps(payload, ensure_ascii=False)


def _tokens(response: ChatResult) -> int:
    return response.usage.prompt_tokens + response.usage.completion_tokens


def _run_tools(
    response: ChatResult,
    search_hits: dict[str, dict[str, Any]],
    full_acts: dict[str, dict[str, Any]],
    *,
    block_id: str,
    step: int,
    trace: AgentTrace,
    token_count: int,
    context_text: str,
    search_queries: list[str],
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for index, call in enumerate(response.tool_calls):
        try:
            if call.name == "get_full_text" and call.arguments.get("act_id") not in search_hits:
                raise ToolDispatchError("get_full_text act_id must come from a preceding search_laws result")
            result = dispatch(call.name, call.arguments)
            if call.name == "search_laws":
                search_hits.update({hit["act_id"]: hit for hit in result})
                search_queries.append(call.arguments["query"])
            elif call.name == "get_full_text":
                full_acts[result["act_id"]] = result
                result = _compact_act_context(result, context_text, search_queries)
            outcomes.append({"name": call.name, "result": result})
        except AdiletUnavailable as exc:
            trace.write(
                block_id=block_id, step=step, tool_name=call.name, args=call.arguments,
                result={"status": "adilet_unavailable", "error": str(exc)},
                tokens=token_count if index == 0 else 0,
            )
            raise
        except AdiletError as exc:
            outcomes.append({"name": call.name, "error": type(exc).__name__, "message": str(exc)})
        except ToolDispatchError as exc:
            outcomes.append({"name": call.name, "error": type(exc).__name__, "message": str(exc)})
        trace.write(
            block_id=block_id,
            step=step,
            tool_name=call.name,
            args=call.arguments,
            result=outcomes[-1],
            tokens=token_count if index == 0 else 0,
        )
    return outcomes


def _compact_act_context(
    act: dict[str, Any], block_text: str, search_queries: list[str]
) -> dict[str, Any]:
    """Send relevant article excerpts to the model while retaining the full act locally."""
    settings = get_settings()
    max_articles = max(1, settings.AGENT_LAW_CONTEXT_ARTICLES)
    max_chars = max(2000, settings.AGENT_LAW_CONTEXT_MAX_CHARS)
    block_terms = _terms(block_text)
    query_terms = _terms(" ".join(search_queries))
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, article in enumerate(act.get("articles", [])):
        article_text = " ".join(
            [str(article.get("heading") or "")]
            + [str(paragraph) for paragraph in article.get("paragraphs", [])]
        )
        article_terms = _terms(article_text)
        score = 4 * len(article_terms & query_terms) + len(article_terms & block_terms)
        scored.append((score, index, article))
    if not scored:
        chosen: list[dict[str, Any]] = []
    else:
        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_articles]
        if selected[0][0] == 0:
            selected = sorted(scored, key=lambda item: item[1])[:max_articles]
        chosen = [item[2] for item in sorted(selected, key=lambda item: item[1])]

    context_articles: list[dict[str, Any]] = []
    budget = max(1000, max_chars - 1200)
    used = 0
    for article in chosen:
        remaining = budget - used
        if remaining <= 0:
            break
        item = {
            "number": str(article.get("number", "")),
            "heading": article.get("heading"),
            "paragraphs": [],
            "anchor": article.get("anchor"),
        }
        for paragraph in article.get("paragraphs", []):
            if remaining <= 0:
                break
            text = str(paragraph)
            allowance = max(0, remaining - 80)
            if len(text) > allowance:
                if allowance > 120:
                    text = text[:allowance].rsplit(" ", 1)[0] + " …"
                else:
                    break
            item["paragraphs"].append(text)
            used += len(text)
            remaining = budget - used
        if item["paragraphs"]:
            context_articles.append(item)

    return {
        "act_id": act.get("act_id"),
        "title": act.get("title"),
        "url": act.get("url"),
        "in_force": act.get("in_force"),
        "fetched_at": act.get("fetched_at"),
        "articles": context_articles,
        "selection_note": "Показаны статьи, наиболее релевантные проверяемому условию; полный текст акта проверяется локально.",
    }


def _terms(text: str) -> set[str]:
    """Return lightweight normalized word prefixes for Russian lexical ranking."""
    return {match.group(0).casefold()[:6] for match in _WORD_RE.finditer(text)}

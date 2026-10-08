"""Dispatch legal analysis to the legacy loop or section-based phases."""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable

from lexaudit.agent.analysis import analyze_contract as analyze_blocks
from lexaudit.agent.draft import draft_section
from lexaudit.agent.evidence import gather_evidence
from lexaudit.agent.trace import AgentTrace
from lexaudit.agent.validate import AnalysisResult, AnalysisStats
from lexaudit.agent.verdict import verdict_findings
from lexaudit.config.settings import get_settings
from lexaudit.extractor import ContractDoc, build_sections
from lexaudit.llm import LLMClient


def analyze_contract(doc: ContractDoc, search_hints: list[str] | None = None, *,
                     llm_client: LLMClient | None = None,
                     on_progress: Callable[[int, int], None] | None = None) -> AnalysisResult:
    """Keep per_block as default until the human approves evaluation results."""
    settings = get_settings()
    started = time.perf_counter()
    total = len(doc.blocks)
    if on_progress:
        on_progress(0, total)
    if settings.AGENT_MODE == "per_block":
        results = []
        for index, block in enumerate(doc.blocks, 1):
            results.append(analyze_blocks(doc.model_copy(update={"blocks": [block]}),
                                          search_hints, llm_client=llm_client))
            if on_progress:
                on_progress(index, total)
        return AnalysisResult(doc_id=doc.doc_id,
            findings=[f for r in results for f in r.findings],
            verdicts=[v for r in results for v in r.verdicts],
            stats=AnalysisStats(blocks=total,
                llm_calls=sum(r.stats.llm_calls for r in results),
                tool_calls=sum(r.stats.tool_calls for r in results),
                tokens=sum(r.stats.tokens for r in results),
                duration_s=round(time.perf_counter() - started, 3)))
    if settings.AGENT_MODE != "sections":
        raise ValueError("AGENT_MODE must be per_block or sections")

    trace = AgentTrace(doc.doc_id)
    lock = Lock()
    llm_calls = tool_calls = tokens = step = 0

    class TrackedClient(LLMClient):
        def __init__(self, phase: str, block_id: str) -> None:
            super().__init__()
            self.phase, self.block_id = phase, block_id

        def chat(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal llm_calls, tokens, step
            response = None
            try:
                return_value = super().chat(*args, **kwargs)
                response = return_value
                return return_value
            finally:
                used = response.usage.prompt_tokens + response.usage.completion_tokens if response else 0
                with lock:
                    llm_calls += 1
                    tokens += used
                    step += 1
                    trace.write(block_id=self.block_id, step=step, tool_name=self.phase,
                        result=response.content if response else {"status": "llm_error"}, tokens=used)

    def record_tool(kind: str, key: str) -> None:
        nonlocal tool_calls, step
        with lock:
            tool_calls += 1
            step += 1
            trace.write(block_id="", step=step,
                tool_name="search_laws" if kind == "search" else "get_full_text",
                args={"query" if kind == "search" else "act_id": key},
                result={"phase": "evidence", "status": "started"})

    sections = build_sections(doc)
    drafts = []
    for section in sections:
        # Empty sections are filtered deterministically and need no model call.
        if not section.block_ids:
            section.status = "drafted"
            continue
        drafts.extend(draft_section(section, doc.domain or "general", search_hints or [],
            client_factory=lambda: TrackedClient("draft", section.block_ids[0])))
    candidates = gather_evidence(drafts,
        client_factory=lambda: TrackedClient("evidence_queries", ""), on_tool=record_tool)
    result = verdict_findings(doc, sections, drafts, candidates,
        client_factory=lambda: TrackedClient("verdict", ""))
    # All blocks complete only after their hypotheses have received verdicts.
    if on_progress:
        on_progress(total, total)
    return result.model_copy(update={"stats": AnalysisStats(blocks=total,
        llm_calls=llm_calls, tool_calls=tool_calls, tokens=tokens,
        duration_s=round(time.perf_counter() - started, 3))})

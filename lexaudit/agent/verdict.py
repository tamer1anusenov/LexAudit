"""Verify each draft against supplied real articles and aggregate block verdicts."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from pydantic import ValidationError

from lexaudit.agent.draft import DraftFinding
from lexaudit.agent.evidence import CandidateArticle
from lexaudit.agent.validate import AnalysisResult, AnalysisStats, BlockVerdict, Finding, OutputValidationError
from lexaudit.config.settings import get_settings
from lexaudit.extractor import ContractDoc, Section
from lexaudit.llm import LLMClient
from lexaudit.prompts.registry import load

logger = logging.getLogger("lexaudit.verdict")
_PLACEHOLDERS = re.compile(r"\[(?:PERSON|ORG|IIN|BIN|PHONE|EMAIL|ADDRESS|IBAN)_\d+\]")


def validate_verdict(content: str, draft: DraftFinding, texts: dict[str, str],
                     candidates: list[CandidateArticle]) -> Finding | None:
    """Check exact quote, candidate identity and placeholder preservation."""
    try:
        payload = json.loads(content)
        if payload is None:
            return None
        finding = Finding.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise OutputValidationError(f"invalid Finding JSON: {exc}") from exc
    if finding.block_id not in draft.block_ids or finding.block_id not in texts:
        raise OutputValidationError("block_id is not a referenced input block")
    if not finding.quote.strip() or finding.quote not in texts[finding.block_id]:
        raise OutputValidationError("quote is not an exact non-empty substring of text_llm")
    if not finding.proposed_text.strip() or not finding.risk_explanation.strip():
        raise OutputValidationError("proposed_text and risk_explanation must be non-empty")
    ref = finding.law_ref
    if not any((ref.act_id, ref.article, ref.act_title, ref.url) ==
               (c.act_id, c.article_number, c.act_title, c.url) for c in candidates):
        raise OutputValidationError("law_ref does not exactly match a supplied fetched candidate")
    if Counter(_PLACEHOLDERS.findall(finding.quote)) != Counter(_PLACEHOLDERS.findall(finding.proposed_text)):
        raise OutputValidationError("proposed_text changes placeholders")
    finding.finding_id = f"f_{draft.draft_id}"
    return finding


def verdict_findings(doc: ContractDoc, sections: list[Section],
                     draft_findings: list[DraftFinding],
                     evidence: dict[str, list[CandidateArticle]], *,
                     client_factory: Callable[[], LLMClient] = LLMClient) -> AnalysisResult:
    """One parallel completion per grounded draft, with one repair at most.

    Evidence must come from gather_evidence, which fetches live Adilet acts.
    Stats cover this phase only; no evidence calls are repeated here.
    """
    started = time.perf_counter()
    concurrency = get_settings().VERDICT_CONCURRENCY
    if concurrency < 1:
        raise ValueError("VERDICT_CONCURRENCY must be positive")
    if len({d.draft_id for d in draft_findings}) != len(draft_findings):
        raise ValueError("draft_id must be unique")
    texts = {b.block_id: getattr(b, "text_llm", b.text) for b in doc.blocks}
    for section in sections:
        texts.update({bid: text for bid, text in section._block_texts.items() if bid in texts})

    def worker(draft: DraftFinding) -> tuple[Finding | None, str | None, int, int]:
        calls = tokens = 0
        candidates = evidence.get(draft.draft_id, [])
        if draft.grounding == "ungrounded" or not candidates:
            logger.info("verdict_no_evidence", extra={"draft_id": draft.draft_id})
            return None, "no_evidence_found", calls, tokens
        try:
            if not draft.block_ids or any(bid not in texts for bid in draft.block_ids):
                raise OutputValidationError("unknown draft block_id")
            context_ids = set(draft.block_ids)
            for section in sections:
                if set(section.block_ids).intersection(draft.block_ids):
                    context_ids.update(section.block_ids)
            messages = [
                {"role": "system", "content": load(None, "verdict")},
                {"role": "user", "content": json.dumps({
                    "blocks": [{"block_id": bid, "text_llm": text}
                               for bid, text in texts.items() if bid in context_ids],
                    "draft": draft.model_dump(),
                    "candidates": [c.model_dump() for c in candidates],
                }, ensure_ascii=False)},
            ]
            with client_factory() as client:
                for attempt in range(2):
                    calls += 1
                    response = client.chat(messages, temperature=0.1, max_tokens=900)
                    tokens += response.usage.prompt_tokens + response.usage.completion_tokens
                    try:
                        if response.tool_calls:
                            raise OutputValidationError("tool calls are forbidden")
                        finding = validate_verdict(response.content, draft, texts, candidates)
                        logger.info("verdict_complete", extra={"draft_id": draft.draft_id,
                            "confirmed": finding is not None, "llm_calls": calls, "tokens": tokens})
                        return finding, None, calls, tokens
                    except OutputValidationError as exc:
                        logger.warning("verdict_invalid", extra={"draft_id": draft.draft_id,
                            "attempt": attempt + 1, "error": str(exc)[:300]})
                        if attempt:
                            return None, "validation_failed", calls, tokens
                        messages.extend([
                            {"role": "assistant", "content": response.content},
                            {"role": "user", "content": load(None, "verdict_repair").replace(
                                "{{VALIDATION_ERROR}}", str(exc))},
                        ])
        except Exception as exc:
            logger.warning("verdict_failed", extra={"draft_id": draft.draft_id, "error_type": type(exc).__name__})
            return None, "llm_error" if not isinstance(exc, OutputValidationError) else "validation_failed", calls, tokens
        return None, "validation_failed", calls, tokens

    verdicts = {bid: BlockVerdict(block_id=bid, verdict="ok", note=None) for bid in texts}
    for section in sections:
        if section.status == "needs_review":
            for bid in [*section.block_ids, *(s.block_id for s in section.skipped)]:
                if bid in verdicts:
                    verdicts[bid] = BlockVerdict(block_id=bid, verdict="needs_review", note=section.note or "section_needs_review")
    findings: list[Finding] = []
    calls = tokens = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(worker, draft_findings))
    for draft, (finding, note, used_calls, used_tokens) in zip(draft_findings, results):
        calls += used_calls
        tokens += used_tokens
        if note:
            for bid in draft.block_ids:
                if bid in verdicts:
                    verdicts[bid] = BlockVerdict(block_id=bid, verdict="needs_review", note=note)
        if finding:
            findings.append(finding)
            bid = finding.block_id
            if verdicts[bid].verdict != "needs_review":
                verdicts[bid] = BlockVerdict(block_id=bid,
                    verdict="needs_review" if finding.confidence == "needs_review" else "risk",
                    note="uncertain_verdict" if finding.confidence == "needs_review" else None)
    return AnalysisResult(doc_id=doc.doc_id, findings=findings, verdicts=list(verdicts.values()),
        stats=AnalysisStats(blocks=len(doc.blocks), llm_calls=calls, tool_calls=0,
                            tokens=tokens, duration_s=round(time.perf_counter() - started, 3)))

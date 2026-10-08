"""Generate unverified risk hypotheses without legal tools."""
from __future__ import annotations

import json

from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from lexaudit.extractor.sections import Section
from lexaudit.llm import LLMClient, LLMError
from lexaudit.prompts.registry import load


class DraftFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    draft_id: str = Field(min_length=1)
    block_ids: list[str] = Field(min_length=1)
    quote_hint: str = Field(min_length=1)
    risk_hypothesis: str = Field(min_length=1)
    law_area: str = Field(min_length=1)
    search_queries: list[str] = Field(min_length=2, max_length=3)
    warnings: list[str] = Field(default_factory=list)
    grounding: Literal["pending", "candidates_found", "ungrounded"] = "pending"
    suggested_article_numbers: list[str] = Field(default_factory=list)


def draft_section(section: Section, domain: str, hints: list[str], *,
                  client_factory: Callable[[], LLMClient] = LLMClient) -> list[DraftFinding]:
    """Make one completion and at most one repair; record failed sections."""
    section.status, section.note = "pending", None
    texts = section._block_texts
    if not texts:
        parts = section.text_llm.split("\n\n") if section.text_llm else []
        if len(parts) != len(section.block_ids):
            section.status, section.note = "needs_review", "block_text_mapping_unavailable"
            return []
        texts = dict(zip(section.block_ids, parts))
    messages = [
        {"role": "system", "content": load(None, "draft")},
        {"role": "user", "content": json.dumps({
            "domain": domain, "search_hints": hints, "title": section.title,
            "text_llm": "\n\n".join(f"[{bid}] {texts[bid]}" for bid in section.block_ids),
        }, ensure_ascii=False)},
    ]
    try:
        with client_factory() as client:
            for attempt in range(2):
                response = client.chat(messages, temperature=0.2, max_tokens=1500)
                try:
                    if response.tool_calls:
                        raise ValueError("tool_calls are forbidden")
                    findings = TypeAdapter(list[DraftFinding]).validate_json(response.content)
                    for finding in findings:
                        if any(bid not in texts for bid in finding.block_ids):
                            raise ValueError("unknown block_id")
                        if any(not query.strip() for query in finding.search_queries):
                            raise ValueError("empty search query")
                    mismatches = [f for f in findings if not f.quote_hint.strip() or not any(
                        f.quote_hint.casefold() in texts[bid].casefold() for bid in f.block_ids
                    )]
                    if mismatches and attempt == 0:
                        raise ValueError("quote_hint is not a substring of its referenced blocks")
                    for finding in findings:
                        finding.warnings = []
                    for finding in mismatches:
                        finding.warnings.append("quote_hint_not_found")
                    for index, finding in enumerate(findings, 1):
                        finding.draft_id = f"df_{section.section_id}_{index:03d}"
                    section.status = "needs_review" if mismatches else "drafted"
                    section.note = "quote_hint_not_found" if mismatches else None
                    return findings
                except (ValidationError, ValueError) as exc:
                    if attempt == 1:
                        section.status, section.note = "needs_review", "draft_validation_failed"
                        return []
                    messages.extend([
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": load(None, "draft_repair").replace(
                            "{{VALIDATION_ERROR}}", str(exc))},
                    ])
    except LLMError:
        section.status, section.note = "needs_review", "llm_error"
    return []

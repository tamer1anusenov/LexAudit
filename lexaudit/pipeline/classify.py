"""Classify a contract's legal domain and produce Adilet search hints."""
from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lexaudit.extractor.parse import ContractDoc
from lexaudit.llm.client import LLMClient
from lexaudit.prompts.registry import load

logger = logging.getLogger("lexaudit.pipeline.classify")

Domain = Literal["tourism", "labor", "real_estate", "services", "supply", "lease", "general"]


class ClassificationResult(BaseModel):
    """Classification contract recorded in docs/schemas.md."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    domain: Domain
    rationale: str = Field(min_length=1)
    search_hints: list[str] = Field(min_length=3, max_length=6)


_DEFAULT: dict[str, object] = {
    "domain": "general",
    "rationale": "Не удалось классифицировать документ из-за некорректного ответа модели.",
    "search_hints": [],
}


def classify_document(doc: ContractDoc, *, llm_client: LLMClient | None = None) -> dict[str, object]:
    """Classify the first 30 blocks using one LLM call and at most one repair."""
    excerpt = "\n".join(block.text for block in doc.blocks[:30])[:8000]
    messages = [
        {"role": "system", "content": load(None, "classify")},
        {"role": "user", "content": excerpt},
    ]

    if llm_client is not None:
        return _classify_with_client(doc, excerpt, messages, llm_client)
    with LLMClient() as client:
        return _classify_with_client(doc, excerpt, messages, client)


def _classify_with_client(
    doc: ContractDoc, excerpt: str, messages: list[dict[str, str]], client: LLMClient
) -> dict[str, object]:
    response = client.chat(messages)
    try:
        return _parse_response(response.content).model_dump()
    except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Invalid classification response; requesting one repair", extra={"doc_id": doc.doc_id})
        repair = load(None, "classify_repair")
        repair = repair.replace("{{document_text}}", excerpt)
        repair = repair.replace("{{response}}", response.content)
        repair = repair.replace("{{validation_error}}", str(exc))
        repaired = client.chat([{"role": "user", "content": repair}])
        try:
            return _parse_response(repaired.content).model_dump()
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Classification repair response invalid", extra={"doc_id": doc.doc_id})
            return dict(_DEFAULT)


def _parse_response(content: str) -> ClassificationResult:
    """Decode a strict JSON classification object."""
    return ClassificationResult.model_validate_json(content)


def main() -> None:
    """Provide standalone module help as required by GLOBAl_RULES.md."""
    import argparse

    parser = argparse.ArgumentParser(description="Classify a contract document")
    parser.parse_args()


if __name__ == "__main__":
    main()

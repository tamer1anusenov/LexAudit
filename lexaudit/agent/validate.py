"""Schema and evidence validation for per-block agent responses."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from lexaudit.extractor.parse import Block


class LawReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    act_id: str
    act_title: str
    article: str
    clause: str | None
    url: str


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    finding_id: str
    block_id: str
    severity: Literal["high", "medium", "low"]
    confidence: Literal["confirmed", "needs_review"]
    quote: str
    proposed_text: str
    risk_explanation: str
    law_ref: LawReference


class BlockAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    block_id: str
    verdict: Literal["ok", "risk", "skip"]
    findings: list[Finding]


class BlockVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    block_id: str
    verdict: Literal["ok", "risk", "skip", "needs_review"]
    note: str | None = None


class AnalysisStats(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    blocks: int
    llm_calls: int
    tool_calls: int
    tokens: int
    duration_s: float


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    doc_id: str
    findings: list[Finding]
    verdicts: list[BlockVerdict]
    stats: AnalysisStats


class OutputValidationError(ValueError):
    """Raised when model output fails schema or citation-evidence validation."""


def validate_block_output(
    content: str,
    block: Block,
    search_hits: dict[str, dict[str, Any]],
    full_acts: dict[str, dict[str, Any]],
) -> BlockAnalysis:
    """Parse one JSON block result and verify its block text and Adilet sources."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OutputValidationError("response is not strict JSON") from exc
    try:
        output = BlockAnalysis.model_validate(payload)
    except ValidationError as exc:
        raise OutputValidationError(_format_validation_error(exc)) from exc
    if output.block_id != block.block_id:
        raise OutputValidationError("block_id does not match the input block")
    if output.verdict == "risk" and not output.findings:
        raise OutputValidationError("risk verdict requires at least one finding")
    if output.verdict != "risk" and output.findings:
        raise OutputValidationError("ok/skip verdicts must not contain findings")

    for finding in output.findings:
        if finding.block_id != block.block_id:
            raise OutputValidationError("finding block_id does not match the input block")
        if not finding.quote or finding.quote not in block.text:
            raise OutputValidationError("finding quote is not an exact non-empty substring of the block")
        _validate_law_reference(finding, search_hits, full_acts)
    return output


def _validate_law_reference(
    finding: Finding,
    search_hits: dict[str, dict[str, Any]],
    full_acts: dict[str, dict[str, Any]],
) -> None:
    law_ref = finding.law_ref
    hit = search_hits.get(law_ref.act_id)
    act = full_acts.get(law_ref.act_id)
    if hit is None:
        raise OutputValidationError("law_ref act_id was not returned by search_laws")
    if act is None:
        raise OutputValidationError("law_ref article was not verified through get_full_text")
    if not hit.get("in_force") or not act.get("in_force"):
        raise OutputValidationError("law_ref act is not confirmed in force")
    if law_ref.act_title != act.get("title") or law_ref.url != act.get("url"):
        raise OutputValidationError("law_ref title or URL does not match get_full_text")
    requested_number = law_ref.article.strip().rstrip(".")
    articles = act.get("articles", [])
    if not any(str(article.get("number", "")).strip().rstrip(".") == requested_number for article in articles):
        raise OutputValidationError("law_ref article number is absent from the full act text")


def _format_validation_error(exc: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_url=False)
    )
    return f"response does not match the block-output schema: {details}"[:2000]


__all__ = [
    "AnalysisResult", "AnalysisStats", "BlockAnalysis", "BlockVerdict", "Finding",
    "LawReference", "OutputValidationError", "validate_block_output",
]

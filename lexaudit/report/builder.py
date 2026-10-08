"""Build a self-contained interactive HTML contract review report."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lexaudit.agent.validate import AnalysisResult
from lexaudit.config.settings import get_settings
from lexaudit.extractor.parse import ContractDoc

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class ReportBuildError(RuntimeError):
    """Raised when report inputs cannot be rendered."""


def build_report(
    doc: ContractDoc,
    analysis: AnalysisResult,
    output_path: str | Path | None = None,
    *,
    prompt_version: str | None = None,
) -> str:
    """Render the interactive report and optionally write it to disk."""
    if doc.doc_id != analysis.doc_id:
        raise ReportBuildError("ContractDoc and AnalysisResult doc_id values differ")

    findings_by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
    finding_index: list[dict[str, Any]] = []
    for finding in analysis.findings:
        item = finding.model_dump()
        findings_by_block[finding.block_id].append(item)
        finding_index.append(item)

    table_blocks: dict[str, list[Any]] = defaultdict(list)
    for block in doc.blocks:
        if block.type == "table_cell" and block.meta.table:
            table_blocks[block.meta.table].append(block)

    items: list[dict[str, Any]] = []
    rendered_tables: set[str] = set()
    for block in doc.blocks:
        block_findings = findings_by_block.get(block.block_id, [])
        if block.type == "table_cell" and block.meta.table:
            table_name = block.meta.table
            if table_name in rendered_tables:
                continue
            rendered_tables.add(table_name)
            rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for cell in sorted(table_blocks[table_name], key=lambda b: (b.meta.row or 0, b.meta.col or 0)):
                rows[cell.meta.row or 0].append(_render_block(cell, findings_by_block.get(cell.block_id, [])))
            items.append({"kind": "table", "table": table_name,
                          "rows": [rows[index] for index in sorted(rows)]})
        else:
            items.append({"kind": "block", "block": _render_block(block, block_findings)})

    verdicts = {verdict.block_id: verdict.model_dump() for verdict in analysis.verdicts}
    needs_review = [
        {"block": _render_block(block, findings_by_block.get(block.block_id, [])),
         "note": verdicts[block.block_id].get("note")}
        for block in doc.blocks
        if block.block_id in verdicts and verdicts[block.block_id]["verdict"] == "needs_review"
    ]
    severities = Counter(finding.severity for finding in analysis.findings)
    template_env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    html = template_env.get_template("review.html").render(
        doc_id=doc.doc_id,
        filename=doc.filename,
        report_date=date.today().isoformat(),
        block_count=len(doc.blocks),
        severities={name: severities.get(name, 0) for name in ("high", "medium", "low")},
        finding_count=len(analysis.findings),
        duration_s=analysis.stats.duration_s,
        prompt_version=prompt_version or get_settings().PROMPTS_VERSION,
        items=items,
        findings=finding_index,
        needs_review=needs_review,
    )
    if output_path is not None:
        try:
            Path(output_path).write_text(html, encoding="utf-8")
        except OSError as exc:
            raise ReportBuildError(f"cannot write report to {output_path}: {exc}") from exc
    return html


def _render_block(block: Any, findings: list[dict[str, Any]]) -> dict[str, Any]:
    text = block.text
    spans: list[dict[str, str | bool]] = []
    if not findings or not text:
        spans.append({"text": text, "highlight": False, "severity": ""})
    else:
        matches: list[tuple[int, int, str]] = []
        lower = text.casefold()
        for finding in findings:
            quote = finding["quote"]
            start = lower.find(quote.casefold()) if quote else -1
            if start >= 0:
                matches.append((start, start + len(quote), finding["severity"]))
        if not matches:
            spans.append({"text": text, "highlight": True,
                          "severity": _strongest_severity(findings)})
        else:
            matches.sort(key=lambda match: (match[0], -(match[1] - match[0])))
            cursor = 0
            for start, end, severity in matches:
                if start < cursor:
                    continue
                if start > cursor:
                    spans.append({"text": text[cursor:start], "highlight": False, "severity": ""})
                matched = next((f for f in findings if f["severity"] == severity and
                                f["quote"].casefold() == text[start:end].casefold()), findings[0])
                spans.append({"text": text[start:end], "highlight": True, "severity": severity,
                              "finding_id": matched["finding_id"],
                              "proposed_text": matched["proposed_text"]})
                cursor = end
            if cursor < len(text):
                spans.append({"text": text[cursor:], "highlight": False, "severity": ""})
    return {
        "block_id": block.block_id,
        "type": block.type,
        "text": text,
        "spans": spans,
        "findings": findings,
    }


def _strongest_severity(findings: list[dict[str, Any]]) -> str:
    order = {"high": 0, "medium": 1, "low": 2}
    return min((finding["severity"] for finding in findings), key=lambda value: order.get(value, 3))


def main() -> None:
    """Expose standalone module help."""
    import argparse

    argparse.ArgumentParser(description="Build an interactive HTML contract report").parse_args()


if __name__ == "__main__":
    main()

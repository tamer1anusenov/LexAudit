"""LexAudit command-line entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from lexaudit import __version__
from lexaudit.pipeline.analyze import analyze_contract
from lexaudit.agent.validate import AnalysisResult, AnalysisStats
from lexaudit.config.settings import get_settings
from lexaudit.extractor.parse import ExtractionError, parse_docx
from lexaudit.logging_setup import setup_logging
from lexaudit.pipeline.classify import classify_document
from lexaudit.prompts.registry import list_versions
from lexaudit.report.builder import build_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lexaudit",
        description=("LexAudit — AI legal auditor for Kazakhstan contracts."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="analyze a DOCX contract and build its review report")
    run.add_argument("contract", help="path to the DOCX contract")
    run.add_argument("--out", default="out/", help="directory for per-document artifacts")
    run.add_argument("--max-blocks", type=_positive_int, help="analyze only the first N selected blocks")
    run.add_argument("--only-blocks", help="comma-separated block IDs to analyze")
    run.add_argument("--prompts", help="prompt version to use; defaults to PROMPTS_VERSION")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 0

    if args.prompts:
        if args.prompts not in list_versions():
            parser.error(f"unknown prompt version {args.prompts!r}; available: {', '.join(list_versions())}")
        os.environ["PROMPTS_VERSION"] = args.prompts
        get_settings.cache_clear()
    settings = get_settings()
    setup_logging(
        level=settings.LOG_LEVEL,
        log_dir=settings.LOG_DIR,
        secrets=[settings.ALEM_API_KEY.get_secret_value()],
    )
    console = Console()
    try:
        doc = parse_docx(args.contract)
    except ExtractionError as exc:
        console.print(f"[red]Ошибка чтения договора:[/red] {exc}", stderr=True)
        return 1

    classification = classify_document(doc)
    doc = doc.model_copy(update={"domain": classification["domain"]})
    selected = doc.blocks
    if args.only_blocks:
        requested = {value.strip() for value in args.only_blocks.split(",") if value.strip()}
        known = {block.block_id for block in doc.blocks}
        unknown = sorted(requested - known)
        if unknown:
            console.print(f"[red]Неизвестные ID блоков:[/red] {', '.join(unknown)}", stderr=True)
            return 2
        selected = [block for block in doc.blocks if block.block_id in requested]
    if args.max_blocks is not None:
        selected = selected[:args.max_blocks]
    if not selected:
        console.print("[red]Нет блоков для анализа.[/red]", stderr=True)
        return 2

    output_dir = Path(args.out) / doc.doc_id
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "blocks.json", [block.model_dump() for block in selected])
    _write_json(output_dir / "classification.json", classification)

    trace_path = settings.LOG_DIR / f"analysis_{doc.doc_id}.jsonl"
    trace_offset = trace_path.stat().st_size if trace_path.exists() else 0
    findings = []
    verdicts = []
    llm_calls = tool_calls = tokens = 0
    analysis_started = time.perf_counter()
    with Progress(
        TextColumn("Анализ блоков"), BarColumn(), "{task.completed}/{task.total}", TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("analysis", total=len(selected))
        result = analyze_contract(doc.model_copy(update={"blocks": selected}),
            classification.get("search_hints", []),
            on_progress=lambda completed, total: progress.update(task_id, completed=completed, total=total))
        findings, verdicts = result.findings, result.verdicts
        llm_calls, tool_calls, tokens = result.stats.llm_calls, result.stats.tool_calls, result.stats.tokens
    duration_s = round(time.perf_counter() - analysis_started, 3)
    analysis = AnalysisResult(
        doc_id=doc.doc_id,
        findings=findings,
        verdicts=verdicts,
        stats=AnalysisStats(
            blocks=len(selected), llm_calls=llm_calls, tool_calls=tool_calls,
            tokens=tokens, duration_s=duration_s,
        ),
    )
    _write_json(output_dir / "findings.json", [finding.model_dump() for finding in findings])
    build_report(doc.model_copy(update={"blocks": selected}), analysis,
                 output_dir / "review.html", prompt_version=args.prompts)
    _copy_trace(trace_path, trace_offset, output_dir / "trace.jsonl")
    _print_summary(console, analysis)
    console.print(f"Артефакты: {output_dir.resolve()}")
    return 0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _copy_trace(source: Path, offset: int, destination: Path) -> None:
    try:
        with source.open("rb") as stream:
            stream.seek(offset)
            content = stream.read()
        destination.write_bytes(content)
    except FileNotFoundError:
        destination.write_text("", encoding="utf-8")


def _print_summary(console: Console, analysis: AnalysisResult) -> None:
    severity_counts = Counter(finding.severity for finding in analysis.findings)
    verdict_counts = Counter(verdict.verdict for verdict in analysis.verdicts)
    console.print("\n[bold]Итог анализа[/bold]")
    console.print(
        "Находки: " + ", ".join(
            f"{severity} — {severity_counts.get(severity, 0)}"
            for severity in ("high", "medium", "low")
        ) + f"; всего — {len(analysis.findings)}"
    )
    console.print("Вердикты: " + ", ".join(
        f"{verdict} — {verdict_counts.get(verdict, 0)}"
        for verdict in ("risk", "ok", "skip", "needs_review")
    ))
    console.print(f"Токены: {analysis.stats.tokens}; длительность анализа: {analysis.stats.duration_s:.2f} с")


if __name__ == "__main__":
    sys.exit(main())

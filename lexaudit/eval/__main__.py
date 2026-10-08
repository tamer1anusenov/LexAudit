"""Run the LexAudit fixture evaluation and write a Markdown quality report."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from lexaudit.prompts.registry import list_versions


class EvaluationError(RuntimeError):
    """Raised when the evaluation inputs or report cannot be processed."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lexaudit.eval")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="evaluate all DOCX fixtures")
    run.add_argument("--fixtures", default="tests/fixtures/", help="fixture directory")
    run.add_argument("--prompts", help="prompt version to use")
    run.add_argument("--out", default="out/eval_report.md", help="Markdown report path")
    run.add_argument("--both", action="store_true", help="compare per_block and sections on every fixture")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 0
    versions = list_versions()
    if args.prompts and args.prompts not in versions:
        parser.error(f"unknown prompt version {args.prompts!r}; available: {', '.join(versions)}")
    fixtures_dir = Path(args.fixtures)
    if not fixtures_dir.is_dir():
        raise EvaluationError(f"fixture directory not found: {fixtures_dir}")
    expected = _read_expected_findings(fixtures_dir / "expected_findings.md")
    docx_files = sorted(fixtures_dir.rglob("*.docx"))
    if not docx_files:
        raise EvaluationError(f"no DOCX fixtures found in {fixtures_dir}")

    from lexaudit.config.settings import get_settings
    modes = ["per_block", "sections"] if args.both else [get_settings().AGENT_MODE]
    results = []
    with tempfile.TemporaryDirectory(prefix="lexaudit-eval-") as temp_dir:
        temp_output = Path(temp_dir) / "out"
        for fixture, mode in [(fixture, mode) for fixture in docx_files for mode in modes]:
            print(f"Оценка: {fixture.name} — {mode}", flush=True)
            started = time.perf_counter()
            fixture_output = temp_output / mode / fixture.stem
            log_dir = Path(temp_dir) / "logs" / mode / fixture.stem
            command = [sys.executable, "-m", "lexaudit.cli", "run", str(fixture.resolve()),
                       "--out", str(fixture_output)]
            if args.prompts:
                command.extend(["--prompts", args.prompts])
            environment = {**os.environ, "AGENT_MODE": mode, "LOG_DIR": str(log_dir)}
            process = subprocess.run(command, capture_output=True, text=True, check=False, env=environment)
            elapsed = round(time.perf_counter() - started, 3)
            artifact_dirs = sorted(fixture_output.glob("*/")) if fixture_output.exists() else []
            artifact_dir = artifact_dirs[-1] if artifact_dirs else None
            calls, tokens = _llm_metrics(log_dir / "app.jsonl")
            if process.returncode != 0 or artifact_dir is None:
                results.append({
                    "fixture": fixture.name,
                    "mode": mode,
                    "issues": [{"sentence": s, "block_id": None, "finding": None}
                               for s in expected.get(fixture.name, [])],
                    "findings": [],
                    "false_positives": [],
                    "llm_calls": calls,
                    "tokens": tokens,
                    "duration_s": elapsed,
                    "error": (process.stderr or process.stdout or "pipeline produced no artifacts").strip()[-2000:],
                })
                continue
            blocks = json.loads((artifact_dir / "blocks.json").read_text(encoding="utf-8"))
            findings = json.loads((artifact_dir / "findings.json").read_text(encoding="utf-8"))
            trace_path = artifact_dir / "trace.jsonl"
            planted = expected.get(fixture.name, [])
            issues = []
            for sentence in planted:
                block_id = next((block["block_id"] for block in blocks if sentence.casefold() in block["text"].casefold()), None)
                match = next((finding for finding in findings
                              if (block_id is not None and finding["block_id"] == block_id)
                              or _overlap_ratio(sentence, finding.get("quote", "")) > 0.60), None)
                issues.append({"sentence": sentence, "block_id": block_id, "finding": match})
            false_positives = findings if fixture.name == "clean.docx" else []
            results.append({
                "fixture": fixture.name,
                "mode": mode,
                "issues": issues,
                "findings": findings,
                "false_positives": false_positives,
                "tokens": tokens,
                "llm_calls": calls,
                "duration_s": elapsed,
                "error": None,
            })

    prompts = args.prompts or get_settings().PROMPTS_VERSION
    report = _render_report(results, prompts)
    if args.both:
        comparison = _comparison_table(results)
        report += "\n" + comparison
        notes_path = Path(__file__).resolve().parents[1] / "TASK_NOTES.md"
        with notes_path.open("a", encoding="utf-8") as stream:
            stream.write("\n## ARCH-5 — A/B evaluation\n\n" + comparison)
    report_path = Path(args.out)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        raise EvaluationError(f"cannot write evaluation report to {report_path}: {exc}") from exc
    print(f"Evaluation report: {report_path.resolve()}")
    return 1 if any(result["error"] for result in results) else 0


def _read_expected_findings(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        raise EvaluationError(f"expected findings file not found: {path}")
    expected: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.lstrip().startswith("| Fixture") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0].endswith(".docx") or cells[1].startswith(("—", "-")):
            continue
        sentence = cells[1].strip().strip("«»\"")
        if sentence:
            expected.setdefault(cells[0], []).append(sentence)
    return expected


def _overlap_ratio(planted: str, quote: str) -> float:
    if not planted or not quote:
        return 0.0
    left, right = planted.casefold(), quote.casefold()
    shared = sum(block.size for block in SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks())
    return shared / len(left)


def _trace_tokens(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            total += int(json.loads(line).get("tokens", 0) or 0)
    return total


def _llm_metrics(path: Path) -> tuple[int, int]:
    """Count provider requests, including retries/classification, without trace duplication."""
    if not path.exists():
        return 0, 0
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [r for r in records if r.get("logger") == "lexaudit.llm" and "status" in r]
    return len(calls), sum(int(r.get("tokens") or 0) for r in calls)


def _comparison_table(results: list[dict[str, Any]]) -> str:
    lines = ["## Сравнение режимов", "",
        "| Файл | Режим | Найдено / посажено | Recall | FP clean | LLM-запросы | Токены | Время, с | Статус |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for result in results:
        found = sum(i["finding"] is not None for i in result["issues"])
        total = len(result["issues"])
        recall = f"{found / total:.1%}" if total else "—"
        fp = str(len(result["false_positives"])) if result["fixture"] == "clean.docx" else "—"
        lines.append(f"| {result['fixture']} | {result['mode']} | {found}/{total} | {recall} | {fp} | "
                     f"{result['llm_calls']} | {result['tokens']} | {result['duration_s']:.2f} | "
                     f"{'ошибка' if result['error'] else 'OK'} |")
    for mode in ("per_block", "sections"):
        subset = [r for r in results if r["mode"] == mode]
        total = sum(len(r["issues"]) for r in subset)
        found = sum(i["finding"] is not None for r in subset for i in r["issues"])
        fp = sum(len(r["false_positives"]) for r in subset)
        recall = f"{found / total:.1%}" if total else "—"
        lines.append(f"| **Итого** | **{mode}** | {found}/{total} | {recall} | {fp} | "
            f"{sum(r['llm_calls'] for r in subset)} | {sum(r['tokens'] for r in subset)} | "
            f"{sum(r['duration_s'] for r in subset):.2f} | "
            f"{'ошибки' if any(r['error'] for r in subset) else 'OK'} |")
    lines.extend(["", "LLM-запросы включают классификацию и повторные HTTP-попытки; токены — usage провайдера.",
        "Время — полный CLI-пайплайн. Режимы используют общее зеркало Adilet: второй запуск может выиграть от кеша.",
        "Документы без посаженных проблем не входят в знаменатель recall; FP измеряются только на clean.docx.",
        "Режим по умолчанию остаётся per_block; решение принимает человек.", ""])
    return "\n".join(lines)


def _render_report(results: list[dict[str, Any]], prompts: str) -> str:
    lines = ["# LexAudit — отчёт оценки", "", f"Промпты: `{prompts}`", ""]
    summary_rows = []
    for result in results:
        fixture = result["fixture"] + " — " + result["mode"]
        lines.extend([f"## {fixture}", ""])
        if result["error"]:
            lines.extend([f"Ошибка запуска: {result['error']}", ""])
        else:
            found = sum(item["finding"] is not None for item in result["issues"])
            lines.append(f"Посаженные проблемы: найдено {found}, пропущено {len(result['issues']) - found} из {len(result['issues'])}.")
            for index, issue in enumerate(result["issues"], 1):
                status = "НАЙДЕНО" if issue["finding"] else "ПРОПУЩЕНО"
                lines.append(f"- {status}: {issue['sentence']}")
                if issue["finding"]:
                    finding = issue["finding"]
                    lines.append(f"  - Совпавшая находка: `{finding['finding_id']}` ({finding['severity']}), блок `{finding['block_id']}`, цитата: {finding['quote']}")
            if result["fixture"] == "clean.docx":
                lines.append(f"Ложные срабатывания на чистом документе: {len(result['false_positives'])}.")
                for finding in result["false_positives"]:
                    lines.append(f"- `{finding['finding_id']}` ({finding['severity']}), блок `{finding['block_id']}`: {finding['quote']}")
            lines.append(f"Стоимость по токенам: {result['tokens']} токенов.")
            lines.append(f"LLM-запросы: {result['llm_calls']}.")
            lines.append(f"Длительность: {result['duration_s']:.2f} с.")
        found = sum(item["finding"] is not None for item in result["issues"])
        summary_rows.append((fixture, found, len(result["issues"]), len(result["false_positives"]), result["tokens"], result["duration_s"], bool(result["error"])))
        lines.append("")
    lines.extend(["## Сводка", "", "| Файл | Найдено / всего | Ложные срабатывания | Токены | Длительность, с | Статус |", "|---|---:|---:|---:|---:|---|"])
    for fixture, found, total, false_positives, tokens, duration, failed in summary_rows:
        status = "ошибка" if failed else "OK"
        lines.append(f"| {fixture} | {found} / {total} | {false_positives} | {tokens} | {duration:.2f} | {status} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())

"""FastAPI application, upload endpoints, and threaded pipeline runner."""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from lexaudit.pipeline.analyze import analyze_contract
from lexaudit.agent.validate import AnalysisResult, AnalysisStats
from lexaudit.config.settings import get_settings
from lexaudit.extractor.parse import parse_docx
from lexaudit.logging_setup import setup_logging
from lexaudit.llm.client import LLMClient
from lexaudit.pipeline.classify import classify_document
from lexaudit.report.builder import build_report
from lexaudit.api.storage import (
    DocumentQueued,
    DocumentStatus,
    create_document,
    find_active_duplicate,
    get_document,
    initialize_database,
    update_document,
)

logger = logging.getLogger("lexaudit.api")
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    if settings.PIPELINE_MODE not in {"inline", "celery"}:
        raise RuntimeError("PIPELINE_MODE must be inline or celery")
    if settings.API_MAX_WORKERS < 1:
        raise RuntimeError("API_MAX_WORKERS must be at least 1")
    initialize_database()
    setup_logging(
        level=settings.LOG_LEVEL,
        log_dir=settings.LOG_DIR,
        secrets=[settings.ALEM_API_KEY.get_secret_value()],
        force=True,
    )
    from concurrent.futures import ThreadPoolExecutor

    application.state.executor = ThreadPoolExecutor(
        max_workers=settings.API_MAX_WORKERS,
        thread_name_prefix="lexaudit-analysis",
    )
    try:
        yield
    finally:
        application.state.executor.shutdown(wait=False, cancel_futures=False)


app = FastAPI(title="LexAudit", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """Render the contract upload and recent-document page."""
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/documents", response_model=DocumentQueued, status_code=202)
async def upload_document(request: Request) -> DocumentQueued:
    """Accept one multipart DOCX and enqueue the local threaded pipeline."""
    settings = get_settings()
    if settings.PIPELINE_MODE == "celery":
        raise HTTPException(status_code=501, detail="Celery mode is not available yet")
    payload, filename = await _read_multipart_docx(request, settings.MAX_UPLOAD_BYTES)
    duplicate_id = find_active_duplicate(payload)
    if duplicate_id is not None:
        return DocumentQueued(document_id=duplicate_id, status="queued")
    document_id = "doc_" + uuid.uuid4().hex
    upload_dir = settings.DATABASE_PATH.parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    source_path = upload_dir / f"{document_id}.docx"
    artifact_dir = settings.PIPELINE_OUTPUT_DIR / document_id
    paths = {
        "source": str(source_path),
        "directory": str(artifact_dir),
        "report": str(artifact_dir / "review.html"),
        "findings": str(artifact_dir / "findings.json"),
        "trace": str(artifact_dir / "trace.jsonl"),
    }
    try:
        source_path.write_bytes(payload)
        create_document(document_id, filename, paths)
        request.app.state.executor.submit(_process_document, document_id, source_path, paths)
    except Exception as exc:
        source_path.unlink(missing_ok=True)
        logger.error("Document enqueue failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Не удалось поставить документ в очередь.") from exc
    return DocumentQueued(document_id=document_id, status="queued")


@app.get("/api/documents/{document_id}", response_model=DocumentStatus)
async def document_status(document_id: str) -> DocumentStatus:
    record = get_document(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Документ не найден.")
    return DocumentStatus(
        document_id=record.id,
        status=record.status,
        progress=record.progress,
        error=record.error,
    )


@app.get("/api/documents/{document_id}/report")
async def document_report(document_id: str) -> FileResponse:
    record = _completed_document(document_id)
    report_path = Path(record.artifact_paths["report"])
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Отчёт не найден.")
    return FileResponse(report_path, media_type="text/html")


@app.get("/api/documents/{document_id}/findings")
async def document_findings(document_id: str) -> JSONResponse:
    record = _completed_document(document_id)
    findings_path = Path(record.artifact_paths["findings"])
    if not findings_path.is_file():
        raise HTTPException(status_code=404, detail="Результаты не найдены.")
    try:
        findings = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read findings artifact (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Не удалось прочитать результаты.") from exc
    return JSONResponse(findings)


def _completed_document(document_id: str):
    record = get_document(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Документ не найден.")
    if record.status != "done":
        raise HTTPException(status_code=409, detail=f"Документ ещё обрабатывается: {record.status}.")
    return record


async def _read_multipart_docx(request: Request, max_bytes: int) -> tuple[bytes, str]:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        raise HTTPException(status_code=415, detail="Ожидается multipart/form-data с файлом DOCX.")
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > max_bytes + 128 * 1024:
        raise HTTPException(status_code=413, detail="Размер файла превышает 10 МБ.")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes + 128 * 1024:
            raise HTTPException(status_code=413, detail="Размер файла превышает 10 МБ.")
    envelope = (
        b"MIME-Version: 1.0\r\nContent-Type: "
        + content_type.encode("latin-1")
        + b"\r\n\r\n"
        + bytes(body)
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    uploads = [
        part for part in message.iter_parts()
        if part.get_content_disposition() == "form-data" and part.get_param("name", header="content-disposition") == "file"
    ]
    if len(uploads) != 1:
        raise HTTPException(status_code=400, detail="Передайте один файл в поле `file`.")
    part = uploads[0]
    original_filename = part.get_filename()
    if not original_filename or not original_filename.lower().endswith(".docx"):
        raise HTTPException(status_code=415, detail="Поддерживаются только файлы .docx.")
    payload = part.get_payload(decode=True) or b""
    if not payload:
        raise HTTPException(status_code=400, detail="Файл пуст.")
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="Размер файла превышает 10 МБ.")
    filename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
    return payload, filename


def _process_document(document_id: str, source_path: Path, paths: dict[str, str]) -> None:
    """Run extraction, classification, per-block analysis, and report building."""
    trace_path: Path | None = None
    trace_offset = 0
    try:
        update_document(document_id, status="extracting", progress="0/0")
        doc = parse_docx(str(source_path))
        total = len(doc.blocks)
        update_document(document_id, status="extracting", progress=f"{total}/{total}")

        findings = []
        verdicts = []
        llm_calls = tool_calls = tokens = 0
        started = time.perf_counter()
        update_document(document_id, status="classifying", progress="0/1")
        with LLMClient() as llm_client:
            classification = classify_document(doc, llm_client=llm_client)
            doc = doc.model_copy(update={"domain": classification.get("domain", "general")})
            update_document(document_id, status="classifying", progress="1/1")

            trace_doc_id = document_id
            trace_path = get_settings().LOG_DIR / f"analysis_{trace_doc_id}.jsonl"
            trace_offset = trace_path.stat().st_size if trace_path.exists() else 0
            update_document(document_id, status="analyzing", progress=f"0/{total}")
            result = analyze_contract(doc.model_copy(update={"doc_id": trace_doc_id}),
                classification.get("search_hints", []), llm_client=llm_client,
                on_progress=lambda completed, count: update_document(
                    document_id, status="analyzing", progress=f"{completed}/{count}"))
            findings, verdicts = result.findings, result.verdicts
            llm_calls, tool_calls, tokens = result.stats.llm_calls, result.stats.tool_calls, result.stats.tokens

        analysis = AnalysisResult(
            doc_id=doc.doc_id,
            findings=findings,
            verdicts=verdicts,
            stats=AnalysisStats(
                blocks=total,
                llm_calls=llm_calls,
                tool_calls=tool_calls,
                tokens=tokens,
                duration_s=round(time.perf_counter() - started, 3),
            ),
        )
        artifact_dir = Path(paths["directory"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json(artifact_dir / "blocks.json", [block.model_dump() for block in doc.blocks])
        _write_json(artifact_dir / "classification.json", classification)
        _write_json(artifact_dir / "findings.json", [finding.model_dump() for finding in findings])
        update_document(document_id, status="building_report", progress=f"{total}/{total}")
        build_report(doc, analysis, paths["report"])
        if trace_path is not None and trace_path.exists():
            with trace_path.open("rb") as stream:
                stream.seek(trace_offset)
                Path(paths["trace"]).write_bytes(stream.read())
        else:
            Path(paths["trace"]).write_text("", encoding="utf-8")
        update_document(document_id, status="done", progress=f"{total}/{total}")
        logger.info("Document processing completed", extra={"document_id": document_id, "blocks": total})
    except Exception as exc:
        logger.error("Document processing failed (%s)", type(exc).__name__, extra={"document_id": document_id})
        try:
            record = get_document(document_id)
            current_progress = record.progress if record else "0/0"
            update_document(
                document_id,
                status="failed",
                progress=current_progress,
                error=f"Ошибка обработки документа ({type(exc).__name__}).",
            )
        except Exception as storage_exc:
            logger.critical("Could not record failed document (%s)", type(storage_exc).__name__)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

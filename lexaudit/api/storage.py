"""SQLite persistence and API response models for uploaded documents."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lexaudit.config.settings import get_settings

DocumentState = Literal[
    "queued", "extracting", "classifying", "analyzing", "building_report", "done", "failed"
]


class DocumentQueued(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    status: Literal["queued"] = "queued"


class DocumentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    status: DocumentState
    progress: str
    error: str | None


class DocumentRecord(BaseModel):
    """Internal row representation, including paths never returned to clients."""
    model_config = ConfigDict(extra="forbid")
    id: str
    filename: str
    status: DocumentState
    progress: str
    error: str | None
    artifact_paths: dict[str, str]
    created_at: str


def connect() -> sqlite3.Connection:
    """Open a configured SQLite connection with rows addressable by name."""
    settings = get_settings()
    settings.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                progress TEXT NOT NULL,
                error TEXT,
                artifact_paths TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )


def create_document(document_id: str, filename: str, paths: dict[str, str]) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO documents (id, filename, status, progress, error, artifact_paths, created_at) "
            "VALUES (?, ?, 'queued', '0/0', NULL, ?, ?)",
            (document_id, filename, json.dumps(paths), datetime.now(timezone.utc).isoformat()),
        )


def get_document(document_id: str) -> DocumentRecord | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        return None
    return DocumentRecord(
        id=row["id"], filename=row["filename"], status=row["status"], progress=row["progress"],
        error=row["error"], artifact_paths=json.loads(row["artifact_paths"]), created_at=row["created_at"],
    )


def find_active_duplicate(content: bytes) -> str | None:
    """Return an active job ID if its uploaded bytes match this upload."""
    digest = hashlib.sha256(content).digest()
    active = ("queued", "extracting", "classifying", "analyzing", "building_report")
    placeholders = ",".join("?" for _ in active)
    with connect() as connection:
        rows = connection.execute(
            f"SELECT id, artifact_paths FROM documents WHERE status IN ({placeholders})", active
        ).fetchall()
    for row in rows:
        try:
            source = Path(json.loads(row["artifact_paths"])["source"])
            if source.is_file() and hashlib.sha256(source.read_bytes()).digest() == digest:
                return str(row["id"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None


def update_document(
    document_id: str,
    *,
    status: DocumentState,
    progress: str | None = None,
    error: str | None = None,
    artifact_paths: dict[str, str] | None = None,
) -> None:
    assignments = ["status = ?", "error = ?"]
    values: list[Any] = [status, error]
    if progress is not None:
        assignments.append("progress = ?")
        values.append(progress)
    if artifact_paths is not None:
        assignments.append("artifact_paths = ?")
        values.append(json.dumps(artifact_paths))
    values.append(document_id)
    with connect() as connection:
        cursor = connection.execute(
            f"UPDATE documents SET {', '.join(assignments)} WHERE id = ?", values
        )
        if cursor.rowcount != 1:
            raise LookupError(f"document not found: {document_id}")

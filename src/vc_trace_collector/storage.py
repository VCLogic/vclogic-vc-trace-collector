"""Content-addressed artifacts and transactional operation state."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import RawArtifact


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: Any, *, indent: int | None = None) -> str:
    separators = None if indent else (",", ":")
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        indent=indent,
        separators=separators,
        sort_keys=True,
        default=str,
    )


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(value, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_jsonl(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (canonical_json(value) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_jsonl(path: Path, values: Iterable[Any]) -> None:
    lines = "".join(canonical_json(value) + "\n" for value in values)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@dataclass(frozen=True)
class StoredArtifact:
    record: RawArtifact
    path: Path
    metadata_path: Path


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def put_bytes(
        self,
        content: bytes,
        *,
        category: str,
        suffix: str = ".bin",
        source_url: str | None = None,
        source_path: str | None = None,
        mime_type: str | None = None,
        collection_method: str = "http",
        original_metadata: dict[str, Any] | None = None,
        parent_artifact_ids: list[str] | None = None,
        rights_notes: str | None = None,
    ) -> StoredArtifact:
        digest = sha256(content).hexdigest()
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        relative = Path("raw") / category / digest[:2] / f"{digest}{safe_suffix}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd, temporary_name = tempfile.mkstemp(prefix=".artifact.", dir=path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

        record = RawArtifact(
            artifact_id=f"sha256:{digest}",
            category=category,
            relative_path=str(relative),
            sha256=digest,
            size_bytes=len(content),
            mime_type=mime_type,
            source_url=source_url,
            source_path=source_path,
            collection_method=collection_method,
            original_metadata=original_metadata or {},
            parent_artifact_ids=parent_artifact_ids or [],
            rights_notes=rights_notes,
        )
        provenance_key = sha256(
            canonical_json(
                {
                    "artifact_id": record.artifact_id,
                    "category": category,
                    "source_url": source_url,
                    "source_path": source_path,
                    "mime_type": mime_type,
                    "collection_method": collection_method,
                    "collection_version": record.collection_version,
                    "original_metadata": original_metadata or {},
                    "parent_artifact_ids": parent_artifact_ids or [],
                    "rights_notes": rights_notes,
                }
            ).encode("utf-8")
        ).hexdigest()[:16]
        metadata_path = path.with_name(f"{path.name}.{provenance_key}.metadata.json")
        if metadata_path.exists():
            existing = RawArtifact.model_validate(read_json(metadata_path))
            return StoredArtifact(
                record=existing, path=path, metadata_path=metadata_path
            )
        write_json(metadata_path, record)
        return StoredArtifact(record=record, path=path, metadata_path=metadata_path)

    def verify(self, record: RawArtifact) -> bool:
        path = self.root / record.relative_path
        return path.exists() and sha256(path.read_bytes()).hexdigest() == record.sha256


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (operation_id, input_hash)
                )
                """
            )

    def start_operation(self, operation_id: str, input_hash: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations(operation_id, input_hash, status, attempts, updated_at)
                VALUES (?, ?, 'running', 1, ?)
                ON CONFLICT(operation_id, input_hash) DO UPDATE SET
                    status='running', attempts=operations.attempts + 1,
                    error=NULL, updated_at=excluded.updated_at
                """,
                (operation_id, input_hash, now),
            )

    def finish_operation(
        self, operation_id: str, input_hash: str, output_id: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations(operation_id, input_hash, status, output_id, attempts, updated_at)
                VALUES (?, ?, 'complete', ?, 1, ?)
                ON CONFLICT(operation_id, input_hash) DO UPDATE SET
                    status='complete', output_id=excluded.output_id,
                    error=NULL, updated_at=excluded.updated_at
                """,
                (operation_id, input_hash, output_id, now),
            )

    def fail_operation(self, operation_id: str, input_hash: str, error: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations(operation_id, input_hash, status, attempts, error, updated_at)
                VALUES (?, ?, 'failed', 1, ?, ?)
                ON CONFLICT(operation_id, input_hash) DO UPDATE SET
                    status='failed', error=excluded.error, updated_at=excluded.updated_at
                """,
                (operation_id, input_hash, error, now),
            )

    def is_complete(self, operation_id: str, input_hash: str) -> bool:
        row = self._row(operation_id, input_hash)
        return bool(row and row["status"] == "complete")

    def output_id(self, operation_id: str, input_hash: str) -> str | None:
        row = self._row(operation_id, input_hash)
        return str(row["output_id"]) if row and row["output_id"] else None

    def operation_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM operations GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def operation_records(self, prefix: str | None = None) -> list[dict[str, object]]:
        """Return public operation state for audit reconciliation."""
        with self._connect() as connection:
            if prefix is None:
                rows = connection.execute(
                    "SELECT * FROM operations ORDER BY updated_at, operation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM operations WHERE operation_id LIKE ? "
                    "ORDER BY updated_at, operation_id",
                    (f"{prefix}%",),
                ).fetchall()
        return [dict(row) for row in rows]

    def retry_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(MAX(attempts - 1, 0)), 0) AS count FROM operations"
            ).fetchone()
        return int(row["count"])

    def _row(self, operation_id: str, input_hash: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM operations WHERE operation_id=? AND input_hash=?",
                (operation_id, input_hash),
            ).fetchone()

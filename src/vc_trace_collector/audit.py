"""Public audit events and enforceable cost reservations."""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import AuditEvent, CostEntry
from .storage import append_jsonl, read_jsonl

_SECRET_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "password",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "bearer_token",
}


def _secret_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("_secret", "_password", "_api_key", "_access_token")
    )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _secret_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        cleaned = re.sub(
            r"(?i)(?:authorization\s*:\s*)?bearer\s+[^\s,;]+",
            "Bearer [REDACTED]",
            value,
        )
        cleaned = re.sub(
            r"\b(?:sk-[A-Za-z0-9_-]{8,}|(?:hf|ghp)_[A-Za-z0-9._-]{8,})\b",
            "[REDACTED]",
            cleaned,
        )
        return re.sub(
            r"(?i)(api[_-]?key|access[_-]?token|token|secret|password)="
            r"[^&\s]+",
            r"\1=[REDACTED]",
            cleaned,
        )
    return value


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, event: AuditEvent) -> None:
        row = event.model_dump(mode="json")
        row["details"] = redact(row.get("details", {}))
        append_jsonl(self.path, row)


class BudgetExceeded(RuntimeError):
    def __init__(self, operation_id: str):
        super().__init__(f"Budget exhausted before operation: {operation_id}")
        self.operation_id = operation_id


class BudgetLedger:
    def __init__(self, path: Path, maximum: Decimal):
        self.path = Path(path)
        self.maximum = Decimal(maximum)
        self.database_path = self.path.with_suffix(".sqlite")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cost_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    media_seconds REAL NOT NULL,
                    provider TEXT,
                    model TEXT,
                    timestamp TEXT NOT NULL
                )
                """
            )
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM cost_events"
            ).fetchone()["count"]
            if count == 0:
                for row in read_jsonl(self.path):
                    self._insert(connection, CostEntry.model_validate(row))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _insert(connection: sqlite3.Connection, entry: CostEntry) -> None:
        connection.execute(
            """
            INSERT INTO cost_events(
                operation_id, kind, amount_usd, input_tokens, output_tokens,
                media_seconds, provider, model, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.operation_id,
                entry.kind,
                str(entry.amount_usd),
                entry.input_tokens,
                entry.output_tokens,
                entry.media_seconds,
                entry.provider,
                entry.model,
                entry.timestamp.isoformat(),
            ),
        )

    def _database_rows(self, connection: sqlite3.Connection | None = None):
        if connection is not None:
            return connection.execute(
                "SELECT * FROM cost_events ORDER BY event_id"
            ).fetchall()
        with self._connect() as owned:
            return owned.execute(
                "SELECT * FROM cost_events ORDER BY event_id"
            ).fetchall()

    @property
    def spent(self) -> Decimal:
        return sum(
            (
                Decimal(str(row["amount_usd"]))
                for row in self._database_rows()
                if row["kind"] == "settlement"
            ),
            start=Decimal(0),
        )

    @property
    def media_seconds(self) -> float:
        return sum(
            float(row["media_seconds"])
            for row in self._database_rows()
            if row["kind"] == "settlement"
        )

    @property
    def reserved(self) -> Decimal:
        reservations: dict[str, Decimal | None] = {}
        for row in self._database_rows():
            operation = str(row["operation_id"])
            if row["kind"] == "reservation":
                reservations[operation] = Decimal(str(row["amount_usd"]))
            elif row["kind"] in {"settlement", "release"}:
                reservations[operation] = None
        return sum(
            (amount for amount in reservations.values() if amount is not None),
            start=Decimal(0),
        )

    @property
    def available(self) -> Decimal:
        return self.maximum - self.spent - self.reserved

    def reserve(
        self,
        operation_id: str,
        amount: Decimal,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        amount = Decimal(amount)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._database_rows(connection)
            active: dict[str, Decimal | None] = {}
            spent = Decimal(0)
            for row in rows:
                if row["kind"] == "settlement":
                    spent += Decimal(str(row["amount_usd"]))
                    active[str(row["operation_id"])] = None
                elif row["kind"] == "reservation":
                    active[str(row["operation_id"])] = Decimal(str(row["amount_usd"]))
                elif row["kind"] == "release":
                    active[str(row["operation_id"])] = None
            if active.get(operation_id) is not None:
                return
            reserved = sum(
                (value for value in active.values() if value is not None),
                start=Decimal(0),
            )
            if spent + reserved + amount > self.maximum:
                raise BudgetExceeded(operation_id)
            entry = CostEntry(
                operation_id=operation_id,
                kind="reservation",
                amount_usd=amount,
                provider=provider,
                model=model,
            )
            self._insert(connection, entry)
            append_jsonl(self.path, entry)

    def settle(
        self,
        operation_id: str,
        amount: Decimal,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        media_seconds: float = 0,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        amount = Decimal(amount)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._database_rows(connection)
            active: dict[str, Decimal | None] = {}
            spent = Decimal(0)
            for row in rows:
                if row["kind"] == "settlement":
                    spent += Decimal(str(row["amount_usd"]))
                    active[str(row["operation_id"])] = None
                elif row["kind"] == "reservation":
                    active[str(row["operation_id"])] = Decimal(str(row["amount_usd"]))
                elif row["kind"] == "release":
                    active[str(row["operation_id"])] = None
            active[operation_id] = None
            other_reserved = sum(
                (value for value in active.values() if value is not None),
                start=Decimal(0),
            )
            if spent + other_reserved + amount > self.maximum:
                raise BudgetExceeded(operation_id)
            entry = CostEntry(
                operation_id=operation_id,
                kind="settlement",
                amount_usd=amount,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                media_seconds=media_seconds,
                provider=provider,
                model=model,
            )
            self._insert(connection, entry)
            append_jsonl(self.path, entry)

    def release(self, operation_id: str) -> None:
        entry = CostEntry(
            operation_id=operation_id, kind="release", amount_usd=Decimal(0)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert(connection, entry)
            append_jsonl(self.path, entry)

    @property
    def provider_operations(self) -> int:
        operations: dict[str, str] = {}
        for row in self._database_rows():
            if not row["provider"]:
                continue
            if row["kind"] == "reservation":
                operations[str(row["operation_id"])] = "active"
            elif row["kind"] == "settlement":
                operations[str(row["operation_id"])] = "settled"
            elif row["kind"] == "release":
                operations.pop(str(row["operation_id"]), None)
        return len(operations)

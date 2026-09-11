"""Public audit events and enforceable cost reservations."""

from __future__ import annotations

import re
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

    @property
    def _rows(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    @property
    def spent(self) -> Decimal:
        return sum(
            (
                Decimal(str(row["amount_usd"]))
                for row in self._rows
                if row["kind"] == "settlement"
            ),
            start=Decimal(0),
        )

    @property
    def reserved(self) -> Decimal:
        reservations: dict[str, Decimal | None] = {}
        for row in self._rows:
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

    def reserve(self, operation_id: str, amount: Decimal) -> None:
        amount = Decimal(amount)
        active_reservation = False
        for row in self._rows:
            if row["operation_id"] != operation_id:
                continue
            if row["kind"] == "reservation":
                active_reservation = True
            elif row["kind"] in {"settlement", "release"}:
                active_reservation = False
        if active_reservation:
            return
        if amount > self.available:
            raise BudgetExceeded(operation_id)
        append_jsonl(
            self.path,
            CostEntry(operation_id=operation_id, kind="reservation", amount_usd=amount),
        )

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
        projected = self.spent + amount
        if projected > self.maximum:
            raise BudgetExceeded(operation_id)
        append_jsonl(
            self.path,
            CostEntry(
                operation_id=operation_id,
                kind="settlement",
                amount_usd=amount,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                media_seconds=media_seconds,
                provider=provider,
                model=model,
            ),
        )

    def release(self, operation_id: str) -> None:
        append_jsonl(
            self.path,
            CostEntry(operation_id=operation_id, kind="release", amount_usd=Decimal(0)),
        )

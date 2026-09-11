from decimal import Decimal

import pytest

from vc_trace_collector.audit import AuditLog, BudgetExceeded, BudgetLedger, redact
from vc_trace_collector.models import AuditEvent, EventStatus
from vc_trace_collector.storage import read_jsonl


def test_budget_refuses_operation_before_limit_is_exceeded(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "costs.jsonl", maximum=Decimal("1.00"))
    ledger.reserve("first", Decimal("0.75"))

    with pytest.raises(BudgetExceeded):
        ledger.reserve("second", Decimal("0.26"))


def test_budget_settlement_releases_unused_reservation(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "costs.jsonl", maximum=Decimal("1.00"))
    ledger.reserve("first", Decimal("0.75"))
    ledger.settle("first", Decimal("0.20"), input_tokens=10, output_tokens=5)

    assert ledger.spent == Decimal("0.20")
    assert ledger.available == Decimal("0.80")


def test_audit_redacts_secrets_recursively(tmp_path) -> None:
    log = AuditLog(tmp_path / "events.jsonl")
    log.append(
        AuditEvent(
            event_id="event-1",
            run_id="run-1",
            stage="fetch",
            action="request",
            status=EventStatus.FAILED,
            summary="provider failed",
            details={"Authorization": "Bearer secret", "nested": {"cookie": "abc"}},
        )
    )

    row = read_jsonl(tmp_path / "events.jsonl")[0]
    assert row["details"]["Authorization"] == "[REDACTED]"
    assert row["details"]["nested"]["cookie"] == "[REDACTED]"


def test_redact_leaves_public_values_intact() -> None:
    assert redact({"url": "https://example.test", "token_count": 12}) == {
        "url": "https://example.test",
        "token_count": 12,
    }

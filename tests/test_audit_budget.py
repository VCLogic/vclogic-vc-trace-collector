from concurrent.futures import ThreadPoolExecutor
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


def test_redact_scrubs_secret_values_embedded_in_error_messages() -> None:
    cleaned = redact(
        {"message": "provider rejected Authorization: Bearer top-secret-token-value"}
    )

    assert "top-secret-token-value" not in cleaned["message"]
    assert "[REDACTED]" in cleaned["message"]


def test_redact_scrubs_common_token_and_url_secret_shapes() -> None:
    cleaned = redact(
        "failed sk-abcdefgh123456 "
        "https://example.test/?api_key=top-secret&token=also-secret"
    )

    assert "abcdefgh" not in cleaned
    assert "top-secret" not in cleaned
    assert "also-secret" not in cleaned
    assert cleaned.count("[REDACTED]") == 3


def test_released_reservation_is_reestablished_before_retry(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "costs.jsonl", maximum=Decimal("1.00"))
    ledger.reserve("retry", Decimal("0.75"))
    ledger.release("retry")
    ledger.reserve("other", Decimal("0.50"))

    with pytest.raises(BudgetExceeded):
        ledger.reserve("retry", Decimal("0.75"))


def test_settlement_preserves_other_active_reservations(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "costs.jsonl", maximum=Decimal("1.00"))
    ledger.reserve("first", Decimal("0.60"))
    ledger.reserve("second", Decimal("0.40"))

    with pytest.raises(BudgetExceeded):
        ledger.settle("first", Decimal("0.70"))


def test_concurrent_reservations_cannot_oversubscribe_budget(tmp_path) -> None:
    path = tmp_path / "costs.jsonl"

    def reserve(operation_id: str) -> bool:
        try:
            BudgetLedger(path, maximum=Decimal("1.00")).reserve(
                operation_id, Decimal("0.75")
            )
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(reserve, ["first", "second"]))

    assert sorted(accepted) == [False, True]

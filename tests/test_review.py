import pytest
from test_collectors import CollectorRegistry, StaticCollector, candidate, context, plan

from vc_trace_collector.collectors import (
    ReviewRequired,
    apply_decisions,
    collect_approved_sources,
)
from vc_trace_collector.models import ApprovalStatus, SourceDecision


def test_collection_refuses_unreviewed_plan(tmp_path) -> None:
    pending = candidate("pending", status=ApprovalStatus.PENDING)

    with pytest.raises(ReviewRequired):
        collect_approved_sources(
            plan(pending),
            context=context(tmp_path),
            registry=CollectorRegistry([StaticCollector()]),
        )


def test_review_decision_updates_candidate_and_preserves_reason() -> None:
    source_plan = plan(candidate("pending", status=ApprovalStatus.PENDING))
    updated = apply_decisions(
        source_plan,
        [
            SourceDecision(
                candidate_id="pending",
                status=ApprovalStatus.APPROVED,
                reason="Confirmed first-person source",
                decided_by="reviewer@example.test",
            )
        ],
    )

    assert updated.candidates[0].approval_status == ApprovalStatus.APPROVED
    assert updated.candidates[0].decision_reason == "Confirmed first-person source"


def test_review_rejects_unknown_candidate_id() -> None:
    with pytest.raises(KeyError):
        apply_decisions(
            plan(candidate("known", status=ApprovalStatus.PENDING)),
            [
                SourceDecision(
                    candidate_id="missing",
                    status=ApprovalStatus.REJECTED,
                    reason="Wrong person",
                    decided_by="reviewer",
                )
            ],
        )

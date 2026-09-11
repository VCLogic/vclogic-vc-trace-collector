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


def test_review_decision_can_assert_material_role() -> None:
    source_plan = plan(candidate("pending", status=ApprovalStatus.PENDING))

    updated = apply_decisions(
        source_plan,
        [
            SourceDecision(
                candidate_id="pending",
                status=ApprovalStatus.APPROVED,
                reason="Human verified target appearance",
                decided_by="reviewer",
                material_role="spoken_by_target",
            )
        ],
    )

    assert updated.candidates[0].material_role == "spoken_by_target"


def test_review_decision_can_attest_supplied_transcript_speaker() -> None:
    source_plan = plan(candidate("pending", status=ApprovalStatus.PENDING))

    updated = apply_decisions(
        source_plan,
        [
            SourceDecision(
                candidate_id="pending",
                status=ApprovalStatus.APPROVED,
                reason="Human checked the transcript against the recording",
                decided_by="reviewer",
                material_role="spoken_by_target",
                speaker_verified=True,
            )
        ],
    )

    assert updated.candidates[0].speaker_verified_by == "reviewer"


def test_review_decision_can_record_channel_programme_and_company() -> None:
    source_plan = plan(candidate("pending", status=ApprovalStatus.PENDING))

    updated = apply_decisions(
        source_plan,
        [
            SourceDecision(
                candidate_id="pending",
                status=ApprovalStatus.APPROVED,
                reason="Human annotated source provenance",
                decided_by="reviewer",
                channel="Example Channel",
                programme="Example Programme",
                company="Example Company",
            )
        ],
    )

    assert updated.candidates[0].channel == "Example Channel"
    assert updated.candidates[0].programme == "Example Programme"
    assert updated.candidates[0].company == "Example Company"


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

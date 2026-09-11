from decimal import Decimal

import pytest
from pydantic import ValidationError

from vc_trace_collector.models import (
    Affiliation,
    Confidence,
    MaterialRole,
    ResolvedIdentity,
    ResolutionStatus,
    SourceCandidate,
    SourceType,
)


def test_identity_preserves_affiliations_and_competing_hypotheses() -> None:
    identity = ResolvedIdentity(
        slug="michael-hyatt",
        canonical_name="Michael Hyatt",
        resolution_status=ResolutionStatus.PROVISIONAL,
        identity_confidence=Confidence(
            score=0.8, method="evidence_policy", version="1"
        ),
        affiliations=[Affiliation(firm="BlueCat", role="co-founder", current=True)],
        competing_hypotheses=["Michael S. Hyatt, author"],
    )

    assert identity.affiliations[0].firm == "BlueCat"
    assert identity.competing_hypotheses == ["Michael S. Hyatt, author"]


def test_source_candidate_requires_discovery_provenance() -> None:
    candidate = SourceCandidate(
        candidate_id="candidate-1",
        url="https://example.test/michael",
        canonical_url="https://example.test/michael",
        source_type=SourceType.WEB_PROFILE,
        material_role=MaterialRole.IDENTITY_EVIDENCE,
        discovery_queries=["Michael Hyatt BlueCat investor"],
        identity_confidence=Confidence(
            score=0.9, method="name_firm_match", version="1"
        ),
        source_confidence=Confidence(
            score=0.8, method="source_policy", version="1"
        ),
        estimated_cost_usd=Decimal("0.02"),
    )

    assert candidate.discovery_queries


def test_confidence_rejects_out_of_range_scores() -> None:
    with pytest.raises(ValidationError):
        Confidence(score=1.1, method="bad", version="1")

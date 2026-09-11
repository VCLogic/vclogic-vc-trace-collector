"""Opt-in contract test for the supplied Michael Hyatt identity profile."""

from __future__ import annotations

import pytest

from vc_trace_collector.models import ApprovalStatus
from vc_trace_collector.pipeline import Pipeline

PROFILE_URL = "https://www.thepitch.show/investors/michael-hyatt"
VOICE_SOURCE_URL = (
    "https://podcasts.apple.com/us/podcast/the-entrepreneurial-journey-with-serial/"
    "id1448289455?i=1000491433138"
)


@pytest.mark.live
def test_michael_hyatt_profile_resolves_identity_but_is_firewalled(tmp_path) -> None:
    result = Pipeline(tmp_path).discover(
        name="Michael Hyatt",
        known_profile_url=PROFILE_URL,
        source_urls=[VOICE_SOURCE_URL],
    )

    assert result.identity.canonical_name == "Michael Hyatt"
    assert any(
        affiliation.firm == "BlueCat" for affiliation in result.identity.affiliations
    )
    assert any(
        "publishing executive" in item for item in result.identity.competing_hypotheses
    )

    pitch_source = next(
        candidate
        for candidate in result.source_plan.candidates
        if candidate.canonical_url == PROFILE_URL
    )
    assert pitch_source.approval_status == ApprovalStatus.REJECTED
    assert "pitch" in (pitch_source.decision_reason or "").casefold()

    voice_source = next(
        candidate
        for candidate in result.source_plan.candidates
        if candidate.canonical_url == VOICE_SOURCE_URL
    )
    assert voice_source.source_type == "podcast"
    assert voice_source.material_role == "reference_voice"
    assert voice_source.identity_confidence.score == 0.9
    assert any(
        item.source_candidate_id == voice_source.candidate_id
        for item in result.reference_voice_candidates
    )

    workspace = tmp_path / "michael-hyatt"
    assert (workspace / "identity/resolved_identity.json").is_file()
    assert (workspace / "raw/web").is_dir()
    assert (workspace / "audit/events.jsonl").is_file()

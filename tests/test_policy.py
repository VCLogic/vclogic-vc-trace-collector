from hashlib import sha256

import pytest

from vc_trace_collector.models import (
    CanonicalDocument,
    Confidence,
    InclusionStatus,
    MaterialRole,
    SourceType,
    SpeakerAttribution,
    SpeakerStatus,
)
from vc_trace_collector.policy import (
    RuleSet,
    UnsafeUrl,
    canonicalize_url,
    eligible_for_corpus,
    validate_public_url,
)


def test_pitch_profile_is_identity_evidence_but_excluded_from_corpus() -> None:
    decision = RuleSet.pitch_default().evaluate(
        url="https://www.thepitch.show/investors/michael-hyatt",
        title="Michael Hyatt | The Pitch",
        channel=None,
        text="investment episodes and outcomes",
        stage="discovery",
    )

    assert decision.status == InclusionStatus.EXCLUDED
    assert decision.rule_id == "exclude-the-pitch"


def test_rule_matches_subdomains_but_not_lookalike_domains() -> None:
    rules = RuleSet.pitch_default()
    assert rules.evaluate(url="https://media.thepitch.show/x", stage="discovery").status == "excluded"
    assert rules.evaluate(url="https://thepitch.show.example.org/x", stage="discovery").status == "included"


def test_canonicalize_url_removes_fragments_and_tracking_parameters() -> None:
    assert canonicalize_url(
        "HTTPS://Example.COM:443/a/?utm_source=x&useful=2#fragment"
    ) == "https://example.com/a?useful=2"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/private",
        "https://user:password@example.com/private",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        validate_public_url(url)


def _document(role: MaterialRole, status: SpeakerStatus) -> CanonicalDocument:
    digest = sha256(b"text").hexdigest()
    return CanonicalDocument(
        source_item_id="source-1",
        content_hash=digest,
        document_version_id="version-1",
        investor_slug="michael-hyatt",
        source_candidate_id="candidate-1",
        raw_artifact_ids=["sha256:raw"],
        canonical_url="https://example.test/article",
        source_type=SourceType.WEB_ARTICLE,
        modality="written",
        material_role=role,
        text="text",
        extraction_method="html",
        identity_confidence=Confidence(score=0.9, method="test", version="1"),
        inclusion_status=InclusionStatus.INCLUDED,
        speaker_attribution=SpeakerAttribution(status=status),
    )


def test_corpus_gate_accepts_authored_writing_without_speaker_attribution() -> None:
    assert eligible_for_corpus(
        _document(MaterialRole.AUTHORED_BY_TARGET, SpeakerStatus.NOT_APPLICABLE)
    )


def test_corpus_gate_rejects_uncertain_target_speech() -> None:
    assert not eligible_for_corpus(
        _document(MaterialRole.SPOKEN_BY_TARGET, SpeakerStatus.UNCERTAIN)
    )

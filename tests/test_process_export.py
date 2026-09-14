from hashlib import sha256

import pytest
from pydantic import ValidationError

from vc_trace_collector.av import AlignedText, TargetSpeechResult
from vc_trace_collector.export import export_persona_sources, export_workspace
from vc_trace_collector.models import (
    CanonicalDocument,
    Confidence,
    InclusionStatus,
    MaterialRole,
    SourceCandidate,
    SourceType,
    SpeakerAttribution,
    SpeakerStatus,
    TranscriptInfo,
)
from vc_trace_collector.policy import ExclusionRule, RuleSet, eligible_for_corpus
from vc_trace_collector.process import (
    deduplicate,
    load_artifact_records,
    normalize_text,
    process_artifact,
    target_speech_document,
)
from vc_trace_collector.storage import ArtifactStore, read_json, read_jsonl


def document(
    identifier: str,
    text: str,
    *,
    role: MaterialRole = MaterialRole.AUTHORED_BY_TARGET,
    inclusion: InclusionStatus = InclusionStatus.INCLUDED,
    speaker: SpeakerStatus = SpeakerStatus.NOT_APPLICABLE,
    source_type: SourceType = SourceType.WEB_ARTICLE,
) -> CanonicalDocument:
    normalized = normalize_text(text)
    content_hash = sha256(normalized.encode()).hexdigest()
    return CanonicalDocument(
        source_item_id=f"source:{identifier}",
        content_hash=content_hash,
        document_version_id=f"version:{identifier}",
        investor_slug="michael-hyatt",
        source_candidate_id=f"candidate:{identifier}",
        raw_artifact_ids=[f"sha256:{identifier}"],
        canonical_url=f"https://example.test/{identifier}",
        source_type=source_type,
        modality="audiovisual" if role == MaterialRole.SPOKEN_BY_TARGET else "written",
        material_role=role,
        title=identifier,
        text=normalized,
        extraction_method="fixture",
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        inclusion_status=inclusion,
        exclusion_reason="test exclusion"
        if inclusion == InclusionStatus.EXCLUDED
        else None,
        speaker_attribution=SpeakerAttribution(status=speaker),
    )


def test_normalization_is_stable_without_rewriting_words() -> None:
    assert normalize_text("  One\r\n\r\n\r\nTwo\u00a0words  ") == "One\n\nTwo words"


def test_artifact_loader_does_not_parse_raw_metadata_payload_as_sidecar(
    tmp_path,
) -> None:
    stored = ArtifactStore(tmp_path).put_bytes(
        b'{"title": "Raw yt-dlp metadata", "formats": []}',
        category="video",
        suffix=".metadata.json",
        source_url="https://www.youtube.com/watch?v=fixture",
        mime_type="application/json",
        collection_method="yt_dlp_metadata",
        original_metadata={"candidate_id": "candidate:fixture"},
    )

    records = load_artifact_records(tmp_path)

    assert records == [stored.record]


def test_artifact_loader_rejects_malformed_provenance_sidecar(tmp_path) -> None:
    stored = ArtifactStore(tmp_path).put_bytes(
        b"audio",
        category="video",
        suffix=".webm",
        collection_method="yt_dlp_audio",
    )
    stored.metadata_path.write_text("{}")

    with pytest.raises(ValidationError):
        load_artifact_records(tmp_path)


def test_quality_counts_merged_target_intervals_not_whole_recording(tmp_path) -> None:
    item = document(
        "talk",
        "Verified target speech",
        role=MaterialRole.SPOKEN_BY_TARGET,
        speaker=SpeakerStatus.VERIFIED_HUMAN,
        source_type=SourceType.PODCAST,
    )
    item.speakers = ["Michael Hyatt"]
    item.transcript = TranscriptInfo(method="existing_transcript")
    item.original_metadata = {
        "media_seconds": 100,
        "target_segments": [
            {"start_seconds": 0, "end_seconds": 3},
            {"start_seconds": 2, "end_seconds": 5},
        ],
    }

    export_workspace(
        tmp_path,
        investor_slug="michael-hyatt",
        identity_id="identity:michael",
        documents=[item],
        config_hash="config",
        exclusion_rules_hash="rules",
    )

    quality = read_json(tmp_path / "quality_report.json")
    assert quality["metrics"]["verified_target_speech_seconds"] == 5


def test_human_verified_supplied_transcript_is_eligible_target_speech(tmp_path) -> None:
    candidate = SourceCandidate(
        candidate_id="candidate:transcript",
        url=(tmp_path / "transcript.txt").as_uri(),
        canonical_url=(tmp_path / "transcript.txt").as_uri(),
        source_type=SourceType.SUPPLIED,
        material_role=MaterialRole.SPOKEN_BY_TARGET,
        discovery_queries=["operator supplied"],
        identity_confidence=Confidence(score=0.95, method="human", version="1"),
        source_confidence=Confidence(score=0.95, method="human", version="1"),
        approval_status="approved",
        speaker_verified_by="reviewer",
    )
    artifact = (
        ArtifactStore(tmp_path)
        .put_bytes(
            b"I focus on capital-efficient companies.",
            category="supplied",
            suffix=".txt",
            source_path=str(tmp_path / "transcript.txt"),
            mime_type="text/plain",
            original_metadata={"candidate_id": candidate.candidate_id},
        )
        .record
    )

    result = process_artifact(
        tmp_path,
        artifact,
        candidate,
        investor_slug="michael-hyatt",
        target_name="Michael Hyatt",
        rules=RuleSet.pitch_default(),
    )

    assert len(result) == 1
    assert result[0].speaker_attribution.status == SpeakerStatus.VERIFIED_HUMAN
    assert eligible_for_corpus(result[0])


def test_uncertain_model_speech_is_routed_to_review_not_corpus(tmp_path) -> None:
    candidate = SourceCandidate(
        candidate_id="candidate:talk",
        url="https://example.test/talk",
        canonical_url="https://example.test/talk",
        source_type=SourceType.PODCAST,
        material_role=MaterialRole.SPOKEN_BY_TARGET,
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        source_confidence=Confidence(score=0.95, method="test", version="1"),
        approval_status="approved",
    )
    artifact = (
        ArtifactStore(tmp_path)
        .put_bytes(
            b"audio",
            category="podcast",
            suffix=".mp3",
            source_url=candidate.url,
            mime_type="audio/mpeg",
            original_metadata={"candidate_id": candidate.candidate_id},
        )
        .record
    )
    attribution = SpeakerAttribution(
        status=SpeakerStatus.UNCERTAIN,
        speaker_label="SPEAKER_1",
        score=0.8,
        runner_up_score=0.75,
        margin=0.05,
        minimum_score=0.75,
        minimum_margin=0.1,
    )
    segment = AlignedText(
        start_seconds=1,
        end_seconds=3,
        text="Potential target speech",
        speaker_label="SPEAKER_1",
        overlap_seconds=2,
    )

    result = target_speech_document(
        investor_slug="michael-hyatt",
        target_name="Michael Hyatt",
        artifact=artifact,
        candidate=candidate,
        result=TargetSpeechResult(
            transcript=TranscriptInfo(method="speech_to_text"),
            attribution=attribution,
            aligned_segments=[segment],
            target_segments=[segment],
            media_seconds=3,
        ),
        rules=RuleSet.pitch_default(),
    )

    assert result is not None
    assert result.inclusion_status == InclusionStatus.REVIEW_REQUIRED
    assert not eligible_for_corpus(result)


def test_channel_rule_is_applied_during_document_processing(tmp_path) -> None:
    candidate = SourceCandidate(
        candidate_id="candidate:blocked-channel",
        url=(tmp_path / "article.txt").as_uri(),
        canonical_url=(tmp_path / "article.txt").as_uri(),
        source_type=SourceType.SUPPLIED,
        material_role=MaterialRole.AUTHORED_BY_TARGET,
        channel="Outcome Reveal Show",
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        source_confidence=Confidence(score=0.95, method="test", version="1"),
        approval_status="approved",
    )
    artifact = (
        ArtifactStore(tmp_path)
        .put_bytes(
            b"Public investor writing",
            category="supplied",
            suffix=".txt",
            source_path=str(tmp_path / "article.txt"),
            mime_type="text/plain",
            original_metadata={"candidate_id": candidate.candidate_id},
        )
        .record
    )
    rules = RuleSet(
        [
            ExclusionRule(
                rule_id="blocked-channel",
                action="exclude",
                reason="Evaluation leakage channel",
                channels=["Outcome Reveal Show"],
            )
        ]
    )

    result = process_artifact(
        tmp_path,
        artifact,
        candidate,
        investor_slug="michael-hyatt",
        target_name="Michael Hyatt",
        rules=rules,
    )

    assert result[0].inclusion_status == InclusionStatus.EXCLUDED
    assert result[0].exclusion_reason == "Evaluation leakage channel"


def test_exact_duplicate_retains_relationship() -> None:
    documents = deduplicate(
        [document("one", "same text"), document("two", "same text")]
    )

    assert documents[0].duplicate_of is None
    assert documents[1].duplicate_of == documents[0].document_version_id
    assert documents[1].inclusion_status == InclusionStatus.DUPLICATE


def test_near_duplicate_is_detected() -> None:
    common = " ".join(f"word{i}" for i in range(100))
    documents = deduplicate(
        [document("one", common), document("two", common + " minor addition")],
        near_threshold=0.95,
    )

    assert documents[1].duplicate_of == documents[0].document_version_id


def test_eligible_duplicate_is_preferred_over_earlier_excluded_copy() -> None:
    excluded = document(
        "excluded", "same useful text", inclusion=InclusionStatus.EXCLUDED
    )
    included = document("included", "same useful text")

    documents = deduplicate([excluded, included])

    assert documents[1].inclusion_status == InclusionStatus.INCLUDED
    assert documents[1].duplicate_of is None
    assert documents[0].inclusion_status == InclusionStatus.EXCLUDED
    assert documents[0].duplicate_of == documents[1].document_version_id


def test_legacy_export_omits_excluded_and_uncertain(tmp_path) -> None:
    export_persona_sources(
        tmp_path,
        [
            document("blog", "public writing"),
            document("excluded", "pitch outcome", inclusion=InclusionStatus.EXCLUDED),
            document(
                "uncertain",
                "possibly target speech",
                role=MaterialRole.SPOKEN_BY_TARGET,
                speaker=SpeakerStatus.UNCERTAIN,
                source_type=SourceType.YOUTUBE,
            ),
        ],
    )

    blog = read_jsonl(tmp_path / "blog.jsonl")
    talks = read_jsonl(tmp_path / "talks.jsonl")
    assert list(blog[0]) == ["doc_id", "full_text", "source", "title"]
    assert talks == []
    assert (tmp_path / "_manifest.json").exists()


def test_accepted_target_speech_exports_as_talk(tmp_path) -> None:
    export_persona_sources(
        tmp_path,
        [
            document(
                "video123",
                "verified target speech",
                role=MaterialRole.SPOKEN_BY_TARGET,
                speaker=SpeakerStatus.ACCEPTED_MODEL,
                source_type=SourceType.YOUTUBE,
            )
        ],
    )

    talks = read_jsonl(tmp_path / "talks.jsonl")
    assert talks[0]["video_id"] == "video123"
    assert talks[0]["text"] == "verified target speech"


def test_workspace_places_legacy_export_at_investor_root(tmp_path) -> None:
    export_workspace(
        tmp_path,
        investor_slug="michael-hyatt",
        identity_id="identity:michael",
        documents=[document("blog", "public writing")],
        config_hash="config-hash",
        exclusion_rules_hash="rules-hash",
    )

    assert read_jsonl(tmp_path / "blog.jsonl")[0]["full_text"] == "public writing"
    assert read_jsonl(tmp_path / "talks.jsonl") == []
    assert (tmp_path / "_manifest.json").is_file()


def test_quality_report_fails_required_source_failures_unless_partial_is_explicit(
    tmp_path,
) -> None:
    blog = document("blog", "public writing")
    blog.authors = ["Michael Hyatt"]
    kwargs = {
        "investor_slug": "michael-hyatt",
        "identity_id": "identity:michael",
        "documents": [blog],
        "config_hash": "config-hash",
        "exclusion_rules_hash": "rules-hash",
        "run_failures": 1,
    }

    export_workspace(tmp_path, **kwargs)
    assert read_json(tmp_path / "quality_report.json")["passed"] is False

    export_workspace(tmp_path, **kwargs, allow_partial_run=True)
    quality = read_json(tmp_path / "quality_report.json")
    assert quality["passed"] is True
    assert quality["counts"]["failures"] == 1
    assert quality["checks"]["approved_work_complete"] is False
    assert quality["checks"]["partial_run_policy_satisfied"] is True


def test_feed_entry_by_different_author_requires_review(tmp_path) -> None:
    feed = b"""<?xml version='1.0'?><rss version='2.0'><channel><item>
      <title>Guest post</title><link>https://example.test/guest</link>
      <author>Other Person</author><description>Not written by the investor.</description>
    </item></channel></rss>"""
    stored = ArtifactStore(tmp_path).put_bytes(
        feed,
        category="web",
        suffix=".xml",
        source_url="https://example.test/feed",
        mime_type="application/rss+xml",
        original_metadata={"candidate_id": "candidate:feed"},
    )
    candidate = SourceCandidate(
        candidate_id="candidate:feed",
        url="https://example.test/feed",
        canonical_url="https://example.test/feed",
        source_type=SourceType.RSS_FEED,
        material_role=MaterialRole.AUTHORED_BY_TARGET,
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        source_confidence=Confidence(score=0.95, method="test", version="1"),
        approval_status="approved",
    )

    result = process_artifact(
        tmp_path,
        stored.record,
        candidate,
        investor_slug="michael-hyatt",
        target_name="Michael Hyatt",
    )

    assert result[0].inclusion_status == InclusionStatus.REVIEW_REQUIRED
    assert "author" in (result[0].exclusion_reason or "").casefold()


def test_excluded_fetched_url_cannot_be_hidden_by_declared_canonical(tmp_path) -> None:
    html = b"""<html><head>
      <link rel='canonical' href='https://example.test/clean'>
      <meta name='author' content='Michael Hyatt'></head>
      <body><main><h1>Pitch outcome</h1><p>Target material.</p></main></body></html>"""
    stored = ArtifactStore(tmp_path).put_bytes(
        html,
        category="web",
        suffix=".html",
        source_url="https://www.thepitch.show/poisoned",
        mime_type="text/html",
        original_metadata={"candidate_id": "candidate:poisoned"},
    )
    candidate = SourceCandidate(
        candidate_id="candidate:poisoned",
        url="https://www.thepitch.show/poisoned",
        canonical_url="https://www.thepitch.show/poisoned",
        source_type=SourceType.WEB_ARTICLE,
        material_role=MaterialRole.AUTHORED_BY_TARGET,
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        source_confidence=Confidence(score=0.95, method="test", version="1"),
        approval_status="approved",
    )

    result = process_artifact(
        tmp_path,
        stored.record,
        candidate,
        investor_slug="michael-hyatt",
        target_name="Michael Hyatt",
        rules=RuleSet.pitch_default(),
    )

    assert result[0].canonical_url == "https://example.test/clean"
    assert result[0].inclusion_status == InclusionStatus.EXCLUDED
    assert "pitch" in (result[0].exclusion_reason or "").casefold()

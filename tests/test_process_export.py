from hashlib import sha256

from vc_trace_collector.export import export_persona_sources
from vc_trace_collector.models import (
    CanonicalDocument,
    Confidence,
    InclusionStatus,
    MaterialRole,
    SourceType,
    SpeakerAttribution,
    SpeakerStatus,
)
from vc_trace_collector.process import deduplicate, normalize_text
from vc_trace_collector.storage import read_jsonl


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
        exclusion_reason="test exclusion" if inclusion == InclusionStatus.EXCLUDED else None,
        speaker_attribution=SpeakerAttribution(status=speaker),
    )


def test_normalization_is_stable_without_rewriting_words() -> None:
    assert normalize_text("  One\r\n\r\n\r\nTwo\u00a0words  ") == "One\n\nTwo words"


def test_exact_duplicate_retains_relationship() -> None:
    documents = deduplicate([document("one", "same text"), document("two", "same text")])

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

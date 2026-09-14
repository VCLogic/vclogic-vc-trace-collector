from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vc_trace_collector.av import (
    DiarizationResult,
    DiarizedTurn,
    TimedText,
)
from vc_trace_collector.av_cache import (
    AttributionCacheRecord,
    DiarizationCacheRecord,
    TranscriptCacheRecord,
    attribution_cache_key,
    diarization_cache_key,
    transcript_cache_key,
)
from vc_trace_collector.models import SpeakerAttribution, TranscriptInfo

ARTIFACT_HASH = "a" * 64
CREATED = datetime(2026, 9, 14, tzinfo=UTC)


def test_reference_change_only_changes_attribution_key() -> None:
    diarization = diarization_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="pyannote",
        model="speaker-diarization-3.1",
        model_version="1",
    )
    transcript = transcript_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="whisper",
        model="turbo",
        model_version="1",
        diarization_cache_key=diarization,
    )

    first = attribution_cache_key(
        diarization_cache_key=diarization,
        reference_profile_ids=["one"],
        minimum_score=0.75,
        minimum_margin=0.10,
    )
    second = attribution_cache_key(
        diarization_cache_key=diarization,
        reference_profile_ids=["two", "one"],
        minimum_score=0.75,
        minimum_margin=0.10,
    )

    assert first != second
    assert diarization == diarization_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="pyannote",
        model="speaker-diarization-3.1",
        model_version="1",
    )
    assert transcript == transcript_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="whisper",
        model="turbo",
        model_version="1",
        diarization_cache_key=diarization,
    )


def test_model_changes_invalidate_only_their_dependent_stage_keys() -> None:
    diarization_one = diarization_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="pyannote",
        model="one",
        model_version="1",
    )
    diarization_two = diarization_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="pyannote",
        model="two",
        model_version="1",
    )
    transcript_one = transcript_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="whisper",
        model="tiny",
        model_version="1",
        diarization_cache_key=diarization_one,
    )
    transcript_two = transcript_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="whisper",
        model="turbo",
        model_version="1",
        diarization_cache_key=diarization_one,
    )

    assert diarization_one != diarization_two
    assert transcript_one != transcript_two
    assert transcript_one != transcript_cache_key(
        artifact_sha256=ARTIFACT_HASH,
        provider="whisper",
        model="tiny",
        model_version="1",
        diarization_cache_key=diarization_two,
    )


def test_stage_records_are_strict_and_round_trip() -> None:
    diarization = DiarizationCacheRecord(
        cache_key="d-key",
        artifact_id="sha256:" + ARTIFACT_HASH,
        artifact_sha256=ARTIFACT_HASH,
        provider="fixture",
        model="fixture-diarization",
        model_version="1",
        result=DiarizationResult(
            model="fixture-diarization",
            turns=[
                DiarizedTurn(
                    start_seconds=0, end_seconds=1, speaker_label="VC"
                )
            ],
            speaker_embeddings={"VC": [1.0, 0.0]},
        ),
        created_at=CREATED,
    )
    transcript = TranscriptCacheRecord(
        cache_key="t-key",
        artifact_id=diarization.artifact_id,
        artifact_sha256=ARTIFACT_HASH,
        provider="fixture",
        model="fixture-transcript",
        model_version="1",
        diarization_cache_key=diarization.cache_key,
        transcript=TranscriptInfo(
            method="speech_to_text", provider="fixture", model="fixture-transcript"
        ),
        segments=[TimedText(start_seconds=0, end_seconds=1, text="hello")],
        created_at=CREATED,
    )
    attribution = AttributionCacheRecord(
        cache_key="a-key",
        diarization_cache_key=diarization.cache_key,
        reference_profile_ids=["voice-profile:one"],
        minimum_score=0.75,
        minimum_margin=0.1,
        attribution=SpeakerAttribution(status="uncertain"),
        created_at=CREATED,
    )

    assert DiarizationCacheRecord.model_validate_json(
        diarization.model_dump_json()
    ) == diarization
    assert TranscriptCacheRecord.model_validate_json(
        transcript.model_dump_json()
    ) == transcript
    assert AttributionCacheRecord.model_validate_json(
        attribution.model_dump_json()
    ) == attribution
    with pytest.raises(ValidationError):
        DiarizationCacheRecord.model_validate(
            {**diarization.model_dump(mode="json"), "unexpected": True}
        )

"""Typed, content-addressed audiovisual stage cache records."""

from __future__ import annotations

from hashlib import sha256

from pydantic import AwareDatetime, Field

from .av import DiarizationResult, TimedText
from .models import SpeakerAttribution, StrictModel, TranscriptInfo, utc_now
from .storage import canonical_json

SEGMENTATION_VERSION = "diarized-turns-v1"
ATTRIBUTION_METHOD = "max_verified_reference"
ATTRIBUTION_VERSION = "1"


def _key(kind: str, payload: dict) -> str:
    digest = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def diarization_cache_key(
    *,
    artifact_sha256: str,
    provider: str,
    model: str,
    model_version: str,
) -> str:
    return _key(
        "av-diarization",
        {
            "schema_version": "1.0",
            "artifact_sha256": artifact_sha256,
            "provider": provider,
            "model": model,
            "model_version": model_version,
        },
    )


def transcript_cache_key(
    *,
    artifact_sha256: str,
    provider: str,
    model: str,
    model_version: str,
    diarization_cache_key: str,
    segmentation_version: str = SEGMENTATION_VERSION,
) -> str:
    return _key(
        "av-transcript",
        {
            "schema_version": "1.0",
            "artifact_sha256": artifact_sha256,
            "provider": provider,
            "model": model,
            "model_version": model_version,
            "diarization_cache_key": diarization_cache_key,
            "segmentation_version": segmentation_version,
        },
    )


def attribution_cache_key(
    *,
    diarization_cache_key: str,
    reference_profile_ids: list[str],
    minimum_score: float,
    minimum_margin: float,
    aggregation_method: str = ATTRIBUTION_METHOD,
    aggregation_version: str = ATTRIBUTION_VERSION,
) -> str:
    return _key(
        "av-attribution",
        {
            "schema_version": "1.0",
            "diarization_cache_key": diarization_cache_key,
            "reference_profile_ids": sorted(set(reference_profile_ids)),
            "minimum_score": minimum_score,
            "minimum_margin": minimum_margin,
            "aggregation_method": aggregation_method,
            "aggregation_version": aggregation_version,
        },
    )


class DiarizationCacheRecord(StrictModel):
    schema_version: str = "1.0"
    cache_key: str
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str
    model: str
    model_version: str
    result: DiarizationResult
    created_at: AwareDatetime = Field(default_factory=utc_now)


class TranscriptCacheRecord(StrictModel):
    schema_version: str = "1.0"
    cache_key: str
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str
    model: str
    model_version: str
    diarization_cache_key: str
    segmentation_version: str = SEGMENTATION_VERSION
    transcript: TranscriptInfo
    segments: list[TimedText]
    migration_source: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class AttributionCacheRecord(StrictModel):
    schema_version: str = "1.0"
    cache_key: str
    diarization_cache_key: str
    reference_profile_ids: list[str]
    minimum_score: float = Field(ge=-1, le=1)
    minimum_margin: float = Field(ge=0, le=2)
    aggregation_method: str = ATTRIBUTION_METHOD
    aggregation_version: str = ATTRIBUTION_VERSION
    attribution: SpeakerAttribution
    created_at: AwareDatetime = Field(default_factory=utc_now)

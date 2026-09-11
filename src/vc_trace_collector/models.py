"""Typed records shared by every pipeline stage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolutionStatus(StrEnum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    REJECTED = "rejected"


class SourceType(StrEnum):
    WEB_PROFILE = "web_profile"
    WEB_ARTICLE = "web_article"
    RSS_FEED = "rss_feed"
    SUBSTACK = "substack"
    MEDIUM = "medium"
    YOUTUBE = "youtube"
    PODCAST = "podcast"
    X = "x"
    LINKEDIN_EXPORT = "linkedin_export"
    SUPPLIED = "supplied"


class MaterialRole(StrEnum):
    IDENTITY_EVIDENCE = "identity_evidence"
    AUTHORED_BY_TARGET = "authored_by_target"
    SPOKEN_BY_TARGET = "spoken_by_target"
    THIRD_PARTY = "third_party"
    REFERENCE_VOICE = "reference_voice"
    UNKNOWN = "unknown"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    REJECTED = "rejected"


class InclusionStatus(StrEnum):
    PENDING = "pending"
    INCLUDED = "included"
    EXCLUDED = "excluded"
    REVIEW_REQUIRED = "review_required"
    DUPLICATE = "duplicate"


class SpeakerStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    VERIFIED_HUMAN = "verified_human"
    ACCEPTED_MODEL = "accepted_model"
    UNCERTAIN = "uncertain"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class ReferenceVoiceStatus(StrEnum):
    HIGH_CONFIDENCE_CANDIDATE = "high_confidence_candidate"
    VERIFIED_HUMAN = "verified_human"
    REJECTED = "rejected"


class EventStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class Confidence(StrictModel):
    score: float = Field(ge=0.0, le=1.0)
    method: str = Field(min_length=1)
    version: str = Field(min_length=1)


class Affiliation(StrictModel):
    firm: str = Field(min_length=1)
    role: str | None = None
    current: bool | None = None
    started_at: str | None = None
    ended_at: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ResolvedIdentity(StrictModel):
    schema_version: str = "1.0"
    slug: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    affiliations: list[Affiliation] = Field(default_factory=list)
    authoritative_profiles: list[str] = Field(default_factory=list)
    resolution_status: ResolutionStatus = ResolutionStatus.PROVISIONAL
    identity_confidence: Confidence
    evidence_ids: list[str] = Field(default_factory=list)
    competing_hypotheses: list[str] = Field(default_factory=list)
    reviewed_by: str | None = None
    resolved_at: AwareDatetime = Field(default_factory=utc_now)


class IdentityEvidence(StrictModel):
    schema_version: str = "1.0"
    evidence_id: str
    url: str
    canonical_url: str
    publisher: str | None = None
    query: str
    search_provider: str
    result_rank: int | None = Field(default=None, ge=1)
    retrieved_at: AwareDatetime = Field(default_factory=utc_now)
    excerpt: str = ""
    claim: str = ""
    raw_artifact_id: str | None = None
    validation_status: str = "unreviewed"


class SourceCandidate(StrictModel):
    schema_version: str = "1.0"
    candidate_id: str
    url: str
    canonical_url: str
    source_type: SourceType
    material_role: MaterialRole = MaterialRole.UNKNOWN
    title: str | None = None
    description: str | None = None
    channel: str | None = None
    programme: str | None = None
    company: str | None = None
    discovered_via: str = "generated_query"
    discovery_queries: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    identity_confidence: Confidence
    source_confidence: Confidence
    estimated_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    estimated_media_seconds: float = Field(default=0, ge=0)
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    decision_reason: str | None = None
    speaker_verified_by: str | None = None


class SourcePlan(StrictModel):
    schema_version: str = "1.0"
    plan_id: str
    investor_slug: str
    generated_at: AwareDatetime = Field(default_factory=utc_now)
    queries: list[str] = Field(default_factory=list)
    candidates: list[SourceCandidate] = Field(default_factory=list)
    requires_review: bool = True
    estimated_cost_usd: Decimal = Decimal(0)
    estimated_media_seconds: float = 0


class SourceDecision(StrictModel):
    candidate_id: str
    status: ApprovalStatus
    reason: str
    decided_by: str
    material_role: MaterialRole | None = None
    speaker_verified: bool = False
    channel: str | None = None
    programme: str | None = None
    company: str | None = None
    decided_at: AwareDatetime = Field(default_factory=utc_now)


class RawArtifact(StrictModel):
    schema_version: str = "1.0"
    artifact_id: str
    category: str
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    mime_type: str | None = None
    source_url: str | None = None
    source_path: str | None = None
    collected_at: AwareDatetime = Field(default_factory=utc_now)
    collection_method: str
    collection_version: str = "1"
    parent_artifact_ids: list[str] = Field(default_factory=list)
    original_metadata: dict[str, Any] = Field(default_factory=dict)
    rights_notes: str | None = None


class TranscriptInfo(StrictModel):
    method: str = "none"
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    source_artifact_id: str | None = None


class SpeakerAttribution(StrictModel):
    status: SpeakerStatus = SpeakerStatus.NOT_APPLICABLE
    speaker_label: str | None = None
    score: float | None = None
    runner_up_score: float | None = None
    margin: float | None = None
    minimum_score: float | None = None
    minimum_margin: float | None = None
    diarization_model: str | None = None
    embedding_model: str | None = None
    reference_artifact_ids: list[str] = Field(default_factory=list)


class SpeechSegment(StrictModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str
    speaker_label: str | None = None
    attribution: SpeakerAttribution = Field(default_factory=SpeakerAttribution)

    @model_validator(mode="after")
    def valid_interval(self) -> SpeechSegment:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be after start_seconds")
        return self


class CanonicalDocument(StrictModel):
    schema_version: str = "1.0"
    source_item_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_version_id: str
    investor_slug: str
    source_candidate_id: str
    raw_artifact_ids: list[str] = Field(min_length=1)
    canonical_url: str | None = None
    local_source_path: str | None = None
    source_type: SourceType
    modality: str
    material_role: MaterialRole
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    published_at: str | None = None
    publication_date_precision: str | None = None
    collected_at: AwareDatetime = Field(default_factory=utc_now)
    text: str
    original_metadata: dict[str, Any] = Field(default_factory=dict)
    extraction_method: str
    extraction_version: str = "1"
    transcript: TranscriptInfo = Field(default_factory=TranscriptInfo)
    speaker_attribution: SpeakerAttribution = Field(default_factory=SpeakerAttribution)
    identity_confidence: Confidence
    inclusion_status: InclusionStatus = InclusionStatus.PENDING
    exclusion_reason: str | None = None
    duplicate_of: str | None = None


class ReferenceVoiceCandidate(StrictModel):
    candidate_id: str
    source_candidate_id: str
    source_url: str
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    detected_speakers: int | None = Field(default=None, ge=1)
    overlap_ratio: float | None = Field(default=None, ge=0, le=1)
    audio_quality_score: float | None = Field(default=None, ge=0, le=1)
    identity_confidence: Confidence
    status: ReferenceVoiceStatus = ReferenceVoiceStatus.HIGH_CONFIDENCE_CANDIDATE
    artifact_id: str | None = None
    reviewed_by: str | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> ReferenceVoiceCandidate:
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise ValueError("reference interval requires both start and end")
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("reference end_seconds must be after start_seconds")
        return self


class ReferenceVoiceProfile(StrictModel):
    investor_slug: str
    candidate_ids: list[str] = Field(min_length=1)
    artifact_ids: list[str] = Field(min_length=1)
    status: ReferenceVoiceStatus
    embedding_model: str
    embedding_model_version: str
    embedding: list[float] = Field(min_length=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ExclusionDecision(StrictModel):
    status: InclusionStatus
    rule_id: str | None = None
    rule_version: str | None = None
    stage: str
    reason: str | None = None
    matched_fields: dict[str, str] = Field(default_factory=dict)


class AuditEvent(StrictModel):
    event_id: str
    run_id: str
    stage: str
    action: str
    status: EventStatus
    timestamp: AwareDatetime = Field(default_factory=utc_now)
    actor: str = "deterministic"
    summary: str
    input_ids: list[str] = Field(default_factory=list)
    output_ids: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    attempt: int = Field(default=1, ge=1)
    details: dict[str, Any] = Field(default_factory=dict)


class CostEntry(StrictModel):
    operation_id: str
    kind: str
    amount_usd: Decimal = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    media_seconds: float = Field(default=0, ge=0)
    provider: str | None = None
    model: str | None = None
    timestamp: AwareDatetime = Field(default_factory=utc_now)


class ManifestFile(StrictModel):
    path: str
    sha256: str
    size_bytes: int
    records: int | None = None


class CollectionManifest(StrictModel):
    schema_version: str = "1.0"
    investor_slug: str
    identity_id: str
    config_hash: str
    exclusion_rules_hash: str
    files: list[ManifestFile] = Field(default_factory=list)
    corpus_documents: int = 0
    excluded_documents: int = 0
    fingerprint: str


class QualityReport(StrictModel):
    schema_version: str = "1.0"
    investor_slug: str
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RunSummary(StrictModel):
    schema_version: str = "1.0"
    run_id: str
    investor_slug: str
    status: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    stages: dict[str, str] = Field(default_factory=dict)
    collected: int = 0
    processed: int = 0
    excluded: int = 0
    failures: int = 0
    cost_usd: Decimal = Decimal(0)
    media_seconds: float = 0

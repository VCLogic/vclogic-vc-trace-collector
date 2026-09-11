"""Normalize raw artifacts into canonical, deduplicated documents."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path

from .av import TargetSpeechResult
from .extract import extract_feed, extract_page
from .models import (
    CanonicalDocument,
    InclusionStatus,
    MaterialRole,
    RawArtifact,
    SourceCandidate,
    SourceType,
    SpeakerAttribution,
    SpeakerStatus,
    TranscriptInfo,
)
from .policy import InclusionStatus as PolicyInclusionStatus
from .policy import RuleSet
from .storage import read_json


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\u00a0", " ")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _identifier(namespace: str, value: str) -> str:
    return f"{namespace}:{sha256(value.encode('utf-8')).hexdigest()}"


def _inclusion(
    candidate: SourceCandidate,
    *,
    rules: RuleSet,
    url: str | None,
    title: str | None,
    text: str,
) -> tuple[InclusionStatus, str | None]:
    policy = rules.evaluate(
        url=url,
        title=title,
        text=text,
        channel=candidate.channel,
        programme=candidate.programme,
        company=candidate.company,
        stage="post_extraction",
    )
    if policy.status == PolicyInclusionStatus.EXCLUDED:
        return InclusionStatus.EXCLUDED, policy.reason
    if policy.status == PolicyInclusionStatus.REVIEW_REQUIRED:
        return InclusionStatus.REVIEW_REQUIRED, policy.reason
    if candidate.material_role == MaterialRole.IDENTITY_EVIDENCE:
        return (
            InclusionStatus.EXCLUDED,
            "Identity evidence is not persona-corpus material",
        )
    if candidate.material_role in {
        MaterialRole.UNKNOWN,
        MaterialRole.THIRD_PARTY,
        MaterialRole.REFERENCE_VOICE,
    }:
        return (
            InclusionStatus.REVIEW_REQUIRED,
            "Material role requires review before corpus use",
        )
    return InclusionStatus.INCLUDED, None


def _make_document(
    *,
    investor_slug: str,
    artifact: RawArtifact,
    candidate: SourceCandidate,
    url: str | None,
    title: str | None,
    text: str,
    authors: list[str],
    published_at: str | None,
    extraction_method: str,
    original_metadata: dict[str, object],
    rules: RuleSet,
    speaker_attribution: SpeakerAttribution | None = None,
    transcript: TranscriptInfo | None = None,
) -> CanonicalDocument | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    content_hash = sha256(normalized.encode("utf-8")).hexdigest()
    source_key = f"{candidate.source_type.value}:{url or artifact.source_path or artifact.sha256}"
    source_item_id = _identifier("source", source_key)
    version_id = _identifier("version", f"{source_item_id}:{content_hash}")
    inclusion, reason = _inclusion(
        candidate,
        rules=rules,
        url=url,
        title=title,
        text=normalized,
    )
    attribution = speaker_attribution or SpeakerAttribution(
        status=(
            SpeakerStatus.UNAVAILABLE
            if candidate.material_role == MaterialRole.SPOKEN_BY_TARGET
            else SpeakerStatus.NOT_APPLICABLE
        )
    )
    return CanonicalDocument(
        source_item_id=source_item_id,
        content_hash=content_hash,
        document_version_id=version_id,
        investor_slug=investor_slug,
        source_candidate_id=candidate.candidate_id,
        raw_artifact_ids=[artifact.artifact_id],
        canonical_url=url,
        local_source_path=artifact.source_path,
        source_type=candidate.source_type,
        modality=(
            "audiovisual"
            if candidate.material_role == MaterialRole.SPOKEN_BY_TARGET
            else "written"
        ),
        material_role=candidate.material_role,
        title=title,
        authors=authors,
        speakers=(
            authors if candidate.material_role == MaterialRole.SPOKEN_BY_TARGET else []
        ),
        published_at=published_at,
        publication_date_precision="timestamp" if published_at else None,
        collected_at=artifact.collected_at,
        text=normalized,
        original_metadata=original_metadata,
        extraction_method=extraction_method,
        transcript=transcript or TranscriptInfo(),
        speaker_attribution=attribution,
        identity_confidence=candidate.identity_confidence,
        inclusion_status=inclusion,
        exclusion_reason=reason,
    )


def process_artifact(
    workspace: Path,
    artifact: RawArtifact,
    candidate: SourceCandidate,
    *,
    investor_slug: str,
    target_name: str,
    rules: RuleSet | None = None,
) -> list[CanonicalDocument]:
    path = Path(workspace) / artifact.relative_path
    content = path.read_bytes()
    rules = rules or RuleSet.pitch_default()
    documents: list[CanonicalDocument] = []

    def create(**kwargs):
        normalized_text = normalize_text(kwargs["text"])
        provenance_urls = {
            value
            for value in (
                kwargs.get("url"),
                artifact.source_url,
                candidate.url,
                candidate.canonical_url,
            )
            if value
        }
        policies = [
            rules.evaluate(
                url=url,
                title=kwargs.get("title"),
                text=normalized_text,
                channel=candidate.channel,
                programme=candidate.programme,
                company=candidate.company,
                stage="post_extraction",
            )
            for url in sorted(provenance_urls)
        ]
        policy = next(
            (
                decision
                for decision in policies
                if decision.status == PolicyInclusionStatus.EXCLUDED
            ),
            next(
                (
                    decision
                    for decision in policies
                    if decision.status == PolicyInclusionStatus.REVIEW_REQUIRED
                ),
                policies[0],
            ),
        )
        document = _make_document(
            investor_slug=investor_slug,
            artifact=artifact,
            candidate=candidate,
            rules=rules,
            **kwargs,
        )
        if document and policy.status != PolicyInclusionStatus.INCLUDED:
            document.inclusion_status = policy.status
            document.exclusion_reason = policy.reason
        elif (
            document
            and candidate.material_role == MaterialRole.AUTHORED_BY_TARGET
            and candidate.source_type != SourceType.SUPPLIED
            and target_name.casefold()
            not in {author.casefold().strip() for author in document.authors}
        ):
            document.inclusion_status = InclusionStatus.REVIEW_REQUIRED
            document.exclusion_reason = (
                "Extracted author does not exactly match the target investor"
            )
        if document:
            documents.append(document)

    mime = (artifact.mime_type or "").split(";", 1)[0]
    if (
        mime
        in {
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
        }
        or path.suffix == ".xml"
    ):
        for entry in extract_feed(content, artifact.source_url or candidate.url):
            create(
                url=entry.canonical_url,
                title=entry.title,
                text=entry.summary_text,
                authors=[entry.author] if entry.author else [],
                published_at=entry.published_at,
                extraction_method="rss_atom",
                original_metadata=entry.metadata,
            )
    elif mime in {"text/html", "application/xhtml+xml"} or path.suffix in {
        ".html",
        ".htm",
    }:
        page = extract_page(content, artifact.source_url or candidate.url)
        create(
            url=page.canonical_url,
            title=page.title,
            text=page.text,
            authors=[page.author] if page.author else [],
            published_at=page.published_at,
            extraction_method="html_main_content",
            original_metadata=page.metadata,
        )
    elif mime.startswith("text/") or path.suffix in {".txt", ".md"}:
        verified_supplied_speech = (
            candidate.source_type == SourceType.SUPPLIED
            and candidate.material_role == MaterialRole.SPOKEN_BY_TARGET
            and bool(candidate.speaker_verified_by)
        )
        create(
            url=artifact.source_url,
            title=Path(artifact.source_path).name
            if artifact.source_path
            else candidate.title,
            text=content.decode("utf-8", errors="replace"),
            authors=[target_name]
            if candidate.material_role
            in {MaterialRole.AUTHORED_BY_TARGET, MaterialRole.SPOKEN_BY_TARGET}
            else [],
            published_at=None,
            extraction_method="plain_text",
            original_metadata=artifact.original_metadata,
            speaker_attribution=SpeakerAttribution(
                status=SpeakerStatus.VERIFIED_HUMAN,
                speaker_label=target_name,
            )
            if verified_supplied_speech
            else None,
        )
    return documents


def target_speech_document(
    *,
    investor_slug: str,
    target_name: str,
    artifact: RawArtifact,
    candidate: SourceCandidate,
    result: TargetSpeechResult,
    rules: RuleSet,
) -> CanonicalDocument | None:
    text = "\n".join(
        segment.text.strip()
        for segment in result.target_segments
        if segment.text.strip()
    )
    if not text:
        return None
    source_url = (
        str(artifact.original_metadata.get("episode_url") or "")
        or artifact.source_url
        or candidate.canonical_url
    )
    document = _make_document(
        investor_slug=investor_slug,
        artifact=artifact,
        candidate=candidate,
        url=source_url,
        title=candidate.title,
        text=text,
        authors=[target_name],
        published_at=None,
        extraction_method="diarized_target_speech",
        original_metadata={
            "candidate_metadata": artifact.original_metadata,
            "media_seconds": result.media_seconds,
            "target_segments": [
                item.model_dump(mode="json") for item in result.target_segments
            ],
        },
        rules=rules,
        speaker_attribution=result.attribution,
        transcript=result.transcript,
    )
    if document is None:
        return None
    document.raw_artifact_ids = list(
        dict.fromkeys(
            [artifact.artifact_id, *result.attribution.reference_artifact_ids]
        )
    )
    if result.attribution.status not in {
        SpeakerStatus.ACCEPTED_MODEL,
        SpeakerStatus.VERIFIED_HUMAN,
    }:
        document.inclusion_status = InclusionStatus.REVIEW_REQUIRED
        document.exclusion_reason = "Target-speaker attribution requires human review"
    return document


def load_artifact_records(workspace: Path) -> list[RawArtifact]:
    records: list[RawArtifact] = []
    for path in sorted((Path(workspace) / "raw").rglob("*.metadata.json")):
        records.append(RawArtifact.model_validate(read_json(path)))
    return records


def process_artifacts(
    workspace: Path,
    artifacts: Iterable[RawArtifact],
    candidates: Iterable[SourceCandidate],
    *,
    investor_slug: str,
    target_name: str,
    rules: RuleSet | None = None,
) -> list[CanonicalDocument]:
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    documents: list[CanonicalDocument] = []
    for artifact in artifacts:
        candidate_id = str(artifact.original_metadata.get("candidate_id", ""))
        candidate = candidate_map.get(candidate_id)
        if not candidate:
            continue
        documents.extend(
            process_artifact(
                workspace,
                artifact,
                candidate,
                investor_slug=investor_slug,
                target_name=target_name,
                rules=rules,
            )
        )
    return deduplicate(documents)


def deduplicate(
    documents: list[CanonicalDocument], *, near_threshold: float = 0.97
) -> list[CanonicalDocument]:
    canonical: list[CanonicalDocument] = []
    exact: dict[str, CanonicalDocument] = {}
    preference = {
        InclusionStatus.INCLUDED: 0,
        InclusionStatus.PENDING: 1,
        InclusionStatus.REVIEW_REQUIRED: 2,
        InclusionStatus.EXCLUDED: 3,
        InclusionStatus.DUPLICATE: 4,
    }
    # Select eligible material before excluded/review copies regardless of
    # collection order. Return order stays unchanged for stable audit views.
    for document in sorted(
        documents,
        key=lambda item: (preference[item.inclusion_status], item.document_version_id),
    ):
        duplicate = exact.get(document.content_hash)
        if duplicate is None:
            for prior in canonical:
                ratio = SequenceMatcher(
                    None, prior.text, document.text, autojunk=False
                ).ratio()
                if ratio >= near_threshold:
                    duplicate = prior
                    break
        if duplicate is not None:
            document.duplicate_of = duplicate.document_version_id
            if document.inclusion_status != InclusionStatus.EXCLUDED:
                document.inclusion_status = InclusionStatus.DUPLICATE
                document.exclusion_reason = "Duplicate of preferred canonical document"
        else:
            exact[document.content_hash] = document
            canonical.append(document)
    return documents

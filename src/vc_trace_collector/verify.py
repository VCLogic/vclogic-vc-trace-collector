"""Independent workspace integrity and corpus-policy verification."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import RunConfig
from .models import (
    ApprovalStatus,
    AVCandidateOutcome,
    CanonicalDocument,
    CollectionManifest,
    QualityReport,
    RawArtifact,
    ReferenceVoiceCandidate,
    ReferenceVoiceProfile,
    ReferenceVoiceStatus,
    ResolutionStatus,
    ResolvedIdentity,
    RunSummary,
    SourcePlan,
    SourceType,
    SpeakerStatus,
)
from .policy import ExclusionRule, RuleSet, eligible_for_corpus
from .storage import canonical_json, read_json, read_jsonl


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    errors: list[str] = Field(default_factory=list)
    checked_files: int = 0
    checked_documents: int = 0


def _contained_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _load_model(path: Path, model: type[BaseModel], label: str, errors: list[str]):
    try:
        return model.model_validate(read_json(path))
    except Exception as error:
        errors.append(f"Invalid {label}: {error}")
        return None


def _validate_model_attribution(
    document: CanonicalDocument,
    config: RunConfig | None,
    profile: ReferenceVoiceProfile | None,
    errors: list[str],
) -> None:
    attribution = document.speaker_attribution
    if attribution.status != SpeakerStatus.ACCEPTED_MODEL:
        return
    if any(
        value is None
        for value in (
            attribution.score,
            attribution.margin,
            attribution.minimum_score,
            attribution.minimum_margin,
        )
    ):
        errors.append(
            f"Model-accepted speech lacks attribution thresholds: "
            f"{document.document_version_id}"
        )
        return
    if attribution.score < attribution.minimum_score:
        errors.append(f"Speaker score below threshold: {document.document_version_id}")
    if attribution.margin < attribution.minimum_margin:
        errors.append(f"Speaker margin below threshold: {document.document_version_id}")
    if config is not None:
        if attribution.minimum_score != config.speaker_minimum_score:
            errors.append(
                f"Speaker minimum score differs from frozen config: "
                f"{document.document_version_id}"
            )
        if attribution.minimum_margin != config.speaker_minimum_margin:
            errors.append(
                f"Speaker minimum margin differs from frozen config: "
                f"{document.document_version_id}"
            )
        if (
            config.diarization_model
            and attribution.diarization_model != config.diarization_model
        ):
            errors.append(
                f"Diarization model differs from frozen config: "
                f"{document.document_version_id}"
            )
        if (
            document.transcript.method == "speech_to_text"
            and config.transcription_model
            and document.transcript.model != config.transcription_model
        ):
            errors.append(
                f"Transcription model differs from frozen config: "
                f"{document.document_version_id}"
            )
    if profile is not None:
        if attribution.embedding_model != profile.embedding_model:
            errors.append(
                f"Embedding model differs from reference profile: "
                f"{document.document_version_id}"
            )
        if set(attribution.reference_artifact_ids) != set(profile.artifact_ids):
            errors.append(
                f"Speaker attribution references a different voice profile: "
                f"{document.document_version_id}"
            )


def verify_workspace(workspace: Path) -> VerificationResult:
    workspace = Path(workspace).resolve()
    errors: list[str] = []
    manifest_path = workspace / "collection_manifest.json"
    if not manifest_path.exists():
        return VerificationResult(
            passed=False, errors=["Missing collection_manifest.json"]
        )
    try:
        manifest = CollectionManifest.model_validate(read_json(manifest_path))
    except Exception as error:
        return VerificationResult(
            passed=False, errors=[f"Invalid collection manifest: {error}"]
        )

    checked_files = 0
    for item in manifest.files:
        path = _contained_path(workspace, item.path)
        if path is None:
            errors.append(f"Manifest path escapes workspace: {item.path}")
            continue
        if not path.exists():
            errors.append(f"Missing manifest file: {item.path}")
            continue
        checked_files += 1
        digest = sha256(path.read_bytes()).hexdigest()
        if digest != item.sha256:
            errors.append(f"File hash mismatch: {item.path}")
        if path.stat().st_size != item.size_bytes:
            errors.append(f"File size mismatch: {item.path}")
        if item.records is not None:
            records = sum(1 for line in path.read_bytes().splitlines() if line.strip())
            if records != item.records:
                errors.append(f"Record count mismatch: {item.path}")

    fingerprint_payload = {
        "investor_slug": manifest.investor_slug,
        "identity_id": manifest.identity_id,
        "config_hash": manifest.config_hash,
        "exclusion_rules_hash": manifest.exclusion_rules_hash,
        "files": [item.model_dump(mode="json") for item in manifest.files],
    }
    expected_fingerprint = sha256(
        canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    if manifest.fingerprint != expected_fingerprint:
        errors.append("Collection manifest fingerprint mismatch")

    identity = _load_model(
        workspace / "identity/resolved_identity.json",
        ResolvedIdentity,
        "resolved identity",
        errors,
    )
    if identity is not None:
        if identity.slug != manifest.investor_slug:
            errors.append("Resolved identity slug does not match manifest")
        if identity.resolution_status != ResolutionStatus.CONFIRMED:
            errors.append("Resolved identity is not confirmed")

    config = _load_model(
        workspace / "config_snapshot.json", RunConfig, "config snapshot", errors
    )
    if config is not None and config.fingerprint != manifest.config_hash:
        errors.append("Config snapshot hash does not match manifest")

    effective_rules = None
    try:
        rules_payload: Any = read_json(workspace / "exclusion_rules_snapshot.json")
        rules_hash = sha256(canonical_json(rules_payload).encode("utf-8")).hexdigest()
        if rules_hash != manifest.exclusion_rules_hash:
            errors.append("Exclusion rules hash does not match manifest")
        effective_rules = RuleSet(
            [ExclusionRule.model_validate(item) for item in rules_payload]
        )
    except Exception as error:
        errors.append(f"Invalid exclusion rules snapshot: {error}")

    plan = _load_model(
        workspace / "discovery/source_plan.json", SourcePlan, "source plan", errors
    )
    candidates = {}
    if plan is not None:
        if plan.investor_slug != manifest.investor_slug:
            errors.append("Source plan investor slug does not match manifest")
        candidates = {item.candidate_id: item for item in plan.candidates}

    reference_candidates: dict[str, ReferenceVoiceCandidate] = {}
    for row in read_jsonl(workspace / "identity/reference_voice_candidates.jsonl"):
        try:
            item = ReferenceVoiceCandidate.model_validate(row)
            reference_candidates[item.candidate_id] = item
        except Exception as error:
            errors.append(f"Invalid reference voice candidate: {error}")

    artifacts: dict[str, list[RawArtifact]] = {}
    for metadata_path in sorted((workspace / "raw").rglob("*.metadata.json")):
        try:
            artifact = RawArtifact.model_validate(read_json(metadata_path))
        except Exception as error:
            errors.append(f"Invalid raw artifact record {metadata_path}: {error}")
            continue
        artifacts.setdefault(artifact.artifact_id, []).append(artifact)
        for parent_id in artifact.parent_artifact_ids:
            # The complete set is checked after all provenance records are read.
            if not parent_id.startswith("sha256:"):
                errors.append(f"Invalid raw parent artifact id: {parent_id}")
        raw_path = _contained_path(workspace, artifact.relative_path)
        if raw_path is None:
            errors.append(
                f"Raw artifact path escapes workspace: {artifact.relative_path}"
            )
            continue
        if not raw_path.exists():
            errors.append(f"Missing raw artifact bytes: {artifact.relative_path}")
            continue
        raw_bytes = raw_path.read_bytes()
        if sha256(raw_bytes).hexdigest() != artifact.sha256:
            errors.append(f"Raw artifact hash mismatch: {artifact.relative_path}")
        if len(raw_bytes) != artifact.size_bytes:
            errors.append(f"Raw artifact size mismatch: {artifact.relative_path}")
    for records in artifacts.values():
        for artifact in records:
            for parent_id in artifact.parent_artifact_ids:
                if parent_id not in artifacts:
                    errors.append(
                        f"Raw artifact references missing parent: {artifact.artifact_id}"
                    )

    profile = None
    profile_path = workspace / "identity/reference_voice_profile.json"
    if profile_path.exists():
        profile = _load_model(
            profile_path, ReferenceVoiceProfile, "reference voice profile", errors
        )
        if profile is not None:
            if profile.investor_slug != manifest.investor_slug:
                errors.append(
                    "Reference voice profile investor does not match manifest"
                )
            if profile.status != ReferenceVoiceStatus.VERIFIED_HUMAN:
                errors.append("Reference voice profile is not human verified")
            if any(item not in artifacts for item in profile.artifact_ids):
                errors.append("Reference voice profile has missing raw lineage")
            if (
                config is not None
                and config.embedding_model
                and profile.embedding_model != config.embedding_model
            ):
                errors.append("Reference embedding model differs from frozen config")
            for candidate_id in profile.candidate_ids:
                voice = reference_candidates.get(candidate_id)
                if voice is None:
                    errors.append("Reference voice profile names an unknown candidate")
                    continue
                if voice.status != ReferenceVoiceStatus.VERIFIED_HUMAN:
                    errors.append("Reference voice candidate is not human verified")
                if voice.artifact_id not in profile.artifact_ids:
                    errors.append(
                        "Reference voice candidate artifact differs from profile"
                    )
                source = candidates.get(voice.source_candidate_id)
                if source is None or source.approval_status not in {
                    ApprovalStatus.APPROVED,
                    ApprovalStatus.AUTO_APPROVED,
                }:
                    errors.append("Reference voice source candidate was not approved")

    processed: list[CanonicalDocument] = []
    for row in read_jsonl(workspace / "processed/documents.jsonl"):
        try:
            processed.append(CanonicalDocument.model_validate(row))
        except Exception as error:
            errors.append(f"Invalid processed document: {error}")

    documents: list[CanonicalDocument] = []
    for row in read_jsonl(workspace / "corpus/all_documents.jsonl"):
        try:
            document = CanonicalDocument.model_validate(row)
        except Exception as error:
            errors.append(f"Invalid canonical document: {error}")
            continue
        documents.append(document)
        if not eligible_for_corpus(document):
            errors.append(
                f"Ineligible document present in corpus: {document.document_version_id}"
            )
        if not document.raw_artifact_ids:
            errors.append(f"Document lacks raw lineage: {document.document_version_id}")
        missing_artifacts = [
            artifact_id
            for artifact_id in document.raw_artifact_ids
            if artifact_id not in artifacts
        ]
        if missing_artifacts:
            errors.append(
                f"Document references missing raw artifacts: "
                f"{document.document_version_id}"
            )
        elif not any(
            str(record.original_metadata.get("candidate_id", ""))
            == document.source_candidate_id
            for artifact_id in document.raw_artifact_ids
            for record in artifacts.get(artifact_id, [])
        ):
            errors.append(
                f"Document raw lineage does not support its source candidate: "
                f"{document.document_version_id}"
            )
        candidate = candidates.get(document.source_candidate_id)
        if candidate is None:
            errors.append(
                f"Document references unknown source candidate: "
                f"{document.document_version_id}"
            )
        elif candidate.approval_status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.AUTO_APPROVED,
        }:
            errors.append(
                f"Corpus document source was not approved: "
                f"{document.document_version_id}"
            )
        _validate_model_attribution(document, config, profile, errors)
        if (
            document.speaker_attribution.status == SpeakerStatus.ACCEPTED_MODEL
            and profile is None
        ):
            errors.append(
                f"Model-attributed speech lacks a reference profile: "
                f"{document.document_version_id}"
            )
        if document.speaker_attribution.status == SpeakerStatus.VERIFIED_HUMAN and (
            candidate is None or not candidate.speaker_verified_by
        ):
            errors.append(
                f"Human-verified speech lacks source-review evidence: "
                f"{document.document_version_id}"
            )
        if (
            document.transcript.source_artifact_id
            and document.transcript.source_artifact_id not in document.raw_artifact_ids
        ):
            errors.append(
                f"Transcript artifact is absent from document lineage: "
                f"{document.document_version_id}"
            )
        if (
            document.material_role == "authored_by_target"
            and candidate is not None
            and candidate.source_type != SourceType.SUPPLIED
            and identity is not None
            and identity.canonical_name.casefold()
            not in {author.casefold().strip() for author in document.authors}
        ):
            errors.append(
                f"Authored document does not name the target as author: "
                f"{document.document_version_id}"
            )
        if effective_rules is not None:
            provenance_urls = (
                {document.canonical_url} if document.canonical_url else set()
            )
            if candidate is not None:
                provenance_urls.update({candidate.url, candidate.canonical_url})
            for artifact_id in document.raw_artifact_ids:
                provenance_urls.update(
                    record.source_url
                    for record in artifacts.get(artifact_id, [])
                    if record.source_url
                )
            if any(
                effective_rules.evaluate(
                    url=url,
                    title=document.title,
                    text=document.text,
                    channel=candidate.channel if candidate else None,
                    programme=candidate.programme if candidate else None,
                    company=candidate.company if candidate else None,
                    stage="verification",
                ).status
                != "included"
                for url in provenance_urls
            ):
                errors.append(
                    f"Corpus document violates effective exclusion rules: "
                    f"{document.document_version_id}"
                )
            for artifact_id in document.raw_artifact_ids:
                for record in artifacts.get(artifact_id, []):
                    metadata = record.original_metadata
                    channel_values = dict.fromkeys(
                        [
                            metadata.get("channel"),
                            metadata.get("channel_id"),
                            metadata.get("uploader_id"),
                            candidate.channel if candidate else None,
                        ]
                    )
                    if any(
                        effective_rules.evaluate(
                            url=record.source_url or document.canonical_url,
                            title=str(metadata.get("title") or document.title or ""),
                            text=document.text,
                            channel=str(value) if value else None,
                            programme=str(
                                metadata.get("programme")
                                or (candidate.programme if candidate else "")
                            )
                            or None,
                            company=str(
                                metadata.get("company")
                                or (candidate.company if candidate else "")
                            )
                            or None,
                            stage="verification_metadata",
                        ).status
                        != "included"
                        for value in channel_values
                    ):
                        errors.append(
                            f"Corpus document violates artifact metadata exclusions: "
                            f"{document.document_version_id}"
                        )
    if len(documents) != manifest.corpus_documents:
        errors.append(
            f"Manifest corpus count {manifest.corpus_documents} does not match {len(documents)}"
        )
    excluded_count = sum(1 for item in processed if not eligible_for_corpus(item))
    if excluded_count != manifest.excluded_documents:
        errors.append(
            f"Manifest excluded count {manifest.excluded_documents} does not match "
            f"{excluded_count}"
        )
    corpus_versions = {item.document_version_id for item in documents}
    processed_versions = {
        item.document_version_id for item in processed if eligible_for_corpus(item)
    }
    if corpus_versions != processed_versions:
        errors.append("Corpus documents do not match eligible processed documents")
    try:
        quality = QualityReport.model_validate(
            read_json(workspace / "quality_report.json")
        )
        if not quality.passed:
            errors.append("Quality report did not pass")
        required_checks = {
            "corpus_nonempty",
            "excluded_absent",
            "lineage_present",
            "metadata_complete",
            "first_person_only",
            "speaker_attribution_complete",
            "transcript_coverage_complete",
            "budget_within_limit",
            "approved_work_complete",
            "partial_run_policy_satisfied",
        }
        if not required_checks.issubset(quality.checks):
            errors.append("Quality report is missing required checks")
    except Exception as error:
        errors.append(f"Invalid quality report: {error}")

    outcome_rows = read_jsonl(workspace / "processed/av_candidate_outcomes.jsonl")
    outcomes: list[AVCandidateOutcome] = []
    for row in outcome_rows:
        try:
            outcomes.append(AVCandidateOutcome.model_validate(row))
        except Exception as error:
            errors.append(f"Invalid audiovisual candidate outcome: {error}")
    approved_spoken = {
        item.candidate_id
        for item in candidates.values()
        if item.approval_status
        in {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
        and item.material_role == "spoken_by_target"
    }
    if approved_spoken != {item.candidate_id for item in outcomes}:
        errors.append("Audiovisual outcomes do not cover every approved spoken source")
    incomplete = [
        item
        for item in outcomes
        if item.status in {"failed", "not_collected", "review_required"}
    ]
    if incomplete and not (config and config.allow_partial_run):
        errors.append("Approved audiovisual work is incomplete")
    summary_path = workspace / "run_summary.json"
    if summary_path.exists():
        summary = _load_model(summary_path, RunSummary, "run summary", errors)
        if summary is not None:
            if (
                summary.failures
                != summary.collection_failures + summary.processing_failures
            ):
                errors.append("Run summary failure totals are inconsistent")
            if incomplete and not (config and config.allow_partial_run):
                errors.append("Run summary contains unresolved required work")
    return VerificationResult(
        passed=not errors,
        errors=errors,
        checked_files=checked_files,
        checked_documents=len(documents),
    )

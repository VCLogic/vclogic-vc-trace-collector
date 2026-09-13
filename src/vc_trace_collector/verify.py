"""Independent workspace integrity and corpus-policy verification."""

from __future__ import annotations

from hashlib import sha256
from math import isclose
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .audit import (
    BudgetLedger,
    attempted_media_seconds,
    provider_attempt_count,
)
from .collectors import CollectionCandidateOutcome
from .config import RunConfig
from .discovery import SearchResult
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
    SourceDecision,
    SourcePlan,
    SourceType,
    SpeakerStatus,
)
from .policy import ExclusionRule, RuleSet, canonicalize_url, eligible_for_corpus
from .source_search import SourceSearchObservation
from .storage import StateStore, canonical_json, read_json, read_jsonl


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


def _state_output_path(root: Path, output_id: object) -> Path | None:
    value = str(output_id or "")
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    return None


def _load_model(path: Path, model: type[BaseModel], label: str, errors: list[str]):
    try:
        return model.model_validate(read_json(path))
    except Exception as error:
        errors.append(f"Invalid {label}: {error}")
        return None


def _rule_blocks(decision, candidate) -> bool:
    if decision.status == "excluded":
        return True
    return decision.status == "review_required" and (
        candidate is None or decision.rule_id not in candidate.override_rule_ids
    )


def _target_interval_seconds(documents: list[CanonicalDocument]) -> float:
    by_source: dict[str, list[tuple[float, float]]] = {}
    for document in documents:
        for segment in document.original_metadata.get("target_segments", []):
            try:
                start = float(segment["start_seconds"])
                end = float(segment["end_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            if end > start:
                by_source.setdefault(document.source_candidate_id, []).append(
                    (start, end)
                )
    total = 0.0
    for intervals in by_source.values():
        merged: list[list[float]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        total += sum(end - start for start, end in merged)
    return total


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
        if not identity.reviewed_by:
            errors.append("Confirmed identity lacks reviewer attestation")
        try:
            identity_review = read_json(workspace / "identity/identity_review.json")
            if identity_review.get("reviewed_by") != identity.reviewed_by:
                errors.append("Identity review record does not match resolved identity")
        except Exception as error:
            errors.append(f"Invalid identity review record: {error}")

    config = _load_model(
        workspace / "config_snapshot.json", RunConfig, "config snapshot", errors
    )
    if config is not None and config.fingerprint != manifest.config_hash:
        errors.append("Config snapshot hash does not match manifest")
    cost_trace_path = workspace / "audit/costs.jsonl"
    cost_database_path = workspace / "audit/costs.sqlite"
    if cost_trace_path.exists() and not cost_database_path.exists():
        errors.append("Transactional cost ledger is missing")
    if cost_database_path.exists() and config is not None:
        ledger = BudgetLedger(
            cost_trace_path,
            config.maximum_cost_usd,
            maximum_provider_operations=config.maximum_provider_operations,
            maximum_media_seconds=config.maximum_media_minutes * 60,
        )
        if not ledger.trace_consistent():
            errors.append("Public cost trace does not match transactional ledger")

    state_path = workspace / "state/state.sqlite"
    latest_operations: dict[str, dict[str, object]] = {}
    if state_path.exists():
        for operation in StateStore(state_path).operation_records():
            latest_operations[str(operation["operation_id"])] = operation

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
        for candidate in plan.candidates:
            if candidate.approval_status in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.AUTO_APPROVED,
            } and (not candidate.reviewed_by or candidate.decision_at is None):
                errors.append(
                    f"Approved source lacks reviewer attestation: {candidate.candidate_id}"
                )

    source_decisions: list[SourceDecision] = []
    for row in read_jsonl(workspace / "discovery/source_decisions.jsonl"):
        try:
            source_decisions.append(SourceDecision.model_validate(row))
        except Exception as error:
            errors.append(f"Invalid source decision: {error}")
    for candidate in candidates.values():
        if candidate.approval_status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.AUTO_APPROVED,
        }:
            continue
        history = [
            item
            for item in source_decisions
            if item.candidate_id == candidate.candidate_id
        ]
        if not history:
            errors.append(
                f"Approved source lacks a manifest-covered decision: "
                f"{candidate.candidate_id}"
            )
            continue
        latest = history[-1]
        exact_latest = (
            latest.status == candidate.approval_status
            and latest.decided_by == candidate.reviewed_by
            and latest.decided_at == candidate.decision_at
            and latest.reason == candidate.decision_reason
        )
        if not exact_latest:
            errors.append(
                f"Approved source does not match its latest decision: "
                f"{candidate.candidate_id}"
            )
        overrides = sorted(
            {rule_id for item in history for rule_id in item.override_rule_ids}
        )
        if overrides != sorted(candidate.override_rule_ids):
            errors.append(
                f"Source exclusion overrides lack exact decision provenance: "
                f"{candidate.candidate_id}"
            )
        for field in ("material_role", "channel", "programme", "company"):
            values = [
                getattr(item, field)
                for item in history
                if getattr(item, field) is not None
            ]
            if values and getattr(candidate, field) != values[-1]:
                errors.append(
                    f"Source {field} does not match decision history: "
                    f"{candidate.candidate_id}"
                )
        estimates = [
            item.estimated_media_seconds
            for item in history
            if item.estimated_media_seconds is not None
        ]
        if estimates and candidate.estimated_media_seconds != estimates[-1]:
            errors.append(
                f"Source duration does not match decision history: {candidate.candidate_id}"
            )
        speaker_reviewers = [
            item.decided_by for item in history if item.speaker_verified
        ]
        if speaker_reviewers and candidate.speaker_verified_by != speaker_reviewers[-1]:
                errors.append(
                    f"Source speaker verification lacks decision provenance: "
                    f"{candidate.candidate_id}"
                )

    search_observations: list[SourceSearchObservation] = []
    for row in read_jsonl(workspace / "discovery/search_observations.jsonl"):
        try:
            search_observations.append(SourceSearchObservation.model_validate(row))
        except Exception as error:
            errors.append(f"Invalid source search observation: {error}")
    search_operations = {
        operation_id: operation
        for operation_id, operation in latest_operations.items()
        if operation_id.startswith("search-source:")
    }
    observations_by_operation: dict[str, list[SourceSearchObservation]] = {}
    for observation in search_observations:
        if observation.operation_id is None:
            if search_operations:
                errors.append("Source search observation lacks operation lineage")
            continue
        observations_by_operation.setdefault(observation.operation_id, []).append(
            observation
        )
        if observation.operation_id not in search_operations:
            errors.append(
                f"Source search observation names unknown operation: "
                f"{observation.operation_id}"
            )
    for operation_id, operation in search_operations.items():
        observations = observations_by_operation.get(operation_id, [])
        if not observations:
            errors.append(
                f"Missing source search observation for operation: {operation_id}"
            )
            continue
        operation_status = str(operation["status"])
        expected_status = "failed" if operation_status == "failed" else "succeeded"
        if operation_status not in {"complete", "failed"}:
            errors.append(f"Source search operation is incomplete: {operation_id}")
        elif observations[-1].status != expected_status:
            errors.append(
                f"Source search observation status disagrees with state: {operation_id}"
            )
        if operation_status != "complete":
            continue
        cache_path = _state_output_path(workspace, operation.get("output_id"))
        if cache_path is None or not cache_path.exists():
            errors.append(f"Missing source search cache for operation: {operation_id}")
            continue
        try:
            cache_payload = read_json(cache_path)
            cached_query = str(cache_payload["query"])
            cached_provider = str(cache_payload["requested_provider"])
            cached_results = [
                SearchResult.model_validate(row)
                for row in cache_payload.get("results", [])
            ]
        except Exception as error:
            errors.append(f"Invalid source search cache {operation_id}: {error}")
            continue
        expected_rows = {
            (
                item.query,
                item.provider,
                item.rank,
                item.url,
                item.title,
            )
            for item in cached_results
        }
        observed_rows = {
            (
                item.query,
                item.result_provider,
                item.rank,
                item.url,
                item.title,
            )
            for item in observations
            if item.url is not None
        }
        if expected_rows != observed_rows:
            errors.append(
                f"Source search observations disagree with cached results: {operation_id}"
            )
        if any(
            item.query != cached_query
            or item.requested_provider != cached_provider
            for item in observations
        ):
            errors.append(
                f"Source search observation query/provider disagrees with cache: "
                f"{operation_id}"
            )
        try:
            operation_source_type = SourceType(operation_id.split(":", 2)[1])
        except (IndexError, ValueError):
            errors.append(f"Invalid source search operation id: {operation_id}")
            continue
        if any(
            item.source_type != operation_source_type for item in observations
        ):
            errors.append(
                f"Source search observation type disagrees with operation: "
                f"{operation_id}"
            )
        for item in cached_results:
            candidate = candidates.get(
                next(
                    (
                        candidate_id
                        for candidate_id, candidate_item in candidates.items()
                        if candidate_item.canonical_url == canonicalize_url(item.url)
                    ),
                    "",
                )
            )
            if (
                candidate is None
                or item.query not in candidate.discovery_queries
            ):
                errors.append(
                    f"Cached source search result lacks candidate lineage: {item.url}"
                )

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

    collection_outcomes: list[CollectionCandidateOutcome] = []
    for row in read_jsonl(
        workspace / "processed/collection_candidate_outcomes.jsonl"
    ):
        try:
            outcome = CollectionCandidateOutcome.model_validate(row)
            collection_outcomes.append(outcome)
        except Exception as error:
            errors.append(f"Invalid collection outcome: {error}")
    collection_outcome_ids: set[str] = set()
    collection_outcomes_by_id: dict[str, CollectionCandidateOutcome] = {}
    for outcome in collection_outcomes:
        if outcome.candidate_id not in candidates:
            errors.append(
                f"Collection outcome names unknown candidate: {outcome.candidate_id}"
            )
        if outcome.candidate_id in collection_outcome_ids:
            errors.append(
                f"Duplicate collection outcome: {outcome.candidate_id}"
            )
        collection_outcome_ids.add(outcome.candidate_id)
        collection_outcomes_by_id[outcome.candidate_id] = outcome
        if any(artifact_id not in artifacts for artifact_id in outcome.artifact_ids):
            errors.append(
                f"Collection outcome references missing raw artifact: "
                f"{outcome.candidate_id}"
            )
        for artifact_id in outcome.artifact_ids:
            if artifact_id in artifacts and not any(
                record.original_metadata.get("candidate_id") == outcome.candidate_id
                for record in artifacts[artifact_id]
            ):
                errors.append(
                    f"Collection outcome artifact belongs to another candidate: "
                    f"{outcome.candidate_id}"
                )
    collection_operations = {
        operation_id.removeprefix("collect:"): operation
        for operation_id, operation in latest_operations.items()
        if operation_id.startswith("collect:")
    }
    for candidate_id, operation in collection_operations.items():
        outcome = collection_outcomes_by_id.get(candidate_id)
        if outcome is None:
            errors.append(
                f"Missing collection outcome for operation: collect:{candidate_id}"
            )
            continue
        operation_status = str(operation["status"])
        if operation_status == "failed" and outcome.status != "failed":
            errors.append(
                f"Collection outcome status disagrees with failed state: {candidate_id}"
            )
        elif operation_status == "complete" and outcome.status == "failed":
            errors.append(
                f"Collection outcome status disagrees with completed state: {candidate_id}"
            )
        elif operation_status not in {"complete", "failed"}:
            errors.append(f"Collection operation is incomplete: {candidate_id}")
        if operation_status != "complete":
            continue
        state_output = str(operation.get("output_id") or "")
        outcome_cache_path = _state_output_path(workspace, state_output)
        cached_outcome = None
        if outcome_cache_path is not None and outcome_cache_path.exists():
            try:
                cached_outcome = CollectionCandidateOutcome.model_validate(
                    read_json(outcome_cache_path)
                )
            except Exception as error:
                errors.append(
                    f"Invalid state-linked collection outcome {candidate_id}: {error}"
                )
                continue
            expected_artifact_ids = set(cached_outcome.artifact_ids)
            if cached_outcome.candidate_id != candidate_id:
                errors.append(
                    f"State-linked collection outcome names wrong candidate: "
                    f"{candidate_id}"
                )
        else:
            expected_artifact_ids = {
                value
                for value in state_output.split(",")
                if value.startswith("sha256:")
            }
        if not expected_artifact_ids:
            errors.append(
                f"Completed collection lacks state-linked artifacts: {candidate_id}"
            )
            continue
        if set(outcome.artifact_ids) != expected_artifact_ids:
            errors.append(
                f"Collection outcome artifact set disagrees with state: {candidate_id}"
            )
        state_records = [
            record
            for artifact_id in expected_artifact_ids
            for record in artifacts.get(artifact_id, [])
            if record.original_metadata.get("candidate_id") == candidate_id
        ]
        if {record.artifact_id for record in state_records} != expected_artifact_ids:
            errors.append(
                f"Collection state artifacts lack candidate lineage: {candidate_id}"
            )
            continue
        post_collection_excluded = any(
            record.original_metadata.get("collection_exclusion", {}).get("status")
            == "excluded"
            for record in state_records
        )
        post_collection_review = any(
            record.original_metadata.get("collection_exclusion", {}).get("status")
            == "review_required"
            and not record.original_metadata.get("review_override_applied", False)
            for record in state_records
        )
        expected_collection_status = (
            "excluded"
            if post_collection_excluded
            else ("review_required" if post_collection_review else "succeeded")
        )
        if outcome.status != expected_collection_status:
            errors.append(
                f"Collection outcome status disagrees with raw metadata: {candidate_id}"
            )
        if cached_outcome and cached_outcome.status != expected_collection_status:
            errors.append(
                f"State-linked collection status disagrees with raw metadata: "
                f"{candidate_id}"
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
                if not voice.reviewed_by:
                    errors.append(
                        "Reference voice candidate lacks reviewer attestation"
                    )
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
                _rule_blocks(
                    effective_rules.evaluate(
                        url=url,
                        title=document.title,
                        text=document.text,
                        channel=candidate.channel if candidate else None,
                        programme=candidate.programme if candidate else None,
                        company=candidate.company if candidate else None,
                        authors=document.authors,
                        speakers=document.speakers,
                        stage="verification",
                    ),
                    candidate,
                )
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
                        _rule_blocks(
                            effective_rules.evaluate(
                                url=record.source_url or document.canonical_url,
                                title=str(
                                    metadata.get("title") or document.title or ""
                                ),
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
                                authors=document.authors,
                                speakers=document.speakers,
                                stage="verification_metadata",
                            ),
                            candidate,
                        )
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
    quality = None
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
            "download_budget_within_limit",
            "provider_operation_budget_within_limit",
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
    summary = None
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
            if summary.collection_failures != sum(
                item.status == "failed" for item in collection_outcomes
            ):
                errors.append("Run summary collection failure count is inconsistent")
            if summary.collection_review_required != sum(
                item.status == "review_required" for item in collection_outcomes
            ):
                errors.append("Run summary collection review count is inconsistent")
            if summary.stages.get("collection") == "complete":
                approved_candidates = {
                    item.candidate_id
                    for item in candidates.values()
                    if item.approval_status
                    in {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
                }
                if approved_candidates != collection_outcome_ids:
                    errors.append(
                        "Collection outcomes do not cover every approved source"
                    )
    if quality is not None:
        corpus_speech = [
            item for item in documents if item.material_role == "spoken_by_target"
        ]
        verified_speech = [
            item
            for item in corpus_speech
            if item.speaker_attribution.status
            in {SpeakerStatus.ACCEPTED_MODEL, SpeakerStatus.VERIFIED_HUMAN}
        ]
        transcribed_speech = [
            item for item in corpus_speech if item.transcript.method != "none"
        ]
        failures = summary.failures if summary is not None else 0
        unresolved = summary.unresolved if summary is not None else 0
        cost_rows = read_jsonl(workspace / "audit/costs.jsonl")
        provider_cost = sum(
            float(row.get("amount_usd", 0))
            for row in cost_rows
            if row.get("kind") == "settlement"
        )
        provider_operations = provider_attempt_count(cost_rows)
        downloaded_bytes = summary.downloaded_bytes if summary is not None else 0
        billed_media_seconds = attempted_media_seconds(cost_rows)
        raw_candidate_ids = {
            str(record.original_metadata.get("candidate_id", ""))
            for records in artifacts.values()
            for record in records
        }
        expected_extraction_ids = {
            candidate.candidate_id
            for candidate in candidates.values()
            if candidate.approval_status
            in {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
            and candidate.material_role in {"authored_by_target", "spoken_by_target"}
            and candidate.candidate_id in raw_candidate_ids
        }
        successful_extraction_ids = {
            item.source_candidate_id for item in processed if item.text.strip()
        } & expected_extraction_ids
        actual_complete = failures == 0 and unresolved == 0
        expected_checks = {
            "corpus_nonempty": bool(documents),
            "excluded_absent": all(eligible_for_corpus(item) for item in documents),
            "lineage_present": all(bool(item.raw_artifact_ids) for item in processed),
            "metadata_complete": all(
                bool(item.title) and bool(item.canonical_url or item.local_source_path)
                for item in documents
            ),
            "first_person_only": all(
                (item.material_role == "authored_by_target" and bool(item.authors))
                or (
                    item.material_role == "spoken_by_target"
                    and bool(item.speakers)
                    and item.speaker_attribution.status
                    in {SpeakerStatus.ACCEPTED_MODEL, SpeakerStatus.VERIFIED_HUMAN}
                )
                for item in documents
            ),
            "speaker_attribution_complete": len(verified_speech) == len(corpus_speech),
            "transcript_coverage_complete": len(transcribed_speech)
            == len(corpus_speech),
            "budget_within_limit": config is None
            or provider_cost <= float(config.maximum_cost_usd),
            "approved_work_complete": actual_complete,
            "partial_run_policy_satisfied": actual_complete
            or bool(config and config.allow_partial_run),
            "download_budget_within_limit": config is None
            or downloaded_bytes <= config.maximum_download_bytes,
            "provider_operation_budget_within_limit": config is None
            or provider_operations <= config.maximum_provider_operations,
        }
        for name, expected in expected_checks.items():
            if quality.checks.get(name) != expected:
                errors.append(f"Quality check does not match workspace: {name}")
        expected_counts = {
            "documents": len(processed),
            "included": len(documents),
            "excluded_or_pending": sum(
                not eligible_for_corpus(item) for item in processed
            ),
            "target_speech": sum(
                item.material_role == "spoken_by_target" for item in processed
            ),
            "verified_target_speech": len(verified_speech),
            "duplicates": sum(
                bool(item.duplicate_of) or item.inclusion_status == "duplicate"
                for item in processed
            ),
            "source_types": len({item.source_type for item in documents}),
            "failures": failures,
            "unresolved_sources": unresolved,
            "uncertain_attributions": sum(
                item.speaker_attribution.status == SpeakerStatus.UNCERTAIN
                for item in processed
            ),
            "unknown_publication_dates": sum(
                item.published_at is None for item in documents
            ),
            "excluded": sum(item.inclusion_status == "excluded" for item in processed),
            "review_required": sum(
                item.inclusion_status == "review_required" for item in processed
            ),
            "downloaded_bytes": downloaded_bytes,
            "provider_operations": provider_operations,
            "expected_extractions": len(expected_extraction_ids),
            "extraction_successes": len(successful_extraction_ids),
        }
        expected_retries = sum(
            max(0, int(record.original_metadata.get("attempts", 1)) - 1)
            for records in artifacts.values()
            for record in records
        )
        state_path = workspace / "state/state.sqlite"
        if state_path.exists():
            expected_retries += StateStore(state_path).retry_count()
        expected_counts["retries"] = expected_retries
        for name, expected in expected_counts.items():
            if quality.counts.get(name) != expected:
                errors.append(f"Quality count does not match workspace: {name}")
        first_person_count = sum(
            (item.material_role == "authored_by_target" and bool(item.authors))
            or (
                item.material_role == "spoken_by_target"
                and bool(item.speakers)
                and item.speaker_attribution.status
                in {SpeakerStatus.ACCEPTED_MODEL, SpeakerStatus.VERIFIED_HUMAN}
            )
            for item in documents
        )
        expected_metrics = {
            "metadata_completeness": (
                sum(
                    bool(item.title)
                    and bool(item.canonical_url or item.local_source_path)
                    for item in documents
                )
                / len(documents)
                if documents
                else 0.0
            ),
            "first_person_ratio": (
                first_person_count / len(documents) if documents else 0.0
            ),
            "transcript_coverage": (
                len(transcribed_speech) / len(corpus_speech) if corpus_speech else 1.0
            ),
            "provider_cost_usd": provider_cost,
            "audiovisual_seconds_billed": billed_media_seconds,
            "verified_target_speech_seconds": _target_interval_seconds(verified_speech),
            "extraction_success_ratio": (
                len(successful_extraction_ids) / len(expected_extraction_ids)
                if expected_extraction_ids
                else 1.0
            ),
            "average_included_characters": (
                sum(len(item.text) for item in documents) / len(documents)
                if documents
                else 0.0
            ),
        }
        for name, expected in expected_metrics.items():
            actual = quality.metrics.get(name)
            if actual is None or not isclose(
                actual, expected, rel_tol=1e-9, abs_tol=1e-9
            ):
                errors.append(f"Quality metric does not match workspace: {name}")
        expected_passed = all(
            value
            for name, value in expected_checks.items()
            if name != "approved_work_complete"
        )
        if quality.passed != expected_passed:
            errors.append("Quality pass result does not match workspace")
    return VerificationResult(
        passed=not errors,
        errors=errors,
        checked_files=checked_files,
        checked_documents=len(documents),
    )

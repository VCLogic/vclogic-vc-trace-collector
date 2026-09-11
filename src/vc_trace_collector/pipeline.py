"""Resumable orchestration for discovery, collection, processing, and export."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .audit import AuditLog, BudgetLedger, redact
from .av import (
    DiarizationProvider,
    EmbeddingProvider,
    PyannoteDiarizationProvider,
    PyannoteEmbeddingProvider,
    TargetSpeechResult,
    TimedText,
    TranscriptProvider,
    TranscriptResult,
    WhisperTranscriptProvider,
    extract_audio_segment,
    probe_media_duration,
    process_target_speech,
)
from .collectors import (
    CollectionContext,
    CollectionResult,
    ReviewRequired,
    apply_decisions,
    auto_approve,
    collect_approved_sources,
    default_registry,
)
from .config import RunConfig
from .discovery import (
    DiscoveryResult,
    DiscoveryService,
    OpenAICompatibleDiscoveryProvider,
    SearxngSearchProvider,
)
from .export import export_workspace
from .fetch import Fetcher
from .models import (
    ApprovalStatus,
    AuditEvent,
    CanonicalDocument,
    CollectionManifest,
    EventStatus,
    InclusionStatus,
    MaterialRole,
    ReferenceVoiceCandidate,
    ReferenceVoiceProfile,
    ReferenceVoiceStatus,
    ResolutionStatus,
    ResolvedIdentity,
    RunSummary,
    SourceDecision,
    SourcePlan,
    SourceType,
    utc_now,
)
from .policy import ExclusionRule, RuleSet
from .process import (
    deduplicate,
    load_artifact_records,
    process_artifacts,
    target_speech_document,
)
from .storage import (
    ArtifactStore,
    StateStore,
    append_jsonl,
    canonical_json,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from .verify import VerificationResult, verify_workspace


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: ResolvedIdentity
    source_plan: SourcePlan


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investor_slug: str
    collection: CollectionResult | None = None
    manifest: CollectionManifest | None = None
    verification: VerificationResult | None = None


class Pipeline:
    def __init__(
        self,
        output_dir: Path | str = "outputs",
        *,
        fetcher: Fetcher | None = None,
        rules: RuleSet | None = None,
        search_provider=None,
        discovery_provider=None,
        transcript_provider: TranscriptProvider | None = None,
        diarization_provider: DiarizationProvider | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        audio_extractor: Callable[..., Path] = extract_audio_segment,
        media_probe: Callable[[Path], float | None] = probe_media_duration,
    ):
        self.output_dir = Path(output_dir)
        self.fetcher = fetcher or Fetcher()
        self.rules = rules or RuleSet.pitch_default()
        search_endpoint = os.environ.get("VC_TRACE_SEARCH_ENDPOINT", "").strip()
        self.search_provider = search_provider or (
            SearxngSearchProvider(search_endpoint) if search_endpoint else None
        )
        self.discovery_provider = discovery_provider
        self.transcript_provider = transcript_provider
        self.diarization_provider = diarization_provider
        self.embedding_provider = embedding_provider
        self.audio_extractor = audio_extractor
        self.media_probe = media_probe

    def workspace(self, investor_slug: str) -> Path:
        return self.output_dir / investor_slug

    def _run_id(self) -> str:
        return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"

    def _audit(self, workspace: Path) -> AuditLog:
        return AuditLog(workspace / "audit/events.jsonl")

    def _event(
        self,
        workspace: Path,
        run_id: str,
        stage: str,
        summary: str,
        *,
        status: EventStatus = EventStatus.SUCCEEDED,
        details: dict | None = None,
    ) -> None:
        self._audit(workspace).append(
            AuditEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                stage=stage,
                action=stage,
                status=status,
                summary=summary,
                details=details or {},
            )
        )

    def _rules_for(self, config: RunConfig) -> RuleSet:
        additions: list[ExclusionRule] = list(config.exclusion_rules)
        if config.excluded_domains or config.excluded_channels:
            additions.append(
                ExclusionRule(
                    rule_id="run-specific-exclusions",
                    version=config.fingerprint[:12],
                    action="exclude",
                    reason="Excluded by run configuration",
                    domains=config.excluded_domains,
                    channels=config.excluded_channels,
                )
            )
        return RuleSet(self.rules.rules + additions)

    def _provider_for(self, model: str | None):
        if self.discovery_provider:
            return self.discovery_provider
        endpoint = os.environ.get("VC_TRACE_LLM_ENDPOINT", "").strip()
        api_key = os.environ.get("VC_TRACE_LLM_API_KEY", "").strip()
        selected_model = model or os.environ.get("VC_TRACE_DISCOVERY_MODEL", "").strip()
        if endpoint and api_key and selected_model:
            return OpenAICompatibleDiscoveryProvider(endpoint, api_key, selected_model)
        return None

    def discover(
        self,
        *,
        name: str,
        firm: str | None = None,
        known_profile_url: str | None = None,
        source_urls: list[str] | None = None,
        supplied_files: list[Path] | None = None,
        supplied_role: MaterialRole = MaterialRole.UNKNOWN,
        exclusion_rules: list[ExclusionRule] | None = None,
        config: RunConfig | None = None,
    ) -> DiscoveryResult:
        config = config or RunConfig(
            name=name,
            firm=firm,
            known_profile_url=known_profile_url,
            source_urls=source_urls or [],
            supplied_files=[str(path) for path in supplied_files or []],
            supplied_role=supplied_role,
            exclusion_rules=exclusion_rules or [],
            output_dir=str(self.output_dir),
        )
        run_rules = self._rules_for(config)
        discovery_provider = self._provider_for(config.discovery_model)
        estimated_workspace = self.workspace(re_slug(name, firm))
        discovery_budget = BudgetLedger(
            estimated_workspace / "audit/costs.jsonl", config.maximum_cost_usd
        )
        discovery_operation_id = f"discover-llm:{config.fingerprint}"
        if discovery_provider is not None:
            if config.discovery_call_budget_usd is None:
                raise ValueError(
                    "Set discovery_call_budget_usd when enabling an LLM provider"
                )
            discovery_budget.reserve(
                discovery_operation_id, config.discovery_call_budget_usd
            )
        service = DiscoveryService(
            fetcher=self.fetcher,
            search_provider=self.search_provider,
            discovery_provider=discovery_provider,
            rules=run_rules,
            maximum_search_operations=config.maximum_search_operations,
        )
        try:
            result = service.discover(
                name=name,
                firm=firm,
                known_profile_url=known_profile_url,
                source_urls=source_urls or config.source_urls,
                supplied_files=supplied_files
                or [Path(path) for path in config.supplied_files],
                supplied_role=supplied_role or config.supplied_role,
            )
        except Exception:
            if discovery_provider is not None:
                discovery_budget.release(discovery_operation_id)
            raise
        if discovery_provider is not None:
            usage = getattr(discovery_provider, "last_usage", {})
            discovery_budget.settle(
                discovery_operation_id,
                config.discovery_call_budget_usd or 0,
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                provider=discovery_provider.provider_name,
                model=discovery_provider.model_name,
            )
        workspace = self.workspace(result.identity.slug)
        workspace.mkdir(parents=True, exist_ok=True)
        run_id = self._run_id()

        artifacts = ArtifactStore(workspace)
        for fetched in service.retrieved_fetches:
            stored = artifacts.put_bytes(
                fetched.content,
                category="web",
                suffix=".html",
                source_url=fetched.final_url,
                mime_type=fetched.headers.get("content-type", "text/html"),
                collection_method="identity_evidence_http",
                original_metadata={
                    "canonical_url": fetched.canonical_url,
                    "headers": fetched.headers,
                    "redirect_chain": fetched.redirect_chain,
                    "status_code": fetched.status_code,
                    "candidate_id": next(
                        (
                            candidate.candidate_id
                            for candidate in result.source_plan.candidates
                            if candidate.canonical_url == fetched.canonical_url
                        ),
                        "",
                    ),
                },
            )
            for evidence in result.evidence:
                if evidence.canonical_url == fetched.canonical_url:
                    evidence.raw_artifact_id = stored.record.artifact_id

        write_json(workspace / "identity/resolved_identity.json", result.identity)
        write_jsonl(workspace / "identity/identity_evidence.jsonl", result.evidence)
        write_jsonl(
            workspace / "identity/reference_voice_candidates.jsonl",
            result.reference_voice_candidates,
        )
        write_json(workspace / "discovery/source_plan.json", result.source_plan)
        self._write_candidate_views(workspace, result.source_plan)
        write_json(workspace / "config_snapshot.json", config)

        summary = RunSummary(
            run_id=run_id,
            investor_slug=result.identity.slug,
            status="review_required",
            started_at=utc_now(),
            stages={"discovery": "complete", "review": "pending"},
        )
        write_json(workspace / "run_summary.json", summary)
        write_json(workspace / f"audit/runs/{run_id}.json", summary)
        self._event(
            workspace,
            run_id,
            "discovery",
            "Identity evidence and a reviewable source plan were produced",
            details={
                "queries": result.source_plan.queries,
                "candidate_count": len(result.source_plan.candidates),
                "provider_operations": result.provider_operations,
            },
        )
        return result

    def _write_candidate_views(self, workspace: Path, plan: SourcePlan) -> None:
        write_jsonl(workspace / "discovery/source_candidates.jsonl", plan.candidates)
        write_jsonl(
            workspace / "discovery/approved_sources.jsonl",
            [
                candidate
                for candidate in plan.candidates
                if candidate.approval_status
                in {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
            ],
        )
        write_jsonl(
            workspace / "discovery/rejected_sources.jsonl",
            [
                candidate
                for candidate in plan.candidates
                if candidate.approval_status == ApprovalStatus.REJECTED
            ],
        )

    def _load_identity(self, slug: str) -> ResolvedIdentity:
        return ResolvedIdentity.model_validate(
            read_json(self.workspace(slug) / "identity/resolved_identity.json")
        )

    def _load_plan(self, slug: str) -> SourcePlan:
        return SourcePlan.model_validate(
            read_json(self.workspace(slug) / "discovery/source_plan.json")
        )

    def review(
        self,
        investor_slug: str,
        *,
        decisions: list[SourceDecision],
        reviewer: str,
        confirm_identity: bool = False,
    ) -> ReviewResult:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        if not confirm_identity:
            raise ReviewRequired("Human review requires explicit identity confirmation")
        plan = apply_decisions(self._load_plan(investor_slug), decisions)
        identity.resolution_status = ResolutionStatus.CONFIRMED
        identity.reviewed_by = reviewer
        identity.resolved_at = utc_now()
        write_json(workspace / "identity/resolved_identity.json", identity)
        write_json(workspace / "discovery/source_plan.json", plan)
        voice_path = workspace / "identity/reference_voice_candidates.jsonl"
        voice_candidates = [
            ReferenceVoiceCandidate.model_validate(row)
            for row in read_jsonl(voice_path)
        ]
        voice_source_ids = {item.source_candidate_id for item in voice_candidates}
        for candidate in plan.candidates:
            if (
                candidate.material_role == MaterialRole.REFERENCE_VOICE
                and candidate.candidate_id not in voice_source_ids
            ):
                voice_candidates.append(
                    ReferenceVoiceCandidate(
                        candidate_id=f"voice:{candidate.candidate_id}",
                        source_candidate_id=candidate.candidate_id,
                        source_url=candidate.canonical_url,
                        identity_confidence=candidate.identity_confidence,
                    )
                )
        write_jsonl(voice_path, voice_candidates)
        self._write_candidate_views(workspace, plan)
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        summary.status = "reviewed"
        summary.stages["review"] = "complete"
        write_json(workspace / "run_summary.json", summary)
        self._event(
            workspace,
            summary.run_id,
            "review",
            "Identity and source decisions were reviewed",
            details={
                "reviewer": reviewer,
                "decisions": [
                    decision.model_dump(mode="json") for decision in decisions
                ],
            },
        )
        return ReviewResult(identity=identity, source_plan=plan)

    def _automatic_review(self, slug: str, config: RunConfig) -> ReviewResult:
        workspace = self.workspace(slug)
        identity = self._load_identity(slug)
        if identity.competing_hypotheses:
            raise ReviewRequired(
                "Human review required while a competing identity hypothesis remains"
            )
        if identity.identity_confidence.score < 0.9:
            raise ReviewRequired("Identity confidence is below the automatic threshold")
        plan = auto_approve(
            self._load_plan(slug), minimum_identity=0.9, minimum_source=0.85
        )
        identity.resolution_status = ResolutionStatus.CONFIRMED
        identity.reviewed_by = "automatic_policy"
        identity.resolved_at = utc_now()
        write_json(workspace / "identity/resolved_identity.json", identity)
        write_json(workspace / "discovery/source_plan.json", plan)
        self._write_candidate_views(workspace, plan)
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        summary.stages["review"] = "complete"
        summary.status = "reviewed"
        write_json(workspace / "run_summary.json", summary)
        self._event(
            workspace,
            summary.run_id,
            "review",
            "Identity and sources passed explicit automatic review thresholds",
            details={
                "config_fingerprint": config.fingerprint,
                "approved_candidates": [
                    candidate.candidate_id
                    for candidate in plan.candidates
                    if candidate.approval_status == ApprovalStatus.AUTO_APPROVED
                ],
            },
        )
        return ReviewResult(identity=identity, source_plan=plan)

    def collect_sources(self, investor_slug: str) -> CollectionResult:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = self._load_plan(investor_slug)
        if identity.resolution_status != ResolutionStatus.CONFIRMED:
            raise ReviewRequired("Identity review required before collection")
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        context = CollectionContext(
            workspace=workspace,
            artifacts=ArtifactStore(workspace),
            state=StateStore(workspace / "state/state.sqlite"),
            rules=self._rules_for(config),
            run_id=summary.run_id,
            fetcher=self.fetcher,
            audit=self._audit(workspace),
            budget=BudgetLedger(
                workspace / "audit/costs.jsonl", config.maximum_cost_usd
            ),
            approved_source_types=(
                {SourceType(value) for value in config.approved_source_types}
                if config.approved_source_types
                else None
            ),
            maximum_media_seconds=config.maximum_media_minutes * 60,
        )
        result = collect_approved_sources(
            plan, context=context, registry=default_registry()
        )
        voice_path = workspace / "identity/reference_voice_candidates.jsonl"
        voice_candidates = [
            ReferenceVoiceCandidate.model_validate(row)
            for row in read_jsonl(voice_path)
        ]
        voice_by_source = {
            candidate.source_candidate_id: candidate for candidate in voice_candidates
        }
        for artifact in result.artifacts:
            candidate_id = str(artifact.original_metadata.get("candidate_id", ""))
            voice = voice_by_source.get(candidate_id)
            if voice and (
                (artifact.mime_type or "").casefold().startswith("audio/")
                or Path(artifact.relative_path).suffix.casefold()
                in {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac"}
            ):
                voice.artifact_id = artifact.artifact_id
        write_jsonl(voice_path, voice_candidates)
        summary.stages["collection"] = "complete"
        summary.status = "collected"
        summary.collected += result.collected
        summary.failures += result.failed
        summary.excluded += result.excluded
        summary.cost_usd = context.budget.spent
        write_json(workspace / "run_summary.json", summary)
        return result

    def approve_reference_voice(
        self,
        investor_slug: str,
        *,
        candidate_id: str,
        reviewer: str,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> ReferenceVoiceProfile:
        """Human-approve a public clip and persist its reproducible voice embedding."""
        workspace = self.workspace(investor_slug)
        candidates_path = workspace / "identity/reference_voice_candidates.jsonl"
        candidates = [
            ReferenceVoiceCandidate.model_validate(row)
            for row in read_jsonl(candidates_path)
        ]
        try:
            selected = next(
                item for item in candidates if item.candidate_id == candidate_id
            )
        except StopIteration as error:
            raise KeyError(
                f"Unknown reference voice candidate: {candidate_id}"
            ) from error
        if not selected.artifact_id:
            raise ReviewRequired(
                "Collect the reference source before approving its voice"
            )
        records = load_artifact_records(workspace)
        try:
            artifact = next(
                item for item in records if item.artifact_id == selected.artifact_id
            )
        except StopIteration as error:
            raise FileNotFoundError(
                "Reference voice raw artifact is missing"
            ) from error
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        provider = self.embedding_provider
        if provider is None:
            if not config.embedding_model:
                raise ReviewRequired(
                    "Configure an embedding model before voice approval"
                )
            provider = PyannoteEmbeddingProvider(
                config.embedding_model,
                token=os.environ.get("HF_TOKEN"),
                device=os.environ.get("VC_TRACE_AV_DEVICE"),
            )
        source_path = workspace / artifact.relative_path
        reference_artifact = artifact
        if start_seconds is not None or end_seconds is not None:
            if start_seconds is None or end_seconds is None:
                raise ValueError("Both reference start and end seconds are required")
            with tempfile.TemporaryDirectory(prefix="vc-trace-reference-") as directory:
                extracted = self.audio_extractor(
                    source_path,
                    Path(directory) / "reference.wav",
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                )
                reference_artifact = (
                    ArtifactStore(workspace)
                    .put_bytes(
                        extracted.read_bytes(),
                        category="voice",
                        suffix=".wav",
                        source_url=artifact.source_url,
                        mime_type="audio/wav",
                        collection_method="human_selected_reference_segment",
                        original_metadata={
                            "candidate_id": selected.source_candidate_id,
                            "reference_candidate_id": selected.candidate_id,
                            "reviewer": reviewer,
                            "start_seconds": start_seconds,
                            "end_seconds": end_seconds,
                        },
                        parent_artifact_ids=[artifact.artifact_id],
                    )
                    .record
                )
                embedding = provider.embed(workspace / reference_artifact.relative_path)
        else:
            embedding = provider.embed(source_path)
        selected.start_seconds = start_seconds
        selected.end_seconds = end_seconds
        selected.status = ReferenceVoiceStatus.VERIFIED_HUMAN
        selected.reviewed_by = reviewer
        selected.artifact_id = reference_artifact.artifact_id
        write_jsonl(candidates_path, candidates)
        profile = ReferenceVoiceProfile(
            investor_slug=investor_slug,
            candidate_ids=[selected.candidate_id],
            artifact_ids=[reference_artifact.artifact_id],
            status=ReferenceVoiceStatus.VERIFIED_HUMAN,
            embedding_model=provider.model_name,
            embedding_model_version=str(
                getattr(provider, "model_version", "unspecified")
            ),
            embedding=embedding,
        )
        write_json(workspace / "identity/reference_voice_profile.json", profile)
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        self._event(
            workspace,
            summary.run_id,
            "reference_voice",
            "Human-approved public voice reference embedded",
            details={
                "candidate_id": candidate_id,
                "artifact_id": reference_artifact.artifact_id,
                "reviewer": reviewer,
                "embedding_provider": provider.provider_name,
                "embedding_model": provider.model_name,
            },
        )
        return profile

    @staticmethod
    def _caption_transcript(
        workspace: Path, artifacts: list, candidate_id: str
    ) -> TranscriptResult | None:
        for artifact in artifacts:
            if artifact.collection_method != "youtube_captions":
                continue
            if str(artifact.original_metadata.get("candidate_id", "")) != candidate_id:
                continue
            try:
                rows = read_json(workspace / artifact.relative_path)
                segments = [TimedText.from_caption(row) for row in rows]
            except Exception:
                return None
            return TranscriptResult(
                info={
                    "method": "existing_caption",
                    "provider": "youtube",
                    "model": None,
                    "source_artifact_id": artifact.artifact_id,
                },
                segments=segments,
            )
        return None

    def _av_providers(
        self, config: RunConfig, *, needs_transcript: bool
    ) -> tuple[TranscriptProvider | None, DiarizationProvider]:
        transcript = self.transcript_provider
        diarization = self.diarization_provider
        if needs_transcript and transcript is None:
            if not config.transcription_model:
                raise ReviewRequired(
                    "No existing transcript; configure a transcription model"
                )
            transcript = WhisperTranscriptProvider(config.transcription_model)
        if diarization is None:
            if not config.diarization_model:
                raise ReviewRequired("Configure a diarization model for target speech")
            diarization = PyannoteDiarizationProvider(
                config.diarization_model,
                token=os.environ.get("HF_TOKEN"),
                device=os.environ.get("VC_TRACE_AV_DEVICE"),
            )
        return transcript, diarization

    def process(self, investor_slug: str) -> list[CanonicalDocument]:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = self._load_plan(investor_slug)
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        artifacts = load_artifact_records(workspace)
        documents = process_artifacts(
            workspace,
            artifacts,
            plan.candidates,
            investor_slug=investor_slug,
            target_name=identity.canonical_name,
            rules=self._rules_for(config),
        )
        profile_path = workspace / "identity/reference_voice_profile.json"
        profile = (
            ReferenceVoiceProfile.model_validate(read_json(profile_path))
            if profile_path.exists()
            else None
        )
        candidate_map = {item.candidate_id: item for item in plan.candidates}
        audio_suffixes = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac"}
        video_suffixes = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
        processed_seconds = 0.0
        av_rows: list[dict] = []
        state = StateStore(workspace / "state/state.sqlite")
        cost = BudgetLedger(workspace / "audit/costs.jsonl", config.maximum_cost_usd)
        for artifact in artifacts:
            candidate_id = str(artifact.original_metadata.get("candidate_id", ""))
            candidate = candidate_map.get(candidate_id)
            path = workspace / artifact.relative_path
            is_audio = (artifact.mime_type or "").casefold().startswith("audio/") or (
                path.suffix.casefold() in audio_suffixes
            )
            is_video = (artifact.mime_type or "").casefold().startswith("video/") or (
                path.suffix.casefold() in video_suffixes
            )
            if not candidate or not (is_audio or is_video):
                continue
            if candidate.material_role != MaterialRole.SPOKEN_BY_TARGET:
                continue
            if profile is None or profile.status != ReferenceVoiceStatus.VERIFIED_HUMAN:
                failure = {
                    "candidate_id": candidate_id,
                    "artifact_id": artifact.artifact_id,
                    "message": "Verified reference voice profile required",
                }
                append_jsonl(workspace / "audit/failures.jsonl", failure)
                continue
            processing_artifact = artifact
            if is_video:
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="vc-trace-video-audio-"
                    ) as directory:
                        extracted = self.audio_extractor(
                            path, Path(directory) / "audio.wav"
                        )
                        processing_artifact = (
                            ArtifactStore(workspace)
                            .put_bytes(
                                extracted.read_bytes(),
                                category="video",
                                suffix=".wav",
                                source_url=artifact.source_url,
                                source_path=artifact.source_path,
                                mime_type="audio/wav",
                                collection_method="ffmpeg_audio_extraction",
                                original_metadata={
                                    "candidate_id": candidate_id,
                                    "source_artifact_id": artifact.artifact_id,
                                },
                                parent_artifact_ids=[artifact.artifact_id],
                            )
                            .record
                        )
                except Exception as error:
                    append_jsonl(
                        workspace / "audit/failures.jsonl",
                        redact(
                            {
                                "candidate_id": candidate_id,
                                "artifact_id": artifact.artifact_id,
                                "error_type": type(error).__name__,
                                "message": str(error),
                            }
                        ),
                    )
                    continue
                path = workspace / processing_artifact.relative_path
            existing = self._caption_transcript(workspace, artifacts, candidate_id)
            probed_seconds = self.media_probe(path)
            if (
                probed_seconds is not None
                and processed_seconds + probed_seconds
                > config.maximum_media_minutes * 60
            ):
                append_jsonl(
                    workspace / "audit/failures.jsonl",
                    {
                        "candidate_id": candidate_id,
                        "artifact_id": artifact.artifact_id,
                        "message": "Media processing budget exhausted before model calls",
                        "media_seconds": probed_seconds,
                    },
                )
                continue
            operation_id = (
                f"process-av:{candidate_id}:{processing_artifact.artifact_id}"
            )
            input_hash = sha256(
                canonical_json(
                    {
                        "artifact": processing_artifact.sha256,
                        "profile": profile.model_dump(mode="json"),
                        "transcription_model": config.transcription_model,
                        "diarization_model": config.diarization_model,
                        "minimum_score": config.speaker_minimum_score,
                        "minimum_margin": config.speaker_minimum_margin,
                        "caption_artifact": (
                            existing.info.source_artifact_id if existing else None
                        ),
                        "probed_media_seconds": probed_seconds,
                    }
                ).encode("utf-8")
            ).hexdigest()
            cache_path = workspace / "state/av_results" / f"{input_hash}.json"
            try:
                if state.is_complete(operation_id, input_hash) and cache_path.exists():
                    result = TargetSpeechResult.model_validate(read_json(cache_path))
                else:
                    transcript_provider, diarization_provider = self._av_providers(
                        config, needs_transcript=existing is None
                    )
                    state.start_operation(operation_id, input_hash)
                    cost.reserve(operation_id, 0)
                    result = process_target_speech(
                        path,
                        reference=profile,
                        transcript_provider=transcript_provider,
                        existing_transcript=existing,
                        diarization_provider=diarization_provider,
                        minimum_score=config.speaker_minimum_score,
                        minimum_margin=config.speaker_minimum_margin,
                    )
                    write_json(cache_path, result)
                    cost.settle(
                        operation_id,
                        0,
                        media_seconds=result.media_seconds,
                        provider=diarization_provider.provider_name,
                        model=diarization_provider.model_name,
                    )
                    state.finish_operation(operation_id, input_hash, str(cache_path))
            except Exception as error:
                cost.release(operation_id)
                state.fail_operation(operation_id, input_hash, str(redact(str(error))))
                failure = redact(
                    {
                        "candidate_id": candidate_id,
                        "artifact_id": artifact.artifact_id,
                        "error_type": type(error).__name__,
                        "message": str(error),
                    }
                )
                append_jsonl(workspace / "audit/failures.jsonl", failure)
                continue
            if (
                processed_seconds + result.media_seconds
                > config.maximum_media_minutes * 60
            ):
                failure = {
                    "candidate_id": candidate_id,
                    "artifact_id": artifact.artifact_id,
                    "message": "Media processing budget exhausted",
                }
                append_jsonl(workspace / "audit/failures.jsonl", failure)
                continue
            processed_seconds += result.media_seconds
            av_rows.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "candidate_id": candidate_id,
                    "result": result.model_dump(mode="json"),
                }
            )
            document = target_speech_document(
                investor_slug=investor_slug,
                target_name=identity.canonical_name,
                artifact=processing_artifact,
                candidate=candidate,
                result=result,
                rules=self._rules_for(config),
            )
            if document:
                documents.append(document)
            summary_for_event = RunSummary.model_validate(
                read_json(workspace / "run_summary.json")
            )
            self._event(
                workspace,
                summary_for_event.run_id,
                "audiovisual_processing",
                "Transcribed, diarized, and matched target speaker",
                details={
                    "candidate_id": candidate_id,
                    "artifact_id": artifact.artifact_id,
                    "transcript": result.transcript.model_dump(mode="json"),
                    "attribution": result.attribution.model_dump(mode="json"),
                    "media_seconds": result.media_seconds,
                },
            )
        documents = deduplicate(documents)
        write_jsonl(workspace / "processed/av_attribution_results.jsonl", av_rows)
        write_jsonl(workspace / "processed/documents.jsonl", documents)
        write_jsonl(
            workspace / "processed/excluded_documents.jsonl",
            [
                document
                for document in documents
                if document.inclusion_status != "included"
            ],
        )
        write_jsonl(
            workspace / "processed/target_speech.jsonl",
            [
                document
                for document in documents
                if document.material_role == "spoken_by_target"
            ],
        )
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        summary.stages["processing"] = "complete"
        summary.status = "processed"
        summary.processed = len(documents)
        summary.media_seconds = processed_seconds
        write_json(workspace / "run_summary.json", summary)
        return documents

    def export(self, investor_slug: str) -> CollectionManifest:
        workspace = self.workspace(investor_slug)
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        documents = [
            CanonicalDocument.model_validate(row)
            for row in read_jsonl(workspace / "processed/documents.jsonl")
        ]
        plan = self._load_plan(investor_slug)
        candidate_map = {item.candidate_id: item for item in plan.candidates}
        artifact_map: dict[str, list] = {}
        for artifact in load_artifact_records(workspace):
            artifact_map.setdefault(artifact.artifact_id, []).append(artifact)
        rules = self._rules_for(config)
        for document in documents:
            candidate = candidate_map.get(document.source_candidate_id)
            urls = {
                value
                for value in (
                    document.canonical_url,
                    candidate.url if candidate else None,
                    candidate.canonical_url if candidate else None,
                )
                if value
            }
            for artifact_id in document.raw_artifact_ids:
                for artifact in artifact_map.get(artifact_id, []):
                    if artifact.source_url:
                        urls.add(artifact.source_url)
                    for key in ("requested_url", "canonical_url", "episode_url"):
                        value = artifact.original_metadata.get(key)
                        if isinstance(value, str) and value:
                            urls.add(value)
            decisions = [
                rules.evaluate(
                    url=url,
                    title=document.title,
                    text=document.text,
                    stage="final_export",
                )
                for url in sorted(urls)
            ]
            excluded = next(
                (item for item in decisions if item.status == InclusionStatus.EXCLUDED),
                None,
            )
            review = next(
                (
                    item
                    for item in decisions
                    if item.status == InclusionStatus.REVIEW_REQUIRED
                ),
                None,
            )
            if excluded:
                document.inclusion_status = InclusionStatus.EXCLUDED
                document.exclusion_reason = excluded.reason
            elif review and document.inclusion_status == InclusionStatus.INCLUDED:
                document.inclusion_status = InclusionStatus.REVIEW_REQUIRED
                document.exclusion_reason = review.reason
        rules_payload = [rule.model_dump(mode="json") for rule in rules.rules]
        write_json(workspace / "exclusion_rules_snapshot.json", rules_payload)
        manifest = export_workspace(
            workspace,
            investor_slug=investor_slug,
            identity_id=f"identity:{investor_slug}",
            documents=documents,
            config_hash=config.fingerprint,
            exclusion_rules_hash=sha256(
                canonical_json(rules_payload).encode("utf-8")
            ).hexdigest(),
        )
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        summary.stages["export"] = "complete"
        summary.status = "exported"
        summary.finished_at = utc_now()
        write_json(workspace / "run_summary.json", summary)
        return manifest

    def verify(self, investor_slug: str) -> VerificationResult:
        result = verify_workspace(self.workspace(investor_slug))
        workspace = self.workspace(investor_slug)
        if (workspace / "run_summary.json").exists():
            summary = RunSummary.model_validate(
                read_json(workspace / "run_summary.json")
            )
            summary.stages["verification"] = "passed" if result.passed else "failed"
            summary.status = "verified" if result.passed else "verification_failed"
            write_json(workspace / "run_summary.json", summary)
        return result

    def collect(
        self,
        *,
        name: str,
        firm: str | None = None,
        known_profile_url: str | None = None,
        source_urls: list[str] | None = None,
        supplied_files: list[Path] | None = None,
        supplied_role: MaterialRole = MaterialRole.UNKNOWN,
        auto_approve_discovery: bool = False,
        resume: str | None = None,
        collection_only: bool = False,
        processing_only: bool = False,
        export_only: bool = False,
        **options,
    ) -> PipelineResult:
        config = RunConfig(
            name=name,
            firm=firm,
            known_profile_url=known_profile_url,
            source_urls=source_urls or [],
            supplied_files=[str(path) for path in supplied_files or []],
            supplied_role=supplied_role,
            output_dir=str(self.output_dir),
            automatic_discovery=auto_approve_discovery,
            resume=resume,
            **options,
        )
        guessed_slug = re_slug(name, firm)
        workspace = self.workspace(guessed_slug)
        if processing_only:
            self.process(guessed_slug)
            return PipelineResult(investor_slug=guessed_slug)
        if export_only:
            manifest = self.export(guessed_slug)
            verification = self.verify(guessed_slug)
            return PipelineResult(
                investor_slug=guessed_slug, manifest=manifest, verification=verification
            )
        if resume and workspace.exists():
            identity = self._load_identity(guessed_slug)
        else:
            discovery = self.discover(
                name=name,
                firm=firm,
                known_profile_url=known_profile_url,
                source_urls=source_urls,
                supplied_files=supplied_files,
                supplied_role=supplied_role,
                config=config,
            )
            guessed_slug = discovery.identity.slug
            identity = discovery.identity
        if identity.resolution_status != ResolutionStatus.CONFIRMED:
            if auto_approve_discovery:
                self._automatic_review(guessed_slug, config)
            else:
                raise ReviewRequired("Identity and source-plan review required")
        source_result = self.collect_sources(guessed_slug)
        if collection_only:
            return PipelineResult(investor_slug=guessed_slug, collection=source_result)
        self.process(guessed_slug)
        manifest = self.export(guessed_slug)
        verification = self.verify(guessed_slug)
        return PipelineResult(
            investor_slug=guessed_slug,
            collection=source_result,
            manifest=manifest,
            verification=verification,
        )

    def status(self, investor_slug: str) -> dict:
        workspace = self.workspace(investor_slug)
        summary = read_json(workspace / "run_summary.json")
        identity = self._load_identity(investor_slug)
        state_path = workspace / "state/state.sqlite"
        summary["identity_status"] = identity.resolution_status.value
        summary["operations"] = (
            StateStore(state_path).operation_counts() if state_path.exists() else {}
        )
        return summary


def re_slug(name: str, firm: str | None = None) -> str:
    import re

    value = f"{name}-{firm}" if firm else name
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "investor"

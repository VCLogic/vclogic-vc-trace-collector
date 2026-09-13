"""Resumable orchestration for discovery, collection, processing, and export."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .audit import AuditLog, BudgetExceeded, BudgetLedger, redact
from .av import (
    DiarizationProvider,
    EmbeddingProvider,
    PyannoteDiarizationProvider,
    PyannoteEmbeddingProvider,
    TargetSpeechResult,
    TranscriptProvider,
    WhisperTranscriptProvider,
    extract_audio_segment,
    probe_media_duration,
    process_target_speech,
)
from .collectors import (
    NETWORK_COLLECTION_METHODS,
    CollectionCandidateOutcome,
    CollectionContext,
    CollectionResult,
    ReviewRequired,
    apply_decisions,
    auto_approve,
    collect_approved_sources,
    default_registry,
    is_acquired_media,
)
from .config import RunConfig
from .discovery import (
    DiscoveryResult,
    DiscoveryService,
    OpenAICompatibleDiscoveryProvider,
    SearchResult,
    SearxngSearchProvider,
    stable_id,
)
from .export import export_workspace
from .fetch import Fetcher
from .models import (
    ApprovalStatus,
    AuditEvent,
    AVCandidateOutcome,
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
from .source_search import (
    SourceSearchObservation,
    SourceSearchSummary,
    build_source_queries,
    candidate_from_search_result,
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
        self.fetcher = fetcher or Fetcher(
            trust_env=os.environ.get("VC_TRACE_ALLOW_ENV_PROXY") == "1"
        )
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

    def _configure_stage(
        self, workspace: Path, stage: str, **updates: object | None
    ) -> RunConfig:
        config_path = workspace / "config_snapshot.json"
        config = RunConfig.model_validate(read_json(config_path))
        supplied = {key: value for key, value in updates.items() if value is not None}
        if not supplied:
            return config
        payload = config.model_dump()
        payload.update(supplied)
        updated = RunConfig.model_validate(payload)
        changes = {
            key: {"from": getattr(config, key), "to": getattr(updated, key)}
            for key in supplied
            if getattr(config, key) != getattr(updated, key)
        }
        if not changes:
            return config
        write_json(config_path, updated)
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        self._event(
            workspace,
            summary.run_id,
            "configuration",
            f"Updated {stage} configuration",
            details={"stage": stage, "changes": changes},
        )
        return updated

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
            estimated_workspace / "audit/costs.jsonl",
            config.maximum_cost_usd,
            maximum_provider_operations=config.maximum_provider_operations,
            maximum_media_seconds=config.maximum_media_minutes * 60,
        )
        discovery_operation_id = f"discover-llm:{config.fingerprint}"
        if discovery_provider is not None and config.discovery_call_budget_usd is None:
            raise ValueError(
                "Set discovery_call_budget_usd when enabling an LLM provider"
            )
        service = DiscoveryService(
            fetcher=self.fetcher,
            search_provider=(
                self.search_provider if config.public_search_enabled else None
            ),
            discovery_provider=discovery_provider,
            rules=run_rules,
            maximum_search_operations=config.maximum_search_operations,
            maximum_download_bytes=config.maximum_download_bytes,
            budget=discovery_budget,
            search_operation_cost_usd=config.search_operation_cost_usd,
            discovery_operation_id=discovery_operation_id,
            discovery_operation_cost_usd=config.discovery_call_budget_usd or 0,
            maximum_provider_operations=config.maximum_provider_operations,
        )
        result = service.discover(
            name=name,
            firm=firm,
            known_profile_url=known_profile_url,
            source_urls=source_urls or config.source_urls,
            supplied_files=supplied_files
            or [Path(path) for path in config.supplied_files],
            supplied_role=supplied_role or config.supplied_role,
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
            downloaded_bytes=service.downloaded_bytes,
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

    def _save_plan(self, slug: str, plan: SourcePlan) -> None:
        workspace = self.workspace(slug)
        write_json(workspace / "discovery/source_plan.json", plan)
        self._write_candidate_views(workspace, plan)

    def search_source(
        self,
        investor_slug: str,
        source_type: SourceType,
        *,
        search_provider=None,
        maximum_queries: int | None = None,
        limit_per_query: int = 10,
    ) -> SourceSearchSummary:
        """Append candidates found by one source-specific search operation."""
        provider = search_provider or self.search_provider
        if provider is None:
            raise RuntimeError("No public search provider is configured")
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = self._load_plan(investor_slug)
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        queries = build_source_queries(
            identity.canonical_name,
            [affiliation.firm for affiliation in identity.affiliations],
            source_type,
        )
        query_limit = (
            config.maximum_search_operations
            if maximum_queries is None
            else min(maximum_queries, config.maximum_search_operations)
        )
        selected_queries = queries[: max(0, query_limit)]
        by_url = {candidate.canonical_url: candidate for candidate in plan.candidates}
        summary = SourceSearchSummary(source_type=source_type, queries=selected_queries)
        operations: list[dict[str, object]] = []
        updated_urls: set[str] = set()
        state = StateStore(workspace / "state/state.sqlite")
        budget = BudgetLedger(
            workspace / "audit/costs.jsonl",
            config.maximum_cost_usd,
            maximum_provider_operations=config.maximum_provider_operations,
        )
        prior_cost_rows = read_jsonl(workspace / "audit/costs.jsonl")
        search_attempts = sum(
            row.get("kind") == "settlement"
            and str(row.get("operation_id", "")).startswith(
                ("search:", "search-source:")
            )
            for row in prior_cost_rows
        )
        for query in selected_queries:
            new_diagnostics: list[dict[str, str]] = []
            provider_cache_identity = getattr(
                provider, "cache_identity", provider.provider_name
            )
            input_hash = sha256(
                canonical_json(
                    {
                        "provider": provider_cache_identity,
                        "query": query,
                        "limit": limit_per_query,
                    }
                ).encode("utf-8")
            ).hexdigest()
            operation_id = f"search-source:{source_type.value}:{input_hash[:20]}"
            cache_path = workspace / "state/source_search" / f"{input_hash}.json"
            cached = state.is_complete(operation_id, input_hash) and cache_path.exists()
            if cached:
                results = [
                    SearchResult.model_validate(row)
                    for row in read_json(cache_path).get("results", [])
                ]
            else:
                if search_attempts >= config.maximum_search_operations:
                    raise BudgetExceeded(operation_id)
                budget.reserve(
                    operation_id,
                    config.search_operation_cost_usd,
                    provider=provider.provider_name,
                )
                state.start_operation(operation_id, input_hash)
                diagnostics = getattr(provider, "diagnostics", [])
                diagnostic_count = len(diagnostics)
                try:
                    results = provider.search(query, limit=limit_per_query)
                except Exception as error:
                    budget.settle(
                        operation_id,
                        config.search_operation_cost_usd,
                        provider=provider.provider_name,
                    )
                    state.fail_operation(operation_id, input_hash, str(redact(str(error))))
                    raise
                budget.settle(
                    operation_id,
                    config.search_operation_cost_usd,
                    provider=provider.provider_name,
                )
                search_attempts += 1
                new_diagnostics = diagnostics[diagnostic_count:]
                if new_diagnostics:
                    summary.failed += 1
                    state.fail_operation(
                        operation_id,
                        input_hash,
                        str(new_diagnostics[0].get("error", "search failed")),
                    )
                else:
                    write_json(
                        cache_path,
                        {
                            "query": query,
                            "requested_provider": provider.provider_name,
                            "provider_cache_identity": provider_cache_identity,
                            "results": [
                                result.model_dump(mode="json") for result in results
                            ]
                        },
                    )
                    state.finish_operation(operation_id, input_hash, str(cache_path))
            summary.result_count += len(results)
            if results:
                for result in results:
                    append_jsonl(
                        workspace / "discovery/search_observations.jsonl",
                        SourceSearchObservation(
                            operation_id=operation_id,
                            source_type=source_type,
                            query=query,
                            requested_provider=provider.provider_name,
                            result_provider=result.provider,
                            status="succeeded",
                            cached=cached,
                            rank=result.rank,
                            url=result.url,
                            title=result.title,
                        ),
                    )
            else:
                append_jsonl(
                    workspace / "discovery/search_observations.jsonl",
                    SourceSearchObservation(
                        operation_id=operation_id,
                        source_type=source_type,
                        query=query,
                        requested_provider=provider.provider_name,
                        status="failed" if new_diagnostics else "succeeded",
                        cached=cached,
                        error=(
                            new_diagnostics[0].get("error")
                            if new_diagnostics
                            else None
                        ),
                    ),
                )
            operations.append(
                {
                    "provider": provider.provider_name,
                    "provider_cache_identity": provider_cache_identity,
                    "query": query,
                    "results": len(results),
                    "cached": cached,
                    "diagnostics": new_diagnostics,
                }
            )
            for result in results:
                candidate = candidate_from_search_result(
                    identity=identity,
                    source_type=source_type,
                    result=result,
                    rules=self._rules_for(config),
                )
                existing = by_url.get(candidate.canonical_url)
                if existing:
                    existing.discovery_queries = sorted(
                        set(existing.discovery_queries + candidate.discovery_queries)
                    )
                    updated_urls.add(candidate.canonical_url)
                    continue
                by_url[candidate.canonical_url] = candidate
                summary.added += 1
        summary.updated = len(updated_urls)
        previous_plan_id = plan.plan_id
        plan.queries = sorted(set(plan.queries + selected_queries))
        plan.candidates = sorted(by_url.values(), key=lambda item: item.canonical_url)
        plan.plan_id = stable_id(
            "plan",
            canonical_json(
                {
                    "investor_slug": plan.investor_slug,
                    "queries": plan.queries,
                    "candidates": [
                        candidate.model_dump(mode="json")
                        for candidate in plan.candidates
                    ],
                }
            ),
        )
        self._save_plan(investor_slug, plan)
        run_summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        run_summary.cost_usd = budget.spent
        write_json(workspace / "run_summary.json", run_summary)
        self._event(
            workspace,
            run_summary.run_id,
            "source_search",
            f"Searched {source_type.value} and updated the source plan",
            details={
                **summary.model_dump(mode="json"),
                "previous_plan_id": previous_plan_id,
                "plan_id": plan.plan_id,
                "provider_operations": operations,
            },
        )
        return summary

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
        write_json(
            workspace / "identity/identity_review.json",
            {
                "investor_slug": investor_slug,
                "status": "confirmed",
                "reviewed_by": reviewer,
                "reviewed_at": identity.resolved_at,
            },
        )
        write_json(workspace / "discovery/source_plan.json", plan)
        decision_path = workspace / "discovery/source_decisions.jsonl"
        existing_decisions = read_jsonl(decision_path)
        write_jsonl(decision_path, [*existing_decisions, *decisions])
        voice_path = workspace / "identity/reference_voice_candidates.jsonl"
        voice_candidates = [
            ReferenceVoiceCandidate.model_validate(row)
            for row in read_jsonl(voice_path)
        ]
        voice_source_ids = {item.source_candidate_id for item in voice_candidates}
        for candidate in plan.candidates:
            if (
                candidate.material_role
                in {MaterialRole.REFERENCE_VOICE, MaterialRole.SPOKEN_BY_TARGET}
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
        identity.reviewed_by = "automatic-confidence-policy"
        identity.resolved_at = utc_now()
        write_json(workspace / "identity/resolved_identity.json", identity)
        write_json(
            workspace / "identity/identity_review.json",
            {
                "investor_slug": slug,
                "status": "confirmed",
                "reviewed_by": "automatic-confidence-policy",
                "reviewed_at": identity.resolved_at,
                "policy": "automatic identity and source confidence thresholds",
            },
        )
        write_json(workspace / "discovery/source_plan.json", plan)
        write_jsonl(
            workspace / "discovery/source_decisions.jsonl",
            [
                SourceDecision(
                    candidate_id=candidate.candidate_id,
                    status=candidate.approval_status,
                    reason=candidate.decision_reason
                    or "Passed automatic approval policy",
                    decided_by=candidate.reviewed_by or "automatic-confidence-policy",
                    decided_at=candidate.decision_at or utc_now(),
                )
                for candidate in plan.candidates
                if candidate.approval_status == ApprovalStatus.AUTO_APPROVED
            ],
        )
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

    def collect_sources(
        self,
        investor_slug: str,
        *,
        source_types: set[SourceType] | None = None,
        candidate_ids: set[str] | None = None,
    ) -> CollectionResult:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = self._load_plan(investor_slug)
        if identity.resolution_status != ResolutionStatus.CONFIRMED:
            raise ReviewRequired("Identity review required before collection")
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        existing_artifacts = load_artifact_records(workspace)
        already_collected_media = sum(
            float(artifact.original_metadata.get("duration_seconds") or 0)
            for artifact in existing_artifacts
            if is_acquired_media(artifact)
        )
        already_downloaded_bytes = max(
            summary.downloaded_bytes,
            sum(
                artifact.size_bytes
                for artifact in existing_artifacts
                if artifact.collection_method in NETWORK_COLLECTION_METHODS
            ),
        )
        context = CollectionContext(
            workspace=workspace,
            artifacts=ArtifactStore(workspace),
            state=StateStore(workspace / "state/state.sqlite"),
            rules=self._rules_for(config),
            run_id=summary.run_id,
            fetcher=self.fetcher,
            audit=self._audit(workspace),
            budget=BudgetLedger(
                workspace / "audit/costs.jsonl",
                config.maximum_cost_usd,
                maximum_provider_operations=config.maximum_provider_operations,
                maximum_media_seconds=config.maximum_media_minutes * 60,
            ),
            approved_source_types=(
                (
                    {SourceType(value) for value in config.approved_source_types}
                    & source_types
                )
                if config.approved_source_types and source_types is not None
                else (
                    {SourceType(value) for value in config.approved_source_types}
                    if config.approved_source_types
                    else source_types
                )
            ),
            candidate_ids=candidate_ids,
            maximum_media_seconds=config.maximum_media_minutes * 60,
            media_seconds_used=already_collected_media,
            maximum_download_bytes=config.maximum_download_bytes,
            downloaded_bytes_used=already_downloaded_bytes,
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
                in {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".webm"}
                or artifact.collection_method == "yt_dlp_audio"
            ):
                voice.artifact_id = artifact.artifact_id
        write_jsonl(voice_path, voice_candidates)
        outcome_path = workspace / "processed/collection_candidate_outcomes.jsonl"
        collection_outcomes = {
            row["candidate_id"]: CollectionCandidateOutcome.model_validate(row)
            for row in read_jsonl(outcome_path)
        }
        for outcome in result.outcomes:
            collection_outcomes[outcome.candidate_id] = outcome
        write_jsonl(
            outcome_path,
            sorted(collection_outcomes.values(), key=lambda item: item.candidate_id),
        )
        partial = source_types is not None or candidate_ids is not None
        summary.stages["collection"] = "partial" if partial else "complete"
        summary.status = "collecting" if partial else "collected"
        summary.collected += result.collected
        summary.collection_failures = sum(
            item.status == "failed" for item in collection_outcomes.values()
        )
        summary.collection_review_required = sum(
            item.status == "review_required"
            for item in collection_outcomes.values()
        )
        summary.failures = summary.collection_failures + summary.processing_failures
        summary.excluded += result.excluded
        summary.downloaded_bytes = context.downloaded_bytes_used
        summary.cost_usd = context.budget.spent
        write_json(workspace / "run_summary.json", summary)
        return result

    def fetch_source(
        self,
        investor_slug: str,
        *,
        source_type: SourceType | None = None,
        candidate_ids: set[str] | None = None,
    ) -> CollectionResult:
        """Collect an approved platform subset or explicit candidate subset."""
        if source_type is None and not candidate_ids:
            raise ValueError("Provide a source type or at least one candidate ID")
        plan = self._load_plan(investor_slug)
        by_id = {candidate.candidate_id: candidate for candidate in plan.candidates}
        requested_ids = candidate_ids or set()
        unknown = sorted(requested_ids - by_id.keys())
        if unknown:
            raise KeyError(f"Unknown candidate: {unknown[0]}")
        for candidate_id in requested_ids:
            if by_id[candidate_id].approval_status not in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.AUTO_APPROVED,
            }:
                raise ReviewRequired(f"Candidate is not approved: {candidate_id}")
        return self.collect_sources(
            investor_slug,
            source_types={source_type} if source_type is not None else None,
            candidate_ids=candidate_ids,
        )

    def approve_reference_voice(
        self,
        investor_slug: str,
        *,
        candidate_id: str,
        reviewer: str,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        diarization_model: str | None = None,
        embedding_model: str | None = None,
        embedding_cost_usd: Decimal | None = None,
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
        config = self._configure_stage(
            workspace,
            "review_voice",
            diarization_model=diarization_model,
            embedding_model=embedding_model,
            embedding_cost_usd=embedding_cost_usd,
        )
        source_path = workspace / artifact.relative_path
        if (start_seconds is None) != (end_seconds is None):
            raise ValueError("Both reference start and end seconds are required")
        source_duration = (
            end_seconds - start_seconds
            if start_seconds is not None and end_seconds is not None
            else self.media_probe(source_path)
        )
        if source_duration is None:
            raise ReviewRequired(
                "Reference media duration is unknown; choose a bounded clip interval"
            )
        if source_duration > config.maximum_media_minutes * 60:
            raise ReviewRequired("Reference voice exceeds the media processing budget")
        cost = BudgetLedger(
            workspace / "audit/costs.jsonl",
            config.maximum_cost_usd,
            maximum_provider_operations=config.maximum_provider_operations,
            maximum_media_seconds=config.maximum_media_minutes * 60,
        )
        if cost.media_seconds + source_duration > config.maximum_media_minutes * 60:
            raise ReviewRequired(
                "Run-wide media processing budget exhausted before voice embedding"
            )
        operation_id = f"reference-embedding:{candidate_id}:{artifact.artifact_id}"
        provider = self.embedding_provider
        if (
            provider is None
            and self.diarization_provider is not None
            and hasattr(self.diarization_provider, "embed")
        ):
            provider = self.diarization_provider
        if cost.provider_operations >= config.maximum_provider_operations:
            raise ReviewRequired(
                "Provider-operation budget exhausted before voice embedding"
            )
        reserved_provider = provider.provider_name if provider else "pyannote"
        reserved_model = (
            provider.model_name
            if provider
            else (config.embedding_model or config.diarization_model)
        )
        if not reserved_model:
            raise ReviewRequired("Configure an embedding model before voice approval")
        cost.reserve(
            operation_id,
            config.embedding_cost_usd,
            provider=reserved_provider,
            model=reserved_model,
            media_seconds=source_duration,
        )
        provider_invoked = False
        try:
            if provider is None:
                if config.embedding_model:
                    provider = PyannoteEmbeddingProvider(
                        reserved_model,
                        token=os.environ.get("HF_TOKEN"),
                        device=os.environ.get("VC_TRACE_AV_DEVICE"),
                    )
                else:
                    provider = PyannoteDiarizationProvider(
                        reserved_model,
                        token=os.environ.get("HF_TOKEN"),
                        device=os.environ.get("VC_TRACE_AV_DEVICE"),
                    )
            reference_artifact = artifact
            if start_seconds is not None or end_seconds is not None:
                assert start_seconds is not None and end_seconds is not None
                with tempfile.TemporaryDirectory(
                    prefix="vc-trace-reference-"
                ) as directory:
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
                                "duration_seconds": source_duration,
                            },
                            parent_artifact_ids=[artifact.artifact_id],
                        )
                        .record
                    )
                    embedding_path = workspace / reference_artifact.relative_path
            else:
                embedding_path = source_path
            provider_invoked = True
            embedding = provider.embed(embedding_path)
        except Exception:
            if provider_invoked:
                cost.settle(
                    operation_id,
                    config.embedding_cost_usd,
                    provider=provider.provider_name,
                    model=provider.model_name,
                )
            else:
                cost.release(operation_id)
            raise
        cost.settle(
            operation_id,
            config.embedding_cost_usd,
            provider=provider.provider_name,
            model=provider.model_name,
        )
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
        summary.cost_usd = cost.spent
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

    def process(
        self,
        investor_slug: str,
        *,
        candidate_ids: set[str] | None = None,
        transcription_model: str | None = None,
        diarization_model: str | None = None,
        transcription_cost_usd: Decimal | None = None,
        diarization_cost_usd: Decimal | None = None,
    ) -> list[CanonicalDocument]:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = self._load_plan(investor_slug)
        config = self._configure_stage(
            workspace,
            "process",
            transcription_model=transcription_model,
            diarization_model=diarization_model,
            transcription_cost_usd=transcription_cost_usd,
            diarization_cost_usd=diarization_cost_usd,
        )
        all_artifacts = load_artifact_records(workspace)
        active_candidates = [
            candidate
            for candidate in plan.candidates
            if candidate_ids is None or candidate.candidate_id in candidate_ids
        ]
        artifacts = [
            artifact
            for artifact in all_artifacts
            if candidate_ids is None
            or str(artifact.original_metadata.get("candidate_id", ""))
            in candidate_ids
        ]
        documents = process_artifacts(
            workspace,
            artifacts,
            active_candidates,
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
        candidate_map = {item.candidate_id: item for item in active_candidates}
        outcomes = {
            item.candidate_id: AVCandidateOutcome(
                candidate_id=item.candidate_id,
                status="not_collected",
                reason="No collected audiovisual artifact was available",
            )
            for item in active_candidates
            if item.approval_status
            in {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
            and item.material_role == MaterialRole.SPOKEN_BY_TARGET
        }

        def mark_outcome(
            candidate_id: str, status: str, reason: str, artifact_id: str | None = None
        ) -> None:
            outcome = outcomes.get(candidate_id)
            if outcome is None:
                return
            priority = {
                "not_collected": 0,
                "failed": 1,
                "review_required": 2,
                "excluded": 3,
                "succeeded": 4,
            }
            if priority[status] >= priority[outcome.status]:
                outcome.status = status
                outcome.reason = reason
            if artifact_id and artifact_id not in outcome.artifact_ids:
                outcome.artifact_ids.append(artifact_id)

        for document in documents:
            if document.material_role != MaterialRole.SPOKEN_BY_TARGET:
                continue
            if document.inclusion_status == InclusionStatus.INCLUDED:
                mark_outcome(
                    document.source_candidate_id,
                    "succeeded",
                    "Human-verified supplied transcript passed corpus policy",
                )
            elif document.inclusion_status == InclusionStatus.EXCLUDED:
                mark_outcome(
                    document.source_candidate_id,
                    "excluded",
                    document.exclusion_reason or "Excluded by corpus policy",
                )
            else:
                mark_outcome(
                    document.source_candidate_id,
                    "review_required",
                    document.exclusion_reason or "Speech source requires review",
                )

        for artifact in artifacts:
            candidate_id = str(artifact.original_metadata.get("candidate_id", ""))
            exclusion = artifact.original_metadata.get("collection_exclusion", {})
            if exclusion.get("status") == InclusionStatus.EXCLUDED:
                mark_outcome(
                    candidate_id,
                    "excluded",
                    str(exclusion.get("reason") or "Excluded by post-metadata rules"),
                    artifact.artifact_id,
                )
            elif exclusion.get(
                "status"
            ) == InclusionStatus.REVIEW_REQUIRED and not artifact.original_metadata.get(
                "review_override_applied", False
            ):
                mark_outcome(
                    candidate_id,
                    "review_required",
                    str(exclusion.get("reason") or "Post-metadata review required"),
                    artifact.artifact_id,
                )
        audio_suffixes = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".flac"}
        video_suffixes = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
        av_rows: list[dict] = []
        state = StateStore(workspace / "state/state.sqlite")
        cost = BudgetLedger(
            workspace / "audit/costs.jsonl",
            config.maximum_cost_usd,
            maximum_provider_operations=config.maximum_provider_operations,
            maximum_media_seconds=config.maximum_media_minutes * 60,
        )
        processed_seconds = cost.media_seconds
        for artifact in artifacts:
            candidate_id = str(artifact.original_metadata.get("candidate_id", ""))
            candidate = candidate_map.get(candidate_id)
            path = workspace / artifact.relative_path
            if artifact.collection_method == "ffmpeg_audio_extraction":
                # This derivative is processed through its parent video branch.
                # Treating it as a fresh source on resume duplicates the talk.
                continue
            is_audio = (
                artifact.collection_method == "yt_dlp_audio"
                or (artifact.mime_type or "").casefold().startswith("audio/")
                or path.suffix.casefold() in audio_suffixes
            )
            is_video = not is_audio and (
                (artifact.mime_type or "").casefold().startswith("video/")
                or path.suffix.casefold() in video_suffixes
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
                mark_outcome(
                    candidate_id,
                    "failed",
                    failure["message"],
                    artifact.artifact_id,
                )
                continue
            probed_seconds = self.media_probe(path)
            if probed_seconds is None:
                failure = {
                    "candidate_id": candidate_id,
                    "artifact_id": artifact.artifact_id,
                    "message": "Media duration is unknown; model calls were not started",
                }
                append_jsonl(workspace / "audit/failures.jsonl", failure)
                mark_outcome(
                    candidate_id,
                    "failed",
                    failure["message"],
                    artifact.artifact_id,
                )
                continue
            processing_artifact = artifact
            if is_video or path.suffix.casefold() != ".wav":
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
                    mark_outcome(
                        candidate_id,
                        "failed",
                        f"Audio extraction failed: {type(error).__name__}",
                        artifact.artifact_id,
                    )
                    continue
                path = workspace / processing_artifact.relative_path
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
                        "segmented_transcription": True,
                        "probed_media_seconds": probed_seconds,
                    }
                ).encode("utf-8")
            ).hexdigest()
            cache_path = workspace / "state/av_results" / f"{input_hash}.json"
            cached = state.is_complete(operation_id, input_hash) and cache_path.exists()
            if (
                not cached
                and processed_seconds + probed_seconds
                > config.maximum_media_minutes * 60
            ):
                failure = {
                    "candidate_id": candidate_id,
                    "artifact_id": artifact.artifact_id,
                    "message": "Media processing budget exhausted before model calls",
                    "media_seconds": probed_seconds,
                }
                append_jsonl(workspace / "audit/failures.jsonl", failure)
                mark_outcome(
                    candidate_id,
                    "failed",
                    failure["message"],
                    artifact.artifact_id,
                )
                continue
            cost_operation_ids: list[str] = []
            try:
                if cached:
                    result = TargetSpeechResult.model_validate(read_json(cache_path))
                else:
                    required_provider_operations = 2
                    if (
                        cost.provider_operations + required_provider_operations
                        > config.maximum_provider_operations
                    ):
                        raise RuntimeError(
                            "Provider-operation budget exhausted before model calls"
                        )
                    diarization_operation = f"{operation_id}:diarization"
                    cost.reserve(
                        diarization_operation,
                        config.diarization_cost_usd,
                        provider=(
                            self.diarization_provider.provider_name
                            if self.diarization_provider
                            else "pyannote"
                        ),
                        model=(
                            self.diarization_provider.model_name
                            if self.diarization_provider
                            else config.diarization_model
                        ),
                        media_seconds=probed_seconds,
                    )
                    cost_operation_ids.append(diarization_operation)
                    transcription_operation = f"{operation_id}:transcription"
                    cost.reserve(
                        transcription_operation,
                        config.transcription_cost_usd,
                        provider=(
                            self.transcript_provider.provider_name
                            if self.transcript_provider
                            else "local-whisper"
                        ),
                        model=(
                            self.transcript_provider.model_name
                            if self.transcript_provider
                            else config.transcription_model
                        ),
                    )
                    cost_operation_ids.append(transcription_operation)
                    transcript_provider, diarization_provider = self._av_providers(
                        config, needs_transcript=True
                    )
                    state.start_operation(operation_id, input_hash)
                    result = process_target_speech(
                        path,
                        reference=profile,
                        transcript_provider=transcript_provider,
                        diarization_provider=diarization_provider,
                        segment_extractor=self.audio_extractor,
                        minimum_score=config.speaker_minimum_score,
                        minimum_margin=config.speaker_minimum_margin,
                    )
                    write_json(cache_path, result)
                    if transcription_operation and transcript_provider:
                        cost.settle(
                            transcription_operation,
                            config.transcription_cost_usd,
                            provider=transcript_provider.provider_name,
                            model=transcript_provider.model_name,
                        )
                    cost.settle(
                        diarization_operation,
                        config.diarization_cost_usd,
                        provider=diarization_provider.provider_name,
                        model=diarization_provider.model_name,
                    )
                    state.finish_operation(operation_id, input_hash, str(cache_path))
            except Exception as error:
                for cost_operation_id in cost_operation_ids:
                    row_provider = "unknown-provider"
                    row_model = None
                    row_cost = config.diarization_cost_usd
                    if cost_operation_id.endswith(":transcription"):
                        row_provider = (
                            self.transcript_provider.provider_name
                            if self.transcript_provider
                            else "local-whisper"
                        )
                        row_model = (
                            self.transcript_provider.model_name
                            if self.transcript_provider
                            else config.transcription_model
                        )
                        row_cost = config.transcription_cost_usd
                    else:
                        row_provider = (
                            self.diarization_provider.provider_name
                            if self.diarization_provider
                            else "pyannote"
                        )
                        row_model = (
                            self.diarization_provider.model_name
                            if self.diarization_provider
                            else config.diarization_model
                        )
                    cost.settle(
                        cost_operation_id,
                        row_cost,
                        provider=row_provider,
                        model=row_model,
                    )
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
                mark_outcome(
                    candidate_id,
                    "failed",
                    str(failure["message"]),
                    artifact.artifact_id,
                )
                continue
            processed_seconds = cost.media_seconds
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
                if document.inclusion_status == InclusionStatus.EXCLUDED:
                    mark_outcome(
                        candidate_id,
                        "excluded",
                        document.exclusion_reason or "Excluded by corpus policy",
                        artifact.artifact_id,
                    )
                elif document.inclusion_status == InclusionStatus.INCLUDED:
                    mark_outcome(
                        candidate_id,
                        "succeeded",
                        "Target speech passed speaker attribution and corpus policy",
                        artifact.artifact_id,
                    )
                else:
                    mark_outcome(
                        candidate_id,
                        "review_required",
                        document.exclusion_reason
                        or "Target-speaker attribution requires review",
                        artifact.artifact_id,
                    )
            else:
                mark_outcome(
                    candidate_id,
                    "failed",
                    "No target-speaker text was extracted",
                    artifact.artifact_id,
                )
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
        if candidate_ids is not None:
            replace_candidate_ids = {
                candidate_id
                for candidate_id, outcome in outcomes.items()
                if outcome.status not in {"failed", "not_collected"}
            }
            prior_documents = [
                CanonicalDocument.model_validate(row)
                for row in read_jsonl(workspace / "processed/documents.jsonl")
                if row.get("source_candidate_id") not in replace_candidate_ids
            ]
            documents = [*prior_documents, *documents]
            prior_av_rows = [
                row
                for row in read_jsonl(
                    workspace / "processed/av_attribution_results.jsonl"
                )
                if row.get("candidate_id") not in replace_candidate_ids
            ]
            av_rows = [*prior_av_rows, *av_rows]
            for row in read_jsonl(
                workspace / "processed/av_candidate_outcomes.jsonl"
            ):
                candidate_id = str(row.get("candidate_id", ""))
                if candidate_id not in candidate_ids:
                    outcomes[candidate_id] = AVCandidateOutcome.model_validate(row)
        documents = deduplicate(documents)
        write_jsonl(workspace / "processed/av_attribution_results.jsonl", av_rows)
        write_jsonl(
            workspace / "processed/av_candidate_outcomes.jsonl",
            sorted(outcomes.values(), key=lambda item: item.candidate_id),
        )
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
        summary.stages["processing"] = (
            "partial" if candidate_ids is not None else "complete"
        )
        summary.status = "processing" if candidate_ids is not None else "processed"
        summary.processed = len(documents)
        summary.media_seconds = processed_seconds
        summary.processing_failures = sum(
            item.status in {"failed", "not_collected"} for item in outcomes.values()
        )
        summary.unresolved = summary.collection_review_required + sum(
            item.status == "review_required" for item in outcomes.values()
        )
        summary.failures = summary.collection_failures + summary.processing_failures
        summary.cost_usd = cost.spent
        write_json(workspace / "run_summary.json", summary)
        return documents

    def process_source(
        self,
        investor_slug: str,
        *,
        candidate_id: str,
        transcription_model: str | None = None,
        diarization_model: str | None = None,
        transcription_cost_usd: Decimal | None = None,
        diarization_cost_usd: Decimal | None = None,
    ) -> list[CanonicalDocument]:
        """Run the AV pipeline for one approved source-plan candidate."""
        plan = self._load_plan(investor_slug)
        try:
            candidate = next(
                item for item in plan.candidates if item.candidate_id == candidate_id
            )
        except StopIteration as error:
            raise KeyError(f"Unknown candidate: {candidate_id}") from error
        if candidate.approval_status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.AUTO_APPROVED,
        }:
            raise ReviewRequired(f"Candidate is not approved: {candidate_id}")
        if candidate.material_role != MaterialRole.SPOKEN_BY_TARGET:
            raise ReviewRequired(
                "Per-source AV processing requires spoken_by_target material"
            )
        return self.process(
            investor_slug,
            candidate_ids={candidate_id},
            transcription_model=transcription_model,
            diarization_model=diarization_model,
            transcription_cost_usd=transcription_cost_usd,
            diarization_cost_usd=diarization_cost_usd,
        )

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
                    channel=candidate.channel if candidate else None,
                    programme=candidate.programme if candidate else None,
                    company=candidate.company if candidate else None,
                    authors=document.authors,
                    speakers=document.speakers,
                    stage="final_export",
                )
                for url in sorted(urls)
            ]
            for artifact_id in document.raw_artifact_ids:
                for artifact in artifact_map.get(artifact_id, []):
                    metadata = artifact.original_metadata
                    channel_values = dict.fromkeys(
                        [
                            metadata.get("channel"),
                            metadata.get("channel_id"),
                            metadata.get("uploader_id"),
                            candidate.channel if candidate else None,
                        ]
                    )
                    for channel_value in channel_values:
                        decisions.append(
                            rules.evaluate(
                                url=artifact.source_url or document.canonical_url,
                                title=str(
                                    metadata.get("title") or document.title or ""
                                ),
                                text=document.text,
                                channel=str(channel_value) if channel_value else None,
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
                                stage="final_export_metadata",
                            )
                        )
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
            elif (
                review
                and document.inclusion_status == InclusionStatus.INCLUDED
                and (
                    candidate is None
                    or review.rule_id not in candidate.override_rule_ids
                )
            ):
                document.inclusion_status = InclusionStatus.REVIEW_REQUIRED
                document.exclusion_reason = review.reason
        rules_payload = [rule.model_dump(mode="json") for rule in rules.rules]
        write_json(workspace / "exclusion_rules_snapshot.json", rules_payload)
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        manifest = export_workspace(
            workspace,
            investor_slug=investor_slug,
            identity_id=f"identity:{investor_slug}",
            documents=documents,
            config_hash=config.fingerprint,
            exclusion_rules_hash=sha256(
                canonical_json(rules_payload).encode("utf-8")
            ).hexdigest(),
            run_failures=summary.failures,
            unresolved_sources=summary.unresolved,
            allow_partial_run=config.allow_partial_run,
            maximum_cost_usd=float(config.maximum_cost_usd),
            maximum_download_bytes=config.maximum_download_bytes,
            maximum_provider_operations=config.maximum_provider_operations,
            downloaded_bytes=summary.downloaded_bytes,
        )
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

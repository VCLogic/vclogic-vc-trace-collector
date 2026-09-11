"""Resumable orchestration for discovery, collection, processing, and export."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .audit import AuditLog, BudgetLedger
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
    IdentityEvidence,
    ReferenceVoiceCandidate,
    ResolvedIdentity,
    ResolutionStatus,
    RunSummary,
    SourceCandidate,
    SourceDecision,
    SourcePlan,
    utc_now,
)
from .policy import ExclusionRule, RuleSet
from .process import load_artifact_records, process_artifacts
from .storage import (
    ArtifactStore,
    StateStore,
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
    ):
        self.output_dir = Path(output_dir)
        self.fetcher = fetcher or Fetcher()
        self.rules = rules or RuleSet.pitch_default()
        search_endpoint = os.environ.get("VC_TRACE_SEARCH_ENDPOINT", "").strip()
        self.search_provider = search_provider or (
            SearxngSearchProvider(search_endpoint) if search_endpoint else None
        )
        self.discovery_provider = discovery_provider

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
        additions: list[ExclusionRule] = []
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
        config: RunConfig | None = None,
    ) -> DiscoveryResult:
        config = config or RunConfig(
            name=name,
            firm=firm,
            known_profile_url=known_profile_url,
            output_dir=str(self.output_dir),
        )
        run_rules = self._rules_for(config)
        service = DiscoveryService(
            fetcher=self.fetcher,
            search_provider=self.search_provider,
            discovery_provider=self._provider_for(config.discovery_model),
            rules=run_rules,
            search_limit_per_query=max(1, config.maximum_search_operations),
        )
        result = service.discover(
            name=name, firm=firm, known_profile_url=known_profile_url
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
    ) -> ReviewResult:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = apply_decisions(self._load_plan(investor_slug), decisions)
        identity.resolution_status = ResolutionStatus.CONFIRMED
        identity.reviewed_by = reviewer
        identity.resolved_at = utc_now()
        write_json(workspace / "identity/resolved_identity.json", identity)
        write_json(workspace / "discovery/source_plan.json", plan)
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
                "decisions": [decision.model_dump(mode="json") for decision in decisions],
            },
        )
        return ReviewResult(identity=identity, source_plan=plan)

    def _automatic_review(self, slug: str, config: RunConfig) -> ReviewResult:
        workspace = self.workspace(slug)
        identity = self._load_identity(slug)
        if identity.identity_confidence.score < 0.9:
            raise ReviewRequired("Identity confidence is below the automatic threshold")
        plan = auto_approve(
            self._load_plan(slug), minimum_identity=0.9, minimum_source=0.85
        )
        identity.resolution_status = ResolutionStatus.CONFIRMED
        identity.reviewed_by = "automatic_policy"
        write_json(workspace / "identity/resolved_identity.json", identity)
        write_json(workspace / "discovery/source_plan.json", plan)
        self._write_candidate_views(workspace, plan)
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
        )
        result = collect_approved_sources(
            plan, context=context, registry=default_registry()
        )
        summary.stages["collection"] = "complete"
        summary.status = "collected"
        summary.collected += result.collected
        summary.failures += result.failed
        summary.excluded += result.excluded
        write_json(workspace / "run_summary.json", summary)
        return result

    def process(self, investor_slug: str) -> list[CanonicalDocument]:
        workspace = self.workspace(investor_slug)
        identity = self._load_identity(investor_slug)
        plan = self._load_plan(investor_slug)
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        documents = process_artifacts(
            workspace,
            load_artifact_records(workspace),
            plan.candidates,
            investor_slug=investor_slug,
            target_name=identity.canonical_name,
            rules=self._rules_for(config),
        )
        write_jsonl(workspace / "processed/documents.jsonl", documents)
        write_jsonl(
            workspace / "processed/excluded_documents.jsonl",
            [document for document in documents if document.inclusion_status != "included"],
        )
        write_jsonl(
            workspace / "processed/target_speech.jsonl",
            [document for document in documents if document.material_role == "spoken_by_target"],
        )
        summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
        summary.stages["processing"] = "complete"
        summary.status = "processed"
        summary.processed = len(documents)
        write_json(workspace / "run_summary.json", summary)
        return documents

    def export(self, investor_slug: str) -> CollectionManifest:
        workspace = self.workspace(investor_slug)
        config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
        documents = [
            CanonicalDocument.model_validate(row)
            for row in read_jsonl(workspace / "processed/documents.jsonl")
        ]
        rules_payload = [rule.model_dump(mode="json") for rule in self._rules_for(config).rules]
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
            summary = RunSummary.model_validate(read_json(workspace / "run_summary.json"))
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
            output_dir=str(self.output_dir),
            automatic_discovery=auto_approve_discovery,
            resume=resume,
            **options,
        )
        guessed_slug = re_slug(name, firm)
        workspace = self.workspace(guessed_slug)
        if processing_only:
            documents = self.process(guessed_slug)
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
        summary["operations"] = StateStore(state_path).operation_counts() if state_path.exists() else {}
        return summary


def re_slug(name: str, firm: str | None = None) -> str:
    import re

    value = f"{name}-{firm}" if firm else name
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "investor"

import socket
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from vc_trace_collector.audit import BudgetExceeded, BudgetLedger
from vc_trace_collector.collectors import (
    CollectionContext,
    CollectorRegistry,
    PodcastCollector,
    SuppliedFileCollector,
    collect_approved_sources,
)
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import (
    ApprovalStatus,
    Confidence,
    MaterialRole,
    SourceCandidate,
    SourcePlan,
    SourceType,
)
from vc_trace_collector.policy import RuleSet
from vc_trace_collector.storage import ArtifactStore, StateStore


def candidate(
    identifier: str,
    source_type: SourceType = SourceType.WEB_ARTICLE,
    *,
    url: str | None = None,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    estimated_cost_usd: Decimal = Decimal(0),
) -> SourceCandidate:
    source_url = url or f"https://example.test/{identifier}"
    return SourceCandidate(
        candidate_id=identifier,
        url=source_url,
        canonical_url=source_url,
        source_type=source_type,
        material_role=MaterialRole.AUTHORED_BY_TARGET,
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.9, method="test", version="1"),
        source_confidence=Confidence(score=0.9, method="test", version="1"),
        approval_status=status,
        estimated_cost_usd=estimated_cost_usd,
    )


def plan(*candidates: SourceCandidate) -> SourcePlan:
    return SourcePlan(
        plan_id="plan-1", investor_slug="michael-hyatt", candidates=list(candidates)
    )


class StaticCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.WEB_ARTICLE}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def collect(self, source: SourceCandidate, context: CollectionContext):
        self.calls.append(source.candidate_id)
        if source.candidate_id == "failure":
            raise RuntimeError("isolated failure")
        return [
            context.artifacts.put_bytes(
                b"collected",
                category="web",
                suffix=".html",
                source_url=source.url,
            ).record
        ]


def context(tmp_path: Path) -> CollectionContext:
    return CollectionContext(
        workspace=tmp_path,
        artifacts=ArtifactStore(tmp_path),
        state=StateStore(tmp_path / "state/state.sqlite"),
        rules=RuleSet.pitch_default(),
        run_id="run-1",
    )


def test_one_failed_source_does_not_remove_success(tmp_path) -> None:
    collector = StaticCollector()
    registry = CollectorRegistry([collector])

    result = collect_approved_sources(
        plan(candidate("success"), candidate("failure")),
        context=context(tmp_path),
        registry=registry,
    )

    assert result.collected == 1
    assert result.failed == 1
    assert next((tmp_path / "raw/web").rglob("*.html")).exists()


def test_unavailable_collector_is_isolated_from_supported_sources(tmp_path) -> None:
    collector = StaticCollector()

    result = collect_approved_sources(
        plan(candidate("unsupported", SourceType.X), candidate("success")),
        context=context(tmp_path),
        registry=CollectorRegistry([collector]),
    )

    assert result.failed == 1
    assert result.collected == 1
    assert result.failures[0]["error_type"] == "CollectorUnavailable"


def test_completed_collection_is_skipped_on_resume(tmp_path) -> None:
    collector = StaticCollector()
    registry = CollectorRegistry([collector])
    run_context = context(tmp_path)
    source_plan = plan(candidate("success"))

    first = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )
    second = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )

    assert first.collected == 1
    assert second.skipped == 1
    assert collector.calls == ["success"]


def test_supplied_file_is_snapshotted(tmp_path) -> None:
    supplied = tmp_path / "interview.txt"
    supplied.write_text("A supplied public interview")
    source = candidate(
        "supplied",
        SourceType.SUPPLIED,
        url=supplied.resolve().as_uri(),
    )
    registry = CollectorRegistry([SuppliedFileCollector()])

    result = collect_approved_sources(
        plan(source), context=context(tmp_path), registry=registry
    )

    assert result.collected == 1
    assert result.artifacts[0].source_path == str(supplied.resolve())
    assert (
        tmp_path / result.artifacts[0].relative_path
    ).read_text() == supplied.read_text()


def test_collection_skips_source_types_outside_run_allowlist(tmp_path) -> None:
    collector = StaticCollector()
    run_context = context(tmp_path)
    run_context.approved_source_types = {SourceType.RSS_FEED}

    result = collect_approved_sources(
        plan(candidate("web")),
        context=run_context,
        registry=CollectorRegistry([collector]),
    )

    assert result.skipped == 1
    assert collector.calls == []


def test_collection_stops_before_exceeding_reserved_provider_budget(tmp_path) -> None:
    collector = StaticCollector()
    run_context = context(tmp_path)
    run_context.budget = BudgetLedger(tmp_path / "audit/costs.jsonl", Decimal("1.00"))

    with pytest.raises(BudgetExceeded):
        collect_approved_sources(
            plan(
                candidate("first", estimated_cost_usd=Decimal("0.75")),
                candidate("second", estimated_cost_usd=Decimal("0.26")),
            ),
            context=run_context,
            registry=CollectorRegistry([collector]),
        )

    assert collector.calls == ["first"]
    assert run_context.budget.spent == Decimal("0.75")
    assert next((tmp_path / "raw/web").rglob("*.html")).exists()


def test_podcast_collector_preserves_page_and_public_audio(tmp_path) -> None:
    page_url = "https://podcast.example.test/episodes/michael-hyatt"
    audio_url = "https://cdn.example.test/michael-hyatt.mp3"

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == audio_url:
            return httpx.Response(
                200,
                content=b"ID3 public audio fixture",
                headers={"content-type": "application/octet-stream"},
                request=request,
            )
        return httpx.Response(
            200,
            content=f'<html><audio src="{audio_url}"></audio></html>'.encode(),
            headers={"content-type": "text/html"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    source = candidate("podcast", SourceType.PODCAST, url=page_url)

    result = collect_approved_sources(
        plan(source),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.collected == 1
    assert {artifact.mime_type for artifact in result.artifacts} == {
        "text/html",
        "application/octet-stream",
    }
    assert result.artifacts[1].parent_artifact_ids == [result.artifacts[0].artifact_id]
    assert result.artifacts[1].relative_path.endswith(".mp3")

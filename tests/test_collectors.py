from pathlib import Path

from vc_trace_collector.collectors import (
    CollectionContext,
    CollectorRegistry,
    SuppliedFileCollector,
    collect_approved_sources,
)
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
    )


def plan(*candidates: SourceCandidate) -> SourcePlan:
    return SourcePlan(plan_id="plan-1", investor_slug="michael-hyatt", candidates=list(candidates))


class StaticCollector:
    source_types = {SourceType.WEB_ARTICLE}

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


def test_completed_collection_is_skipped_on_resume(tmp_path) -> None:
    collector = StaticCollector()
    registry = CollectorRegistry([collector])
    run_context = context(tmp_path)
    source_plan = plan(candidate("success"))

    first = collect_approved_sources(source_plan, context=run_context, registry=registry)
    second = collect_approved_sources(source_plan, context=run_context, registry=registry)

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

    result = collect_approved_sources(plan(source), context=context(tmp_path), registry=registry)

    assert result.collected == 1
    assert result.artifacts[0].source_path == str(supplied.resolve())
    assert (tmp_path / result.artifacts[0].relative_path).read_text() == supplied.read_text()

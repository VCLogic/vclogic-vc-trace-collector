from test_process_export import document

from vc_trace_collector.collectors import CollectionCandidateOutcome
from vc_trace_collector.config import RunConfig
from vc_trace_collector.export import export_workspace
from vc_trace_collector.models import (
    ApprovalStatus,
    Confidence,
    ResolutionStatus,
    ResolvedIdentity,
    SourceCandidate,
    SourceDecision,
    SourcePlan,
    utc_now,
)
from vc_trace_collector.source_search import SourceSearchObservation
from vc_trace_collector.storage import (
    ArtifactStore,
    StateStore,
    canonical_json,
    read_json,
    write_json,
    write_jsonl,
)
from vc_trace_collector.verify import verify_workspace


def exported_workspace(
    tmp_path,
    *,
    empty: bool = False,
    outcome_candidate_id: str = "candidate:blog",
    outcome_status: str = "succeeded",
    observation_url: str | None = None,
    search_source_type: str = "web_article",
    include_search_observations: bool = True,
    include_collection_outcomes: bool = True,
):
    config = RunConfig(name="Michael Hyatt")
    write_json(tmp_path / "config_snapshot.json", config)
    identity = ResolvedIdentity(
        slug="michael-hyatt",
        canonical_name="Michael Hyatt",
        resolution_status=ResolutionStatus.CONFIRMED,
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        reviewed_by="reviewer",
    )
    write_json(
        tmp_path / "identity/resolved_identity.json",
        identity,
    )
    write_json(
        tmp_path / "identity/identity_review.json",
        {
            "investor_slug": "michael-hyatt",
            "status": "confirmed",
            "reviewed_by": "reviewer",
            "reviewed_at": identity.resolved_at,
        },
    )
    decision_time = utc_now()
    candidate = SourceCandidate(
        candidate_id="candidate:blog",
        url="https://example.test/blog",
        canonical_url="https://example.test/blog",
        source_type="web_article",
        material_role="authored_by_target",
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.95, method="test", version="1"),
        source_confidence=Confidence(score=0.95, method="test", version="1"),
        approval_status=ApprovalStatus.APPROVED,
        decision_reason="test approval",
        reviewed_by="reviewer",
        decision_at=decision_time,
    )
    write_json(
        tmp_path / "discovery/source_plan.json",
        SourcePlan(
            plan_id="plan:test",
            investor_slug="michael-hyatt",
            candidates=[candidate],
        ),
    )
    write_jsonl(
        tmp_path / "discovery/source_decisions.jsonl",
        [
            SourceDecision(
                candidate_id=candidate.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="test approval",
                decided_by="reviewer",
                decided_at=decision_time,
            )
        ],
    )
    artifact = ArtifactStore(tmp_path).put_bytes(
        b"public writing",
        category="web",
        suffix=".html",
        source_url=candidate.url,
        mime_type="text/html",
        original_metadata={"candidate_id": candidate.candidate_id},
    )
    search_operation_id = f"search-source:{search_source_type}:test"
    search_cache_path = tmp_path / "state/source_search/search-hash.json"
    write_json(
        search_cache_path,
        {
            "query": "query",
            "requested_provider": "test-search",
            "provider_cache_identity": "test-search",
            "results": [
                {
                    "url": candidate.url,
                    "title": "Michael Hyatt article",
                    "snippet": "Michael Hyatt",
                    "rank": 1,
                    "query": "query",
                    "provider": "test-search",
                }
            ]
        },
    )
    state = StateStore(tmp_path / "state/state.sqlite")
    state.start_operation(search_operation_id, "search-hash")
    state.finish_operation(
        search_operation_id, "search-hash", str(search_cache_path)
    )
    state.start_operation("collect:candidate:blog", "collect-hash")
    state.finish_operation(
        "collect:candidate:blog", "collect-hash", artifact.record.artifact_id
    )
    if include_search_observations:
        write_jsonl(
            tmp_path / "discovery/search_observations.jsonl",
            [
                SourceSearchObservation(
                    operation_id=search_operation_id,
                    source_type=search_source_type,
                    query="query",
                    requested_provider="test-search",
                    result_provider="test-search",
                    status="succeeded",
                    rank=1,
                    title="Michael Hyatt article",
                    url=observation_url or candidate.url,
                )
            ],
        )
    if include_collection_outcomes:
        write_jsonl(
            tmp_path / "processed/collection_candidate_outcomes.jsonl",
            [
                CollectionCandidateOutcome(
                    candidate_id=outcome_candidate_id,
                    status=outcome_status,
                    reason="Source collected",
                    artifact_ids=[artifact.record.artifact_id],
                )
            ],
        )
    documents = []
    if not empty:
        item = document("blog", "public writing")
        item.source_candidate_id = candidate.candidate_id
        item.raw_artifact_ids = [artifact.record.artifact_id]
        item.collected_at = artifact.record.collected_at
        item.authors = ["Michael Hyatt"]
        documents = [item]
    rules_payload = []
    write_json(tmp_path / "exclusion_rules_snapshot.json", rules_payload)
    rules_hash = (
        __import__("hashlib")
        .sha256(canonical_json(rules_payload).encode("utf-8"))
        .hexdigest()
    )
    manifest = export_workspace(
        tmp_path,
        investor_slug="michael-hyatt",
        identity_id="identity:michael",
        documents=documents,
        config_hash=config.fingerprint,
        exclusion_rules_hash=rules_hash,
    )
    return manifest, artifact


def test_exported_workspace_verifies(tmp_path) -> None:
    manifest, _artifact = exported_workspace(tmp_path)

    result = verify_workspace(tmp_path)
    assert result.passed is True
    assert result.errors == []
    paths = {item.path for item in manifest.files}
    assert "discovery/search_observations.jsonl" in paths
    assert "processed/collection_candidate_outcomes.jsonl" in paths


def test_verification_rejects_unknown_collection_outcome_candidate(tmp_path) -> None:
    exported_workspace(tmp_path, outcome_candidate_id="candidate:unknown")

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("collection outcome" in error.casefold() for error in result.errors)


def test_verification_requires_search_observations_for_search_operations(
    tmp_path,
) -> None:
    exported_workspace(tmp_path, include_search_observations=False)

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("search observation" in error.casefold() for error in result.errors)


def test_verification_requires_collection_outcomes_for_collection_operations(
    tmp_path,
) -> None:
    exported_workspace(tmp_path, include_collection_outcomes=False)

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("collection outcome" in error.casefold() for error in result.errors)


def test_verification_reconstructs_collection_status_from_raw_metadata(
    tmp_path,
) -> None:
    exported_workspace(tmp_path, outcome_status="review_required")

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("collection outcome status" in error.casefold() for error in result.errors)


def test_verification_reconciles_search_observations_with_cached_results(
    tmp_path,
) -> None:
    exported_workspace(tmp_path, observation_url="https://example.test/substituted")

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("search observation" in error.casefold() for error in result.errors)


def test_verification_allows_cross_source_type_url_deduplication(tmp_path) -> None:
    exported_workspace(tmp_path, search_source_type="podcast")

    result = verify_workspace(tmp_path)

    assert result.passed is True


def test_verification_detects_tampered_corpus(tmp_path) -> None:
    exported_workspace(tmp_path)
    with (tmp_path / "corpus/blog.jsonl").open("a") as handle:
        handle.write('{"tampered":true}\n')

    result = verify_workspace(tmp_path)
    assert result.passed is False
    assert any("hash" in error.casefold() for error in result.errors)


def test_verification_fails_when_quality_report_rejects_empty_corpus(tmp_path) -> None:
    exported_workspace(tmp_path, empty=True)

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert "quality report did not pass" in " ".join(result.errors).casefold()


def test_verification_requires_confirmed_identity(tmp_path) -> None:
    exported_workspace(tmp_path)
    (tmp_path / "identity/resolved_identity.json").unlink()

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert "identity" in " ".join(result.errors).casefold()


def test_verification_recomputes_manifest_fingerprint(tmp_path) -> None:
    exported_workspace(tmp_path)
    manifest = read_json(tmp_path / "collection_manifest.json")
    manifest["fingerprint"] = "0" * 64
    write_json(tmp_path / "collection_manifest.json", manifest)

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert "fingerprint" in " ".join(result.errors).casefold()


def test_verification_hashes_raw_lineage(tmp_path) -> None:
    _manifest, artifact = exported_workspace(tmp_path)
    artifact.path.write_bytes(b"tampered raw evidence")

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("hash" in error.casefold() for error in result.errors)


def test_verification_rejects_unattested_exclusion_override(tmp_path) -> None:
    exported_workspace(tmp_path)
    plan_payload = read_json(tmp_path / "discovery/source_plan.json")
    plan_payload["candidates"][0]["override_rule_ids"] = ["injected-override"]
    write_json(tmp_path / "discovery/source_plan.json", plan_payload)

    result = verify_workspace(tmp_path)

    assert result.passed is False
    assert any("override" in error.casefold() for error in result.errors)

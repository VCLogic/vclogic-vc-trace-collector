from test_process_export import document

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
from vc_trace_collector.storage import (
    ArtifactStore,
    canonical_json,
    read_json,
    write_json,
    write_jsonl,
)
from vc_trace_collector.verify import verify_workspace


def exported_workspace(tmp_path, *, empty: bool = False):
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
    exported_workspace(tmp_path)

    result = verify_workspace(tmp_path)
    assert result.passed is True
    assert result.errors == []


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

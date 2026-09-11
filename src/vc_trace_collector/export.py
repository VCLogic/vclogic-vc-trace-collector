"""Deterministic canonical and downstream-compatible corpus exports."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import (
    CanonicalDocument,
    CollectionManifest,
    ManifestFile,
    MaterialRole,
    QualityReport,
)
from .policy import eligible_for_corpus
from .storage import canonical_json, read_json, read_jsonl, write_json, write_jsonl


def _video_id(document: CanonicalDocument) -> str:
    if document.canonical_url:
        parsed = urlsplit(document.canonical_url)
        query_id = parse_qs(parsed.query).get("v")
        if query_id:
            return query_id[0]
        if parsed.hostname == "youtu.be" and parsed.path.strip("/"):
            return parsed.path.strip("/")
    return document.source_item_id.rsplit(":", 1)[-1]


def export_persona_sources(
    output: Path, documents: list[CanonicalDocument]
) -> dict[str, object]:
    output = Path(output)
    included = [document for document in documents if eligible_for_corpus(document)]
    blogs = [
        document
        for document in included
        if document.material_role == MaterialRole.AUTHORED_BY_TARGET
    ]
    talks = [
        document
        for document in included
        if document.material_role == MaterialRole.SPOKEN_BY_TARGET
    ]
    blog_rows = [
        {
            "doc_id": document.source_item_id,
            "title": document.title,
            "source": document.source_type.value,
            "full_text": document.text,
        }
        for document in sorted(blogs, key=lambda item: item.source_item_id)
    ]
    talk_rows = [
        {
            "doc_id": document.source_item_id,
            "video_id": _video_id(document),
            "source": "youtube_talk"
            if document.source_type.value == "youtube"
            else "talk",
            "text": document.text,
        }
        for document in sorted(talks, key=lambda item: item.source_item_id)
    ]
    write_jsonl(output / "blog.jsonl", blog_rows)
    write_jsonl(output / "talks.jsonl", talk_rows)
    manifest = {
        "blog_docs": len(blog_rows),
        "talk_docs": len(talk_rows),
        "corpus_chars": sum(len(row["full_text"]) for row in blog_rows)
        + sum(len(row["text"]) for row in talk_rows),
        "thin_corpus": (
            sum(len(row["full_text"]) for row in blog_rows)
            + sum(len(row["text"]) for row in talk_rows)
        )
        < 600_000,
        "pitch_excluded": [
            document.source_item_id
            for document in documents
            if document.exclusion_reason and "Pitch" in document.exclusion_reason
        ],
        "channel_unknown": [],
        "no_pitch_sources": all(
            "thepitch.show"
            not in json.dumps(document.model_dump(mode="json")).casefold()
            for document in included
        ),
    }
    write_json(output / "_manifest.json", manifest)
    return manifest


def _file(path: Path, root: Path) -> ManifestFile:
    content = path.read_bytes()
    records = None
    if path.suffix == ".jsonl":
        records = sum(1 for line in content.splitlines() if line.strip())
    return ManifestFile(
        path=str(path.relative_to(root)),
        sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
        records=records,
    )


def export_workspace(
    workspace: Path,
    *,
    investor_slug: str,
    identity_id: str,
    documents: list[CanonicalDocument],
    config_hash: str,
    exclusion_rules_hash: str,
    run_failures: int = 0,
    unresolved_sources: int = 0,
    allow_partial_run: bool = False,
    maximum_cost_usd: float | None = None,
) -> CollectionManifest:
    workspace = Path(workspace)
    ordered = sorted(documents, key=lambda item: item.document_version_id)
    included = [document for document in ordered if eligible_for_corpus(document)]
    excluded = [document for document in ordered if document not in included]
    speech = [
        document
        for document in ordered
        if document.material_role == MaterialRole.SPOKEN_BY_TARGET
    ]
    corpus_speech = [document for document in speech if document in included]
    write_jsonl(workspace / "processed/documents.jsonl", ordered)
    write_jsonl(workspace / "processed/target_speech.jsonl", speech)
    write_jsonl(workspace / "processed/excluded_documents.jsonl", excluded)
    write_jsonl(workspace / "corpus/all_documents.jsonl", included)
    export_persona_sources(workspace / "corpus", ordered)
    # Keep a drop-in compatibility view at the investor directory root.  The
    # canonical corpus remains under corpus/, but legacy consumers expect these
    # three files directly in persona_sources/<investor-slug>/.
    export_persona_sources(workspace, ordered)

    raw_records = []
    for metadata_path in sorted((workspace / "raw").rglob("*.metadata.json")):
        raw_records.append(read_json(metadata_path))
    cost_rows = read_jsonl(workspace / "audit/costs.jsonl")
    settlements = [row for row in cost_rows if row.get("kind") == "settlement"]
    provider_cost = sum(float(row.get("amount_usd", 0)) for row in settlements)
    billed_media_seconds = sum(
        float(row.get("media_seconds", 0)) for row in settlements
    )
    retry_count = sum(
        max(0, int(row.get("original_metadata", {}).get("attempts", 1)) - 1)
        for row in raw_records
    )
    duplicate_count = sum(
        bool(document.duplicate_of) or document.inclusion_status == "duplicate"
        for document in ordered
    )
    first_person_count = sum(
        document.material_role
        in {MaterialRole.AUTHORED_BY_TARGET, MaterialRole.SPOKEN_BY_TARGET}
        for document in included
    )
    verified_speech = [
        document
        for document in included
        if document.material_role == MaterialRole.SPOKEN_BY_TARGET
        and document.speaker_attribution.status in {"accepted_model", "verified_human"}
    ]
    transcribed_speech = [
        document
        for document in included
        if document.material_role == MaterialRole.SPOKEN_BY_TARGET
        and document.transcript.method != "none"
    ]
    metadata_complete = all(
        bool(document.title)
        and bool(document.canonical_url or document.local_source_path)
        for document in included
    )
    approved_work_complete = run_failures == 0 and unresolved_sources == 0
    checks = {
        "corpus_nonempty": bool(included),
        "excluded_absent": all(eligible_for_corpus(item) for item in included),
        "lineage_present": all(bool(item.raw_artifact_ids) for item in ordered),
        "metadata_complete": metadata_complete,
        "first_person_only": first_person_count == len(included),
        "speaker_attribution_complete": len(verified_speech) == len(corpus_speech),
        "transcript_coverage_complete": len(transcribed_speech) == len(corpus_speech),
        "budget_within_limit": maximum_cost_usd is None
        or provider_cost <= maximum_cost_usd,
        "approved_work_complete": approved_work_complete,
        "partial_run_policy_satisfied": approved_work_complete or allow_partial_run,
    }
    warnings: list[str] = []
    if not included:
        warnings.append("Corpus contains no eligible documents")
    if not approved_work_complete:
        warnings.append(
            f"Run has {run_failures} failures and {unresolved_sources} unresolved sources"
        )
    if len({document.source_type for document in included}) < 2:
        warnings.append("Corpus has fewer than two source types")
    quality = QualityReport(
        investor_slug=investor_slug,
        passed=all(
            value for name, value in checks.items() if name != "approved_work_complete"
        ),
        checks=checks,
        counts={
            "documents": len(ordered),
            "included": len(included),
            "excluded_or_pending": len(excluded),
            "target_speech": len(speech),
            "verified_target_speech": len(verified_speech),
            "duplicates": duplicate_count,
            "source_types": len({document.source_type for document in included}),
            "failures": run_failures,
            "unresolved_sources": unresolved_sources,
            "retries": retry_count,
        },
        metrics={
            "metadata_completeness": (
                sum(
                    bool(document.title)
                    and bool(document.canonical_url or document.local_source_path)
                    for document in included
                )
                / len(included)
                if included
                else 0.0
            ),
            "first_person_ratio": (
                first_person_count / len(included) if included else 0.0
            ),
            "transcript_coverage": (
                len(transcribed_speech) / len(corpus_speech) if corpus_speech else 1.0
            ),
            "provider_cost_usd": provider_cost,
            "audiovisual_seconds_billed": billed_media_seconds,
        },
        warnings=warnings,
    )
    write_json(workspace / "quality_report.json", quality)

    paths = [
        workspace / "processed/documents.jsonl",
        workspace / "processed/target_speech.jsonl",
        workspace / "processed/excluded_documents.jsonl",
        workspace / "corpus/all_documents.jsonl",
        workspace / "corpus/blog.jsonl",
        workspace / "corpus/talks.jsonl",
        workspace / "corpus/_manifest.json",
        workspace / "blog.jsonl",
        workspace / "talks.jsonl",
        workspace / "_manifest.json",
        workspace / "quality_report.json",
    ]
    optional_processed = workspace / "processed/av_attribution_results.jsonl"
    if optional_processed.exists():
        paths.append(optional_processed)
    outcome_path = workspace / "processed/av_candidate_outcomes.jsonl"
    if outcome_path.exists():
        paths.append(outcome_path)
    provenance_paths = [
        workspace / "config_snapshot.json",
        workspace / "exclusion_rules_snapshot.json",
        workspace / "identity/resolved_identity.json",
        workspace / "identity/identity_evidence.jsonl",
        workspace / "identity/reference_voice_candidates.jsonl",
        workspace / "identity/reference_voice_profile.json",
        workspace / "discovery/source_plan.json",
        workspace / "discovery/source_candidates.jsonl",
        workspace / "discovery/approved_sources.jsonl",
        workspace / "discovery/rejected_sources.jsonl",
    ]
    provenance_paths.extend((workspace / "raw").rglob("*"))
    paths.extend(path for path in provenance_paths if path.is_file())
    files = [_file(path, workspace) for path in sorted(set(paths))]
    fingerprint_payload = {
        "investor_slug": investor_slug,
        "identity_id": identity_id,
        "config_hash": config_hash,
        "exclusion_rules_hash": exclusion_rules_hash,
        "files": [item.model_dump(mode="json") for item in files],
    }
    fingerprint = sha256(
        canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    manifest = CollectionManifest(
        investor_slug=investor_slug,
        identity_id=identity_id,
        config_hash=config_hash,
        exclusion_rules_hash=exclusion_rules_hash,
        files=files,
        corpus_documents=len(included),
        excluded_documents=len(excluded),
        fingerprint=fingerprint,
    )
    write_json(workspace / "collection_manifest.json", manifest)
    return manifest

"""Deterministic canonical and downstream-compatible corpus exports."""

from __future__ import annotations

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
from .storage import canonical_json, write_json, write_jsonl


def _video_id(document: CanonicalDocument) -> str:
    if document.canonical_url:
        parsed = urlsplit(document.canonical_url)
        query_id = parse_qs(parsed.query).get("v")
        if query_id:
            return query_id[0]
        if parsed.hostname == "youtu.be" and parsed.path.strip("/"):
            return parsed.path.strip("/")
    return document.source_item_id.rsplit(":", 1)[-1]


def export_persona_sources(output: Path, documents: list[CanonicalDocument]) -> dict[str, object]:
    output = Path(output)
    included = [document for document in documents if eligible_for_corpus(document)]
    blogs = [document for document in included if document.material_role == MaterialRole.AUTHORED_BY_TARGET]
    talks = [document for document in included if document.material_role == MaterialRole.SPOKEN_BY_TARGET]
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
            "source": "youtube_talk" if document.source_type.value == "youtube" else "talk",
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
        "no_pitch_sources": True,
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
    write_jsonl(workspace / "processed/documents.jsonl", ordered)
    write_jsonl(workspace / "processed/target_speech.jsonl", speech)
    write_jsonl(workspace / "processed/excluded_documents.jsonl", excluded)
    write_jsonl(workspace / "corpus/all_documents.jsonl", included)
    export_persona_sources(workspace / "corpus", ordered)

    warnings: list[str] = []
    if not included:
        warnings.append("Corpus contains no eligible documents")
    quality = QualityReport(
        investor_slug=investor_slug,
        passed=bool(included) and all(eligible_for_corpus(item) for item in included),
        checks={
            "corpus_nonempty": bool(included),
            "excluded_absent": all(eligible_for_corpus(item) for item in included),
            "lineage_present": all(bool(item.raw_artifact_ids) for item in ordered),
        },
        counts={
            "documents": len(ordered),
            "included": len(included),
            "excluded_or_pending": len(excluded),
            "target_speech": len(speech),
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
        workspace / "quality_report.json",
    ]
    files = [_file(path, workspace) for path in sorted(paths)]
    fingerprint_payload = {
        "investor_slug": investor_slug,
        "identity_id": identity_id,
        "config_hash": config_hash,
        "exclusion_rules_hash": exclusion_rules_hash,
        "files": [item.model_dump(mode="json") for item in files],
    }
    fingerprint = sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()
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

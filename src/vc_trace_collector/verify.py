"""Independent workspace integrity and corpus-policy verification."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import CanonicalDocument, CollectionManifest
from .policy import eligible_for_corpus
from .storage import read_json, read_jsonl


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    errors: list[str] = Field(default_factory=list)
    checked_files: int = 0
    checked_documents: int = 0


def verify_workspace(workspace: Path) -> VerificationResult:
    workspace = Path(workspace)
    errors: list[str] = []
    manifest_path = workspace / "collection_manifest.json"
    if not manifest_path.exists():
        return VerificationResult(passed=False, errors=["Missing collection_manifest.json"])
    try:
        manifest = CollectionManifest.model_validate(read_json(manifest_path))
    except Exception as error:
        return VerificationResult(passed=False, errors=[f"Invalid collection manifest: {error}"])

    checked_files = 0
    for item in manifest.files:
        path = workspace / item.path
        if not path.exists():
            errors.append(f"Missing manifest file: {item.path}")
            continue
        checked_files += 1
        digest = sha256(path.read_bytes()).hexdigest()
        if digest != item.sha256:
            errors.append(f"File hash mismatch: {item.path}")

    documents: list[CanonicalDocument] = []
    for row in read_jsonl(workspace / "corpus/all_documents.jsonl"):
        try:
            document = CanonicalDocument.model_validate(row)
        except Exception as error:
            errors.append(f"Invalid canonical document: {error}")
            continue
        documents.append(document)
        if not eligible_for_corpus(document):
            errors.append(f"Ineligible document present in corpus: {document.document_version_id}")
        if not document.raw_artifact_ids:
            errors.append(f"Document lacks raw lineage: {document.document_version_id}")
    if len(documents) != manifest.corpus_documents:
        errors.append(
            f"Manifest corpus count {manifest.corpus_documents} does not match {len(documents)}"
        )
    return VerificationResult(
        passed=not errors,
        errors=errors,
        checked_files=checked_files,
        checked_documents=len(documents),
    )

"""Typed terminal choices and artifact-backed source status."""

import shutil
from typing import Protocol

from pydantic import BaseModel, Field

from .collectors import is_acquired_media
from .models import SourceCandidate
from .process import load_artifact_records
from .storage import read_jsonl


class Option(BaseModel):
    value: str
    label: str


class SourceItem(BaseModel):
    candidate: SourceCandidate
    downloaded: bool = False
    media: bool = False
    collection_status: str = "not downloaded"
    processing_status: str = "not processed"
    paths: list[str] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)

    @property
    def label(self) -> str:
        c = self.candidate
        return (
            f"{c.title or c.canonical_url} | {c.source_type.value} | "
            f"identity {c.identity_confidence.score:.2f} | {c.approval_status.value} | "
            f"{self.collection_status} | {self.processing_status}"
        )


class Prompts(Protocol):
    def text(self, key: str, message: str, default: str = "") -> str: ...
    def confirm(self, key: str, message: str) -> bool: ...
    def select(self, key: str, message: str, options: list[Option]) -> str: ...
    def check(self, key: str, message: str, options: list[Option]) -> list[str]: ...
    def pick_sources(self, items: list[SourceItem], purpose: str) -> list[str]: ...
    def show(self, message: object) -> None: ...


def options(values) -> list[Option]:
    return [
        Option(value=str(value), label=str(value).replace("_", " ")) for value in values
    ]


def backend_options() -> list[Option]:
    status = (
        "installed; connectivity checked on search"
        if shutil.which("mcporter")
        else "missing mcporter; run doctor for setup"
    )
    return [
        Option(value="agent-reach", label=f"Agent Reach / Exa — {status}"),
        Option(
            value="default",
            label="Default — DDG or configured SearXNG; YouTube needs yt-dlp",
        ),
    ]


def source_items(pipeline, slug: str) -> list[SourceItem]:
    workspace = pipeline.workspace(slug).resolve()
    artifacts = load_artifact_records(workspace)
    evidence = read_jsonl(workspace / "identity/identity_evidence.jsonl")
    collected = {
        row["candidate_id"]: row
        for row in read_jsonl(
            workspace / "processed/collection_candidate_outcomes.jsonl"
        )
    }
    outcomes = {
        row["candidate_id"]: row
        for row in read_jsonl(workspace / "processed/av_candidate_outcomes.jsonl")
    }
    docs = {
        row["source_candidate_id"]: row
        for row in read_jsonl(workspace / "processed/documents.jsonl")
    }
    items = []
    for candidate in pipeline._load_plan(slug).candidates:
        cid = candidate.candidate_id
        local = [
            a
            for a in artifacts
            if a.original_metadata.get("candidate_id") == cid
            and (workspace / a.relative_path).resolve().is_relative_to(workspace)
            and (workspace / a.relative_path).is_file()
            and a.collection_method != "human_selected_reference_segment"
        ]
        media = any(is_acquired_media(a) for a in local)
        needs_media = candidate.source_type in {"youtube", "podcast"}
        downloaded = bool(local) and (media or not needs_media)
        items.append(
            SourceItem(
                candidate=candidate,
                downloaded=downloaded,
                media=media,
                collection_status=(
                    collected.get(cid, {}).get("status", "downloaded")
                    if downloaded
                    else (
                        collected.get(cid, {}).get("status", "not downloaded")
                        if collected.get(cid, {}).get("status") != "succeeded"
                        else "missing local artifact"
                    )
                ),
                processing_status=outcomes.get(cid, {}).get(
                    "status", docs.get(cid, {}).get("inclusion_status", "not processed")
                ),
                paths=[str(workspace / a.relative_path) for a in local],
                evidence=[
                    e
                    for e in evidence
                    if e.get("evidence_id") in candidate.evidence_ids
                    or e.get("canonical_url") == candidate.canonical_url
                ],
            )
        )
    return items

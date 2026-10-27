"""Read-only source inventory. Stage completion and review are distinct facts."""

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .av_cache import DiarizationCacheRecord, TranscriptCacheRecord
from .dashboard_state import APPROVED, UNFINISHED
from .process import load_artifact_records
from .storage import read_json, read_jsonl
from .wizard_models import SourceItem, source_items


def is_recording(item: SourceItem) -> bool:
    return bool(
        item.media
        or item.candidate.source_type in {"youtube", "podcast"}
        or Path(item.candidate.canonical_url).suffix.lower()
        in {
            ".mp4",
            ".webm",
            ".mkv",
            ".mov",
            ".avi",
            ".wav",
            ".mp3",
            ".m4a",
            ".ogg",
            ".opus",
            ".flac",
        }
    )


def ready_recording(item: SourceItem) -> bool:
    return bool(
        item.media
        and item.downloaded
        and item.candidate.approval_status in APPROVED
        and item.candidate.material_role in {"spoken_by_target", "unknown"}
        and item.processing_status in UNFINISHED
        and item.collection_status != "excluded"
    )


class InventoryRow(BaseModel):
    item: SourceItem
    transcription: str = "n/a"
    diarization: str = "n/a"
    attribution: str = "n/a"
    match: dict = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    decisions: list[dict] = Field(default_factory=list)

    @property
    def candidate_id(self):
        return self.item.candidate.candidate_id

    @property
    def approval(self):
        return self.item.candidate.approval_status.value

    @property
    def download(self):
        return (
            "downloaded"
            if self.item.downloaded
            else {
                "not downloaded": "missing",
                "missing local artifact": "missing file",
            }.get(self.item.collection_status, self.item.collection_status)
        )

    @property
    def processing(self):
        return self.item.processing_status

    @property
    def ready(self):
        return ready_recording(self.item)

    def details(self):
        c = self.item.candidate
        return (
            f"{c.title or 'Untitled'}\n{c.canonical_url}\n\n"
            f"Platform: {c.source_type.value}\nSource approval: {self.approval}\n"
            f"Download: {self.download}\nTranscription: {self.transcription}\n"
            f"Diarization: {self.diarization}\nSpeaker attribution: {self.attribution}\n"
            f"Processing result: {self.processing}\nMaterial role: {c.material_role.value}\n"
            f"Identity confidence: {c.identity_confidence.score:.2f} (automated score)\n"
            f"Reviewer: {c.reviewed_by or 'none'}\nDecision: {c.decision_reason or 'none'}\n\n"
            "Reasons / errors:\n"
            + ("\n".join(self.reasons) or "None recorded")
            + "\n\nFiles:\n"
            + ("\n".join(self.item.paths) or "None downloaded")
            + "\n\nVoice-match evidence:\n"
            + json.dumps(self.match, indent=2)
            + "\n\nReview decisions:\n"
            + json.dumps(self.decisions, indent=2)
            + "\n\nDiscovery / identity evidence:\n"
            + json.dumps(self.item.evidence, indent=2)
            + "\n\nSource approval is not speaker verification. Complete means saved stage output; changed model settings may require reprocessing."
        )


def inventory(pipeline, slug: str) -> list[InventoryRow]:
    workspace = pipeline.workspace(slug)
    items = source_items(pipeline, slug)
    results = {
        r["candidate_id"]: r["result"]
        for r in read_jsonl(workspace / "processed/av_attribution_results.jsonl")
    }
    outcomes = {}
    for name in ("collection_candidate_outcomes", "av_candidate_outcomes"):
        for r in read_jsonl(workspace / f"processed/{name}.jsonl"):
            outcomes.setdefault(r["candidate_id"], []).append(r)
    decisions = {}
    for r in read_jsonl(workspace / "discovery/source_decisions.jsonl"):
        decisions.setdefault(r["candidate_id"], []).append(r)
    # Partial failures may still have a completed upstream stage.
    partial = {
        i.candidate.candidate_id
        for i in items
        if is_recording(i) and i.candidate.candidate_id not in results
    }
    artifact_candidates = {
        a.artifact_id: a.original_metadata.get("candidate_id")
        for a in load_artifact_records(workspace)
    }
    completed = {"transcription": set(), "diarization": set()}
    if partial:
        for stage, folder, model in (
            ("transcription", "av_transcripts", TranscriptCacheRecord),
            ("diarization", "av_diarization", DiarizationCacheRecord),
        ):
            for path in (workspace / "state" / folder).glob("*.json"):
                try:
                    record = read_json(path)
                    if not isinstance(record, dict):
                        continue
                    if not isinstance(record.get("artifact_id"), str):
                        continue
                    cid = artifact_candidates.get(record.get("artifact_id"))
                    if cid in partial:
                        model.model_validate(record)
                        completed[stage].add(cid)
                except (OSError, ValueError, ValidationError):
                    continue
    rows = []
    for item in items:
        cid = item.candidate.candidate_id
        row = InventoryRow(
            item=item,
            decisions=decisions.get(cid, []),
            reasons=[r["reason"] for r in outcomes.get(cid, []) if r.get("reason")],
        )
        if is_recording(item):
            result = results.get(cid)
            default = "unknown" if item.processing_status == "failed" else "pending"
            row.transcription = (
                "complete" if result or cid in completed["transcription"] else default
            )
            row.diarization = (
                "complete" if result or cid in completed["diarization"] else default
            )
            row.match = result.get("attribution", {}) if result else {}
            row.attribution = {
                "accepted_model": "matched",
                "verified_human": "operator-approved",
            }.get(row.match.get("status"), row.match.get("status", "pending"))
        rows.append(row)
    return rows

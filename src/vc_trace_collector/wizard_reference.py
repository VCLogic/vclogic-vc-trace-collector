"""Human reference decisions and AV-only dependency checks."""

import importlib.util
import math
import shutil
from pathlib import Path

from .collectors import is_acquired_media
from .config import RunConfig
from .discovery import stable_id
from .models import (
    ApprovalStatus,
    Confidence,
    MaterialRole,
    SourceCandidate,
    SourceDecision,
    SourceType,
)
from .process import load_artifact_records
from .storage import read_json, read_jsonl, write_jsonl
from .wizard_models import Option, options, source_items

MEDIA_SUFFIXES = {
    ".wav",
    ".mp3",
    ".mp4",
    ".m4a",
    ".webm",
    ".ogg",
    ".flac",
    ".opus",
    ".mov",
}


def missing_av_dependencies():
    missing = []
    for module in ("whisper", "pyannote.audio"):
        try:
            available = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            available = False
        if not available:
            missing.append(module)
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            missing.append(executable)
    return missing


def stage_local_reference(pipeline, slug, path: Path, reviewer):
    path = path.expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.casefold() not in MEDIA_SUFFIXES:
        raise ValueError("Reference must be a local audio/video file")
    if pipeline._load_identity(slug).resolution_status != "confirmed":
        raise ValueError("Confirm identity before staging a reference")
    url = path.as_uri()
    plan = pipeline._load_plan(slug)
    existing = next((c for c in plan.candidates if c.canonical_url == url), None)
    if existing and existing.material_role != MaterialRole.REFERENCE_VOICE:
        raise ValueError(
            "This file already has another source role. Select its downloaded recording instead."
        )
    cid = existing.candidate_id if existing else stable_id("candidate", url)
    if not existing:
        confidence = Confidence(
            score=1, method="human_supplied_reference_candidate", version="1"
        )
        plan.candidates.append(
            SourceCandidate(
                candidate_id=cid,
                url=url,
                canonical_url=url,
                source_type=SourceType.SUPPLIED,
                title=path.name,
                material_role=MaterialRole.REFERENCE_VOICE,
                discovery_queries=["human supplied local reference"],
                discovered_via="wizard_local_reference",
                identity_confidence=confidence,
                source_confidence=confidence,
            )
        )
        pipeline._save_plan(slug, plan)
    pipeline.review(
        slug,
        decisions=[
            SourceDecision(
                candidate_id=cid,
                status=ApprovalStatus.APPROVED,
                reason="Human requested local reference collection; voice not yet verified",
                decided_by=reviewer,
                material_role=MaterialRole.REFERENCE_VOICE,
            )
        ],
        reviewer=reviewer,
        confirm_identity=True,
    )
    return cid


def prepare_reference(pipeline, ui, slug, dependency_check=None):
    workspace = pipeline.workspace(slug)
    config = RunConfig.model_validate(read_json(workspace / "config_snapshot.json"))
    profiles = [
        p
        for p in pipeline._load_reference_profiles(workspace)
        if p.status == "verified_human"
    ]
    action = (
        "existing"
        if profiles
        else ui.select(
            "reference_action",
            "A human-approved voice reference is needed",
            options(["recording", "local_file", "later"]),
        )
    )
    if action == "later":
        ui.show(
            "Processing paused before model calls. Supply a reference or select written sources only."
        )
        return None
    if action == "local_file":
        path = Path(ui.text("reference_path", "Local public audio/video sample path"))
        if ui.confirm(
            "stage_reference",
            "Stage this file for the separate download step (no copying or embedding now)?",
        ):
            cid = stage_local_reference(
                pipeline, slug, path, ui.text("reviewer", "Reviewer", "human")
            )
            ui.show(
                f"Reference staged: {cid}. Run wizard --stage download --investor {slug}, "
                "select this file, then return to processing to approve the voice interval."
            )
        return None
    missing = (dependency_check or missing_av_dependencies)()
    if missing:
        ui.show(
            f"Missing AV dependencies: {', '.join(missing)}. "
            "Run uv sync --extra av --extra av-local (plus --extra youtube for YouTube), "
            "and install FFmpeg. Configure HF_TOKEN in .env and accept required model access."
        )
        return None
    models = {
        "transcription_model": ui.text(
            "transcription", "Whisper model", config.transcription_model or "turbo"
        ),
        "diarization_model": ui.text(
            "diarization",
            "Diarization model",
            config.diarization_model or "pyannote/speaker-diarization-3.1",
        ),
    }
    compatible = [
        p for p in profiles if p.embedding_model == models["diarization_model"]
    ]
    if profiles and not compatible:
        ui.show(
            "Saved references use another embedding space. Select a downloaded reference "
            "for the chosen diarization model or stop; no transcription has started."
        )
        if (
            ui.select(
                "incompatible_reference",
                "Reference action",
                options(["later", "recording"]),
            )
            == "later"
        ):
            return None
    if compatible:
        ui.show(
            {
                "approved_references": [
                    p.model_dump(mode="json", exclude={"embedding"}) for p in compatible
                ],
                "speaker_minimum_score": config.speaker_minimum_score,
                "speaker_minimum_margin": config.speaker_minimum_margin,
            }
        )
        return models
    available = {
        item.candidate.candidate_id: item
        for item in source_items(pipeline, slug)
        if item.downloaded
        and item.media
        and item.candidate.approval_status
        in {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
    }
    voice_path = workspace / "identity/reference_voice_candidates.jsonl"
    voice_rows = read_jsonl(voice_path)
    # Supplied video records can lack the audio-only backlink created by collection.
    # Resolve it from real local artifacts, saving only after voice confirmation.
    for row in voice_rows:
        if not row.get("artifact_id") and row["source_candidate_id"] in available:
            artifact = next(
                (
                    a
                    for a in load_artifact_records(workspace)
                    if a.original_metadata.get("candidate_id")
                    == row["source_candidate_id"]
                    and str(workspace.resolve() / a.relative_path)
                    in available[row["source_candidate_id"]].paths
                    and is_acquired_media(a)
                ),
                None,
            )
            if artifact:
                row["artifact_id"] = artifact.artifact_id
    voices = [
        row
        for row in voice_rows
        if row.get("artifact_id") and row["source_candidate_id"] in available
    ]
    if not voices:
        ui.show(
            "No downloaded reference candidate. In discovery review an appearance as spoken_by_target "
            "or reference_voice, then download it. No media will be fetched here."
        )
        return None
    cid = ui.select(
        "reference",
        "Recording containing the target voice",
        [
            Option(
                value=row["candidate_id"],
                label=available[row["source_candidate_id"]].label,
            )
            for row in voices
        ],
    )
    voice = next(row for row in voices if row["candidate_id"] == cid)
    ui.show(
        {
            "listen_locally": available[voice["source_candidate_id"]].paths,
            "instruction": "Listen yourself. Choose an interval with only the target; no host, music or crosstalk.",
        }
    )
    start = float(ui.text("start", "Start seconds", "0"))
    end = float(ui.text("end", "End seconds"))
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError("Invalid reference interval: require finite 0 <= start < end")
    records = {a.artifact_id: a for a in load_artifact_records(workspace)}
    original = records[voice["artifact_id"]]
    visited = set()
    while (
        original.collection_method == "human_selected_reference_segment"
        and original.parent_artifact_ids
    ):
        if original.artifact_id in visited:
            raise ValueError("Reference artifact lineage cycle")
        visited.add(original.artifact_id)
        original = records[original.parent_artifact_ids[0]]
    media_path = (workspace / original.relative_path).resolve()
    if not media_path.is_relative_to(workspace.resolve()) or not media_path.is_file():
        raise ValueError("Reference source is not a local workspace artifact")
    duration = pipeline.media_probe(media_path)
    if (
        duration is None
        or not math.isfinite(duration)
        or duration <= 0
        or end > duration
    ):
        raise ValueError(
            "Reference interval exceeds recording duration or duration is unknown"
        )
    ui.show(
        f"Reference embedding will use the selected diarization pipeline's matching embedding space "
        f"({models['diarization_model']}); saved cost bound ${config.embedding_cost_usd}; "
        f"total budget ${config.maximum_cost_usd}; recording duration {duration:.1f}s. "
        "Any standalone embedding-model setting will be cleared for this reference."
    )
    if not ui.confirm(
        "confirm_voice",
        "I listened: only the target speaks in this interval. Create the reference embedding?",
    ):
        return None
    write_jsonl(voice_path, voice_rows)
    pipeline.approve_reference_voice(
        slug,
        candidate_id=cid,
        reviewer=ui.text("reviewer", "Reviewer", "human"),
        start_seconds=start,
        end_seconds=end,
        diarization_model=models["diarization_model"],
        embedding_model="",
    )
    return models

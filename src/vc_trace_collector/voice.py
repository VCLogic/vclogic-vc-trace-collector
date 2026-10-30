"""Voice-reference helpers: suggestions, speaker grouping, clips, candidate lookup."""

import importlib.util
import math
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .av import DiarizedTurn, extract_audio_segment
from .collectors import is_acquired_media
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
from .storage import read_jsonl, write_jsonl

APPROVED = {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
SPOKEN = {MaterialRole.SPOKEN_BY_TARGET, MaterialRole.REFERENCE_VOICE}
MAX_REFERENCE_SECONDS = 30.0
# Parts per speaker offered for inspection in the voice step.
MAX_SPEAKER_CLIPS = 3

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


def reference_suggestions(items, name: str, limit: int = 3):
    eligible = [
        i
        for i in items
        if i.downloaded
        and i.media
        and i.candidate.approval_status in APPROVED
        and i.candidate.material_role != "third_party"
    ]

    def rank(item):
        c = item.candidate
        named = bool(
            re.search(r"\b" + re.escape(name) + r"\b", c.title or "", re.IGNORECASE)
        )
        return (
            -int(named),
            -c.identity_confidence.score,
            -int(c.material_role in {"spoken_by_target", "reference_voice"}),
            c.candidate_id,
        )

    return sorted(eligible, key=rank)[:limit]


def speakers(
    turns: list[DiarizedTurn], limit: int = MAX_SPEAKER_CLIPS
) -> list[tuple[str, float, list[DiarizedTurn]]]:
    """(label, total seconds, up to `limit` longest turns), most talkative first."""
    total: dict[str, float] = defaultdict(float)
    by_label: dict[str, list[DiarizedTurn]] = defaultdict(list)
    for turn in turns:
        total[turn.speaker_label] += turn.end_seconds - turn.start_seconds
        by_label[turn.speaker_label].append(turn)
    return sorted(
        (
            (
                label,
                total[label],
                sorted(
                    by_label[label], key=lambda t: t.start_seconds - t.end_seconds
                )[:limit],
            )
            for label in total
        ),
        key=lambda row: -row[1],
    )


def reference_interval(turn: DiarizedTurn) -> tuple[float, float]:
    return turn.start_seconds, min(
        turn.end_seconds, turn.start_seconds + MAX_REFERENCE_SECONDS
    )


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def parse_range(text: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", text)
    if not match:
        raise ValueError("Use a range like 03:10-03:40")
    try:
        start, end = _seconds(match[1]), _seconds(match[2])
    except ValueError:
        raise ValueError("Use a range like 03:10-03:40") from None
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError("Start must be before end")
    return start, end


def voice_candidate(pipeline, slug, item, *, reviewer) -> tuple[str, Path]:
    """Voice-candidate id and local media path for a downloaded source."""
    workspace = pipeline.workspace(slug).resolve()
    cid = item.candidate.candidate_id
    if item.candidate.material_role not in SPOKEN:
        # review() creates the voice-candidate row for spoken sources.
        pipeline.review(
            slug,
            decisions=[
                SourceDecision(
                    candidate_id=cid,
                    status=item.candidate.approval_status,
                    reason="Chosen as voice reference recording",
                    decided_by=reviewer,
                    material_role=MaterialRole.SPOKEN_BY_TARGET,
                )
            ],
            reviewer=reviewer,
            confirm_identity=True,
        )
    artifact = next(
        (
            a
            for a in load_artifact_records(workspace)
            if a.original_metadata.get("candidate_id") == cid
            and is_acquired_media(a)
            and a.collection_method != "human_selected_reference_segment"
        ),
        None,
    )
    media = (workspace / artifact.relative_path).resolve() if artifact else None
    if media is None or not media.is_relative_to(workspace) or not media.is_file():
        raise ValueError("No downloaded media for this source")
    path = workspace / "identity/reference_voice_candidates.jsonl"
    rows = read_jsonl(path)
    row = next(r for r in rows if r["source_candidate_id"] == cid)
    if not row.get("artifact_id"):
        row["artifact_id"] = artifact.artifact_id
        write_jsonl(path, rows)
    return row["candidate_id"], media


def clock(seconds: float) -> str:
    """03:10 or 1:03:10, the format parse_range accepts."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def timestamp_url(url: str, seconds: float) -> str:
    """Link opening a YouTube video at `seconds`; other URLs are returned unchanged."""
    host = urlsplit(url).hostname or ""
    if host != "youtu.be" and not host.endswith("youtube.com"):
        return url
    return f"{url}{'&' if '?' in url else '?'}t={int(seconds)}s"


def can_play() -> bool:
    # Over SSH, ffplay would play on the remote machine's speakers.
    return bool(shutil.which("ffplay")) and not os.environ.get("SSH_CONNECTION")


def play(media: Path, start: float, end: float, workdir: Path) -> str | None:
    """Play a clip with ffplay; return the clip path when ffplay is unavailable."""
    clip = workdir / f"clip-{start:.0f}-{end:.0f}.wav"
    extract_audio_segment(media, clip, start_seconds=start, end_seconds=end)
    if not shutil.which("ffplay"):
        return str(clip)
    subprocess.run(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(clip)],
        check=False,
    )
    return None

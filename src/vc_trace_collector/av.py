"""Provider-neutral audiovisual processing and speaker attribution."""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    ReferenceVoiceProfile,
    SpeakerAttribution,
    SpeakerStatus,
    TranscriptInfo,
)


class TimedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def valid_interval(self) -> TimedText:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be after start_seconds")
        return self

    @classmethod
    def from_caption(cls, item: dict) -> TimedText:
        start = float(item.get("start", item.get("start_seconds", 0)))
        if "end" in item or "end_seconds" in item:
            end = float(item.get("end", item.get("end_seconds")))
        else:
            end = start + float(item.get("duration", 0))
        return cls(start_seconds=start, end_seconds=end, text=str(item["text"]))


class DiarizedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    speaker_label: str

    @model_validator(mode="after")
    def valid_interval(self) -> DiarizedTurn:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be after start_seconds")
        return self


class AlignedText(TimedText):
    speaker_label: str | None = None
    overlap_seconds: float = 0


class TranscriptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    info: TranscriptInfo
    segments: list[TimedText]


class DiarizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    turns: list[DiarizedTurn]
    speaker_embeddings: dict[str, list[float]]


class TargetSpeechResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: TranscriptInfo
    attribution: SpeakerAttribution
    aligned_segments: list[AlignedText]
    target_segments: list[AlignedText]
    media_seconds: float = Field(ge=0)


class TranscriptProvider(Protocol):
    provider_name: str
    model_name: str

    def transcribe(self, audio_path: Path) -> TranscriptResult: ...


class DiarizationProvider(Protocol):
    provider_name: str
    model_name: str

    def diarize(self, audio_path: Path) -> DiarizationResult: ...


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    def embed(self, audio_path: Path) -> list[float]: ...


def cosine_similarity(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second) or not first:
        raise ValueError("Embeddings must have the same non-zero dimension")
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0 or second_norm == 0:
        raise ValueError("Cannot compare a zero-length embedding vector")
    return sum(a * b for a, b in zip(first, second, strict=True)) / (
        first_norm * second_norm
    )


def match_target_speaker(
    reference: Sequence[float],
    speakers: dict[str, Sequence[float]],
    minimum_score: float,
    minimum_margin: float,
    *,
    diarization_model: str | None = None,
    embedding_model: str | None = None,
    reference_artifact_ids: list[str] | None = None,
) -> SpeakerAttribution:
    if not speakers:
        return SpeakerAttribution(
            status=SpeakerStatus.UNAVAILABLE,
            minimum_score=minimum_score,
            minimum_margin=minimum_margin,
            diarization_model=diarization_model,
            embedding_model=embedding_model,
            reference_artifact_ids=reference_artifact_ids or [],
        )
    ranked = sorted(
        (
            (label, cosine_similarity(reference, vector))
            for label, vector in speakers.items()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    label, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
    margin = score - runner_up
    status = (
        SpeakerStatus.ACCEPTED_MODEL
        if score >= minimum_score and margin >= minimum_margin
        else SpeakerStatus.UNCERTAIN
    )
    return SpeakerAttribution(
        status=status,
        speaker_label=label,
        score=score,
        runner_up_score=runner_up,
        margin=margin,
        minimum_score=minimum_score,
        minimum_margin=minimum_margin,
        diarization_model=diarization_model,
        embedding_model=embedding_model,
        reference_artifact_ids=reference_artifact_ids or [],
    )


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def align_transcript_to_speakers(
    transcript: list[TimedText], turns: list[DiarizedTurn]
) -> list[AlignedText]:
    aligned: list[AlignedText] = []
    for item in transcript:
        overlaps = [
            (
                turn.speaker_label,
                _overlap(
                    item.start_seconds,
                    item.end_seconds,
                    turn.start_seconds,
                    turn.end_seconds,
                ),
            )
            for turn in turns
        ]
        label, seconds = (
            max(overlaps, key=lambda entry: entry[1]) if overlaps else (None, 0.0)
        )
        aligned.append(
            AlignedText(
                start_seconds=item.start_seconds,
                end_seconds=item.end_seconds,
                text=item.text,
                speaker_label=label if seconds > 0 else None,
                overlap_seconds=seconds,
            )
        )
    return aligned


def process_target_speech(
    audio_path: Path,
    *,
    reference: ReferenceVoiceProfile,
    diarization_provider: DiarizationProvider,
    transcript_provider: TranscriptProvider | None = None,
    existing_transcript: TranscriptResult | None = None,
    segment_extractor: Callable[..., Path] | None = None,
    minimum_score: float = 0.75,
    minimum_margin: float = 0.10,
) -> TargetSpeechResult:
    if existing_transcript is None and transcript_provider is None:
        raise ValueError("A transcript or transcript provider is required")
    diarization = diarization_provider.diarize(audio_path)
    attribution = match_target_speaker(
        reference.embedding,
        diarization.speaker_embeddings,
        minimum_score,
        minimum_margin,
        diarization_model=diarization.model,
        embedding_model=reference.embedding_model,
        reference_artifact_ids=reference.artifact_ids,
    )
    if existing_transcript is not None:
        transcript = existing_transcript
        aligned = align_transcript_to_speakers(transcript.segments, diarization.turns)
    else:
        assert transcript_provider is not None
        extractor = segment_extractor or extract_audio_segment
        aligned = []
        transcript_info = TranscriptInfo(
            method="speech_to_text",
            provider=transcript_provider.provider_name,
            model=transcript_provider.model_name,
        )
        with tempfile.TemporaryDirectory(prefix="vc-trace-segments-") as directory:
            segment_dir = Path(directory)
            for index, turn in enumerate(diarization.turns):
                safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", turn.speaker_label)
                segment_path = segment_dir / f"segment_{safe_label}_{index:05d}.wav"
                extractor(
                    audio_path,
                    segment_path,
                    start_seconds=turn.start_seconds,
                    end_seconds=turn.end_seconds,
                )
                segment_transcript = transcript_provider.transcribe(segment_path)
                transcript_info = segment_transcript.info
                for item in segment_transcript.segments:
                    start = turn.start_seconds + item.start_seconds
                    end = min(turn.end_seconds, turn.start_seconds + item.end_seconds)
                    if end <= start:
                        continue
                    aligned.append(
                        AlignedText(
                            start_seconds=start,
                            end_seconds=end,
                            text=item.text,
                            speaker_label=turn.speaker_label,
                            overlap_seconds=end - start,
                        )
                    )
        transcript = TranscriptResult(
            info=transcript_info,
            segments=[
                TimedText(
                    start_seconds=item.start_seconds,
                    end_seconds=item.end_seconds,
                    text=item.text,
                )
                for item in aligned
            ],
        )
    target = [
        item
        for item in aligned
        if attribution.speaker_label is not None
        and item.speaker_label == attribution.speaker_label
    ]
    media_seconds = max(
        [item.end_seconds for item in aligned]
        + [item.end_seconds for item in diarization.turns]
        + [0.0]
    )
    return TargetSpeechResult(
        transcript=transcript.info,
        attribution=attribution,
        aligned_segments=aligned,
        target_segments=target,
        media_seconds=media_seconds,
    )


Runner = Callable[..., subprocess.CompletedProcess]


def probe_media_duration(
    source: Path,
    *,
    ffprobe: str = "ffprobe",
    runner: Runner = subprocess.run,
) -> float | None:
    try:
        completed = runner(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        duration = float(completed.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def extract_audio_segment(
    source: Path,
    output: Path,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    sample_rate: int = 16_000,
    channels: int = 1,
    ffmpeg: str = "ffmpeg",
    runner: Runner = subprocess.run,
) -> Path:
    source = Path(source)
    output = Path(output)
    if start_seconds is not None and start_seconds < 0:
        raise ValueError("start_seconds must be non-negative")
    if (
        end_seconds is not None
        and start_seconds is not None
        and end_seconds <= start_seconds
    ):
        raise ValueError("end_seconds must be after start_seconds")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid4().hex}.tmp{output.suffix}")
    command = [ffmpeg, "-y", "-loglevel", "error"]
    if start_seconds is not None:
        command.extend(["-ss", str(start_seconds)])
    if end_seconds is not None:
        duration = end_seconds - (start_seconds or 0)
        if duration <= 0:
            raise ValueError("end_seconds must be positive")
        command.extend(["-t", str(duration)])
    command.extend(
        [
            "-i",
            str(source),
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ]
    )
    try:
        runner(command, check=True, capture_output=True)
        if not temporary.exists() or temporary.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced no audio output")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


class WhisperTranscriptProvider:
    provider_name = "local-whisper"

    def __init__(self, model: str):
        self.model_name = model
        try:
            import whisper
        except ImportError as error:
            raise RuntimeError("Install the av-local extra to use Whisper") from error
        self._model = whisper.load_model(model)

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        result = self._model.transcribe(str(audio_path))
        segments = [
            TimedText(
                start_seconds=float(item["start"]),
                end_seconds=float(item["end"]),
                text=str(item["text"]).strip(),
            )
            for item in result.get("segments", [])
        ]
        return TranscriptResult(
            info=TranscriptInfo(
                method="speech_to_text",
                provider=self.provider_name,
                model=self.model_name,
                language=result.get("language"),
            ),
            segments=segments,
        )


class PyannoteDiarizationProvider:
    provider_name = "pyannote"

    def __init__(
        self, model: str, *, token: str | None = None, device: str | None = None
    ):
        self.model_name = model
        try:
            from pyannote.audio import Pipeline
        except ImportError as error:
            raise RuntimeError("Install the av-local extra to use pyannote") from error
        try:
            self._pipeline = Pipeline.from_pretrained(model, token=token)
        except TypeError:
            self._pipeline = Pipeline.from_pretrained(model, use_auth_token=token)
        if device:
            import torch

            self._pipeline.to(torch.device(device))

    def diarize(self, audio_path: Path) -> DiarizationResult:
        annotation, embeddings = self._pipeline(str(audio_path), return_embeddings=True)
        labels = annotation.labels()
        turns = [
            DiarizedTurn(
                start_seconds=float(segment.start),
                end_seconds=float(segment.end),
                speaker_label=label,
            )
            for segment, _, label in annotation.itertracks(yield_label=True)
        ]
        by_label = {
            label: [float(value) for value in embeddings[index]]
            for index, label in enumerate(labels)
        }
        return DiarizationResult(
            model=self.model_name,
            turns=turns,
            speaker_embeddings=by_label,
        )


class PyannoteEmbeddingProvider:
    provider_name = "pyannote"

    def __init__(
        self, model: str, *, token: str | None = None, device: str | None = None
    ):
        self.model_name = model
        try:
            from pyannote.audio import Inference
        except ImportError as error:
            raise RuntimeError("Install the av-local extra to use pyannote") from error
        try:
            self._inference = Inference(model, token=token, window="whole")
        except TypeError:
            self._inference = Inference(model, use_auth_token=token, window="whole")
        if device:
            import torch

            self._inference.to(torch.device(device))

    def embed(self, audio_path: Path) -> list[float]:
        values = self._inference(str(audio_path))
        return [float(value) for value in values]

"""Provider-neutral audiovisual processing and speaker attribution."""

from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import SpeakerAttribution, SpeakerStatus, TranscriptInfo


class TimedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str


class DiarizedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    speaker_label: str


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


Runner = Callable[..., subprocess.CompletedProcess]


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
        command.extend(["-to", str(end_seconds)])
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

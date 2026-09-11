import subprocess
from pathlib import Path

import pytest

from vc_trace_collector.av import (
    DiarizationResult,
    DiarizedTurn,
    TimedText,
    TranscriptResult,
    align_transcript_to_speakers,
    extract_audio_segment,
    match_target_speaker,
    probe_media_duration,
    process_target_speech,
)
from vc_trace_collector.models import (
    ReferenceVoiceProfile,
    SpeakerStatus,
    TranscriptInfo,
)


def test_weak_best_match_is_uncertain() -> None:
    decision = match_target_speaker(
        reference=[1.0, 0.0],
        speakers={"A": [0.80, 0.60], "B": [0.78, 0.625]},
        minimum_score=0.75,
        minimum_margin=0.10,
    )

    assert decision.status == SpeakerStatus.UNCERTAIN
    assert decision.speaker_label == "A"
    assert decision.margin < 0.10


def test_clear_match_records_score_and_margin() -> None:
    decision = match_target_speaker(
        reference=[1.0, 0.0],
        speakers={"A": [1.0, 0.0], "B": [0.0, 1.0]},
        minimum_score=0.75,
        minimum_margin=0.10,
    )

    assert decision.status == SpeakerStatus.ACCEPTED_MODEL
    assert decision.score == pytest.approx(1.0)
    assert decision.margin == pytest.approx(1.0)


def test_zero_length_embedding_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero"):
        match_target_speaker(
            reference=[0.0, 0.0],
            speakers={"A": [1.0, 0.0]},
            minimum_score=0.75,
            minimum_margin=0.10,
        )


def test_transcript_is_aligned_by_maximum_time_overlap() -> None:
    aligned = align_transcript_to_speakers(
        transcript=[
            TimedText(start_seconds=0, end_seconds=2, text="host"),
            TimedText(start_seconds=2, end_seconds=5, text="investor"),
        ],
        turns=[
            DiarizedTurn(start_seconds=0, end_seconds=2.2, speaker_label="HOST"),
            DiarizedTurn(start_seconds=2.2, end_seconds=5, speaker_label="VC"),
        ],
    )

    assert [item.speaker_label for item in aligned] == ["HOST", "VC"]


def test_audio_segment_extraction_uses_atomic_output(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    output = tmp_path / "reference.wav"
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"wave")
        return subprocess.CompletedProcess(command, 0)

    extract_audio_segment(
        source, output, start_seconds=10, end_seconds=40, runner=runner
    )

    assert output.read_bytes() == b"wave"
    assert "-ss" in calls[0]
    assert "-t" in calls[0]
    assert calls[0][calls[0].index("-t") + 1] == "30"
    assert not list(tmp_path.glob("*.tmp.wav"))


def test_media_duration_probe_is_bounded_and_parsed(tmp_path) -> None:
    source = tmp_path / "episode.mp3"
    source.write_bytes(b"fixture")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="2561.67\n", stderr="")

    duration = probe_media_duration(source, runner=runner)

    assert duration == pytest.approx(2561.67)
    assert calls[0][1]["timeout"] == 60


class FakeTranscript:
    provider_name = "fixture-stt"
    model_name = "fixture-stt-1"

    def transcribe(self, audio_path):
        return TranscriptResult(
            info=TranscriptInfo(
                method="speech_to_text",
                provider=self.provider_name,
                model=self.model_name,
            ),
            segments=[
                TimedText(start_seconds=0, end_seconds=2, text="Host asks."),
                TimedText(start_seconds=2, end_seconds=5, text="I back durable firms."),
            ],
        )


class FakeDiarization:
    provider_name = "fixture-diarization"
    model_name = "fixture-diarization-1"

    def __init__(self, vc_embedding=None):
        self.vc_embedding = vc_embedding or [1.0, 0.0]

    def diarize(self, audio_path):
        return DiarizationResult(
            model=self.model_name,
            turns=[
                DiarizedTurn(start_seconds=0, end_seconds=2, speaker_label="HOST"),
                DiarizedTurn(start_seconds=2, end_seconds=5, speaker_label="VC"),
            ],
            speaker_embeddings={"HOST": [0.0, 1.0], "VC": self.vc_embedding},
        )


def reference_profile() -> ReferenceVoiceProfile:
    return ReferenceVoiceProfile(
        investor_slug="michael-hyatt",
        candidate_ids=["voice:1"],
        artifact_ids=["sha256:" + "a" * 64],
        status="verified_human",
        embedding_model="fixture-embedding",
        embedding_model_version="1",
        embedding=[1.0, 0.0],
    )


def test_target_speech_pipeline_extracts_only_clear_matched_speaker(tmp_path) -> None:
    audio = tmp_path / "episode.wav"
    audio.write_bytes(b"fixture")

    result = process_target_speech(
        audio,
        reference=reference_profile(),
        transcript_provider=FakeTranscript(),
        diarization_provider=FakeDiarization(),
    )

    assert result.attribution.status == SpeakerStatus.ACCEPTED_MODEL
    assert [item.text for item in result.target_segments] == ["I back durable firms."]
    assert result.media_seconds == 5


def test_target_speech_pipeline_flags_weak_match_for_review(tmp_path) -> None:
    audio = tmp_path / "episode.wav"
    audio.write_bytes(b"fixture")

    result = process_target_speech(
        audio,
        reference=reference_profile(),
        transcript_provider=FakeTranscript(),
        diarization_provider=FakeDiarization([0.72, 0.69]),
        minimum_score=0.9,
    )

    assert result.attribution.status == SpeakerStatus.UNCERTAIN

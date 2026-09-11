import subprocess
from pathlib import Path

import pytest

from vc_trace_collector.av import (
    DiarizedTurn,
    TimedText,
    align_transcript_to_speakers,
    extract_audio_segment,
    match_target_speaker,
)
from vc_trace_collector.models import SpeakerStatus


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
    assert "-to" in calls[0]
    assert not list(tmp_path.glob("*.tmp.wav"))

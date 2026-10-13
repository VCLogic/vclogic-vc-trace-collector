from pathlib import Path

import pytest

from vc_trace_collector.av import (
    DiarizationResult,
    DiarizedTurn,
    TimedText,
    TranscriptResult,
    assemble_target_speech,
    transcribe_diarized_turns,
)
from vc_trace_collector.av_cache import transcript_cache_key
from vc_trace_collector.models import SpeakerAttribution, SpeakerStatus, TranscriptInfo


def test_full_audio_transcribes_once_without_extracting(monkeypatch):
    monkeypatch.setenv("VC_TRACE_TRANSCRIPTION_STRATEGY", "full-audio-words-v1")

    class Provider:
        calls = 0

        def transcribe_words(self, path):
            self.calls += 1
            return TranscriptResult(
                info=TranscriptInfo(method="speech_to_text_full_audio_words_v1"),
                segments=[TimedText(start_seconds=0, end_seconds=1, text="Hello")],
            )

    provider = Provider()
    result = transcribe_diarized_turns(
        Path("audio.wav"),
        DiarizationResult(model="fixture", turns=[], speaker_embeddings={}),
        provider,
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Must not extract turns")),
    )
    assert provider.calls == 1
    assert result.segments[0].text == "Hello"


def test_full_audio_does_not_assign_overlapping_or_uncovered_words():
    diarization = DiarizationResult(
        model="fixture",
        speaker_embeddings={},
        turns=[
            DiarizedTurn(start_seconds=0, end_seconds=2, speaker_label="HOST"),
            DiarizedTurn(start_seconds=1, end_seconds=3, speaker_label="VC"),
        ],
    )
    transcript = TranscriptResult(
        info=TranscriptInfo(method="speech_to_text_full_audio_words_v1"),
        segments=[
            TimedText(start_seconds=0, end_seconds=0.5, text="Host"),
            TimedText(start_seconds=1.2, end_seconds=1.5, text="overlap"),
            TimedText(start_seconds=2.1, end_seconds=2.4, text="My"),
            TimedText(start_seconds=2.4, end_seconds=2.9, text="thesis"),
            TimedText(start_seconds=4, end_seconds=5, text="uncovered"),
        ],
    )
    result = assemble_target_speech(
        diarization=diarization,
        transcript=transcript,
        attribution=SpeakerAttribution(
            status=SpeakerStatus.ACCEPTED_MODEL, speaker_label="VC"
        ),
    )
    assert [s.text for s in result.target_segments] == ["My thesis"]
    assert (
        next(s for s in result.aligned_segments if s.text == "overlap").speaker_label
        is None
    )


def test_cache_strategies_are_separate():
    inputs = {
        "artifact_sha256": "a" * 64,
        "provider": "local-whisper",
        "model": "turbo",
        "model_version": "unspecified",
        "diarization_cache_key": "key",
    }
    assert transcript_cache_key(
        **inputs, segmentation_version="diarized-turns-v1"
    ) != transcript_cache_key(**inputs, segmentation_version="full-audio-words-v1")


def test_word_timestamp_can_include_silence_before_single_speaker():
    from vc_trace_collector.av import align_words_to_speakers

    aligned = align_words_to_speakers(
        [TimedText(start_seconds=0, end_seconds=1, text="Hello")],
        [DiarizedTurn(start_seconds=0.8, end_seconds=1, speaker_label="VC")],
    )
    assert aligned[0].speaker_label == "VC"


def test_cache_default_follows_selected_strategy(monkeypatch):
    inputs = {
        "artifact_sha256": "a" * 64,
        "provider": "local-whisper",
        "model": "turbo",
        "model_version": "unspecified",
        "diarization_cache_key": "key",
    }
    monkeypatch.setenv("VC_TRACE_TRANSCRIPTION_STRATEGY", "diarized-turns-v1")
    old = transcript_cache_key(**inputs)
    monkeypatch.setenv("VC_TRACE_TRANSCRIPTION_STRATEGY", "full-audio-words-v1")
    assert old != transcript_cache_key(**inputs)


def test_unsupported_provider_fails_explicitly(monkeypatch):
    monkeypatch.setenv("VC_TRACE_TRANSCRIPTION_STRATEGY", "full-audio-words-v1")
    with pytest.raises(ValueError, match="word-timestamp provider"):
        transcribe_diarized_turns(
            Path("audio.wav"),
            DiarizationResult(model="fixture", turns=[], speaker_embeddings={}),
            object(),
        )


def test_whisper_requests_words_without_loading_model():
    from vc_trace_collector.av import WhisperTranscriptProvider

    class Model:
        def transcribe(self, path, **kwargs):
            assert kwargs["word_timestamps"] is True
            return {
                "language": "en",
                "segments": [
                    {
                        "text": "Hello",
                        "words": [{"start": 0, "end": 0.5, "word": " Hello"}],
                    }
                ],
            }

    provider = WhisperTranscriptProvider.__new__(WhisperTranscriptProvider)
    provider.model_name = "fake"
    provider._model = Model()
    result = provider.transcribe_words(Path("audio.wav"))
    assert result.segments[0].text == "Hello"
    assert result.info.method == "speech_to_text_full_audio_words_v1"


def test_full_strategy_does_not_migrate_phrase_transcripts(monkeypatch, tmp_path):
    from vc_trace_collector.pipeline import Pipeline

    monkeypatch.setenv("VC_TRACE_TRANSCRIPTION_STRATEGY", "full-audio-words-v1")
    assert (
        Pipeline._legacy_transcript(
            tmp_path,
            candidate_id="c",
            artifact_id="a",
            provider_name="whisper",
            model_name="turbo",
        )
        is None
    )

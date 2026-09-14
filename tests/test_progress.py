import warnings
from io import StringIO

import pytest
from rich.console import Console

from vc_trace_collector import progress as ui


def test_only_known_torchaudio_notices_are_hidden():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        with ui.quiet_torchaudio_notices():
            warnings.warn_explicit(
                "torchaudio._backend.utils.info has been deprecated. Migration notice",
                UserWarning, "io.py", 85, module="pyannote.audio.core.io",
            )
            warnings.warn_explicit(
                "In 2.9, this function's implementation will be changed to use torchaudio.load_with_torchcodec",
                UserWarning, "utils.py", 213, module="torchaudio._backend.utils",
            )
            warnings.warn_explicit(
                "Audio decoding failed", UserWarning, "io.py", 1,
                module="pyannote.audio.core.io",
            )
        warnings.warn("Filter restored", UserWarning)
    assert [str(item.message) for item in seen] == ["Audio decoding failed", "Filter restored"]


def test_terminal_progress_handles_stage_totals_and_restores_context(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(ui, "Console", lambda **kw: Console(
        file=output, force_terminal=True, width=100))

    @ui.with_processing_progress
    def run():
        ui.recording("Example [title]")
        ui.diarization_hook("embeddings", total=10, completed=4)
        progress, task = ui._active.get()
        assert progress.tasks[task].completed == 4
        ui.report("Loading Whisper")
        assert progress.tasks[task].total is None
        ui.report("Whisper", completed=1, total=2)
        raise ValueError("fixture failure")

    with pytest.raises(ValueError, match="fixture failure"):
        run()
    assert ui._active.get() is None
    assert "Example [title]" in output.getvalue()


def test_redirected_output_has_no_progress(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(ui, "Console", lambda **kw: Console(file=output))

    @ui.with_processing_progress
    def run():
        ui.report("Loading model")
        return 42

    assert run() == 42
    assert output.getvalue() == ""

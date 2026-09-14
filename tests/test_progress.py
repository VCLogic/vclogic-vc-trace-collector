from io import StringIO

import pytest
from rich.console import Console

from vc_trace_collector import progress as ui


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

import subprocess
import sys
from io import StringIO

import pytest
from rich.console import Console

from vc_trace_collector import progress as ui


def test_download_display_resets_and_keeps_literal_titles(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(ui, 'Console', lambda **kw: Console(file=output, force_terminal=True))

    @ui.with_download_progress
    def run():
        ui.download_batch(1, 3, failed=1, skipped=0)
        ui.download_item('Article [literal]')
        ui.download_bytes(50, 100)
        progress, batch, item = ui._downloads.get()
        assert progress.tasks[batch].completed == 1
        assert progress.tasks[item].completed == 50
        ui.download_item('Next item')
        assert progress.tasks[item].total is None
        assert progress.tasks[item].completed == 0
        raise ValueError('fixture')

    with pytest.raises(ValueError):
        run()
    assert ui._downloads.get() is None


def test_download_redirected_output_is_clean(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(ui, 'Console', lambda **kw: Console(file=output))

    @ui.with_download_progress
    def run():
        ui.download_bytes(12, None)
        return 42

    assert run() == 42
    assert not output.getvalue()


def test_download_runner_parses_progress_and_preserves_output(monkeypatch):
    events = []
    monkeypatch.setattr(ui, 'download_bytes', lambda done, total: events.append((done, total)))
    result = ui.run_download([sys.executable, '-c',
        'import sys; print("VC_TRACE_PROGRESS:50:100", file=sys.stderr); print("audio.webm")'],
        check=True, capture_output=True, text=True, timeout=5)
    assert result.stdout.strip() == 'audio.webm'
    assert events == [(50, 100)]
    with pytest.raises(subprocess.CalledProcessError):
        ui.run_download([sys.executable, '-c', 'raise SystemExit(2)'],
                        check=True, capture_output=True, text=True, timeout=5)
    with pytest.raises(subprocess.TimeoutExpired):
        ui.run_download([sys.executable, '-c', 'import time; time.sleep(10)'],
                        check=True, capture_output=True, text=True, timeout=0.05)

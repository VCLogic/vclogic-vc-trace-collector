"""Terminal-only processing feedback; never persisted in corpus records."""

import subprocess
import tempfile
import time
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

_active: ContextVar = ContextVar("processing_progress", default=None)
_downloads: ContextVar = ContextVar("download_progress", default=None)


def download_batch(completed, total, *, failed=0, skipped=0):
    active = _downloads.get()
    if active:
        progress, batch, _ = active
        progress.update(
            batch,
            completed=completed,
            total=total,
            description=Text(
                f"Sources {completed}/{total} | failed {failed} | skipped {skipped}"
            ),
        )


def download_item(title):
    active = _downloads.get()
    if active:
        progress, _, item = active
        progress.reset(item, total=None, description=Text(title))
        progress.tasks[item].total = None


def download_bytes(completed, total):
    active = _downloads.get()
    if active:
        progress, _, item = active
        progress.update(item, completed=completed, total=total)
        if total is None:
            progress.tasks[item].total = None


def with_download_progress(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        console = Console(stderr=True)
        if not console.is_terminal or _downloads.get() is not None:
            return function(*args, **kwargs)
        # Only the transfer row uses byte units.
        from rich.console import Group
        from rich.live import Live

        batch_progress = Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn()
        )
        transfer = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )
        batch = batch_progress.add_task("Preparing downloads", total=None)
        item = transfer.add_task("Waiting", total=None)

        # A small adapter keeps both independent rows behind the same update API.
        class Rows:
            tasks = (batch_progress.tasks[batch], transfer.tasks[item])

            def update(self, task, **kw):
                (batch_progress if task == 0 else transfer).update(0, **kw)

            def reset(self, task, **kw):
                (batch_progress if task == 0 else transfer).reset(0, **kw)

        with Live(Group(batch_progress, transfer), console=console):
            token = _downloads.set((Rows(), 0, 1))
            try:
                return function(*args, **kwargs)
            finally:
                _downloads.reset(token)

    return wrapped


def run_download(command, *, check, capture_output, text, timeout):
    """Capture yt-dlp output while consuming only our numeric progress protocol."""
    started = time.monotonic()
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout,
        tempfile.TemporaryFile(mode="w+b") as stderr,
        subprocess.Popen(command, stdout=stdout, stderr=stderr) as process,
    ):
        offset = 0
        pending = b""

        def read_progress():
            nonlocal offset, pending
            # pread avoids changing the file offset shared with the child.
            import os

            chunk = os.pread(stderr.fileno(), 65536, offset)
            while chunk:
                offset += len(chunk)
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith(b"VC_TRACE_PROGRESS:"):
                        parts = line.split(b":")
                        try:
                            done = int(float(parts[1]))
                            total = (
                                int(float(parts[2]))
                                if parts[2] not in (b"NA", b"None")
                                else None
                            )
                            download_bytes(done, total)
                        except (ValueError, IndexError):
                            pass
                chunk = os.pread(stderr.fileno(), 65536, offset)

        try:
            while process.poll() is None:
                read_progress()
                if time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.1)
            read_progress()
        except BaseException:
            process.kill()
            process.wait()
            raise
        stdout.seek(0)
        stderr.seek(0)
        result = subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )
        if check:
            result.check_returncode()
        return result


@contextmanager
def quiet_torchaudio_notices():
    """Hide known migration notices only, preserving runtime diagnostics."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"torchaudio\._backend\.[\w.]+ has been deprecated\.",
            category=UserWarning,
            module=r"(?:pyannote|torchaudio|speechbrain)(?:\.|$)",
        )
        warnings.filterwarnings(
            "ignore",
            message=r"In 2\.9, this function's implementation will be changed to use torchaudio\.load_with_torchcodec",
            category=UserWarning,
            module=r"torchaudio(?:\.|$)",
        )
        yield


def report(stage: str, *, completed: int = 0, total: int | None = None) -> None:
    active = _active.get()
    if active is not None:
        progress, task = active
        progress.update(
            task,
            description=Text(stage),
            completed=completed,
            total=total,
            counts=f"{completed}/{total}" if total is not None else "",
        )
        # Rich retains an old total when passed None: explicitly reset for
        # indeterminate stages such as model loading and audio extraction.
        if total is None:
            progress.tasks[task].total = None


def recording(title: str) -> None:
    active = _active.get()
    if active is not None:
        active[0].console.print(Text(f"Processing: {title}"))


def diarization_hook(
    step_name, _artifact=None, *, total=None, completed=None, **kwargs
):
    report(f"Pyannote: {step_name}", completed=completed or 0, total=total)


def with_processing_progress(function):
    @wraps(function)
    @quiet_torchaudio_notices()
    def wrapped(*args, **kwargs):
        console = Console(stderr=True)
        if not console.is_terminal:
            return function(*args, **kwargs)
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.fields[counts]}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Preparing processing", total=None, counts="")
            token = _active.set((progress, task))
            try:
                return function(*args, **kwargs)
            finally:
                _active.reset(token)

    return wrapped

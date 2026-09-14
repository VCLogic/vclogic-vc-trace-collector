"""Terminal-only processing feedback; never persisted in corpus records."""

from contextvars import ContextVar
from functools import wraps

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

_active: ContextVar = ContextVar("processing_progress", default=None)


def report(stage: str, *, completed: int = 0, total: int | None = None) -> None:
    active = _active.get()
    if active is not None:
        progress, task = active
        progress.update(task, description=Text(stage), completed=completed, total=total,
                        counts=f"{completed}/{total}" if total is not None else "")
        # Rich retains an old total when passed None: explicitly reset for
        # indeterminate stages such as model loading and audio extraction.
        if total is None:
            progress.tasks[task].total = None


def recording(title: str) -> None:
    active = _active.get()
    if active is not None:
        active[0].console.print(Text(f"Processing: {title}"))


def diarization_hook(step_name, _artifact=None, *, total=None, completed=None, **kwargs):
    report(f"Pyannote: {step_name}", completed=completed or 0, total=total)


def with_processing_progress(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        console = Console(stderr=True)
        if not console.is_terminal:
            return function(*args, **kwargs)
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
            TextColumn("{task.fields[counts]}"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("Preparing processing", total=None, counts="")
            token = _active.set((progress, task))
            try:
                return function(*args, **kwargs)
            finally:
                _active.reset(token)
    return wrapped

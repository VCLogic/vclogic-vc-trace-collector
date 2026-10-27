"""Detached, resumable-by-selection dashboard jobs (Linux process identity)."""

from __future__ import annotations

import ast
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .audit import BudgetExceeded, redact
from .download_backoff import DownloadBackoff

ACTIVE = {"starting", "running", "pause_requested"}
OPTIONS = {
    "download": set(),
    "process": {"transcription_model", "diarization_model"},
    "reference": {
        "confirmed",
        "reviewer",
        "start_seconds",
        "end_seconds",
        "diarization_model",
        "embedding_model",
    },
    "search": {
        "platforms",
        "backend",
        "maximum_queries",
        "maximum_search_operations",
        "limit_per_query",
        "queries",
    },
}


def _now():
    return datetime.now(UTC).isoformat()


def _identity(pid):
    try:
        parts = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return parts[19] if parts[0] != "Z" else None
    except (OSError, ValueError, IndexError):
        return None


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".job-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _job_path(output_dir, job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job ID")
    return Path(output_dir).resolve() / ".dashboard-jobs" / f"{job_id}.json"


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another job holds this workspace or GPU") from None
        yield


def external_jobs(output_dir):
    root = Path(output_dir).resolve()
    runner = root / "remaining-youtube-downloads"
    try:
        pid = json.loads((runner / "pid.json").read_text())["pid"]
        state = json.loads((runner / "status.json").read_text())
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        cwd = Path(f"/proc/{pid}/cwd").resolve()
        script = runner / "run.py"
        if not _identity(pid) or cwd != root.parent or str(script).encode() not in args:
            return []
        reserved = [state.get("investor", "")]
        for statement in ast.parse(script.read_text()).body:
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SLUGS"
                for target in statement.targets
            ):
                values = ast.literal_eval(statement.value)
                if isinstance(values, list) and all(
                    isinstance(value, str) for value in values
                ):
                    current = state.get("investor", "")
                    remaining = (
                        values[values.index(current) :] if current in values else values
                    )
                    reserved = list(dict.fromkeys(reserved + remaining))
        return [
            {
                "id": f"external-{pid}",
                "slug": state.get("investor", ""),
                "reserved_slugs": reserved,
                "stage": "download",
                "status": "running",
                "pid": pid,
                "external": True,
                "total": state.get("total", 0),
                "completed": state.get("offset", 0),
                "failed": 0,
                "detail": "Existing download runner",
                "progress_completed": 0,
                "progress_total": None,
                "error": None,
            }
        ]
    except (OSError, ValueError, KeyError, SyntaxError):
        return []


def _uses_workspace(row, slug):
    return row.get("slug") == slug or slug in row.get("reserved_slugs", [])


def list_jobs(output_dir):
    rows = []
    for path in sorted((Path(output_dir).resolve() / ".dashboard-jobs").glob("*.json")):
        try:
            row = json.loads(path.read_text())
            if (
                row["status"] in ACTIVE
                and not row.get("pid")
                and (
                    datetime.now(UTC) - datetime.fromisoformat(row["created_at"])
                ).total_seconds()
                > 30
            ):
                row = dict(row, status="interrupted", error="Worker startup expired")
            if row["status"] in ACTIVE and row.get("pid"):
                identity = _identity(row["pid"])
                if identity is None or identity != row.get("start_identity"):
                    row = dict(
                        row,
                        status="interrupted",
                        error="Worker process is no longer running",
                    )
            if row["status"] in ACTIVE and path.with_suffix(".pause").exists():
                row = dict(row, status="pause_requested")
            rows.append(row)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(
        rows + external_jobs(output_dir),
        key=lambda row: row.get("created_at", ""),
        reverse=True,
    )


@contextmanager
def workspace_guard(output_dir, slug):
    """Serialize direct dashboard mutations with workers and known external work."""
    root = Path(output_dir).resolve()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Invalid investor slug")
    if (root / slug).resolve().parent != root:
        raise ValueError("Investor workspace escapes output directory")
    with _lock(root / ".dashboard-jobs" / f"{slug}.lock"):
        if any(
            _uses_workspace(item, slug) and item["status"] in ACTIVE
            for item in list_jobs(root)
        ):
            raise RuntimeError("An active job uses this investor workspace")
        yield


def launch_job(
    output_dir: Path, slug: str, stage: str, candidate_ids: list[str], **options
) -> dict:
    root = Path(output_dir).resolve()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Invalid investor slug")
    workspace = (root / slug).resolve()
    if workspace.parent != root or not workspace.is_dir():
        raise ValueError(
            "Investor workspace does not exist or escapes output directory"
        )
    if stage not in OPTIONS or set(options) - OPTIONS[stage]:
        raise ValueError("Unsupported stage or job options")
    if stage == "reference" and (
        options.get("confirmed") is not True or not options.get("reviewer")
    ):
        raise ValueError("Reference approval needs explicit confirmation and reviewer")
    from .models import SourceType

    items = list(dict.fromkeys(candidate_ids))
    if stage == "search":
        items = [SourceType(value).value for value in options.get("platforms", [])]
    if not items or not all(isinstance(item, str) and item for item in items):
        raise ValueError("Select at least one item")
    json.dumps(options)
    with _lock(root / ".dashboard-jobs" / "launch.lock"):
        if any(
            row["status"] in ACTIVE and _uses_workspace(row, slug)
            for row in list_jobs(root)
        ):
            raise RuntimeError("An active job already uses this investor workspace")
        job_id = uuid4().hex
        row = {
            "id": job_id,
            "slug": slug,
            "stage": stage,
            "status": "starting",
            "total": len(items),
            "completed": 0,
            "failed": 0,
            "detail": "Starting worker",
            "progress_completed": 0,
            "progress_total": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
            "candidate_ids": items,
            "options": options,
            "outcomes": [],
            "pid": None,
            "start_identity": None,
        }
        path = _job_path(root, job_id)
        _write(path, row)
        with path.with_suffix(".log").open("ab") as log:
            try:
                child = subprocess.Popen(
                    [sys.executable, "-m", __name__, str(root), job_id],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    cwd=Path.cwd(),
                )
                row.update(pid=child.pid, start_identity=_identity(child.pid))
                # Worker waits on the launch lock before reading its request.
                _write(path, row)
            except Exception:
                row.update(status="failed", error="Could not start worker")
                _write(path, row)
                raise
        return row


def request_pause(output_dir, job_id):
    path = _job_path(output_dir, job_id)
    if not path.exists():
        raise KeyError(job_id)
    _write(path.with_suffix(".pause"), {"requested_at": _now()})


def resume_job(output_dir, job_id):
    """Start one new attempt for unfinished selections, retaining saved options."""
    _job_path(output_dir, job_id)
    row = next((item for item in list_jobs(output_dir) if item["id"] == job_id), None)
    if row is None:
        raise KeyError(job_id)
    if row["status"] in ACTIVE or row.get("external"):
        raise ValueError("Only stopped managed jobs can be resumed")
    if row["stage"] == "reference" and row["status"] == "completed":
        raise ValueError("Completed reference approvals cannot be repeated")
    done = {
        item["candidate_id"]
        for item in row.get("outcomes", [])
        if item["status"] in {"succeeded", "excluded", "review_required", "no_content"}
    }
    remaining = [cid for cid in row["candidate_ids"] if cid not in done]
    if not remaining:
        raise ValueError("No failed or remaining selections to resume")
    options = dict(row["options"])
    if row["stage"] == "search":
        options["platforms"] = remaining
    return launch_job(output_dir, row["slug"], row["stage"], remaining, **options)


def _make_pipeline(root):
    from dotenv import load_dotenv

    from .pipeline import Pipeline
    from .public_search import default_public_search_provider

    load_dotenv(Path.cwd() / ".env", override=False)
    return Pipeline(
        root,
        search_provider=default_public_search_provider(
            searxng_endpoint=os.environ.get("VC_TRACE_SEARCH_ENDPOINT", "").strip()
            or None
        ),
    )


def _validate_reference(pipeline, workspace, candidate_id, options):
    from .process import load_artifact_records
    from .storage import read_jsonl

    start, end = options.get("start_seconds"), options.get("end_seconds")
    if (
        not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end)
    ):
        raise ValueError("Invalid reference interval: require finite 0 <= start < end")
    voice = next(
        row
        for row in read_jsonl(workspace / "identity/reference_voice_candidates.jsonl")
        if row["candidate_id"] == candidate_id
    )
    records = {item.artifact_id: item for item in load_artifact_records(workspace)}
    original = records[voice["artifact_id"]]
    visited = set()
    while (
        original.collection_method == "human_selected_reference_segment"
        and original.parent_artifact_ids
    ):
        if original.artifact_id in visited:
            raise ValueError("Reference artifact lineage cycle")
        visited.add(original.artifact_id)
        original = records[original.parent_artifact_ids[0]]
    media = (workspace / original.relative_path).resolve()
    if not media.is_relative_to(workspace.resolve()) or not media.is_file():
        raise ValueError("Reference source is not a local workspace artifact")
    duration = pipeline.media_probe(media)
    if (
        duration is None
        or not math.isfinite(duration)
        or duration <= 0
        or end > duration
    ):
        raise ValueError(
            "Reference interval exceeds recording duration or duration is unknown"
        )


def run_job(output_dir, job_id):
    root = Path(output_dir).resolve()
    path = _job_path(root, job_id)
    # The parent publishes the PID while holding this lock.
    with (path.parent / "launch.lock").open("a") as launch:
        fcntl.flock(launch, fcntl.LOCK_EX)
    row = json.loads(path.read_text())
    pause = path.with_suffix(".pause")

    def save(**fields):
        row.update(fields, updated_at=_now())
        _write(path, row)

    try:
        with ExitStack() as stack:
            stack.enter_context(_lock(root / ".dashboard-jobs" / f"{row['slug']}.lock"))
            if row["stage"] in {"process", "reference"}:
                stack.enter_context(
                    _lock(
                        Path(tempfile.gettempdir()) / f"vc-trace-gpu-{os.getuid()}.lock"
                    )
                )
            if any(_uses_workspace(item, row["slug"]) for item in external_jobs(root)):
                raise RuntimeError(
                    "Existing download runner uses this investor workspace"
                )
            save(
                status="running", pid=os.getpid(), start_identity=_identity(os.getpid())
            )
            pipeline = _make_pipeline(root)
            backoff = DownloadBackoff()
            options = dict(row["options"])
            options.pop("confirmed", None)
            options.pop("platforms", None)
            backend = options.pop("backend", "default")
            if backend not in {"default", "agent-reach"}:
                raise ValueError("Unsupported search backend")
            if row["stage"] == "search" and backend == "agent-reach":
                from .public_search import agent_reach_public_search_provider

                options["search_provider"] = agent_reach_public_search_provider()
            from .progress import observe_progress

            stack.enter_context(
                observe_progress(
                    lambda stage, completed=0, total=None: save(
                        detail=redact(stage),
                        progress_completed=completed,
                        progress_total=total,
                    )
                )
            )
            for candidate_id in row["candidate_ids"]:
                if pause.exists():
                    save(status="paused", detail="Paused between items")
                    return
                if (
                    row["stage"] == "download"
                    and shutil.disk_usage(root).free < 46 * 1024**3
                ):
                    save(
                        status="paused",
                        detail="Paused: less than 46 GiB free disk space",
                    )
                    return
                save(detail=candidate_id, progress_completed=0, progress_total=None)
                outcome = {
                    "candidate_id": candidate_id,
                    "started_at": _now(),
                    "status": "succeeded",
                }
                reason = None
                try:
                    if row["stage"] == "download":
                        result = pipeline.fetch_source(
                            row["slug"], candidate_ids={candidate_id}
                        )
                        if result.failed:
                            raise RuntimeError(
                                "; ".join(
                                    str(item.reason)
                                    for item in result.outcomes
                                    if item.status == "failed"
                                )
                            )
                    elif row["stage"] == "process":
                        documents = pipeline.process(
                            row["slug"],
                            candidate_ids={candidate_id},
                            allow_unknown_role=True,
                            **options,
                        )
                        from .storage import read_jsonl

                        recorded = next(
                            (
                                item
                                for item in read_jsonl(
                                    root
                                    / row["slug"]
                                    / "processed/av_candidate_outcomes.jsonl"
                                )
                                if item.get("candidate_id") == candidate_id
                            ),
                            None,
                        )
                        if recorded:
                            outcome.update(
                                status=recorded["status"],
                                reason=redact(recorded.get("reason", "")),
                            )
                        elif isinstance(documents, list) and not any(
                            getattr(doc, "source_candidate_id", None) == candidate_id
                            for doc in documents
                        ):
                            outcome.update(
                                status="no_content",
                                reason="No selected content produced",
                            )
                    elif row["stage"] == "reference":
                        _validate_reference(
                            pipeline, root / row["slug"], candidate_id, options
                        )
                        pipeline.approve_reference_voice(
                            row["slug"], candidate_id=candidate_id, **options
                        )
                    else:
                        from .models import SourceType

                        result = pipeline.search_source(
                            row["slug"], SourceType(candidate_id), **options
                        )
                        outcome.update(
                            failed_queries=result.failed,
                            stopped_early=result.stopped_early,
                            result_count=result.result_count,
                        )
                        if result.failed or result.stopped_early:
                            outcome.update(
                                status="stopped_early"
                                if result.stopped_early
                                else "failed",
                                reason=redact("; ".join(result.errors)),
                            )
                except BudgetExceeded:
                    raise
                except Exception as error:
                    reason = redact(str(error))
                    outcome.update(status="failed", error=reason)
                if outcome["status"] in {"failed", "not_collected", "stopped_early"}:
                    row["failed"] += 1
                outcome["finished_at"] = _now()
                row["outcomes"].append(outcome)
                save(completed=row["completed"] + 1)
                if row["stage"] == "download":
                    delay = backoff.record(reason)
                    if delay is None:
                        save(
                            status="paused",
                            detail="Paused after repeated download failures",
                        )
                        return
                    deadline = time.monotonic() + max(30, delay)
                    if row["completed"] < row["total"]:
                        save(detail=f"Pacing downloads ({max(30, delay)} seconds)")
                        while time.monotonic() < deadline and not pause.exists():
                            time.sleep(min(1, max(0, deadline - time.monotonic())))
            save(
                status="completed_with_errors"
                if row["failed"]
                else (
                    "review_required"
                    if any(
                        item["status"] == "review_required" for item in row["outcomes"]
                    )
                    else "completed"
                ),
                detail="Finished",
                finished_at=_now(),
            )
    except Exception as error:
        save(status="failed", error=redact(str(error)), finished_at=_now())


if __name__ == "__main__":
    run_job(Path(sys.argv[1]), sys.argv[2])

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from vc_trace_collector import dashboard_jobs as jobs


def test_expired_start_and_resume_filter(tmp_path, monkeypatch):
    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(
        tmp_path, "investor", "process", ["ok", "review", "bad", "pending"]
    )
    row.update(
        pid=None,
        created_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        outcomes=[
            {"candidate_id": "ok", "status": "succeeded"},
            {"candidate_id": "review", "status": "review_required"},
            {"candidate_id": "bad", "status": "failed"},
        ],
    )
    jobs._write(jobs._job_path(tmp_path, row["id"]), row)
    assert jobs.list_jobs(tmp_path)[0]["status"] == "interrupted"
    resumed = jobs.resume_job(tmp_path, row["id"])
    assert resumed["candidate_ids"] == ["bad", "pending"]


def test_download_low_disk_pauses_before_fetch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "download", ["one"])
    pipeline = Mock()
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: pipeline)
    monkeypatch.setattr(jobs.shutil, "disk_usage", lambda root: SimpleNamespace(free=1))
    jobs.run_job(tmp_path, row["id"])
    assert jobs.list_jobs(tmp_path)[0]["status"] == "paused"
    pipeline.fetch_source.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "review_required", "not_collected"])
def test_process_actual_outcome_used(tmp_path, monkeypatch, status):
    workspace = tmp_path / "investor"
    workspace.mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "process", ["one"])
    pipeline = Mock()
    pipeline.process.return_value = []
    jobs._write(
        workspace / "processed/av_candidate_outcomes.jsonl",
        {"candidate_id": "one", "status": status, "reason": "detail"},
    )
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: pipeline)
    jobs.run_job(tmp_path, row["id"])
    assert jobs.list_jobs(tmp_path)[0]["outcomes"][0]["status"] == status


@pytest.mark.parametrize(
    "failed,stopped,status", [(1, False, "failed"), (0, True, "stopped_early")]
)
def test_search_partial_failure_is_not_success(
    tmp_path, monkeypatch, failed, stopped, status
):
    from types import SimpleNamespace

    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "search", [], platforms=["youtube"])
    pipeline = Mock()
    pipeline.search_source.return_value = SimpleNamespace(
        failed=failed, stopped_early=stopped, result_count=2, errors=["budget"]
    )
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: pipeline)
    jobs.run_job(tmp_path, row["id"])
    result = jobs.list_jobs(tmp_path)[0]
    assert result["status"] == "completed_with_errors"
    assert result["outcomes"][0]["status"] == status
    resumed = jobs.resume_job(tmp_path, row["id"])
    assert resumed["options"]["platforms"] == ["youtube"]


def test_rejects_escape_and_unconfirmed_reference(tmp_path):
    with pytest.raises(ValueError):
        jobs.launch_job(tmp_path, "../escape", "download", ["one"])
    with pytest.raises(ValueError):
        jobs.launch_job(tmp_path, "investor", "reference", ["one"])


def test_worker_persists_failure_and_pause(tmp_path, monkeypatch):
    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "process", ["one", "two"])
    pipeline = Mock()
    pipeline.process.side_effect = lambda *a, **kw: jobs.request_pause(
        tmp_path, row["id"]
    )
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: pipeline)
    jobs.run_job(tmp_path, row["id"])
    result = jobs.list_jobs(tmp_path)[0]
    assert result["status"] == "paused"
    assert result["completed"] == 1
    assert result["outcomes"][0]["candidate_id"] == "one"
    pipeline.process.assert_called_once_with(
        "investor", candidate_ids={"one"}, allow_unknown_role=True
    )


def test_stale_pid_is_interrupted(tmp_path, monkeypatch):
    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "process", ["one"])
    assert jobs.list_jobs(tmp_path)[0]["status"] == "interrupted"
    assert row["id"]


@pytest.mark.parametrize("gpu", [False, True])
def test_lock_prevents_conflicting_worker(tmp_path, monkeypatch, gpu):
    import tempfile
    from pathlib import Path

    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "process", ["one"])
    factory = Mock()
    monkeypatch.setattr(jobs, "_make_pipeline", factory)
    lock = (
        Path(tempfile.gettempdir()) / f"vc-trace-gpu-{os.getuid()}.lock"
        if gpu
        else tmp_path / ".dashboard-jobs" / "investor.lock"
    )
    with jobs._lock(lock):
        jobs.run_job(tmp_path, row["id"])
    assert jobs.list_jobs(tmp_path)[0]["status"] == "failed"
    factory.assert_not_called()


def test_progress_and_item_failure_are_persisted(tmp_path, monkeypatch):
    from vc_trace_collector.progress import report

    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(tmp_path, "investor", "process", ["one"])
    pipeline = Mock()

    def process(*args, **kwargs):
        report("Transcribing", completed=4, total=10)
        raise RuntimeError("model unavailable")

    pipeline.process.side_effect = process
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: pipeline)
    jobs.run_job(tmp_path, row["id"])
    result = jobs.list_jobs(tmp_path)[0]
    assert result["status"] == "completed_with_errors"
    assert result["failed"] == 1
    assert result["progress_completed"] == 4
    assert result["outcomes"][0]["error"] == "model unavailable"


def test_external_runner_blocks_launch(tmp_path, monkeypatch):
    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(
        jobs, "external_jobs", lambda root: [{"slug": "investor", "status": "running"}]
    )
    child = Mock()
    monkeypatch.setattr(jobs.subprocess, "Popen", child)
    with pytest.raises(RuntimeError, match="active job"):
        jobs.launch_job(tmp_path, "investor", "download", ["one"])
    child.assert_not_called()


def test_pause_requested_visible_for_live_worker(tmp_path, monkeypatch):
    (tmp_path / "investor").mkdir()
    monkeypatch.setattr(
        jobs.subprocess, "Popen", Mock(return_value=Mock(pid=os.getpid()))
    )
    row = jobs.launch_job(tmp_path, "investor", "process", ["one"])
    jobs.request_pause(tmp_path, row["id"])
    assert jobs.list_jobs(tmp_path)[0]["status"] == "pause_requested"


def test_queued_external_workspace_reserved(tmp_path, monkeypatch):
    (tmp_path / "queued").mkdir()
    monkeypatch.setattr(
        jobs,
        "external_jobs",
        lambda root: [
            {
                "slug": "current",
                "status": "running",
                "reserved_slugs": ["current", "queued"],
            }
        ],
    )
    with pytest.raises(RuntimeError):
        jobs.launch_job(tmp_path, "queued", "download", ["one"])
    with pytest.raises(RuntimeError), jobs.workspace_guard(tmp_path, "queued"):
        pass


@pytest.mark.parametrize("start,end", [(-1, 3), (3, 3), (float("nan"), 3), (0, 11)])
def test_reference_rejects_invalid_interval_before_model(
    tmp_path, monkeypatch, start, end
):
    from types import SimpleNamespace

    from vc_trace_collector import process

    workspace = tmp_path / "investor"
    (workspace / "identity").mkdir(parents=True)
    jobs._write(
        workspace / "identity/reference_voice_candidates.jsonl",
        {"candidate_id": "voice:one", "artifact_id": "artifact"},
    )
    (workspace / "media.wav").touch()
    monkeypatch.setattr(
        process,
        "load_artifact_records",
        lambda root: [
            SimpleNamespace(
                artifact_id="artifact",
                collection_method="download",
                relative_path="media.wav",
            )
        ],
    )
    monkeypatch.setattr(jobs.subprocess, "Popen", Mock(return_value=Mock(pid=99999999)))
    row = jobs.launch_job(
        tmp_path,
        "investor",
        "reference",
        ["voice:one"],
        confirmed=True,
        reviewer="human",
        start_seconds=start,
        end_seconds=end,
    )
    pipeline = Mock()
    pipeline.media_probe.return_value = 10
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: pipeline)
    jobs.run_job(tmp_path, row["id"])
    pipeline.approve_reference_voice.assert_not_called()
    assert jobs.list_jobs(tmp_path)[0]["failed"] == 1

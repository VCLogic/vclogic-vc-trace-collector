"""Offline controller-to-worker workflow with real persisted pipeline records."""

from types import SimpleNamespace
from unittest.mock import Mock

from test_pipeline_cli import FixtureDiarization, FixtureTranscript, pipeline
from test_wizard import Prompts, prepared

from vc_trace_collector import dashboard_jobs as jobs
from vc_trace_collector.dashboard import Dashboard
from vc_trace_collector.dashboard_state import summarize
from vc_trace_collector.discovery import SearchResult
from vc_trace_collector.models import SourceDecision
from vc_trace_collector.storage import read_jsonl, write_json
from vc_trace_collector.wizard_models import source_items


class OfflinePrompts(Prompts):
    def __init__(self, **answers):
        super().__init__(**answers)
        self.watched = []

    def busy(self, message, function):
        return function()

    def watch(self, output_dir, job_id):
        self.watched.append(job_id)


class PodcastSearch:
    provider_name = "offline-fixture"
    cache_identity = "offline-fixture-v1"

    def search(self, query, limit=10):
        return [
            SearchResult(
                title="Michael Hyatt BlueCat interview podcast",
                url="https://open.spotify.com/episode/offline-fixture",
                snippet="Investor Michael Hyatt, co-founder of BlueCat, discusses investing.",
                rank=1,
                query=query,
                provider=self.provider_name,
            )
        ]


def test_offline_new_investor_through_processing_and_reopened_jobs(
    tmp_path, monkeypatch
):
    collector = pipeline(tmp_path / "outputs")
    collector.search_provider = PodcastSearch()
    collector.diarization_provider = FixtureDiarization()
    collector.transcript_provider = FixtureTranscript()
    collector.media_probe = lambda path: 5.0

    def extract(source, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"offline extracted audio")
        return destination

    collector.audio_extractor = extract
    monkeypatch.setattr("vc_trace_collector.dashboard.missing_av_dependencies", list)
    monkeypatch.setattr(jobs, "_make_pipeline", lambda root: collector)
    monkeypatch.setattr(
        jobs.subprocess, "Popen", Mock(return_value=SimpleNamespace(pid=99999999))
    )
    real_launch = jobs.launch_job

    def inline_launch(root, slug, stage, candidate_ids, **options):
        job = real_launch(root, slug, stage, candidate_ids, **options)
        jobs.run_job(root, job["id"])
        finished = next(row for row in jobs.list_jobs(root) if row["id"] == job["id"])
        assert finished["status"] == "completed", finished
        assert finished["failed"] == 0, finished
        return finished

    monkeypatch.setattr("vc_trace_collector.dashboard.launch_job", inline_launch)
    ui = OfflinePrompts(
        platforms=["podcast"],
        name="Michael Hyatt",
        firm="BlueCat",
        profile_url="https://www.thepitch.show/investors/michael-hyatt",
        confirm_profile=True,
        confirm_identity=True,
        confirm_search=True,
        backend="default",
        reviewer="human:offline",
        review_mode="all",
        confirm_bulk=True,
        selection="all",
        confirm_job=True,
        start="0:01",
        end="0:02",
        confirm_voice=True,
        advanced_models=True,
        transcription_model="fixture-transcript",
        diarization_model="fixture-diarization",
    )
    dashboard = Dashboard(collector, ui)
    dashboard.discover()
    slug = "michael-hyatt"
    discovered = source_items(collector, slug)
    podcast = next(
        item.candidate for item in discovered if item.candidate.source_type == "podcast"
    )
    assert not next(
        i for i in discovered if i.candidate.candidate_id == podcast.candidate_id
    ).downloaded
    dashboard.review(slug)
    assert (
        next(
            c
            for c in collector._load_plan(slug).candidates
            if c.candidate_id == podcast.candidate_id
        ).approval_status
        == "approved"
    )

    # Keep download selection explicit: identity/profile evidence is not this recording.
    ui.answers.update(selection="choose", download=[podcast.candidate_id])
    dashboard.download(slug)
    item = next(
        i
        for i in source_items(collector, slug)
        if i.candidate.candidate_id == podcast.candidate_id
    )
    assert item.downloaded and item.media
    assert not summarize(collector, slug)["reference"]
    ui.answers["reference"] = podcast.candidate_id
    dashboard.reference(slug)
    assert summarize(collector, slug)["reference"]
    profiles = read_jsonl(
        collector.workspace(slug) / "identity/reference_voice_profiles.jsonl"
    )
    assert profiles and all(p["status"] == "verified_human" for p in profiles)
    assert all(p["artifact_ids"] for p in profiles)

    ui.answers.update(process=[podcast.candidate_id])
    dashboard.process(slug)
    outcomes = read_jsonl(
        collector.workspace(slug) / "processed/av_candidate_outcomes.jsonl"
    )
    assert any(
        row["candidate_id"] == podcast.candidate_id
        and row["status"] in {"succeeded", "review_required"}
        for row in outcomes
    )
    saved_jobs = jobs.list_jobs(collector.output_dir)
    assert {j["stage"] for j in saved_jobs} == {
        "search",
        "download",
        "reference",
        "process",
    }
    assert len(ui.watched) == 4
    reopened_ui = OfflinePrompts(job=saved_jobs[0]["id"])
    Dashboard(collector, reopened_ui).jobs(slug)
    assert reopened_ui.watched == [saved_jobs[0]["id"]]
    assert summarize(collector, slug)["processed"] >= 1


def test_accept_all_pending_preserves_actual_rejection_and_confidence(tmp_path):
    collector, pending = prepared(tmp_path, approve=False)
    slug = "michael-hyatt"
    plan = collector._load_plan(slug)
    rejected = pending.model_copy(update={"candidate_id": "candidate:rejected-fixture"})
    plan.candidates.append(rejected)
    write_json(collector.workspace(slug) / "discovery/source_plan.json", plan)
    collector.review(
        slug,
        decisions=[
            SourceDecision(
                candidate_id=rejected.candidate_id,
                status="rejected",
                reason="Wrong person",
                decided_by="human:offline",
            )
        ],
        reviewer="human:offline",
        confirm_identity=True,
    )
    before = {
        c.candidate_id: c.model_dump(mode="json")
        for c in collector._load_plan(slug).candidates
    }
    Dashboard(collector, OfflinePrompts(review_mode="all", confirm_bulk=True)).review(
        slug
    )
    after = {
        c.candidate_id: c.model_dump(mode="json")
        for c in collector._load_plan(slug).candidates
    }
    assert after[rejected.candidate_id] == before[rejected.candidate_id]
    assert after[pending.candidate_id]["approval_status"] == "approved"
    for cid in before:
        assert after[cid]["identity_confidence"] == before[cid]["identity_confidence"]
        assert after[cid]["material_role"] == before[cid]["material_role"]

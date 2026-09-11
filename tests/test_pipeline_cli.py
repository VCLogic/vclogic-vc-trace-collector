import socket
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from vc_trace_collector.cli import create_app
from vc_trace_collector.collectors import ReviewRequired
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import ApprovalStatus, SourceDecision
from vc_trace_collector.pipeline import Pipeline
from vc_trace_collector.storage import read_json, read_jsonl

FIXTURES = Path(__file__).parent / "fixtures"


def public_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def pipeline(tmp_path: Path) -> Pipeline:
    html = (FIXTURES / "michael_hyatt_profile.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        content = html
        content_type = "text/html"
        if "cdn.example.test" in request.url.host:
            content = b"ID3 reference voice"
            content_type = "audio/mpeg"
        if "blog.example.test" in request.url.host:
            content = (
                b"<html><head><meta name='author' content='Michael Hyatt'></head>"
                b"<body><main><h1>Building BlueCat with durable economics</h1>"
                b"<p>By Michael Hyatt. I learned that capital must serve the business.</p>"
                b"</main></body></html>"
            )
        if "spotify.com" in request.url.host:
            content = (
                b"<html><head><title>Michael Hyatt BlueCat interview podcast</title></head>"
                b"<body>A conversation with investor Michael Hyatt, co-founder of BlueCat."
                b"<audio src='https://cdn.example.test/reference.mp3'></audio></body>"
                b"</html>"
            )
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": content_type, "etag": '"fixture"'},
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )
    return Pipeline(tmp_path, fetcher=fetcher)


def test_discover_writes_reviewable_plan_and_raw_identity_evidence(tmp_path) -> None:
    result = pipeline(tmp_path).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    workspace = tmp_path / "michael-hyatt"

    assert result.identity.canonical_name == "Michael Hyatt"
    assert (workspace / "identity/resolved_identity.json").exists()
    assert (workspace / "discovery/source_plan.json").exists()
    assert next((workspace / "raw/web").rglob("*.html")).exists()
    assert (
        read_json(workspace / "discovery/source_plan.json")["requires_review"] is True
    )


def test_collect_stops_at_identity_review_checkpoint(tmp_path) -> None:
    with pytest.raises(ReviewRequired):
        pipeline(tmp_path).collect(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        )


def test_cli_collect_uses_exit_code_three_for_review(tmp_path) -> None:
    runner = CliRunner()
    app = create_app(lambda output_dir: pipeline(Path(output_dir)))
    result = runner.invoke(
        app,
        [
            "collect",
            "--name",
            "Michael Hyatt",
            "--known-profile-url",
            "https://www.thepitch.show/investors/michael-hyatt",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 3
    assert "review required" in result.output.casefold()


def test_review_confirms_identity_without_approving_rejected_pitch_source(
    tmp_path,
) -> None:
    collector = pipeline(tmp_path)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    updated = collector.review(
        "michael-hyatt", decisions=[], reviewer="human@example.test"
    )

    assert updated.identity.resolution_status == "confirmed"
    assert updated.source_plan.candidates[0].approval_status == "rejected"


def test_status_reports_persisted_stage_state(tmp_path) -> None:
    collector = pipeline(tmp_path)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    status = collector.status("michael-hyatt")
    assert status["stages"]["discovery"] == "complete"
    assert status["identity_status"] == "provisional"


def test_cli_discover_accepts_additional_source_url(tmp_path) -> None:
    app = create_app(lambda output_dir: pipeline(Path(output_dir)))
    podcast_url = (
        "https://podcasters.spotify.com/pod/show/example/episodes/michael-hyatt"
    )

    result = CliRunner().invoke(
        app,
        [
            "discover",
            "--name",
            "Michael Hyatt",
            "--known-profile-url",
            "https://www.thepitch.show/investors/michael-hyatt",
            "--source-url",
            podcast_url,
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    rows = read_jsonl(tmp_path / "michael-hyatt/discovery/source_candidates.jsonl")
    assert any(row["url"] == podcast_url for row in rows)


def test_collected_voice_audio_is_linked_to_reference_candidate(tmp_path) -> None:
    collector = pipeline(tmp_path)
    podcast_url = (
        "https://podcasters.spotify.com/pod/show/example/episodes/michael-hyatt"
    )
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[podcast_url],
    )
    podcast = next(
        candidate
        for candidate in discovered.source_plan.candidates
        if candidate.url == podcast_url
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=podcast.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed interview identity",
                decided_by="reviewer",
            )
        ],
        reviewer="reviewer",
    )

    collector.collect_sources("michael-hyatt")

    voice = read_jsonl(
        tmp_path / "michael-hyatt/identity/reference_voice_candidates.jsonl"
    )[0]
    assert voice["artifact_id"].startswith("sha256:")


def test_automatic_review_is_persisted_as_completed_stage(tmp_path) -> None:
    podcast_url = (
        "https://podcasters.spotify.com/pod/show/example/episodes/michael-hyatt"
    )

    pipeline(tmp_path).collect(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[podcast_url],
        auto_approve_discovery=True,
        collection_only=True,
    )

    status = pipeline(tmp_path).status("michael-hyatt")
    assert status["stages"]["review"] == "complete"


def test_complete_pipeline_exports_verified_non_pitch_corpus(tmp_path) -> None:
    result = pipeline(tmp_path).collect(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=["https://blog.example.test/michael-hyatt-bluecat"],
        auto_approve_discovery=True,
    )

    assert result.verification is not None
    assert result.verification.passed is True
    blogs = read_jsonl(tmp_path / "michael-hyatt/blog.jsonl")
    assert len(blogs) == 1
    assert "durable economics" in blogs[0]["title"]
    assert all("thepitch.show" not in str(row) for row in blogs)

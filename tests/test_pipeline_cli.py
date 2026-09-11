from pathlib import Path
import socket

import httpx
import pytest
from typer.testing import CliRunner

from vc_trace_collector.cli import create_app
from vc_trace_collector.collectors import ReviewRequired
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.pipeline import Pipeline
from vc_trace_collector.storage import read_json


FIXTURES = Path(__file__).parent / "fixtures"


def public_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def pipeline(tmp_path: Path) -> Pipeline:
    html = (FIXTURES / "michael_hyatt_profile.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=html,
            headers={"content-type": "text/html", "etag": '"fixture"'},
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=public_resolver, minimum_interval=0
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
    assert read_json(workspace / "discovery/source_plan.json")["requires_review"] is True


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


def test_review_confirms_identity_without_approving_rejected_pitch_source(tmp_path) -> None:
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

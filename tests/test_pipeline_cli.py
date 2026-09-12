import socket
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from typer.testing import CliRunner

import vc_trace_collector.cli as cli_module
from vc_trace_collector.av import (
    DiarizationResult,
    DiarizedTurn,
    TimedText,
    TranscriptResult,
)
from vc_trace_collector.cli import create_app
from vc_trace_collector.collectors import CollectorRegistry, ReviewRequired
from vc_trace_collector.config import RunConfig
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import (
    ApprovalStatus,
    MaterialRole,
    SourceDecision,
    TranscriptInfo,
)
from vc_trace_collector.pipeline import Pipeline
from vc_trace_collector.policy import RuleSet
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
                b"<body>Total time: -00:05. A conversation with investor Michael Hyatt, co-founder of BlueCat."
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


class FixtureEmbedding:
    provider_name = "fixture"
    model_name = "fixture-voice-embedding"
    model_version = "1"

    def embed(self, audio_path):
        return [1.0, 0.0]


class FixtureTranscript:
    provider_name = "fixture"
    model_name = "fixture-transcript"

    def transcribe(self, audio_path):
        is_target = "VC" in Path(audio_path).stem
        return TranscriptResult(
            info=TranscriptInfo(
                method="speech_to_text",
                provider=self.provider_name,
                model=self.model_name,
            ),
            segments=[
                TimedText(
                    start_seconds=0,
                    end_seconds=1,
                    text=(
                        "I invest in durable customer value."
                        if is_target
                        else "Host question"
                    ),
                ),
            ],
        )


class FixtureDiarization:
    provider_name = "fixture"
    model_name = "fixture-diarization"

    def diarize(self, audio_path):
        return DiarizationResult(
            model=self.model_name,
            turns=[
                DiarizedTurn(start_seconds=0, end_seconds=2, speaker_label="HOST"),
                DiarizedTurn(start_seconds=2, end_seconds=5, speaker_label="VC"),
            ],
            speaker_embeddings={"HOST": [0.0, 1.0], "VC": [1.0, 0.0]},
        )


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
        "michael-hyatt",
        decisions=[],
        reviewer="human@example.test",
        confirm_identity=True,
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


def test_default_cli_constructs_credential_free_public_search(
    tmp_path, monkeypatch
) -> None:
    marker = object()
    created: list[tuple[Path, object]] = []

    class FakePipeline:
        def __init__(self, output_dir, *, search_provider=None):
            created.append((Path(output_dir), search_provider))

        def status(self, investor):
            return {"investor": investor}

    monkeypatch.delenv("VC_TRACE_SEARCH_ENDPOINT", raising=False)
    monkeypatch.setattr(cli_module, "Pipeline", FakePipeline)
    monkeypatch.setattr(
        cli_module, "default_public_search_provider", lambda **kwargs: marker
    )

    result = CliRunner().invoke(
        create_app(),
        ["status", "--investor", "michael-hyatt", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert created == [(tmp_path, marker)]


def test_cli_can_disable_public_search_for_discovery(tmp_path) -> None:
    collector = pipeline(tmp_path)

    class ExplodingSearch:
        provider_name = "must-not-run"

        def search(self, query, limit=10):
            raise AssertionError("public search was not disabled")

    collector.search_provider = ExplodingSearch()
    app = create_app(lambda _output_dir: collector)
    result = CliRunner().invoke(
        app,
        [
            "discover",
            "--name",
            "Michael Hyatt",
            "--known-profile-url",
            "https://www.thepitch.show/investors/michael-hyatt",
            "--disable-public-search",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    config = read_json(tmp_path / "michael-hyatt/config_snapshot.json")
    assert config["public_search_enabled"] is False
    event = read_jsonl(tmp_path / "michael-hyatt/audit/events.jsonl")[0]
    assert event["details"]["provider_operations"] == []


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
                material_role=MaterialRole.REFERENCE_VOICE,
            )
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )

    collector.collect_sources("michael-hyatt")

    voice = read_jsonl(
        tmp_path / "michael-hyatt/identity/reference_voice_candidates.jsonl"
    )[0]
    assert voice["artifact_id"].startswith("sha256:")


def test_youtube_webm_audio_is_linked_to_reference_candidate(
    tmp_path, monkeypatch
) -> None:
    youtube_url = "https://www.youtube.com/watch?v=voice123"
    collector = pipeline(tmp_path)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[youtube_url],
    )
    collector.rules = RuleSet([])
    youtube = next(
        item for item in discovered.source_plan.candidates if item.url == youtube_url
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=youtube.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed interview identity",
                decided_by="reviewer",
                material_role=MaterialRole.REFERENCE_VOICE,
            )
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )

    class WebmAudioCollector:
        source_types: ClassVar = {youtube.source_type}

        def collect(self, source, context):
            return [
                context.artifacts.put_bytes(
                    b"webm audio fixture",
                    category="video",
                    suffix=".webm",
                    source_url=source.url,
                    mime_type="video/webm",
                    collection_method="yt_dlp_audio",
                    original_metadata={"candidate_id": source.candidate_id},
                ).record
            ]

    monkeypatch.setattr(
        "vc_trace_collector.pipeline.default_registry",
        lambda: CollectorRegistry([WebmAudioCollector()]),
    )
    collection = collector.collect_sources("michael-hyatt")

    voices = read_jsonl(
        tmp_path / "michael-hyatt/identity/reference_voice_candidates.jsonl"
    )
    voice = next(
        item for item in voices if item["source_candidate_id"] == youtube.candidate_id
    )
    assert collection.failed == 0
    assert collection.collected == 1
    assert len(collection.artifacts) == 1
    assert voice["artifact_id"].startswith("sha256:")


def test_automatic_review_stops_when_namesake_hypothesis_remains(tmp_path) -> None:
    podcast_url = (
        "https://podcasters.spotify.com/pod/show/example/episodes/michael-hyatt"
    )

    with pytest.raises(ReviewRequired, match="competing identity"):
        pipeline(tmp_path).collect(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            source_urls=[podcast_url],
            auto_approve_discovery=True,
            collection_only=True,
        )


def test_complete_pipeline_exports_verified_non_pitch_corpus(tmp_path) -> None:
    collector = pipeline(tmp_path)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=["https://blog.example.test/michael-hyatt-bluecat"],
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=candidate.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed source",
                decided_by="reviewer",
            )
            for candidate in discovered.source_plan.candidates
            if candidate.approval_status == ApprovalStatus.PENDING
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    result = collector.collect(name="Michael Hyatt", resume="latest")

    assert result.verification is not None
    assert result.verification.passed is True
    manifest_paths = {item.path for item in result.manifest.files}
    assert "identity/resolved_identity.json" in manifest_paths
    assert "discovery/source_plan.json" in manifest_paths
    assert any(
        path.startswith("raw/web/") and path.endswith(".html")
        for path in manifest_paths
    )
    blogs = read_jsonl(tmp_path / "michael-hyatt/blog.jsonl")
    assert len(blogs) == 1
    assert "durable economics" in blogs[0]["title"]
    assert all("thepitch.show" not in str(row) for row in blogs)


def test_cli_discover_accepts_supplied_file_and_role(tmp_path) -> None:
    supplied = tmp_path / "public-notes.txt"
    supplied.write_text("Public notes authored by Michael Hyatt.")
    app = create_app(lambda output_dir: pipeline(Path(output_dir)))

    result = CliRunner().invoke(
        app,
        [
            "discover",
            "--name",
            "Michael Hyatt",
            "--known-profile-url",
            "https://www.thepitch.show/investors/michael-hyatt",
            "--supplied-file",
            str(supplied),
            "--supplied-role",
            "authored_by_target",
            "--output-dir",
            str(tmp_path / "outputs"),
        ],
    )

    assert result.exit_code == 0
    rows = read_jsonl(
        tmp_path / "outputs/michael-hyatt/discovery/source_candidates.jsonl"
    )
    supplied_row = next(row for row in rows if row["source_type"] == "supplied")
    assert supplied_row["material_role"] == "authored_by_target"


def test_supplied_public_text_runs_through_complete_pipeline(tmp_path) -> None:
    supplied = tmp_path / "michael-hyatt-public-notes.txt"
    supplied.write_text("I prefer durable businesses with strong customers.")

    collector = pipeline(tmp_path / "outputs")
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[supplied],
        supplied_role=MaterialRole.AUTHORED_BY_TARGET,
    )
    supplied_candidate = next(
        candidate
        for candidate in discovered.source_plan.candidates
        if candidate.source_type == "supplied"
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=supplied_candidate.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human attested supplied authorship",
                decided_by="reviewer",
            )
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    result = collector.collect(name="Michael Hyatt", resume="latest")

    assert result.verification is not None and result.verification.passed
    rows = read_jsonl(tmp_path / "outputs/michael-hyatt/blog.jsonl")
    assert [row["full_text"] for row in rows] == [supplied.read_text()]


def test_review_requires_explicit_identity_confirmation(tmp_path) -> None:
    collector = pipeline(tmp_path)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    with pytest.raises(ReviewRequired, match="explicit identity confirmation"):
        collector.review("michael-hyatt", decisions=[], reviewer="reviewer")


def test_complete_supplied_video_pipeline_exports_verified_target_speech(
    tmp_path,
) -> None:
    reference_audio = tmp_path / "known-michael-hyatt.wav"
    reference_audio.write_bytes(b"reference voice fixture")
    interview_audio = tmp_path / "michael-hyatt-interview.mp4"
    interview_audio.write_bytes(b"video interview fixture")
    output = tmp_path / "outputs"
    base = pipeline(output)

    def fixture_audio_extractor(source, destination, **kwargs):
        destination.write_bytes(b"extracted audio fixture")
        return destination

    collector = Pipeline(
        output,
        fetcher=base.fetcher,
        embedding_provider=FixtureEmbedding(),
        transcript_provider=FixtureTranscript(),
        diarization_provider=FixtureDiarization(),
        audio_extractor=fixture_audio_extractor,
        media_probe=lambda _path: 5.0,
    )
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[reference_audio, interview_audio],
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            supplied_files=[str(reference_audio), str(interview_audio)],
            output_dir=str(output),
            transcription_model="fixture-transcript",
            diarization_model="fixture-diarization",
            embedding_model="fixture-voice-embedding",
            transcription_cost_usd=Decimal("0.10"),
            diarization_cost_usd=Decimal("0.20"),
            embedding_cost_usd=Decimal("0.30"),
            maximum_cost_usd=Decimal("1.00"),
        ),
    )
    supplied = [
        item
        for item in discovered.source_plan.candidates
        if item.source_type == "supplied"
    ]
    reference = next(item for item in supplied if "known-" in item.url)
    interview = next(item for item in supplied if "interview" in item.url)
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=reference.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human verified isolated reference voice",
                decided_by="reviewer",
                material_role=MaterialRole.REFERENCE_VOICE,
            ),
            SourceDecision(
                candidate_id=interview.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human verified target interview",
                decided_by="reviewer",
                material_role=MaterialRole.SPOKEN_BY_TARGET,
            ),
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    collector.collect_sources("michael-hyatt")
    voice_candidate = read_jsonl(
        output / "michael-hyatt/identity/reference_voice_candidates.jsonl"
    )[0]
    collector.approve_reference_voice(
        "michael-hyatt",
        candidate_id=voice_candidate["candidate_id"],
        reviewer="reviewer",
    )
    documents = collector.process("michael-hyatt")
    resumed_documents = collector.process("michael-hyatt")
    collector.export("michael-hyatt")
    verified = collector.verify("michael-hyatt")

    assert verified.passed is True
    speech = [item for item in documents if item.material_role == "spoken_by_target"]
    assert len(speech) == 1
    assert (
        len(
            [
                item
                for item in resumed_documents
                if item.material_role == "spoken_by_target"
            ]
        )
        == 1
    )
    assert speech[0].speaker_attribution.status == "accepted_model"
    assert any(
        row["collection_method"] == "ffmpeg_audio_extraction"
        for path in (output / "michael-hyatt/raw/video").rglob("*.metadata.json")
        for row in [read_json(path)]
    )
    derived = [
        read_json(path)
        for path in (output / "michael-hyatt/raw/video").rglob("*.metadata.json")
        if read_json(path)["collection_method"] == "ffmpeg_audio_extraction"
    ]
    assert len(derived) == 1
    assert derived[0]["artifact_id"] not in derived[0]["parent_artifact_ids"]
    talks = read_jsonl(output / "michael-hyatt/talks.jsonl")
    assert [item["text"] for item in talks] == ["I invest in durable customer value."]
    summary = read_json(output / "michael-hyatt/run_summary.json")
    assert Decimal(summary["cost_usd"]) == Decimal("0.60")


def test_failed_approved_av_source_prevents_otherwise_nonempty_export(tmp_path) -> None:
    article = tmp_path / "michael-hyatt-article.txt"
    article.write_text("I invest in durable, customer-led businesses.")
    interview = tmp_path / "michael-hyatt-interview.mp4"
    interview.write_bytes(b"video fixture")
    collector = pipeline(tmp_path)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[article, interview],
    )
    supplied = {
        Path(item.url.removeprefix("file://")).suffix: item
        for item in discovered.source_plan.candidates
        if item.source_type == "supplied"
    }
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=supplied[".txt"].candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed authorship",
                decided_by="reviewer",
                material_role=MaterialRole.AUTHORED_BY_TARGET,
            ),
            SourceDecision(
                candidate_id=supplied[".mp4"].candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed interview",
                decided_by="reviewer",
                material_role=MaterialRole.SPOKEN_BY_TARGET,
            ),
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )

    collector.collect_sources("michael-hyatt")
    collector.process("michael-hyatt")
    collector.export("michael-hyatt")
    verification = collector.verify("michael-hyatt")

    assert verification.passed is False
    assert "incomplete" in " ".join(verification.errors).casefold()
    quality = read_json(tmp_path / "michael-hyatt/quality_report.json")
    assert quality["counts"]["included"] == 1
    assert quality["counts"]["failures"] == 1
    assert quality["passed"] is False


def test_media_limit_is_shared_by_reference_and_target_processing(tmp_path) -> None:
    reference_audio = tmp_path / "known-michael-hyatt.wav"
    reference_audio.write_bytes(b"reference voice fixture")
    interview_audio = tmp_path / "michael-hyatt-interview.mp4"
    interview_audio.write_bytes(b"video interview fixture")
    output = tmp_path / "outputs"
    base = pipeline(output)

    def fixture_audio_extractor(source, destination, **kwargs):
        destination.write_bytes(b"extracted audio fixture")
        return destination

    collector = Pipeline(
        output,
        fetcher=base.fetcher,
        embedding_provider=FixtureEmbedding(),
        transcript_provider=FixtureTranscript(),
        diarization_provider=FixtureDiarization(),
        audio_extractor=fixture_audio_extractor,
        media_probe=lambda _path: 50.0,
    )
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[reference_audio, interview_audio],
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            supplied_files=[str(reference_audio), str(interview_audio)],
            output_dir=str(output),
            transcription_model="fixture-transcript",
            diarization_model="fixture-diarization",
            embedding_model="fixture-voice-embedding",
            maximum_media_minutes=1,
        ),
    )
    supplied = [
        item
        for item in discovered.source_plan.candidates
        if item.source_type == "supplied"
    ]
    reference = next(item for item in supplied if "known-" in item.url)
    interview = next(item for item in supplied if "interview" in item.url)
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=reference.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human verified isolated reference voice",
                decided_by="reviewer",
                material_role=MaterialRole.REFERENCE_VOICE,
            ),
            SourceDecision(
                candidate_id=interview.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human verified target interview",
                decided_by="reviewer",
                material_role=MaterialRole.SPOKEN_BY_TARGET,
            ),
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    collector.collect_sources("michael-hyatt")
    voice = read_jsonl(
        output / "michael-hyatt/identity/reference_voice_candidates.jsonl"
    )[0]
    collector.approve_reference_voice(
        "michael-hyatt",
        candidate_id=voice["candidate_id"],
        reviewer="reviewer",
    )

    collector.process("michael-hyatt")

    outcomes = read_jsonl(
        output / "michael-hyatt/processed/av_candidate_outcomes.jsonl"
    )
    assert len(outcomes) == 1
    assert outcomes[0]["candidate_id"] == interview.candidate_id
    assert outcomes[0]["status"] == "failed"
    assert outcomes[0]["reason"] == (
        "Media processing budget exhausted before model calls"
    )
    provider_attempts = [
        row
        for row in read_jsonl(output / "michael-hyatt/audit/costs.jsonl")
        if row["kind"] == "reservation" and row.get("provider")
    ]
    assert [
        row["media_seconds"] for row in provider_attempts if row["media_seconds"]
    ] == [50.0]


def test_cli_collect_exits_nonzero_when_export_cannot_verify(tmp_path) -> None:
    collector = pipeline(tmp_path)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    collector.review(
        "michael-hyatt",
        decisions=[],
        reviewer="reviewer",
        confirm_identity=True,
    )
    app = create_app(lambda output_dir: pipeline(Path(output_dir)))

    result = CliRunner().invoke(
        app,
        [
            "collect",
            "--name",
            "Michael Hyatt",
            "--resume",
            "latest",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "verification failed" in result.output.casefold()


def test_llm_discovery_requires_an_explicit_cost_reservation(tmp_path) -> None:
    class DiscoveryProvider:
        provider_name = "fixture"
        model_name = "fixture-model"

        def refine(self, **kwargs):
            raise AssertionError("provider must not run without a budget")

    base = pipeline(tmp_path)
    collector = Pipeline(
        tmp_path,
        fetcher=base.fetcher,
        discovery_provider=DiscoveryProvider(),
    )

    with pytest.raises(ValueError, match="discovery_call_budget_usd"):
        collector.discover(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        )


def test_reference_budget_is_reserved_before_model_initialization(
    tmp_path, monkeypatch
) -> None:
    reference_audio = tmp_path / "known-michael-hyatt.wav"
    reference_audio.write_bytes(b"reference voice fixture")
    output = tmp_path / "outputs"
    collector = pipeline(output)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[reference_audio],
        supplied_role=MaterialRole.REFERENCE_VOICE,
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            supplied_files=[str(reference_audio)],
            supplied_role=MaterialRole.REFERENCE_VOICE,
            output_dir=str(output),
            embedding_model="fixture-model",
        ),
    )
    source = next(
        item
        for item in discovered.source_plan.candidates
        if item.source_type == "supplied"
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=source.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Verified reference source",
                decided_by="reviewer",
                material_role=MaterialRole.REFERENCE_VOICE,
            )
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    collector.collect_sources("michael-hyatt")
    voice = read_jsonl(
        output / "michael-hyatt/identity/reference_voice_candidates.jsonl"
    )[0]
    costs_path = output / "michael-hyatt/audit/costs.jsonl"

    class GuardedProvider:
        def __init__(self, *args, **kwargs):
            rows = read_jsonl(costs_path)
            assert rows[-1]["kind"] == "reservation"
            assert rows[-1]["provider"] == "pyannote"
            raise RuntimeError("model initialization stopped")

    monkeypatch.setattr(
        "vc_trace_collector.pipeline.PyannoteEmbeddingProvider", GuardedProvider
    )
    with pytest.raises(RuntimeError, match="initialization stopped"):
        collector.approve_reference_voice(
            "michael-hyatt",
            candidate_id=voice["candidate_id"],
            reviewer="reviewer",
            start_seconds=0,
            end_seconds=10,
        )

    rows = read_jsonl(costs_path)
    assert [row["kind"] for row in rows[-2:]] == ["reservation", "release"]

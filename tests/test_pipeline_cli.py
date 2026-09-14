import os
import socket
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from typer.main import get_command
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
    ReferenceVoiceProfile,
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


def _reference_profile(candidate_id: str, value: float) -> ReferenceVoiceProfile:
    return ReferenceVoiceProfile(
        investor_slug="michael-hyatt",
        candidate_ids=[candidate_id],
        artifact_ids=["sha256:" + candidate_id[-1] * 64],
        status="verified_human",
        embedding_model="fixture-embedding",
        embedding_model_version="1",
        embedding=[value, 1.0 - value],
    )


def test_reference_profile_set_preserves_multiple_and_upserts_idempotently(
    tmp_path,
) -> None:
    collector = pipeline(tmp_path)
    workspace = tmp_path / "michael-hyatt"

    collector._upsert_reference_profile(
        workspace, _reference_profile("voice:1", 1.0)
    )
    collector._upsert_reference_profile(
        workspace, _reference_profile("voice:2", 0.0)
    )
    collector._upsert_reference_profile(
        workspace, _reference_profile("voice:1", 1.0)
    )

    profiles = read_jsonl(
        workspace / "identity/reference_voice_profiles.jsonl"
    )
    assert len(profiles) == 2
    assert len({row["profile_id"] for row in profiles}) == 2
    assert read_json(workspace / "identity/reference_voice_profile.json")[
        "profile_id"
    ] in {row["profile_id"] for row in profiles}


def test_legacy_reference_profile_is_migrated_without_deleting_projection(
    tmp_path,
) -> None:
    collector = pipeline(tmp_path)
    workspace = tmp_path / "michael-hyatt"
    legacy_path = workspace / "identity/reference_voice_profile.json"
    from vc_trace_collector.storage import write_json

    write_json(legacy_path, _reference_profile("voice:legacy", 1.0))

    profiles = collector._load_reference_profiles(workspace)

    assert len(profiles) == 1
    assert profiles[0].profile_id is not None
    assert legacy_path.exists()
    assert len(
        read_jsonl(workspace / "identity/reference_voice_profiles.jsonl")
    ) == 1


class FixtureDiarization:
    provider_name = "fixture"
    model_name = "fixture-diarization"
    model_version = "1"

    def embed(self, audio_path):
        return [1.0, 0.0]

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


def test_cli_fetch_source_collects_one_approved_candidate(tmp_path) -> None:
    collector = pipeline(tmp_path)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=["https://blog.example.test/michael-hyatt-bluecat"],
    )
    article = next(
        item
        for item in discovered.source_plan.candidates
        if item.canonical_url.startswith("https://blog.example.test/")
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=article.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed authorship",
                decided_by="reviewer",
                material_role=MaterialRole.AUTHORED_BY_TARGET,
            )
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    app = create_app(lambda _output_dir: collector)

    result = CliRunner().invoke(
        app,
        [
            "fetch-source",
            "--investor",
            "michael-hyatt",
            "--candidate-id",
            article.candidate_id,
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Collected 1 source" in result.stdout


def test_fetch_source_rejects_pending_candidate(tmp_path) -> None:
    collector = pipeline(tmp_path)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=["https://blog.example.test/michael-hyatt-bluecat"],
    )
    article = next(
        item
        for item in discovered.source_plan.candidates
        if item.canonical_url.startswith("https://blog.example.test/")
    )

    with pytest.raises(ReviewRequired, match="not approved"):
        collector.fetch_source(
            "michael-hyatt", candidate_ids={article.candidate_id}
        )


def test_partial_fetch_preserves_and_then_clears_terminal_failure_counts(
    tmp_path, monkeypatch
) -> None:
    collector = pipeline(tmp_path)
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[
            "https://blog.example.test/michael-hyatt-first",
            "https://blog.example.test/michael-hyatt-second",
        ],
    )
    articles = [
        item
        for item in discovered.source_plan.candidates
        if item.canonical_url.startswith("https://blog.example.test/")
    ]
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=item.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human confirmed source",
                decided_by="reviewer",
                material_role=MaterialRole.AUTHORED_BY_TARGET,
            )
            for item in articles
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    failing_id = articles[0].candidate_id

    class SelectiveCollector:
        source_types: ClassVar = {articles[0].source_type}
        fail = True

        def collect(self, source, context):
            if source.candidate_id == failing_id and self.fail:
                raise RuntimeError("fixture failure")
            return [
                context.artifacts.put_bytes(
                    source.candidate_id.encode(),
                    category="web",
                    suffix=".html",
                    source_url=source.url,
                    collection_method="http",
                    original_metadata={"candidate_id": source.candidate_id},
                ).record
            ]

    fixture_collector = SelectiveCollector()
    monkeypatch.setattr(
        "vc_trace_collector.pipeline.default_registry",
        lambda: CollectorRegistry([fixture_collector]),
    )

    collector.fetch_source("michael-hyatt", candidate_ids={failing_id})
    collector.fetch_source(
        "michael-hyatt", candidate_ids={articles[1].candidate_id}
    )
    after_success = read_json(tmp_path / "michael-hyatt/run_summary.json")
    fixture_collector.fail = False
    collector.fetch_source("michael-hyatt", candidate_ids={failing_id})
    after_retry = read_json(tmp_path / "michael-hyatt/run_summary.json")

    assert after_success["collection_failures"] == 1
    assert after_retry["collection_failures"] == 0


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


def test_stage_specific_options_keep_av_models_out_of_discovery() -> None:
    command = get_command(create_app())
    option_names = {parameter.name for parameter in command.commands["discover"].params}

    assert "discovery_model" in option_names
    assert "transcription_model" not in option_names
    assert "diarization_model" not in option_names
    assert "embedding_model" not in option_names


def test_process_cli_routes_av_stage_configuration(tmp_path) -> None:
    calls = []

    class FakePipeline:
        def process(self, investor, **options):
            calls.append((investor, options))
            return []

    app = create_app(lambda _output_dir: FakePipeline())
    result = CliRunner().invoke(
        app,
        [
            "process",
            "--investor",
            "michael-hyatt",
            "--transcription-model",
            "turbo",
            "--diarization-model",
            "pyannote/speaker-diarization-3.1",
            "--transcription-cost-usd",
            "0.10",
            "--diarization-cost-usd",
            "0.20",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "michael-hyatt",
            {
                "transcription_model": "turbo",
                "diarization_model": "pyannote/speaker-diarization-3.1",
                "transcription_cost_usd": Decimal("0.10"),
                "diarization_cost_usd": Decimal("0.20"),
            },
        )
    ]


def test_review_voice_cli_routes_av_stage_configuration(tmp_path) -> None:
    calls = []

    class Profile:
        embedding: ClassVar = [1.0, 0.0]

    class FakePipeline:
        def approve_reference_voice(self, investor, **options):
            calls.append((investor, options))
            return Profile()

    app = create_app(lambda _output_dir: FakePipeline())
    result = CliRunner().invoke(
        app,
        [
            "review-voice",
            "--investor",
            "michael-hyatt",
            "--candidate-id",
            "voice:candidate",
            "--diarization-model",
            "pyannote/speaker-diarization-3.1",
            "--embedding-cost-usd",
            "0.30",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "michael-hyatt",
            {
                "candidate_id": "voice:candidate",
                "reviewer": "human",
                "start_seconds": None,
                "end_seconds": None,
                "diarization_model": "pyannote/speaker-diarization-3.1",
                "embedding_model": None,
                "embedding_cost_usd": Decimal("0.30"),
            },
        )
    ]


def test_process_persists_av_configuration_without_duplicate_audit_events(
    tmp_path,
) -> None:
    collector = pipeline(tmp_path)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    options = {
        "transcription_model": "turbo",
        "diarization_model": "pyannote/speaker-diarization-3.1",
        "transcription_cost_usd": Decimal("0.10"),
        "diarization_cost_usd": Decimal("0.20"),
    }
    collector.process("michael-hyatt", **options)
    collector.process("michael-hyatt", **options)

    workspace = tmp_path / "michael-hyatt"
    config = read_json(workspace / "config_snapshot.json")
    assert config["transcription_model"] == "turbo"
    assert config["diarization_model"] == "pyannote/speaker-diarization-3.1"
    assert config["transcription_cost_usd"] == "0.10"
    assert config["diarization_cost_usd"] == "0.20"
    configuration_events = [
        event
        for event in read_jsonl(workspace / "audit/events.jsonl")
        if event["stage"] == "configuration"
    ]
    assert len(configuration_events) == 1
    assert configuration_events[0]["details"]["stage"] == "process"
    assert set(configuration_events[0]["details"]["changes"]) == set(options)


def test_cli_loads_dotenv_without_overriding_process_environment(
    tmp_path, monkeypatch
) -> None:
    dotenv_variable = "VC_TRACE_TEST_DOTENV_LOADING"
    shell_variable = "VC_TRACE_TEST_DOTENV_PRECEDENCE"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(dotenv_variable, raising=False)
    monkeypatch.setenv(shell_variable, "from-shell")
    (tmp_path / ".env").write_text(
        f"{dotenv_variable}=from-dotenv\n{shell_variable}=from-dotenv\n"
    )

    try:
        create_app(lambda output_dir: pipeline(Path(output_dir)))
        assert os.environ[dotenv_variable] == "from-dotenv"
        assert os.environ[shell_variable] == "from-shell"
    finally:
        os.environ.pop(dotenv_variable, None)


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


def test_process_source_runs_and_merges_one_av_candidate_at_a_time(tmp_path) -> None:
    reference_audio = tmp_path / "known-michael-hyatt.wav"
    reference_audio.write_bytes(b"reference voice fixture")
    first_video = tmp_path / "michael-hyatt-first.mp4"
    first_video.write_bytes(b"first interview fixture")
    second_video = tmp_path / "michael-hyatt-second.mp4"
    second_video.write_bytes(b"second interview fixture")
    output = tmp_path / "outputs"
    base = pipeline(output)

    class CountingTranscript(FixtureTranscript):
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, audio_path):
            self.calls += 1
            return super().transcribe(audio_path)

    class CountingDiarization(FixtureDiarization):
        def __init__(self) -> None:
            self.calls = 0

        def diarize(self, audio_path):
            self.calls += 1
            return super().diarize(audio_path)

    transcript = CountingTranscript()
    diarization = CountingDiarization()

    def fixture_audio_extractor(source, destination, **kwargs):
        destination.write_bytes(b"extracted audio fixture")
        return destination

    collector = Pipeline(
        output,
        fetcher=base.fetcher,
        transcript_provider=transcript,
        diarization_provider=diarization,
        audio_extractor=fixture_audio_extractor,
        media_probe=lambda _path: 5.0,
    )
    discovered = collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[reference_audio, first_video, second_video],
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            supplied_files=[str(reference_audio), str(first_video), str(second_video)],
            output_dir=str(output),
            transcription_model="fixture-transcript",
            diarization_model="fixture-diarization",
            maximum_cost_usd=Decimal("1.00"),
        ),
    )
    supplied = [
        item for item in discovered.source_plan.candidates if item.source_type == "supplied"
    ]
    reference = next(item for item in supplied if "known-" in item.url)
    interviews = sorted(
        (item for item in supplied if "michael-hyatt-" in item.url and "known-" not in item.url),
        key=lambda item: item.url,
    )
    collector.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=reference.candidate_id,
                status=ApprovalStatus.APPROVED,
                reason="Human verified reference",
                decided_by="reviewer",
                material_role=MaterialRole.REFERENCE_VOICE,
            ),
            *[
                SourceDecision(
                    candidate_id=item.candidate_id,
                    status=ApprovalStatus.APPROVED,
                    reason="Human verified interview",
                    decided_by="reviewer",
                    material_role=MaterialRole.SPOKEN_BY_TARGET,
                )
                for item in interviews
            ],
        ],
        reviewer="reviewer",
        confirm_identity=True,
    )
    collector.collect_sources("michael-hyatt")
    voice = next(
        row
        for row in read_jsonl(
            output / "michael-hyatt/identity/reference_voice_candidates.jsonl"
        )
        if row["source_candidate_id"] == reference.candidate_id
    )
    collector.approve_reference_voice(
        "michael-hyatt", candidate_id=voice["candidate_id"], reviewer="reviewer"
    )

    first = collector.process_source(
        "michael-hyatt", candidate_id=interviews[0].candidate_id
    )
    first_diarization_calls = diarization.calls
    first_transcript_calls = transcript.calls
    collector._upsert_reference_profile(
        output / "michael-hyatt",
        ReferenceVoiceProfile(
            investor_slug="michael-hyatt",
            candidate_ids=["voice:second"],
            artifact_ids=["sha256:" + "b" * 64],
            status="verified_human",
            embedding_model="fixture-diarization",
            embedding_model_version="1",
            embedding=[0.9, 0.1],
        ),
    )
    rematched = collector.process_source(
        "michael-hyatt", candidate_id=interviews[0].candidate_id
    )

    assert diarization.calls == first_diarization_calls
    assert transcript.calls == first_transcript_calls
    rematched_document = next(
        item
        for item in rematched
        if item.source_candidate_id == interviews[0].candidate_id
        and item.inclusion_status == "included"
    )
    assert all(
        len(item.reference_scores) == 2
        for item in rematched_document.speaker_attribution.score_evidence
    )
    for stage in ("av_diarization", "av_transcripts", "av_attributions"):
        for cache_file in (output / "michael-hyatt/state" / stage).glob("*.json"):
            cache_file.unlink()
    diarization.calls = 0
    transcript.calls = 0

    collector.process_source(
        "michael-hyatt", candidate_id=interviews[0].candidate_id
    )

    assert diarization.calls == 1
    assert transcript.calls == 0
    migrated_transcript = read_jsonl(
        output / "michael-hyatt/audit/events.jsonl"
    )[-1]
    assert (
        migrated_transcript["details"]["cache"]["transcript_migration_source"]
        == "legacy_av_result"
    )

    class FailingDiarization(CountingDiarization):
        model_name = "fixture-diarization-v2"

        def diarize(self, audio_path):
            raise RuntimeError("new model failed")

    collector.diarization_provider = FailingDiarization()
    failed_retry = collector.process_source(
        "michael-hyatt",
        candidate_id=interviews[0].candidate_id,
        diarization_model="fixture-diarization-v2",
    )
    collector.diarization_provider = diarization
    second = collector.process_source(
        "michael-hyatt", candidate_id=interviews[1].candidate_id
    )

    assert {item.source_candidate_id for item in first} == {
        interviews[0].candidate_id
    }
    assert {item.source_candidate_id for item in second} == {
        interviews[0].candidate_id,
        interviews[1].candidate_id,
    }
    assert {item.source_candidate_id for item in failed_retry} == {
        interviews[0].candidate_id
    }
    assert diarization.calls == 1
    assert transcript.calls == 0


def test_cli_process_source_passes_models_to_one_candidate(tmp_path) -> None:
    calls = []

    class FakePipeline:
        def process_source(self, investor, **kwargs):
            calls.append((investor, kwargs))
            return []

    app = create_app(lambda _output_dir: FakePipeline())
    result = CliRunner().invoke(
        app,
        [
            "process-source",
            "--investor",
            "michael-hyatt",
            "--candidate-id",
            "candidate:abc",
            "--transcription-model",
            "turbo",
            "--diarization-model",
            "pyannote/speaker-diarization-3.1",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "michael-hyatt",
            {
                "candidate_id": "candidate:abc",
                "transcription_model": "turbo",
                "diarization_model": "pyannote/speaker-diarization-3.1",
                "transcription_cost_usd": None,
                "diarization_cost_usd": None,
            },
        )
    ]


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
            config = read_json(output / "michael-hyatt/config_snapshot.json")
            assert config["embedding_model"] == "fixture-model"
            assert config["embedding_cost_usd"] == "0.30"
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
            embedding_model="fixture-model",
            embedding_cost_usd=Decimal("0.30"),
        )

    rows = read_jsonl(costs_path)
    assert [row["kind"] for row in rows[-2:]] == ["reservation", "release"]

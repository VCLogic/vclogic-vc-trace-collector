import json
import socket
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from vc_trace_collector.audit import BudgetExceeded, BudgetLedger
from vc_trace_collector.collectors import (
    CollectionContext,
    CollectorRegistry,
    PodcastCollector,
    SuppliedFileCollector,
    WebCollector,
    YouTubeCollector,
    _podcast_duration_seconds,
    collect_approved_sources,
)
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import (
    ApprovalStatus,
    Confidence,
    MaterialRole,
    SourceCandidate,
    SourcePlan,
    SourceType,
)
from vc_trace_collector.policy import ExclusionRule, RuleSet
from vc_trace_collector.storage import ArtifactStore, StateStore, write_jsonl


def candidate(
    identifier: str,
    source_type: SourceType = SourceType.WEB_ARTICLE,
    *,
    url: str | None = None,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
    estimated_cost_usd: Decimal = Decimal(0),
) -> SourceCandidate:
    source_url = url or f"https://example.test/{identifier}"
    return SourceCandidate(
        candidate_id=identifier,
        url=source_url,
        canonical_url=source_url,
        source_type=source_type,
        material_role=MaterialRole.AUTHORED_BY_TARGET,
        discovery_queries=["query"],
        identity_confidence=Confidence(score=0.9, method="test", version="1"),
        source_confidence=Confidence(score=0.9, method="test", version="1"),
        approval_status=status,
        estimated_cost_usd=estimated_cost_usd,
    )


def plan(*candidates: SourceCandidate) -> SourcePlan:
    return SourcePlan(
        plan_id="plan-1", investor_slug="michael-hyatt", candidates=list(candidates)
    )


class StaticCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.WEB_ARTICLE}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def collect(self, source: SourceCandidate, context: CollectionContext):
        self.calls.append(source.candidate_id)
        if source.candidate_id == "failure":
            raise RuntimeError("isolated failure")
        return [
            context.artifacts.put_bytes(
                b"collected",
                category="web",
                suffix=".html",
                source_url=source.url,
            ).record
        ]


class PostMetadataReviewCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.WEB_ARTICLE}

    def __init__(self) -> None:
        self.calls = 0

    def collect(self, source: SourceCandidate, context: CollectionContext):
        self.calls += 1
        return [
            context.artifacts.put_bytes(
                b"collected",
                category="web",
                suffix=".html",
                source_url=source.url,
                original_metadata={
                    "candidate_id": source.candidate_id,
                    "collection_exclusion": {
                        "status": "review_required",
                        "reason": "metadata requires review",
                    },
                },
            ).record
        ]


def context(tmp_path: Path) -> CollectionContext:
    return CollectionContext(
        workspace=tmp_path,
        artifacts=ArtifactStore(tmp_path),
        state=StateStore(tmp_path / "state/state.sqlite"),
        rules=RuleSet.pitch_default(),
        run_id="run-1",
    )


def test_one_failed_source_does_not_remove_success(tmp_path) -> None:
    collector = StaticCollector()
    registry = CollectorRegistry([collector])

    result = collect_approved_sources(
        plan(candidate("success"), candidate("failure")),
        context=context(tmp_path),
        registry=registry,
    )

    assert result.collected == 1
    assert result.failed == 1
    assert next((tmp_path / "raw/web").rglob("*.html")).exists()


def test_unavailable_collector_is_isolated_from_supported_sources(tmp_path) -> None:
    collector = StaticCollector()

    result = collect_approved_sources(
        plan(candidate("unsupported", SourceType.X), candidate("success")),
        context=context(tmp_path),
        registry=CollectorRegistry([collector]),
    )

    assert result.failed == 1
    assert result.collected == 1
    assert result.failures[0]["error_type"] == "CollectorUnavailable"


def test_completed_collection_is_skipped_on_resume(tmp_path) -> None:
    collector = StaticCollector()
    registry = CollectorRegistry([collector])
    run_context = context(tmp_path)
    source_plan = plan(candidate("success"))

    first = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )
    second = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )

    assert first.collected == 1
    assert second.skipped == 1
    assert collector.calls == ["success"]


def test_cached_collection_preserves_post_metadata_review_status(tmp_path) -> None:
    collector = PostMetadataReviewCollector()
    registry = CollectorRegistry([collector])
    run_context = context(tmp_path)
    source_plan = plan(candidate("review"))

    first = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )
    second = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )

    assert first.outcomes[0].status == "review_required"
    assert second.outcomes[0].status == "review_required"
    assert second.review_required == 1
    assert collector.calls == 1


def test_legacy_cached_collection_recovers_persisted_review_status(tmp_path) -> None:
    collector = PostMetadataReviewCollector()
    registry = CollectorRegistry([collector])
    run_context = context(tmp_path)
    source_plan = plan(candidate("review"))
    first = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )
    write_jsonl(
        tmp_path / "processed/collection_candidate_outcomes.jsonl",
        first.outcomes,
    )
    for cache_path in (tmp_path / "state/collection_results").glob("*.json"):
        cache_path.unlink()

    resumed = collect_approved_sources(
        source_plan, context=run_context, registry=registry
    )

    assert resumed.outcomes[0].status == "review_required"
    assert resumed.review_required == 1
    assert collector.calls == 1


def test_supplied_file_is_snapshotted(tmp_path) -> None:
    supplied = tmp_path / "interview.txt"
    supplied.write_text("A supplied public interview")
    source = candidate(
        "supplied",
        SourceType.SUPPLIED,
        url=supplied.resolve().as_uri(),
    )
    registry = CollectorRegistry([SuppliedFileCollector()])

    result = collect_approved_sources(
        plan(source), context=context(tmp_path), registry=registry
    )

    assert result.collected == 1
    assert result.artifacts[0].source_path == str(supplied.resolve())
    assert (
        tmp_path / result.artifacts[0].relative_path
    ).read_text() == supplied.read_text()


def test_collection_skips_source_types_outside_run_allowlist(tmp_path) -> None:
    collector = StaticCollector()
    run_context = context(tmp_path)
    run_context.approved_source_types = {SourceType.RSS_FEED}

    result = collect_approved_sources(
        plan(candidate("web")),
        context=run_context,
        registry=CollectorRegistry([collector]),
    )

    assert result.skipped == 1
    assert collector.calls == []


def test_collection_runs_only_requested_candidate_ids(tmp_path) -> None:
    collector = StaticCollector()
    run_context = context(tmp_path)
    run_context.candidate_ids = {"second"}

    result = collect_approved_sources(
        plan(candidate("first"), candidate("second")),
        context=run_context,
        registry=CollectorRegistry([collector]),
    )

    assert result.collected == 1
    assert result.skipped == 1
    assert collector.calls == ["second"]


def test_candidate_filter_ignores_unreviewed_items_outside_selection(tmp_path) -> None:
    collector = StaticCollector()
    run_context = context(tmp_path)
    run_context.candidate_ids = {"approved"}

    result = collect_approved_sources(
        plan(
            candidate("pending", status=ApprovalStatus.PENDING),
            candidate("approved"),
        ),
        context=run_context,
        registry=CollectorRegistry([collector]),
    )

    assert result.collected == 1
    assert collector.calls == ["approved"]


def test_collection_stops_before_exceeding_reserved_provider_budget(tmp_path) -> None:
    collector = StaticCollector()
    run_context = context(tmp_path)
    run_context.budget = BudgetLedger(tmp_path / "audit/costs.jsonl", Decimal("1.00"))

    with pytest.raises(BudgetExceeded):
        collect_approved_sources(
            plan(
                candidate("first", estimated_cost_usd=Decimal("0.75")),
                candidate("second", estimated_cost_usd=Decimal("0.26")),
            ),
            context=run_context,
            registry=CollectorRegistry([collector]),
        )

    assert collector.calls == ["first"]
    assert run_context.budget.spent == Decimal("0.75")
    assert next((tmp_path / "raw/web").rglob("*.html")).exists()


def test_downloaded_byte_limit_is_aggregate_across_web_sources(tmp_path) -> None:
    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"123456", request=request)

    run_context = context(tmp_path)
    run_context.maximum_download_bytes = 10
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    result = collect_approved_sources(
        plan(candidate("first"), candidate("second")),
        context=run_context,
        registry=CollectorRegistry([WebCollector()]),
    )

    assert result.collected == 1
    assert result.failed == 1
    assert result.downloaded_bytes == 6


def test_podcast_collector_preserves_page_and_public_audio(tmp_path) -> None:
    page_url = "https://podcast.example.test/episodes/michael-hyatt"
    audio_url = "https://cdn.example.test/michael-hyatt.mp3"

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == audio_url:
            return httpx.Response(
                200,
                content=b"ID3 public audio fixture",
                headers={"content-type": "application/octet-stream"},
                request=request,
            )
        return httpx.Response(
            200,
            content=(
                f"<html><body>Total time: -00:05"
                f'<audio src="{audio_url}"></audio></body></html>'
            ).encode(),
            headers={"content-type": "text/html"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    source = candidate("podcast", SourceType.PODCAST, url=page_url)

    result = collect_approved_sources(
        plan(source),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.collected == 1
    assert {artifact.mime_type for artifact in result.artifacts} == {
        "text/html",
        "application/octet-stream",
    }
    assert result.artifacts[1].parent_artifact_ids == [result.artifacts[0].artifact_id]
    assert result.artifacts[1].relative_path.endswith(".mp3")
    assert result.media_seconds == 5


def test_podcast_duration_ignores_zero_placeholder_before_real_runtime() -> None:
    html = b'{"duration": 0}<div>Total time: -42:41</div>'

    assert _podcast_duration_seconds(html) == 2561


def test_podcast_refuses_unbounded_media_before_enclosure_download(tmp_path) -> None:
    page_url = "https://podcast.example.test/episode"
    audio_url = "https://cdn.example.test/episode.mp3"
    requested: list[str] = []

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            content=f'<html><audio src="{audio_url}"></audio></html>'.encode(),
            headers={"content-type": "text/html"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )

    result = collect_approved_sources(
        plan(candidate("podcast", SourceType.PODCAST, url=page_url)),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.failed == 1
    assert requested == [page_url, page_url]  # HEAD classification, then page GET


def test_direct_podcast_audio_requires_duration_before_get(tmp_path) -> None:
    audio_url = "https://cdn.example.test/episode.mp3"
    requested: list[str] = []

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            content=b"ID3 audio",
            headers={"content-type": "audio/mpeg"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    result = collect_approved_sources(
        plan(candidate("podcast", SourceType.PODCAST, url=audio_url)),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.failed == 1
    assert requested == []


def test_direct_audio_over_remaining_duration_is_rejected_before_get(tmp_path) -> None:
    audio_url = "https://cdn.example.test/episode.mp3"
    requested: list[str] = []

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"ID3 audio", request=request)

    source = candidate("podcast", SourceType.PODCAST, url=audio_url)
    source.estimated_media_seconds = 100
    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    result = collect_approved_sources(
        plan(source),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.failed == 1
    assert requested == []


def test_youtube_failed_attempt_accounts_partial_file_and_disables_retries(
    tmp_path,
) -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        if "--dump-single-json" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"id": "abc", "duration": 40}),
                stderr="",
            )
        template = Path(command[command.index("-o") + 1])
        template.with_name("audio.webm.part").write_bytes(b"partial-media")
        raise subprocess.CalledProcessError(1, command)

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    result = collect_approved_sources(
        plan(
            candidate(
                "youtube",
                SourceType.YOUTUBE,
                url="https://www.youtube.com/watch?v=abc",
            )
        ),
        context=run_context,
        registry=CollectorRegistry([YouTubeCollector(runner=runner)]),
    )

    assert result.failed == 1
    assert result.downloaded_bytes >= len(b"partial-media")
    download = calls[-1]
    assert download[download.index("--retries") + 1] == "0"
    assert download[download.index("--fragment-retries") + 1] == "0"
    metadata_command = calls[0]
    assert "--write-pages" in metadata_command
    assert metadata_command[metadata_command.index("--retries") + 1] == "0"


def test_extensionless_direct_audio_is_classified_by_head_before_get(tmp_path) -> None:
    audio_url = "https://cdn.example.test/download"
    requested: list[tuple[str, str]] = []

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            content=b"" if request.method == "HEAD" else b"ID3 audio",
            headers={"content-type": "audio/mpeg"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    result = collect_approved_sources(
        plan(candidate("podcast", SourceType.PODCAST, url=audio_url)),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.failed == 1
    assert requested == [("HEAD", audio_url)]


def test_failed_collection_retains_downloaded_byte_charge(tmp_path) -> None:
    page_url = "https://podcast.example.test/episode"

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><audio src='https://cdn.example.test/a.mp3'></audio></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    result = collect_approved_sources(
        plan(candidate("podcast", SourceType.PODCAST, url=page_url)),
        context=run_context,
        registry=CollectorRegistry([PodcastCollector()]),
    )

    assert result.failed == 1
    assert result.downloaded_bytes > 0
    assert run_context.downloaded_bytes_used == result.downloaded_bytes


def test_oversized_stream_retains_bytes_received_before_failure(tmp_path) -> None:
    page_url = "https://blog.example.test/article"

    def resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"12345"),
            headers={"content-type": "text/html", "transfer-encoding": "chunked"},
            request=request,
        )

    run_context = context(tmp_path)
    run_context.maximum_download_bytes = 4
    run_context.fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=resolver, minimum_interval=0
    )
    result = collect_approved_sources(
        plan(candidate("article", SourceType.WEB_ARTICLE, url=page_url)),
        context=run_context,
        registry=CollectorRegistry([WebCollector()]),
    )

    assert result.failed == 1
    assert result.downloaded_bytes == 5
    assert run_context.downloaded_bytes_used == 5


def test_youtube_metadata_exclusion_stops_before_captions_or_download(tmp_path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        Path(kwargs["cwd"], "youtube-page.dump").write_bytes(b"metadata-page")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {"id": "abc123", "duration": 42, "channel": "Prohibited Channel"}
            ),
            stderr="",
        )

    run_context = context(tmp_path)
    run_context.rules = RuleSet(
        [
            ExclusionRule(
                rule_id="blocked-channel",
                action="exclude",
                reason="test exclusion",
                channels=["Prohibited Channel"],
            )
        ]
    )
    source = candidate(
        "youtube",
        SourceType.YOUTUBE,
        url="https://www.youtube.com/watch?v=abc123",
    )

    result = collect_approved_sources(
        plan(source),
        context=run_context,
        registry=CollectorRegistry([YouTubeCollector(runner=runner)]),
    )

    assert result.collected == 1
    assert result.excluded == 1
    assert len(calls) == 1
    assert result.downloaded_bytes == len(b"metadata-page")
    assert "--write-pages" in calls[0]
    assert calls[0][calls[0].index("--retries") + 1] == "0"
    assert result.artifacts[0].original_metadata["channel"] == "Prohibited Channel"
    assert source.channel is None


def test_youtube_channel_id_exclusion_stops_before_download(tmp_path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "id": "abc123",
                    "duration": 42,
                    "channel": "Safe Display Name",
                    "channel_id": "UC_BLOCKED",
                }
            ),
            stderr="",
        )

    run_context = context(tmp_path)
    run_context.rules = RuleSet(
        [
            ExclusionRule(
                rule_id="blocked-channel-id",
                action="exclude",
                reason="test exclusion",
                channels=["UC_BLOCKED"],
            )
        ]
    )
    result = collect_approved_sources(
        plan(
            candidate(
                "youtube",
                SourceType.YOUTUBE,
                url="https://www.youtube.com/watch?v=abc123",
            )
        ),
        context=run_context,
        registry=CollectorRegistry([YouTubeCollector(runner=runner)]),
    )

    assert result.excluded == 1
    assert len(calls) == 1
    assert result.artifacts[0].original_metadata["channel_id"] == "UC_BLOCKED"


def test_post_metadata_review_pauses_media_until_explicit_rule_override(
    tmp_path,
) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "--dump-single-json" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"id": "abc123", "duration": 42, "channel": "Needs Review"}
                ),
                stderr="",
            )
        template = Path(command[command.index("-o") + 1])
        media = template.with_name("audio.m4a")
        media.write_bytes(b"youtube audio fixture")
        return subprocess.CompletedProcess(command, 0, stdout=f"{media}\n", stderr="")

    run_context = context(tmp_path)
    run_context.rules = RuleSet(
        [
            ExclusionRule(
                rule_id="review-channel",
                action="review",
                reason="human review required",
                channels=["Needs Review"],
            )
        ]
    )
    source = candidate(
        "youtube",
        SourceType.YOUTUBE,
        url="https://www.youtube.com/watch?v=abc123",
    )
    registry = CollectorRegistry([YouTubeCollector(runner=runner)])

    paused = collect_approved_sources(
        plan(source), context=run_context, registry=registry
    )
    source.override_rule_ids = ["review-channel"]
    resumed = collect_approved_sources(
        plan(source), context=run_context, registry=registry
    )

    assert paused.review_required == 1
    assert paused.media_seconds == 0
    assert resumed.collected == 1
    assert resumed.media_seconds == 42
    assert sum("--print" in call for call in calls) == 1


def test_youtube_collector_preserves_metadata_and_audio_without_captions(
    tmp_path,
) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "--dump-single-json" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"id": "abc123", "duration": 42}),
                stderr="",
            )
        template = Path(command[command.index("-o") + 1])
        media = template.with_name("audio.m4a")
        media.write_bytes(b"youtube audio fixture")
        return subprocess.CompletedProcess(
            command, 0, stdout=str(media) + "\n", stderr=""
        )

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    source = candidate(
        "youtube",
        SourceType.YOUTUBE,
        url="https://www.youtube.com/watch?v=abc123",
    )

    result = collect_approved_sources(
        plan(source),
        context=run_context,
        registry=CollectorRegistry([YouTubeCollector(runner=runner)]),
    )

    assert result.collected == 1
    assert any(item.collection_method == "yt_dlp_audio" for item in result.artifacts)
    assert not any(
        item.collection_method == "youtube_captions" for item in result.artifacts
    )
    assert any("--max-filesize" in command for command in calls)


def test_youtube_collector_rejects_over_budget_media_before_download(tmp_path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"id": "abc123", "duration": 61}),
            stderr="",
        )

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    source = candidate(
        "youtube",
        SourceType.YOUTUBE,
        url="https://www.youtube.com/watch?v=abc123",
    )

    result = collect_approved_sources(
        plan(source),
        context=run_context,
        registry=CollectorRegistry([YouTubeCollector(runner=runner)]),
    )

    assert result.failed == 1
    assert len(calls) == 1


def test_media_collection_limit_is_aggregate_across_sources(tmp_path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "--dump-single-json" in command:
            video_id = "one" if "one" in command[-1] else "two"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"id": video_id, "duration": 40}),
                stderr="",
            )
        template = Path(command[command.index("-o") + 1])
        media = template.with_name("audio.m4a")
        media.write_bytes(b"youtube audio fixture")
        return subprocess.CompletedProcess(command, 0, stdout=f"{media}\n", stderr="")

    run_context = context(tmp_path)
    run_context.maximum_media_seconds = 60
    result = collect_approved_sources(
        plan(
            candidate(
                "one",
                SourceType.YOUTUBE,
                url="https://www.youtube.com/watch?v=one",
            ),
            candidate(
                "two",
                SourceType.YOUTUBE,
                url="https://www.youtube.com/watch?v=two",
            ),
        ),
        context=run_context,
        registry=CollectorRegistry([YouTubeCollector(runner=runner)]),
    )

    assert result.collected == 1
    assert result.failed == 1
    assert result.media_seconds == 40
    assert sum("--dump-single-json" in call for call in calls) == 2
    assert sum("--print" in call for call in calls) == 1


def test_youtube_collector_rejects_lookalike_domains_without_running_tool(
    tmp_path,
) -> None:
    calls = []
    source = candidate(
        "youtube-lookalike",
        SourceType.YOUTUBE,
        url="https://evilyoutube.com/watch?v=abc123",
    )

    result = collect_approved_sources(
        plan(source),
        context=context(tmp_path),
        registry=CollectorRegistry(
            [YouTubeCollector(runner=lambda *args, **kwargs: calls.append(args))]
        ),
    )

    assert result.failed == 1
    assert calls == []

import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import vc_trace_collector.cli as cli_module
from vc_trace_collector.audit import BudgetExceeded
from vc_trace_collector.cli import create_app
from vc_trace_collector.config import RunConfig
from vc_trace_collector.discovery import SearchResult
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import ApprovalStatus, SourceType
from vc_trace_collector.pipeline import Pipeline
from vc_trace_collector.source_search import (
    build_source_queries,
    candidate_from_search_result,
)
from vc_trace_collector.storage import read_json, read_jsonl

FIXTURES = Path(__file__).parent / "fixtures"


def public_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class SearchFixture:
    provider_name = "agent-reach:yt-dlp"

    def __init__(self, *, cache_identity: str | None = None) -> None:
        self.queries: list[str] = []
        if cache_identity is not None:
            self.cache_identity = cache_identity

    def search(self, query: str, limit: int = 10):
        self.queries.append(query)
        return [
            SearchResult(
                url="https://www.youtube.com/watch?v=voice123",
                title="Michael Hyatt of BlueCat — investor interview",
                snippet="Michael Hyatt discusses investing at the Hyatt Family Office.",
                rank=1,
                query=query,
                provider=self.provider_name,
            )
        ]


def pipeline(tmp_path: Path, provider: SearchFixture) -> Pipeline:
    html = (FIXTURES / "michael_hyatt_profile.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=html,
            headers={"content-type": "text/html"},
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )
    return Pipeline(tmp_path, fetcher=fetcher, search_provider=provider)


def initialize(collector: Pipeline, tmp_path: Path) -> None:
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            output_dir=str(tmp_path),
            public_search_enabled=False,
        ),
    )


def test_build_source_queries_are_platform_specific() -> None:
    youtube = build_source_queries(
        "Michael Hyatt", ["Hyatt Family Office"], SourceType.YOUTUBE
    )
    podcasts = build_source_queries(
        "Michael Hyatt", ["Hyatt Family Office"], SourceType.PODCAST
    )

    assert youtube
    assert all("youtube" in query.casefold() for query in youtube)
    assert podcasts
    assert all("podcast" in query.casefold() for query in podcasts)
    assert set(youtube).isdisjoint(podcasts)


def test_search_source_appends_and_deduplicates_candidates(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)

    first = collector.search_source("michael-hyatt", SourceType.YOUTUBE)
    first_plan_id = collector._load_plan("michael-hyatt").plan_id
    second = collector.search_source("michael-hyatt", SourceType.YOUTUBE)

    assert first.added == 1
    assert second.added == 0
    assert second.updated == 1
    rows = read_jsonl(
        tmp_path / "michael-hyatt/discovery/source_candidates.jsonl"
    )
    videos = [row for row in rows if row["source_type"] == "youtube"]
    assert len(videos) == 1
    assert videos[0]["discovered_via"] == "agent-reach:yt-dlp"
    assert len(videos[0]["discovery_queries"]) == len(set(provider.queries))
    assert videos[0]["approval_status"] == ApprovalStatus.PENDING
    assert len(provider.queries) == 3  # second invocation reuses cached observations
    assert first_plan_id == collector._load_plan("michael-hyatt").plan_id
    observations = read_jsonl(
        tmp_path / "michael-hyatt/discovery/search_observations.jsonl"
    )
    assert observations[0]["url"] == "https://www.youtube.com/watch?v=voice123"
    assert observations[0]["rank"] == 1
    assert observations[0]["result_provider"] == "agent-reach:yt-dlp"


def test_search_cache_isolated_by_backend_identity(tmp_path) -> None:
    first = SearchFixture(cache_identity="composite:ddg:yt-dlp")
    collector = pipeline(tmp_path, first)
    initialize(collector, tmp_path)
    collector.search_source(
        "michael-hyatt", SourceType.YOUTUBE, maximum_queries=1
    )

    second = SearchFixture(cache_identity="composite:agent-reach:exa:yt-dlp")
    collector.search_source(
        "michael-hyatt",
        SourceType.YOUTUBE,
        maximum_queries=1,
        search_provider=second,
    )

    assert len(first.queries) == 1
    assert len(second.queries) == 1


def test_search_cache_remains_usable_when_backend_later_fails_preflight(
    tmp_path,
) -> None:
    class InitiallyAvailable(SearchFixture):
        def __init__(self) -> None:
            super().__init__(cache_identity="stable-backend")
            self.available = True

        def preflight(self, query: str):
            if self.available:
                return None
            return {
                "provider": self.provider_name,
                "query": query,
                "error": "backend unavailable",
                "provider_reached": False,
            }

    provider = InitiallyAvailable()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    first = collector.search_source(
        "michael-hyatt", SourceType.YOUTUBE, maximum_queries=1
    )
    provider.available = False

    second = collector.search_source(
        "michael-hyatt", SourceType.YOUTUBE, maximum_queries=1
    )

    assert first.failed == 0
    assert second.failed == 0
    assert len(provider.queries) == 1


def test_unavailable_backend_still_replays_later_cached_query(tmp_path) -> None:
    class ToggleSearch(SearchFixture):
        def __init__(self) -> None:
            super().__init__(cache_identity="stable-backend")
            self.available = True

        def preflight(self, query: str):
            if self.available:
                return None
            return {
                "provider": self.provider_name,
                "query": query,
                "error": "backend unavailable",
                "provider_reached": False,
            }

    cached_query = '"Michael Hyatt" cached YouTube'
    uncached_query = '"Michael Hyatt" unavailable YouTube'
    provider = ToggleSearch()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    collector.search_source(
        "michael-hyatt",
        SourceType.YOUTUBE,
        queries=[cached_query],
    )
    provider.available = False

    result = collector.search_source(
        "michael-hyatt",
        SourceType.YOUTUBE,
        queries=[uncached_query, cached_query],
    )

    assert result.failed == 1
    assert result.result_count == 1
    assert len(provider.queries) == 1
    observations = read_jsonl(
        tmp_path / "michael-hyatt/discovery/search_observations.jsonl"
    )
    assert observations[-1]["query"] == cached_query
    assert observations[-1]["cached"] is True


def test_search_source_preserves_existing_review_decision(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    collector.search_source("michael-hyatt", SourceType.YOUTUBE)
    plan = collector._load_plan("michael-hyatt")
    video = next(item for item in plan.candidates if item.source_type == "youtube")
    video.approval_status = ApprovalStatus.APPROVED
    collector._save_plan("michael-hyatt", plan)

    collector.search_source("michael-hyatt", SourceType.YOUTUBE)

    updated = collector._load_plan("michael-hyatt")
    video = next(item for item in updated.candidates if item.source_type == "youtube")
    assert video.approval_status == ApprovalStatus.APPROVED


def test_search_source_enriches_pending_candidate_with_channel_exclusion(
    tmp_path,
) -> None:
    class ChannelSearch(SearchFixture):
        def search(self, query: str, limit: int = 10):
            self.queries.append(query)
            return [
                SearchResult(
                    url="https://www.youtube.com/watch?v=voice123",
                    title="Michael Hyatt investor interview",
                    snippet="Michael Hyatt of BlueCat",
                    channel="The Pitch Show",
                    rank=1,
                    query=query,
                    provider=self.provider_name,
                )
            ]

    original = SearchFixture(cache_identity="old-search")
    collector = pipeline(tmp_path, original)
    initialize(collector, tmp_path)
    collector.search_source(
        "michael-hyatt", SourceType.YOUTUBE, maximum_queries=1
    )

    replacement = ChannelSearch(cache_identity="channel-aware-search")
    collector.search_source(
        "michael-hyatt",
        SourceType.YOUTUBE,
        maximum_queries=1,
        search_provider=replacement,
    )

    video = next(
        candidate
        for candidate in collector._load_plan("michael-hyatt").candidates
        if candidate.canonical_url
        == "https://www.youtube.com/watch?v=voice123"
    )
    assert video.channel == "The Pitch Show"
    assert video.approval_status == ApprovalStatus.REJECTED
    assert "leakage firewall" in video.decision_reason


def test_search_source_reserves_budget_before_backend_call(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            output_dir=str(tmp_path),
            public_search_enabled=False,
            search_operation_cost_usd=Decimal("0.02"),
            maximum_cost_usd=Decimal("0.01"),
        ),
    )

    with pytest.raises(BudgetExceeded):
        collector.search_source(
            "michael-hyatt", SourceType.YOUTUBE, maximum_queries=1
        )

    assert provider.queries == []


def test_search_source_updates_run_cost_summary(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    collector.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        config=RunConfig(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
            output_dir=str(tmp_path),
            public_search_enabled=False,
            search_operation_cost_usd=Decimal("0.02"),
            maximum_cost_usd=Decimal("1.00"),
        ),
    )

    collector.search_source(
        "michael-hyatt", SourceType.YOUTUBE, maximum_queries=1
    )

    summary = read_json(tmp_path / "michael-hyatt/run_summary.json")
    assert Decimal(summary["cost_usd"]) == Decimal("0.02")


def test_search_source_records_backend_diagnostics_as_failures(tmp_path) -> None:
    class BrokenSearch:
        provider_name = "agent-reach"

        def __init__(self) -> None:
            self.diagnostics = []

        def search(self, query: str, limit: int = 10):
            self.diagnostics.append(
                {
                    "provider": "agent-reach:exa",
                    "query": query,
                    "error": "mcporter is not installed",
                }
            )
            return []

    provider = BrokenSearch()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    original_plan_id = collector._load_plan("michael-hyatt").plan_id

    result = collector.search_source(
        "michael-hyatt", SourceType.WEB_ARTICLE, maximum_queries=1
    )

    assert result.failed == 1
    assert collector._load_plan("michael-hyatt").plan_id != original_plan_id
    observation = read_jsonl(
        tmp_path / "michael-hyatt/discovery/search_observations.jsonl"
    )[-1]
    assert observation["status"] == "failed"
    assert observation["error"] == "mcporter is not installed"


def test_search_source_preflight_failure_is_not_charged_and_stops(tmp_path) -> None:
    class UnavailableSearch:
        provider_name = "agent-reach:exa"

        def __init__(self) -> None:
            self.queries: list[str] = []

        def preflight(self, query: str):
            return {
                "provider": self.provider_name,
                "query": query,
                "error": "mcporter is not installed",
                "provider_reached": False,
            }

        def search(self, query: str, limit: int = 10):
            self.queries.append(query)
            return []

    provider = UnavailableSearch()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    original_queries = set(collector._load_plan("michael-hyatt").queries)

    result = collector.search_source(
        "michael-hyatt", SourceType.WEB_ARTICLE, maximum_queries=3
    )

    assert result.failed == 1
    assert result.errors == ["mcporter is not installed"]
    assert provider.queries == []
    costs = read_jsonl(tmp_path / "michael-hyatt/audit/costs.jsonl")
    assert not [row for row in costs if row["kind"] == "settlement"]
    observations = read_jsonl(
        tmp_path / "michael-hyatt/discovery/search_observations.jsonl"
    )
    assert len(observations) == 1
    assert observations[0]["status"] == "failed"
    plan = collector._load_plan("michael-hyatt")
    assert set(plan.queries) - original_queries == {observations[0]["query"]}


def test_unreachable_backend_releases_reservation_and_stops(tmp_path) -> None:
    class OfflineSearch:
        provider_name = "agent-reach:exa"

        def __init__(self) -> None:
            self.queries: list[str] = []
            self.diagnostics: list[dict] = []

        def search(self, query: str, limit: int = 10):
            self.queries.append(query)
            self.diagnostics.append(
                {
                    "provider": self.provider_name,
                    "query": query,
                    "error": "Agent Reach Exa backend is unreachable",
                    "provider_reached": False,
                }
            )
            return []

    provider = OfflineSearch()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)

    result = collector.search_source(
        "michael-hyatt", SourceType.WEB_ARTICLE, maximum_queries=3
    )

    assert result.failed == 1
    assert len(provider.queries) == 1
    kinds = [
        row["kind"]
        for row in read_jsonl(tmp_path / "michael-hyatt/audit/costs.jsonl")
    ]
    assert kinds == ["reservation", "release"]


def test_search_source_uses_explicit_queries_instead_of_generated_queries(
    tmp_path,
) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    queries = [
        '"Michael Hyatt" "The Pitch" YouTube',
        '"Michael Hyatt" BlueCat founder interview YouTube',
    ]

    result = collector.search_source(
        "michael-hyatt",
        SourceType.YOUTUBE,
        search_provider=provider,
        queries=queries,
    )

    assert result.queries == queries
    assert provider.queries == queries


def test_search_source_can_auditably_raise_search_operation_limit(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)

    collector.search_source(
        "michael-hyatt",
        SourceType.YOUTUBE,
        maximum_queries=1,
        maximum_search_operations=40,
    )

    config = read_json(tmp_path / "michael-hyatt/config_snapshot.json")
    assert config["maximum_search_operations"] == 40
    events = read_jsonl(tmp_path / "michael-hyatt/audit/events.jsonl")
    update = next(
        event
        for event in events
        if event["stage"] == "configuration"
        and event["details"].get("stage") == "source_search"
    )
    assert update["details"]["changes"]["maximum_search_operations"] == {
        "from": 20,
        "to": 40,
    }


def test_search_source_refuses_to_lower_search_operation_limit(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)

    with pytest.raises(ValueError, match="cannot be lower"):
        collector.search_source(
            "michael-hyatt",
            SourceType.YOUTUBE,
            maximum_search_operations=10,
        )


def test_youtube_channel_is_preserved_and_pitch_show_is_rejected(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    identity = collector._load_identity("michael-hyatt")
    result = SearchResult(
        url="https://www.youtube.com/watch?v=pitch123",
        title="Michael Hyatt pitches and invests",
        snippet="Michael Hyatt of BlueCat",
        channel="The Pitch Show",
        rank=1,
        query='"Michael Hyatt" "The Pitch" YouTube',
        provider="yt-dlp",
    )

    candidate = candidate_from_search_result(
        identity=identity,
        source_type=SourceType.YOUTUBE,
        result=result,
        rules=collector.rules,
    )

    assert candidate.channel == "The Pitch Show"
    assert candidate.approval_status == ApprovalStatus.REJECTED
    assert "leakage firewall" in candidate.decision_reason


def test_cli_search_source_runs_one_platform(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    app = create_app(lambda _output_dir: collector)

    result = CliRunner().invoke(
        app,
        [
            "search-source",
            "--investor",
            "michael-hyatt",
            "--source",
            "youtube",
            "--max-queries",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Added 1 youtube candidate" in result.stdout
    assert len(provider.queries) == 1


def test_cli_search_source_can_select_agent_reach_backends(
    tmp_path, monkeypatch
) -> None:
    original = SearchFixture()
    agent_reach = SearchFixture()
    collector = pipeline(tmp_path, original)
    initialize(collector, tmp_path)
    monkeypatch.setattr(
        cli_module,
        "agent_reach_public_search_provider",
        lambda: agent_reach,
    )
    app = create_app(lambda _output_dir: collector)

    result = CliRunner().invoke(
        app,
        [
            "search-source",
            "--investor",
            "michael-hyatt",
            "--source",
            "youtube",
            "--backend",
            "agent-reach",
            "--max-queries",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert original.queries == []
    assert len(agent_reach.queries) == 1


def test_cli_search_source_accepts_repeatable_queries_and_limit_increase(
    tmp_path,
) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    app = create_app(lambda _output_dir: collector)

    result = CliRunner().invoke(
        app,
        [
            "search-source",
            "--investor",
            "michael-hyatt",
            "--source",
            "youtube",
            "--query",
            '"Michael Hyatt" "The Pitch" YouTube',
            "--query",
            '"Michael Hyatt" BlueCat YouTube',
            "--max-search-operations",
            "40",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert provider.queries == [
        '"Michael Hyatt" "The Pitch" YouTube',
        '"Michael Hyatt" BlueCat YouTube',
    ]


def test_cli_search_source_prints_actionable_backend_failure(tmp_path) -> None:
    class UnavailableSearch(SearchFixture):
        def preflight(self, query: str):
            return {
                "provider": self.provider_name,
                "query": query,
                "error": "mcporter is not installed",
                "provider_reached": False,
            }

    provider = UnavailableSearch()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    app = create_app(lambda _output_dir: collector)

    result = CliRunner().invoke(
        app,
        [
            "search-source",
            "--investor",
            "michael-hyatt",
            "--source",
            "web_article",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Search stopped: mcporter is not installed" in result.stdout


def test_cli_list_sources_exposes_candidate_ids_and_urls(tmp_path) -> None:
    provider = SearchFixture()
    collector = pipeline(tmp_path, provider)
    initialize(collector, tmp_path)
    collector.search_source("michael-hyatt", SourceType.YOUTUBE, maximum_queries=1)
    app = create_app(lambda _output_dir: collector)

    result = CliRunner().invoke(
        app,
        [
            "list-sources",
            "--investor",
            "michael-hyatt",
            "--source",
            "youtube",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "candidate:" in result.stdout
    assert "https://www.youtube.com/watch?v=voice123" in result.stdout
    assert "pending" in result.stdout

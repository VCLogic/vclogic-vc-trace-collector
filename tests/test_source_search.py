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
from vc_trace_collector.source_search import build_source_queries
from vc_trace_collector.storage import read_jsonl

FIXTURES = Path(__file__).parent / "fixtures"


def public_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class SearchFixture:
    provider_name = "agent-reach:yt-dlp"

    def __init__(self) -> None:
        self.queries: list[str] = []

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

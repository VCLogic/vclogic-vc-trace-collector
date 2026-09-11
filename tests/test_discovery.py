from pathlib import Path
import socket

import httpx

from vc_trace_collector.discovery import (
    DiscoveryService,
    SearchResult,
    generate_queries,
)
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import ApprovalStatus, MaterialRole, SourceType


FIXTURES = Path(__file__).parent / "fixtures"


def public_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def profile_fetcher() -> Fetcher:
    html = (FIXTURES / "michael_hyatt_profile.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, headers={"content-type": "text/html"}, request=request)

    return Fetcher(
        transport=httpx.MockTransport(handler), resolver=public_resolver, minimum_interval=0
    )


class StaticSearch:
    provider_name = "fixture-search"

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if "YouTube" not in query:
            return []
        return [
            SearchResult(
                url="https://www.youtube.com/watch?v=voice123",
                title="Michael Hyatt of BlueCat interview",
                snippet="Michael Hyatt discusses building BlueCat.",
                rank=1,
                query=query,
                provider=self.provider_name,
            )
        ]


def test_known_profile_resolves_bluecat_michael_not_author() -> None:
    result = DiscoveryService(fetcher=profile_fetcher()).discover(
        name="Michael Hyatt",
        firm=None,
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    assert "BlueCat" in {affiliation.firm for affiliation in result.identity.affiliations}
    assert any("Michael S. Hyatt" in item for item in result.identity.competing_hypotheses)
    assert result.source_plan.requires_review is True

    profile = next(
        candidate
        for candidate in result.source_plan.candidates
        if candidate.canonical_url.endswith("/investors/michael-hyatt")
    )
    assert profile.material_role == MaterialRole.IDENTITY_EVIDENCE
    assert profile.approval_status == ApprovalStatus.REJECTED
    assert "leakage" in (profile.decision_reason or "").casefold()


def test_discovery_proposes_voice_queries() -> None:
    queries = generate_queries("Michael Hyatt", ["BlueCat"])

    assert '"Michael Hyatt" BlueCat interview' in queries
    assert any("podcast" in query for query in queries)
    assert any("YouTube" in query for query in queries)


def test_youtube_search_result_becomes_reference_voice_candidate() -> None:
    result = DiscoveryService(fetcher=profile_fetcher(), search_provider=StaticSearch()).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    youtube = next(
        candidate
        for candidate in result.source_plan.candidates
        if candidate.source_type == SourceType.YOUTUBE
    )
    assert youtube.material_role == MaterialRole.REFERENCE_VOICE
    assert youtube.discovery_queries
    assert result.reference_voice_candidates[0].source_candidate_id == youtube.candidate_id


def test_duplicate_search_results_are_merged_with_all_queries() -> None:
    result = DiscoveryService(fetcher=profile_fetcher(), search_provider=StaticSearch()).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    youtube = [
        candidate
        for candidate in result.source_plan.candidates
        if candidate.source_type == SourceType.YOUTUBE
    ]

    assert len(youtube) == 1
    assert len(youtube[0].discovery_queries) >= 1

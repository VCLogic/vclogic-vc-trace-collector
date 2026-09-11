import json
import socket
from pathlib import Path

import httpx

from vc_trace_collector.discovery import (
    DiscoveryService,
    OpenAICompatibleDiscoveryProvider,
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
        content = html
        if "spotify.com" in request.url.host:
            content = (
                b"<html><head><title>Michael Hyatt BlueCat interview podcast</title></head>"
                b"<body>A conversation with investor Michael Hyatt, co-founder of BlueCat.</body>"
                b"</html>"
            )
        return httpx.Response(
            200, content=content, headers={"content-type": "text/html"}, request=request
        )

    return Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )


class StaticSearch:
    provider_name = "fixture-search"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        self.queries.append(query)
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

    assert "BlueCat" in {
        affiliation.firm for affiliation in result.identity.affiliations
    }
    assert any(
        "Michael S. Hyatt" in item for item in result.identity.competing_hypotheses
    )
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
    result = DiscoveryService(
        fetcher=profile_fetcher(), search_provider=StaticSearch()
    ).discover(
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
    assert (
        result.reference_voice_candidates[0].source_candidate_id == youtube.candidate_id
    )


def test_duplicate_search_results_are_merged_with_all_queries() -> None:
    result = DiscoveryService(
        fetcher=profile_fetcher(), search_provider=StaticSearch()
    ).discover(
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


def test_discovery_caps_total_search_operations() -> None:
    search = StaticSearch()
    DiscoveryService(
        fetcher=profile_fetcher(),
        search_provider=search,
        maximum_search_operations=2,
    ).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    assert len(search.queries) == 2


def test_operator_supplied_podcast_url_becomes_voice_candidate() -> None:
    podcast_url = (
        "https://podcasters.spotify.com/pod/show/example/episodes/michael-hyatt"
    )
    result = DiscoveryService(fetcher=profile_fetcher()).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[podcast_url],
    )

    podcast = next(
        candidate
        for candidate in result.source_plan.candidates
        if candidate.url == podcast_url
    )
    assert podcast.source_type == SourceType.PODCAST
    assert podcast.material_role == MaterialRole.REFERENCE_VOICE
    assert podcast.approval_status == ApprovalStatus.PENDING
    assert any(
        voice.source_candidate_id == podcast.candidate_id
        for voice in result.reference_voice_candidates
    )


def test_openai_compatible_adapter_sends_portable_json_message(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "canonical_name": "Michael Hyatt",
                                    "aliases": [],
                                    "affiliations": [],
                                    "competing_hypotheses": [],
                                    "candidate_roles": {},
                                }
                            )
                        }
                    }
                ]
            }

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("vc_trace_collector.discovery.httpx.post", fake_post)
    OpenAICompatibleDiscoveryProvider(
        "https://provider.example/v1/chat/completions", "secret", "model"
    ).refine(name="Michael Hyatt", evidence=[], candidates=[])

    user_message = captured["json"]["messages"][1]["content"]
    assert json.loads(user_message)["name"] == "Michael Hyatt"


def test_source_plan_id_changes_when_candidate_set_changes() -> None:
    first = DiscoveryService(fetcher=profile_fetcher()).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    second = DiscoveryService(fetcher=profile_fetcher()).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[
            "https://podcasters.spotify.com/pod/show/example/episodes/michael-hyatt"
        ],
    )

    assert first.source_plan.plan_id != second.source_plan.plan_id

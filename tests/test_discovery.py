import json
import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from vc_trace_collector.audit import BudgetExceeded, BudgetLedger
from vc_trace_collector.discovery import (
    DiscoveryService,
    OpenAICompatibleDiscoveryProvider,
    SearchResult,
    generate_queries,
)
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import ApprovalStatus, MaterialRole, SourceType
from vc_trace_collector.public_search import CompositeSearchProvider

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


def test_youtube_search_result_becomes_target_speech_and_voice_candidate() -> None:
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
    assert youtube.material_role == MaterialRole.SPOKEN_BY_TARGET
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


def test_namesake_result_without_target_affiliation_stays_below_auto_approval() -> None:
    class NamesakeSearch:
        provider_name = "fixture-search"

        def search(self, query: str, limit: int = 10):
            if "YouTube" not in query:
                return []
            return [
                SearchResult(
                    url="https://www.youtube.com/watch?v=correct",
                    title="Michael Hyatt BlueCat interview",
                    snippet="The Hyatt Family Office investor discusses BlueCat.",
                    rank=1,
                    query=query,
                    provider=self.provider_name,
                ),
                SearchResult(
                    url="https://www.youtube.com/watch?v=namesake",
                    title="The Double Win with Michael Hyatt",
                    snippet="A productivity and leadership podcast from Full Focus.",
                    rank=2,
                    query=query,
                    provider=self.provider_name,
                ),
            ]

    result = DiscoveryService(
        fetcher=profile_fetcher(), search_provider=NamesakeSearch()
    ).discover(
        name="Michael Hyatt",
        firm="Hyatt Family Office",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    by_url = {
        candidate.canonical_url: candidate
        for candidate in result.source_plan.candidates
    }

    assert (
        by_url["https://www.youtube.com/watch?v=correct"].identity_confidence.score
        >= 0.8
    )
    assert (
        by_url["https://www.youtube.com/watch?v=namesake"].identity_confidence.score
        < 0.8
    )


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


def test_search_provider_reserves_monetary_budget_before_each_call(tmp_path) -> None:
    search = StaticSearch()
    ledger = BudgetLedger(tmp_path / "costs.jsonl", Decimal("0.10"))

    with pytest.raises(BudgetExceeded):
        DiscoveryService(
            fetcher=profile_fetcher(),
            search_provider=search,
            maximum_search_operations=2,
            budget=ledger,
            search_operation_cost_usd=Decimal("0.06"),
        ).discover(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        )

    assert len(search.queries) == 1


def test_failed_discovery_provider_call_is_conservatively_settled(tmp_path) -> None:
    class FailingProvider:
        provider_name = "fixture"
        model_name = "fixture-model"

        def refine(self, **kwargs):
            raise RuntimeError("provider failed after dispatch")

    ledger = BudgetLedger(tmp_path / "costs.jsonl", Decimal("1.00"))
    with pytest.raises(RuntimeError, match="after dispatch"):
        DiscoveryService(
            fetcher=profile_fetcher(),
            discovery_provider=FailingProvider(),
            budget=ledger,
            discovery_operation_cost_usd=Decimal("0.25"),
        ).discover(
            name="Michael Hyatt",
            known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        )

    assert ledger.provider_operations == 1
    assert ledger.spent == Decimal("0.25")


def test_search_adapter_diagnostics_are_preserved_in_provider_operations() -> None:
    class FailingWeb:
        provider_name = "fixture-web"

        def search(self, query: str, limit: int = 10):
            raise RuntimeError("private provider detail")

    class EmptyYoutube:
        provider_name = "fixture-youtube"

        def search(self, query: str, limit: int = 10):
            return []

    search = CompositeSearchProvider(web=FailingWeb(), youtube=EmptyYoutube())
    result = DiscoveryService(
        fetcher=profile_fetcher(), search_provider=search
    ).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )

    failures = [
        operation
        for operation in result.provider_operations
        if operation.get("diagnostics")
    ]
    assert failures
    assert failures[0]["diagnostics"][0]["provider"] == "fixture-web"
    assert failures[0]["diagnostics"][0]["error"] == "RuntimeError"
    assert "private provider detail" not in str(failures)


def test_operator_supplied_podcast_url_becomes_target_speech_and_voice_candidate() -> (
    None
):
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
    assert podcast.material_role == MaterialRole.SPOKEN_BY_TARGET
    assert podcast.approval_status == ApprovalStatus.PENDING
    assert any(
        voice.source_candidate_id == podcast.candidate_id
        for voice in result.reference_voice_candidates
    )


def test_direct_media_url_is_planned_without_downloading_during_discovery() -> None:
    direct_url = "https://cdn.example.test/michael-hyatt.mp3"
    requested: list[str] = []
    profile = (FIXTURES / "michael_hyatt_profile.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == direct_url:
            raise AssertionError("direct media must not be fetched during discovery")
        return httpx.Response(200, content=profile, request=request)

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )
    result = DiscoveryService(fetcher=fetcher).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[direct_url],
    )

    media = next(
        item for item in result.source_plan.candidates if item.url == direct_url
    )
    assert media.material_role == "reference_voice"
    assert requested == ["https://www.thepitch.show/investors/michael-hyatt"]


def test_embedded_audio_page_is_classified_as_target_podcast_source() -> None:
    page_url = "https://tanktalks.substack.com/p/michael-hyatt"

    def handler(request: httpx.Request) -> httpx.Response:
        content = (FIXTURES / "michael_hyatt_profile.html").read_bytes()
        if request.url.host == "tanktalks.substack.com":
            content = (
                b"<html><head><title>Interview with Michael Hyatt</title></head>"
                b"<body><main>Michael Hyatt co-founder of BlueCat interview."
                b"<audio src='https://cdn.example.test/voice.mp3'></audio>"
                b"</main></body></html>"
            )
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "text/html"},
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )
    result = DiscoveryService(fetcher=fetcher).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        source_urls=[page_url],
    )

    source = next(
        item for item in result.source_plan.candidates if item.url == page_url
    )
    assert source.source_type == SourceType.PODCAST
    assert source.material_role == MaterialRole.SPOKEN_BY_TARGET


def test_openai_compatible_adapter_sends_portable_json_message(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
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
                ],
            }

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("vc_trace_collector.discovery.httpx.post", fake_post)
    provider = OpenAICompatibleDiscoveryProvider(
        "https://provider.example/v1/chat/completions", "secret", "model"
    )
    provider.refine(name="Michael Hyatt", evidence=[], candidates=[])

    user_message = captured["json"]["messages"][1]["content"]
    assert json.loads(user_message)["name"] == "Michael Hyatt"
    assert provider.last_usage == {"input_tokens": 123, "output_tokens": 45}


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


def test_supplied_file_is_added_to_reviewable_plan(tmp_path) -> None:
    supplied = tmp_path / "michael-hyatt-notes.txt"
    supplied.write_text("First-person public notes supplied by the operator.")

    result = DiscoveryService(fetcher=profile_fetcher()).discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        supplied_files=[supplied],
        supplied_role=MaterialRole.AUTHORED_BY_TARGET,
    )

    candidate = next(
        item
        for item in result.source_plan.candidates
        if item.source_type == SourceType.SUPPLIED
    )
    assert candidate.url == supplied.resolve().as_uri()
    assert candidate.material_role == MaterialRole.AUTHORED_BY_TARGET
    assert candidate.approval_status == ApprovalStatus.PENDING

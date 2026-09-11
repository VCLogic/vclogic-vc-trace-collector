from pathlib import Path
import socket

import httpx
import pytest

from vc_trace_collector.extract import extract_feed, extract_page
from vc_trace_collector.fetch import FetchTooLarge, Fetcher
from vc_trace_collector.policy import UnsafeUrl


FIXTURES = Path(__file__).parent / "fixtures"


def public_resolver(host: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_extracts_identity_from_michael_hyatt_profile() -> None:
    html = (FIXTURES / "michael_hyatt_profile.html").read_bytes()
    page = extract_page(html, "https://www.thepitch.show/investors/michael-hyatt")

    assert page.title == "Michael Hyatt"
    assert "BlueCat" in page.text
    assert "Unrelated navigation" not in page.text
    assert page.canonical_url == "https://www.thepitch.show/investors/michael-hyatt"
    assert "https://www.thepitch.show/episodes/example" in page.links


def test_feed_entries_preserve_author_and_source() -> None:
    entries = extract_feed((FIXTURES / "feed.xml").read_bytes(), "https://example.test/feed")

    assert entries[0].author == "Michael Hyatt"
    assert entries[0].canonical_url == "https://example.test/durable"
    assert entries[0].discovered_from == "https://example.test/feed"


def test_fetcher_records_public_response_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "etag": '"abc"'},
            content=b"<main>evidence</main>",
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=public_resolver, minimum_interval=0
    )
    result = fetcher.fetch("https://example.test/evidence")

    assert result.status_code == 200
    assert result.content == b"<main>evidence</main>"
    assert result.headers["etag"] == '"abc"'


def test_fetcher_rejects_redirect_to_private_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"}, request=request)

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), resolver=public_resolver, minimum_interval=0
    )
    with pytest.raises(UnsafeUrl):
        fetcher.fetch("https://example.test/redirect")


def test_fetcher_rejects_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345", request=request)

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        maximum_response_bytes=4,
        minimum_interval=0,
    )
    with pytest.raises(FetchTooLarge):
        fetcher.fetch("https://example.test/large")

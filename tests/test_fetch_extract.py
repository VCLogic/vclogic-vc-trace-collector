import socket
from pathlib import Path

import httpx
import pytest

from vc_trace_collector.extract import extract_feed, extract_page
from vc_trace_collector.fetch import Fetcher, FetchTooLarge
from vc_trace_collector.policy import UnsafeUrl

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize('known_size', [True, False])
def test_http_progress_reports_streamed_bytes(monkeypatch, known_size):
    from vc_trace_collector import fetch
    events = []
    monkeypatch.setattr(fetch, 'download_bytes', lambda done, total: events.append((done, total)))

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'abc'
            yield b'def'

    with Fetcher(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'content-length': '6'} if known_size else {}, stream=Stream())),
        resolver=public_resolver, minimum_interval=0) as fetcher:
        assert fetcher.fetch('https://example.test/file').content == b'abcdef'
    total = 6 if known_size else None
    assert events[-2:] == [(3, total), (6, total)]


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
    entries = extract_feed(
        (FIXTURES / "feed.xml").read_bytes(), "https://example.test/feed"
    )

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
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )
    result = fetcher.fetch("https://example.test/evidence")

    assert result.status_code == 200
    assert result.content == b"<main>evidence</main>"
    assert result.headers["etag"] == '"abc"'


def test_fetcher_rejects_redirect_to_private_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/private"}, request=request
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
    )
    with pytest.raises(UnsafeUrl):
        fetcher.fetch("https://example.test/redirect")


def test_fetcher_rejects_dns_answer_change_during_request() -> None:
    calls = 0

    def resolver(host: str, port: int):
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls < 3 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"public", request=request)

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=resolver,
        minimum_interval=0,
    )

    with pytest.raises(UnsafeUrl, match="non-public|changed"):
        fetcher.fetch("https://example.test/public")


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


def test_fetcher_supports_a_bounded_per_request_media_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345", request=request)

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        maximum_response_bytes=4,
        minimum_interval=0,
    )

    result = fetcher.fetch("https://example.test/media.mp3", maximum_bytes=8)

    assert result.content == b"12345"


def test_html_extraction_preserves_json_ld_before_removing_scripts() -> None:
    page = extract_page(
        b"""<html><head><script type='application/ld+json'>
        {"@type":"Article","author":{"name":"Michael Hyatt"}}
        </script></head><body><main><h1>Article</h1><p>Text</p></main></body></html>""",
        "https://example.test/article",
    )

    assert page.metadata["json_ld"][0]["@type"] == "Article"


def test_empty_heading_falls_back_to_open_graph_title() -> None:
    page = extract_page(
        b"<html><head><meta property='og:title' content='Michael Hyatt interview'>"
        b"</head><body><h1></h1><main>Interview</main></body></html>",
        "https://example.test/interview",
    )

    assert page.title == "Michael Hyatt interview"


def test_retry_after_is_capped() -> None:
    responses = 0
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal responses
        responses += 1
        return httpx.Response(
            429 if responses == 1 else 200,
            headers={"retry-after": "999999"},
            content=b"ok",
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
        maximum_retry_delay=2,
        sleep=sleeps.append,
    )

    result = fetcher.fetch("https://example.test/retry")

    assert result.content == b"ok"
    assert result.transferred_bytes == 4
    assert sleeps == [2]


def test_retry_attempts_share_one_aggregate_byte_limit() -> None:
    responses = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal responses
        responses += 1
        return httpx.Response(
            429 if responses == 1 else 200,
            stream=httpx.ByteStream(b"12"),
            headers={"transfer-encoding": "chunked"},
            request=request,
        )

    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
        minimum_interval=0,
        maximum_retry_delay=0,
    )

    with pytest.raises(FetchTooLarge) as caught:
        fetcher.fetch("https://example.test/retry", maximum_bytes=3)

    assert caught.value.downloaded_bytes == 4

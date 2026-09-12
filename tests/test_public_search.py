import json
from types import SimpleNamespace

from vc_trace_collector.discovery import SearchResult
from vc_trace_collector.public_search import (
    CompositeSearchProvider,
    DdgSearchProvider,
    YtDlpSearchProvider,
)


def test_ddg_normalizes_and_bounds_public_results() -> None:
    calls: list[tuple[str, int]] = []

    def search(query: str, *, max_results: int):
        calls.append((query, max_results))
        return [
            {
                "href": f"https://example.test/{index}",
                "title": f"Michael Hyatt result {index}",
                "body": "BlueCat and Hyatt Family Office",
            }
            for index in range(4)
        ]

    provider = DdgSearchProvider(search=search)
    results = provider.search('"Michael Hyatt" BlueCat', limit=2)

    assert calls == [('"Michael Hyatt" BlueCat', 2)]
    assert len(results) == 2
    assert results[0] == SearchResult(
        url="https://example.test/0",
        title="Michael Hyatt result 0",
        snippet="BlueCat and Hyatt Family Office",
        rank=1,
        query='"Michael Hyatt" BlueCat',
        provider="ddg",
    )


def test_ytdlp_uses_shell_safe_arguments_and_normalizes_watch_urls() -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(command: list[str], **kwargs):
        calls.append((command, kwargs))
        rows = [
            {
                "id": "abc123",
                "title": "Michael Hyatt BlueCat interview",
                "description": "Michael Hyatt discusses investing.",
                "channel": "Example Channel",
            },
            {
                "id": "def456",
                "webpage_url": "https://www.youtube.com/watch?v=def456",
                "title": "Another Michael Hyatt conversation",
                "description": "Hyatt Family Office",
                "uploader": "Another Channel",
            },
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(row) for row in rows),
            stderr="",
        )

    provider = YtDlpSearchProvider(runner=runner, timeout=17)
    results = provider.search('"Michael Hyatt" BlueCat YouTube', limit=2)

    command, kwargs = calls[0]
    assert isinstance(command, list)
    assert command == [
        "yt-dlp",
        "--dump-json",
        "--flat-playlist",
        "--no-download",
        "--playlist-end",
        "2",
        'ytsearch2:"Michael Hyatt" BlueCat YouTube',
    ]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 17
    assert results[0].url == "https://www.youtube.com/watch?v=abc123"
    assert results[0].provider == "yt-dlp"
    assert results[1].url == "https://www.youtube.com/watch?v=def456"


def test_ytdlp_ignores_malformed_rows_and_reports_command_failure() -> None:
    outputs = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout='not-json\n{"id":"ok","title":"Valid"}',
                stderr="",
            ),
            SimpleNamespace(returncode=1, stdout="", stderr="blocked by remote"),
        ]
    )
    provider = YtDlpSearchProvider(runner=lambda *args, **kwargs: next(outputs))

    assert [item.url for item in provider.search("first YouTube", limit=5)] == [
        "https://www.youtube.com/watch?v=ok"
    ]
    assert provider.search("second YouTube", limit=5) == []
    assert provider.diagnostics[-1]["provider"] == "yt-dlp"
    assert provider.diagnostics[-1]["error"] == "search command failed"


def test_composite_routes_queries_and_isolates_provider_failures() -> None:
    class Web:
        provider_name = "web"

        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str, limit: int = 10):
            self.queries.append(query)
            if "broken" in query:
                raise RuntimeError("credential=do-not-record")
            return [
                SearchResult(
                    url="https://example.test/interview",
                    title="Michael Hyatt interview",
                    snippet="BlueCat",
                    rank=1,
                    query=query,
                    provider=self.provider_name,
                )
            ]

    class YouTube(Web):
        provider_name = "youtube"

    web = Web()
    youtube = YouTube()
    provider = CompositeSearchProvider(web=web, youtube=youtube)

    web_results = provider.search("Michael Hyatt interview")
    youtube_results = provider.search("Michael Hyatt YouTube")
    failed_results = provider.search("Michael Hyatt broken interview")

    assert len(web_results) == 1
    assert len(youtube_results) == 1
    assert failed_results == []
    assert web.queries == ["Michael Hyatt interview", "Michael Hyatt broken interview"]
    assert youtube.queries == ["Michael Hyatt YouTube"]
    assert provider.diagnostics == [
        {
            "provider": "web",
            "query": "Michael Hyatt broken interview",
            "error": "RuntimeError",
        }
    ]

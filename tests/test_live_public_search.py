"""Opt-in checks against public, credential-free discovery services."""

import pytest

from vc_trace_collector.public_search import DdgSearchProvider, YtDlpSearchProvider


@pytest.mark.live
def test_public_search_finds_michael_hyatt_web_and_youtube_results() -> None:
    try:
        web_results = DdgSearchProvider().search(
            '"Michael Hyatt" "Hyatt Family Office" interview', limit=5
        )
    except ModuleNotFoundError:
        raise
    except Exception as error:
        pytest.skip(
            f"public web search blocked in this environment: {type(error).__name__}"
        )

    youtube = YtDlpSearchProvider(timeout=120)
    youtube_results = youtube.search(
        '"Michael Hyatt" BlueCat Hyatt Family Office', limit=5
    )
    if (
        not youtube_results
        and youtube.diagnostics
        and youtube.diagnostics[-1]["error"] != "yt-dlp is not installed"
    ):
        pytest.skip(
            "public YouTube search blocked in this environment: "
            f"{youtube.diagnostics[-1]['error']}"
        )

    assert web_results
    assert youtube_results
    assert all("youtube.com/watch" in result.url for result in youtube_results)

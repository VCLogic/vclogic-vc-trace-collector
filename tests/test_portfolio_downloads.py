import json
import socket
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from vc_trace_collector import cli
from vc_trace_collector.discovery import SearchResult
from vc_trace_collector.fetch import Fetcher
from vc_trace_collector.models import Confidence, ResolvedIdentity
from vc_trace_collector.portfolio import PortfolioOptions, PortfolioService
from vc_trace_collector.storage import read_jsonl, write_json


def identity():
    return ResolvedIdentity(
        slug="jane-doe",
        canonical_name="Jane Doe",
        identity_confidence=Confidence(score=0.9, method="fixture", version="1"),
    )


class Search:
    provider_name = "fixture"

    def __init__(self):
        self.calls = []
        self.diagnostics = []

    def search(self, query, limit=10):
        self.calls.append(query)
        return [
            SearchResult(
                url="https://www.thepitch.show/deal",
                title="Jane Doe invests",
                snippet="Jane Doe invested in Acme.",
                query=query,
                provider="fixture",
                rank=1,
            )
        ]


def fetcher(**kwargs):
    return Fetcher(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                text="<article>Jane Doe invested in Acme. Acme makes accounting software.</article>",
            )
        ),
        resolver=lambda h, p: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))
        ],
        minimum_interval=0,
    )


def test_download_only_handoff_retains_pitch_and_resumes(tmp_path):
    search = Search()
    with fetcher() as fetch:
        service = PortfolioService(
            tmp_path, identity(), search, fetch, PortfolioOptions(max_searches=2)
        )
        report = service.run()
        assert report["documents"] == 1
        assert report["extraction_enabled"] is False
        documents = read_jsonl(tmp_path / "portfolio/documents.jsonl")
        assert documents[0]["source_url"] == "https://www.thepitch.show/deal"
        assert "Acme makes accounting software" in documents[0]["text"]
        assert documents[0]["content_sha256"]
        assert not (tmp_path / "portfolio/portfolio.jsonl").exists()
        assert not (tmp_path / "portfolio/assessment_schema.json").exists()
        handoff = json.loads((tmp_path / "portfolio/handoff.json").read_text())
        assert handoff["documents"] == "documents.jsonl"
        before = len(search.calls)
        assert service.run()["documents"] == 1
        assert len(search.calls) == before


@pytest.mark.parametrize("legacy_flag", [[], ["--collect-only"]])
def test_cli_needs_no_llm_and_preserves_legacy_flag(tmp_path, monkeypatch, legacy_flag):
    from vc_trace_collector import fetch as fetch_module

    workspace = tmp_path / "jane-doe"
    write_json(workspace / "identity/resolved_identity.json", identity())
    monkeypatch.setattr(cli, "agent_reach_public_search_provider", Search)
    monkeypatch.setattr(fetch_module, "Fetcher", fetcher)
    # An invalid configured model endpoint must never be consulted.
    monkeypatch.setenv("VC_TRACE_LLM_ENDPOINT", "https://must-not-be-called.invalid")
    monkeypatch.setenv("VC_TRACE_PORTFOLIO_MODEL", "must-not-be-used")
    result = CliRunner().invoke(
        cli.create_app(),
        [
            "portfolio",
            "--investor",
            "jane-doe",
            "--output-dir",
            str(tmp_path),
            "--max-search-operations",
            "1",
            *legacy_flag,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "downloaded" in result.output.lower()
    assert "documents.jsonl" in result.output
    assert "assessment-file" not in result.output


def test_extraction_flags_and_provider_are_removed():
    help_result = CliRunner().invoke(cli.create_app(), ["portfolio", "--help"])
    assert "--model" not in help_result.output
    assert not Path("src/vc_trace_collector/portfolio_provider.py").exists()
    result = CliRunner().invoke(
        cli.create_app(),
        ["portfolio", "--investor", "jane-doe", "--assessment-file", "x.jsonl"],
    )
    assert result.exit_code == 2


def test_legacy_outputs_are_not_modified(tmp_path):
    legacy = tmp_path / "portfolio/portfolio.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"legacy":"preserve"}\n')
    with fetcher() as fetch:
        PortfolioService(
            tmp_path, identity(), Search(), fetch, PortfolioOptions(max_searches=1)
        ).run()
    assert legacy.read_text() == '{"legacy":"preserve"}\n'


def test_corrupt_cache_is_reported_and_explicit_refresh_recovers(tmp_path):
    with fetcher() as fetch:
        service = PortfolioService(
            tmp_path, identity(), Search(), fetch, PortfolioOptions(max_searches=1)
        )
        service.run()
        source = next((tmp_path / "portfolio/sources").glob("*.json"))
        row = json.loads(source.read_text())
        row["metadata_sha256"] = "bad"
        source.write_text(json.dumps(row))
        assert service.run()["status"] == "partial"
        service.options = PortfolioOptions(max_searches=1, refresh=True)
        assert service.run()["documents"] == 1
    assert list((tmp_path / "portfolio/cache_history").rglob("*.json"))


def test_backend_cache_identity_is_preserved(tmp_path):
    first, second = Search(), Search()
    first.cache_identity, second.cache_identity = "one", "two"
    with fetcher() as fetch:
        service = PortfolioService(
            tmp_path, identity(), first, fetch, PortfolioOptions()
        )
        service.search_query("query")
        service.search = second
        service.search_query("query")
    assert second.calls == ["query"]


def test_failed_response_bytes_are_accounted(tmp_path):
    with Fetcher(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(404, content=b"12345")
        ),
        resolver=lambda h, p: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))
        ],
        minimum_interval=0,
    ) as fetch:
        service = PortfolioService(
            tmp_path,
            identity(),
            Search(),
            fetch,
            PortfolioOptions(max_download_bytes=6),
        )
        with pytest.raises(httpx.HTTPStatusError):
            service.page("https://example.test/fail")
    assert read_jsonl(tmp_path / "portfolio/audit/transfers.jsonl")[0]["bytes"] == 5

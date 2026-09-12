"""Credential-free public search adapters used by the command-line workflow."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

from .discovery import SearchProvider, SearchResult, SearxngSearchProvider


class DdgSearchProvider:
    """Best-effort general web search through the public DDG interface."""

    provider_name = "ddg"

    def __init__(
        self,
        search: Callable[..., Iterable[dict[str, Any]]] | None = None,
    ) -> None:
        self._search = search or self._default_search

    @staticmethod
    def _default_search(
        query: str, *, max_results: int
    ) -> Iterable[dict[str, Any]]:
        from ddgs import DDGS

        return DDGS().text(query, max_results=max_results)

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        rows = self._search(query, max_results=limit)
        results: list[SearchResult] = []
        for row in rows:
            url = str(row.get("href") or row.get("url") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=str(row.get("title") or ""),
                    snippet=str(row.get("body") or row.get("content") or ""),
                    rank=len(results) + 1,
                    query=query,
                    provider=self.provider_name,
                )
            )
            if len(results) >= limit:
                break
        return results


class YtDlpSearchProvider:
    """Search YouTube metadata directly using yt-dlp's ytsearch extractor."""

    provider_name = "yt-dlp"

    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        *,
        timeout: float = 120,
    ) -> None:
        self._runner = runner
        self.timeout = timeout
        self.diagnostics: list[dict[str, str]] = []

    def _diagnose(self, query: str, error: str) -> None:
        self.diagnostics.append(
            {"provider": self.provider_name, "query": query, "error": error}
        )

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        command = [
            "yt-dlp",
            "--dump-json",
            "--flat-playlist",
            "--no-download",
            "--playlist-end",
            str(limit),
            f"ytsearch{limit}:{query}",
        ]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except FileNotFoundError:
            self._diagnose(query, "yt-dlp is not installed")
            return []
        except subprocess.TimeoutExpired:
            self._diagnose(query, "search command timed out")
            return []

        if completed.returncode != 0:
            self._diagnose(query, "search command failed")
            return []

        results: list[SearchResult] = []
        for line in completed.stdout.splitlines():
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            video_id = str(row.get("id") or "").strip()
            url = str(row.get("webpage_url") or "").strip()
            if not url and video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
            if not url:
                continue
            channel = str(row.get("channel") or row.get("uploader") or "").strip()
            description = str(row.get("description") or "").strip()
            snippet = " — ".join(part for part in (channel, description) if part)
            results.append(
                SearchResult(
                    url=url,
                    title=str(row.get("title") or ""),
                    snippet=snippet,
                    rank=len(results) + 1,
                    query=query,
                    provider=self.provider_name,
                )
            )
            if len(results) >= limit:
                break
        return results


class CompositeSearchProvider:
    """Route YouTube queries directly and all other queries to web search."""

    provider_name = "public-search"

    def __init__(self, *, web: SearchProvider, youtube: SearchProvider) -> None:
        self.web = web
        self.youtube = youtube
        self.diagnostics: list[dict[str, str]] = []

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        provider = self.youtube if "youtube" in query.casefold() else self.web
        try:
            results = provider.search(query, limit=limit)
        except Exception as error:
            self.diagnostics.append(
                {
                    "provider": provider.provider_name,
                    "query": query,
                    "error": type(error).__name__,
                }
            )
            return []
        diagnostics = getattr(provider, "diagnostics", [])
        if diagnostics:
            self.diagnostics.extend(diagnostics)
            diagnostics.clear()
        return results


def default_public_search_provider(
    *, searxng_endpoint: str | None = None
) -> CompositeSearchProvider:
    """Build the normal credential-free provider, with optional SearXNG web search."""
    web: SearchProvider = (
        SearxngSearchProvider(searxng_endpoint)
        if searxng_endpoint
        else DdgSearchProvider()
    )
    return CompositeSearchProvider(web=web, youtube=YtDlpSearchProvider())

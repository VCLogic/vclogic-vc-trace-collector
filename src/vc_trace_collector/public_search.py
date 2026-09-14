"""Credential-free public search adapters used by the command-line workflow."""

from __future__ import annotations

import json
import shutil
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
    def _default_search(query: str, *, max_results: int) -> Iterable[dict[str, Any]]:
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


class AgentReachWebSearchProvider:
    """Call the Exa backend configured by Agent Reach through mcporter."""

    provider_name = "agent-reach:exa"

    def __init__(
        self,
        runner: Callable[..., Any] = subprocess.run,
        *,
        timeout: float = 120,
        executable_finder: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner
        self._executable_finder = executable_finder
        self.timeout = timeout
        self.diagnostics: list[dict[str, Any]] = []

    def _diagnose(
        self, query: str, error: str, *, provider_reached: bool = True
    ) -> None:
        self.diagnostics.append(
            {
                "provider": self.provider_name,
                "query": query,
                "error": error,
                "provider_reached": provider_reached,
            }
        )

    def preflight(self, query: str) -> dict[str, Any] | None:
        """Return an actionable diagnostic before reserving provider budget."""
        if self._executable_finder("mcporter") is None:
            return {
                "provider": self.provider_name,
                "query": query,
                "error": "mcporter is not installed",
                "provider_reached": False,
            }
        return None

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return [row for row in results if isinstance(row, dict)]
            data = payload.get("data")
            if isinstance(data, dict):
                return AgentReachWebSearchProvider._rows(data)
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                        continue
                    try:
                        nested = json.loads(item["text"])
                    except json.JSONDecodeError:
                        continue
                    rows = AgentReachWebSearchProvider._rows(nested)
                    if rows:
                        return rows
        return []

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        command = [
            "mcporter",
            "call",
            "exa.web_search_exa",
            f"query={query}",
            f"numResults={limit}",
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
            self._diagnose(query, "mcporter is not installed", provider_reached=False)
            return []
        except subprocess.TimeoutExpired:
            self._diagnose(query, "Exa search command timed out")
            return []
        if completed.returncode != 0:
            stderr = str(getattr(completed, "stderr", ""))
            offline = any(
                marker in stderr.casefold()
                for marker in (
                    "appears offline",
                    "era_negotiation_failed",
                    "version negotiation probe failed",
                )
            )
            self._diagnose(
                query,
                (
                    "Agent Reach Exa backend is unreachable; check its connection "
                    "and Node proxy configuration"
                    if offline
                    else "Exa search command failed"
                ),
                provider_reached=not offline,
            )
            return []
        try:
            rows = self._rows(json.loads(completed.stdout))
        except (json.JSONDecodeError, TypeError):
            self._diagnose(query, "Exa search returned invalid JSON")
            return []
        results: list[SearchResult] = []
        for row in rows:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=str(row.get("title") or ""),
                    snippet=str(
                        row.get("text") or row.get("snippet") or row.get("content") or ""
                    ),
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
                    channel=channel or None,
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
        self.cache_identity = (
            f"composite:{web.provider_name}:{youtube.provider_name}"
        )
        self.diagnostics: list[dict[str, Any]] = []

    def _provider_for(self, query: str) -> SearchProvider:
        return self.youtube if "youtube" in query.casefold() else self.web

    def preflight(self, query: str) -> dict[str, Any] | None:
        provider = self._provider_for(query)
        check = getattr(provider, "preflight", None)
        return check(query) if check is not None else None

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        provider = self._provider_for(query)
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


def agent_reach_public_search_provider() -> CompositeSearchProvider:
    """Use Agent Reach's configured Exa backend and its normal yt-dlp backend."""
    return CompositeSearchProvider(
        web=AgentReachWebSearchProvider(), youtube=YtDlpSearchProvider()
    )

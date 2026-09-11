"""Deterministic HTML and syndication-feed extraction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import struct_time
from urllib.parse import urljoin, urlsplit

import feedparser
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from .policy import UnsafeUrl, canonicalize_url


class ExtractedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_url: str
    title: str
    text: str
    html: str
    author: str | None = None
    published_at: str | None = None
    description: str | None = None
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class FeedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    canonical_url: str
    title: str
    author: str | None = None
    published_at: str | None = None
    summary_text: str = ""
    summary_html: str = ""
    discovered_from: str
    metadata: dict[str, object] = Field(default_factory=dict)


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        element = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if element and element.get("content"):
            return str(element["content"]).strip()
    return None


def _json_ld(soup: BeautifulSoup) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                records.append(item)
    return records


def extract_page(content: bytes | str, source_url: str) -> ExtractedPage:
    soup = BeautifulSoup(content, "html.parser")
    canonical_element = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical_source = (
        urljoin(source_url, str(canonical_element["href"]))
        if canonical_element and canonical_element.get("href")
        else source_url
    )
    canonical_url = canonicalize_url(canonical_source)

    title_element = soup.find("h1")
    title = (
        title_element.get_text(" ", strip=True)
        if title_element
        else (_meta(soup, "og:title") or (soup.title.get_text(" ", strip=True) if soup.title else ""))
    )
    author = _meta(soup, "author", "article:author")
    published = _meta(soup, "article:published_time", "date", "publish-date")
    if not published:
        time_element = soup.find("time", attrs={"datetime": True})
        published = str(time_element["datetime"]) if time_element else None

    links: set[str] = set()
    source_host = urlsplit(canonical_url).hostname
    for anchor in soup.find_all("a", href=True):
        joined = urljoin(canonical_url, str(anchor["href"]))
        if urlsplit(joined).hostname == source_host:
            try:
                links.add(canonicalize_url(joined))
            except UnsafeUrl:
                continue

    for unwanted in soup.find_all(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        unwanted.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = root.get_text("\n\n", strip=True)

    return ExtractedPage(
        canonical_url=canonical_url,
        title=title,
        text=text,
        html=str(root),
        author=author,
        published_at=published,
        description=_meta(soup, "description", "og:description"),
        links=sorted(links),
        metadata={"json_ld": _json_ld(soup)},
    )


def _parsed_time(value: struct_time | None) -> str | None:
    if value is None:
        return None
    return datetime(*value[:6], tzinfo=UTC).isoformat()


def extract_feed(content: bytes | str, source_url: str) -> list[FeedEntry]:
    feed = feedparser.parse(content)
    results: list[FeedEntry] = []
    for entry in feed.entries:
        link = str(entry.get("link", "")).strip()
        if not link:
            continue
        summary_html = str(entry.get("summary", ""))
        summary_text = BeautifulSoup(summary_html, "html.parser").get_text("\n\n", strip=True)
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        results.append(
            FeedEntry(
                url=link,
                canonical_url=canonicalize_url(link),
                title=str(entry.get("title", "Untitled")),
                author=str(entry.get("author")) if entry.get("author") else None,
                published_at=_parsed_time(published),
                summary_text=summary_text,
                summary_html=summary_html,
                discovered_from=canonicalize_url(source_url),
                metadata={
                    "id": entry.get("id"),
                    "tags": [tag.get("term") for tag in entry.get("tags", [])],
                },
            )
        )
    return results

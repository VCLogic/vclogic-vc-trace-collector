"""Safe HTTP retrieval with redirect checks, limits, and retry metadata."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Self
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .policy import Resolver, canonicalize_url, validate_public_url


class FetchTooLarge(RuntimeError):
    pass


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_url: str
    final_url: str
    canonical_url: str
    status_code: int
    content: bytes
    headers: dict[str, str] = Field(default_factory=dict)
    redirect_chain: list[str] = Field(default_factory=list)
    fetched_at: datetime
    attempts: int


_PUBLIC_HEADERS = {
    "cache-control",
    "content-language",
    "content-length",
    "content-type",
    "date",
    "etag",
    "last-modified",
}


class Fetcher:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
        user_agent: str = "vc-trace-collector/0.1 (public research)",
        timeout: float = 30,
        maximum_response_bytes: int = 10_000_000,
        minimum_interval: float = 1.0,
        maximum_redirects: int = 5,
        maximum_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.resolver = resolver
        self.maximum_response_bytes = maximum_response_bytes
        self.minimum_interval = minimum_interval
        self.maximum_redirects = maximum_redirects
        self.maximum_attempts = maximum_attempts
        self.sleep = sleep
        self._last_request: dict[str, float] = {}
        self.client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5",
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _rate_limit(self, url: str) -> None:
        hostname = httpx.URL(url).host
        now = time.monotonic()
        remaining = self.minimum_interval - (now - self._last_request.get(hostname, 0))
        if remaining > 0:
            self.sleep(remaining)
        self._last_request[hostname] = time.monotonic()

    def _request(
        self,
        url: str,
        conditional_headers: dict[str, str],
        maximum_bytes: int,
    ) -> tuple[httpx.Response, bytes]:
        self._rate_limit(url)
        with self.client.stream("GET", url, headers=conditional_headers) as response:
            length = response.headers.get("content-length")
            if length and int(length) > maximum_bytes:
                raise FetchTooLarge(f"Response exceeds {maximum_bytes} bytes")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > maximum_bytes:
                    raise FetchTooLarge(f"Response exceeds {maximum_bytes} bytes")
                chunks.append(chunk)
            return response, b"".join(chunks)

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        maximum_bytes: int | None = None,
    ) -> FetchResult:
        requested_url = validate_public_url(url, resolver=self.resolver)
        current_url = requested_url
        redirects: list[str] = []
        conditional: dict[str, str] = {}
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified

        attempts = 0
        response_limit = maximum_bytes or self.maximum_response_bytes
        if response_limit <= 0:
            raise ValueError("maximum_bytes must be positive")
        while True:
            attempts += 1
            try:
                response, content = self._request(
                    current_url, conditional, response_limit
                )
            except (httpx.TransportError, httpx.TimeoutException):
                if attempts >= self.maximum_attempts:
                    raise
                self.sleep(min(2 ** (attempts - 1), 4))
                continue

            if (
                response.status_code in {429, 500, 502, 503, 504}
                and attempts < self.maximum_attempts
            ):
                retry_after = response.headers.get("retry-after")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(2 ** (attempts - 1), 4)
                )
                self.sleep(delay)
                continue

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                if len(redirects) >= self.maximum_redirects:
                    raise httpx.TooManyRedirects(
                        "Maximum redirect count exceeded", request=response.request
                    )
                redirected = urljoin(current_url, location)
                current_url = validate_public_url(redirected, resolver=self.resolver)
                redirects.append(current_url)
                conditional = {}
                continue

            response.raise_for_status()
            public_headers = {
                key.casefold(): value
                for key, value in response.headers.items()
                if key.casefold() in _PUBLIC_HEADERS
            }
            return FetchResult(
                requested_url=requested_url,
                final_url=current_url,
                canonical_url=canonicalize_url(current_url),
                status_code=response.status_code,
                content=content,
                headers=public_headers,
                redirect_chain=redirects,
                fetched_at=datetime.now(UTC),
                attempts=attempts,
            )

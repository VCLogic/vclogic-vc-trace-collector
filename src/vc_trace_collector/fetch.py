"""Safe HTTP retrieval with redirect checks, limits, and retry metadata."""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .policy import Resolver, UnsafeUrl, canonicalize_url, validate_public_url


class FetchTooLarge(RuntimeError):
    def __init__(self, message: str, *, downloaded_bytes: int = 0):
        super().__init__(message)
        self.downloaded_bytes = downloaded_bytes


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
    transferred_bytes: int = Field(default=0, ge=0)


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
        maximum_retry_delay: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        trust_env: bool = False,
    ):
        self.resolver = resolver
        self.maximum_response_bytes = maximum_response_bytes
        self.minimum_interval = minimum_interval
        self.maximum_redirects = maximum_redirects
        self.maximum_attempts = maximum_attempts
        self.maximum_retry_delay = maximum_retry_delay
        self.sleep = sleep
        self.trust_env = trust_env
        self.environment_proxy_enabled = trust_env and any(
            os.environ.get(name)
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
        )
        self._last_request: dict[str, float] = {}
        self.client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5",
            },
            follow_redirects=False,
            trust_env=trust_env,
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
        expected_addresses = self._public_addresses(url)
        self._rate_limit(url)
        with self.client.stream("GET", url, headers=conditional_headers) as response:
            self._verify_connected_peer(url, response, expected_addresses)
            length = response.headers.get("content-length")
            if length and int(length) > maximum_bytes:
                raise FetchTooLarge(f"Response exceeds {maximum_bytes} bytes")
            chunks: list[bytes] = []
            size = 0
            try:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise FetchTooLarge(
                            f"Response exceeds {maximum_bytes} bytes",
                            downloaded_bytes=size,
                        )
                    chunks.append(chunk)
            except (httpx.TransportError, httpx.TimeoutException) as error:
                error.downloaded_bytes = size
                raise
            return response, b"".join(chunks)

    def _head_request(self, url: str) -> httpx.Response:
        expected_addresses = self._public_addresses(url)
        self._rate_limit(url)
        response = self.client.request("HEAD", url, follow_redirects=False)
        self._verify_connected_peer(url, response, expected_addresses)
        return response

    def _public_addresses(self, url: str) -> set[str]:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        answers = self.resolver(
            hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        addresses = {
            str(ipaddress.ip_address(answer[4][0]))
            for answer in answers
            if len(answer) > 4 and answer[4]
        }
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise UnsafeUrl(f"Host {hostname} changed to a non-public address")
        return addresses

    def _verify_connected_peer(
        self,
        url: str,
        response: httpx.Response,
        expected_addresses: set[str],
    ) -> None:
        stream = response.extensions.get("network_stream")
        peer = None
        if stream is not None and hasattr(stream, "get_extra_info"):
            peer = stream.get_extra_info("server_addr") or stream.get_extra_info(
                "peername"
            )
        if peer and not self.environment_proxy_enabled:
            peer_value = peer[0] if isinstance(peer, tuple) else peer
            try:
                address = ipaddress.ip_address(str(peer_value))
            except ValueError as error:
                raise UnsafeUrl("Could not validate connected peer address") from error
            if not address.is_global or str(address) not in expected_addresses:
                raise UnsafeUrl("Connected peer did not match validated public DNS")
            return
        # Mock/custom transports may not expose the socket. Re-resolving does
        # not replace peer verification for real transports, but prevents a
        # changed answer from being silently accepted in those environments.
        if self._public_addresses(url) != expected_addresses:
            raise UnsafeUrl("DNS answers changed during request")

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
        transferred_bytes = 0
        response_limit = maximum_bytes or self.maximum_response_bytes
        if response_limit <= 0:
            raise ValueError("maximum_bytes must be positive")
        while True:
            attempts += 1
            try:
                response, content = self._request(
                    current_url, conditional, response_limit
                )
            except FetchTooLarge as error:
                error.downloaded_bytes += transferred_bytes
                raise
            except (httpx.TransportError, httpx.TimeoutException) as error:
                transferred_bytes += int(getattr(error, "downloaded_bytes", 0))
                if attempts >= self.maximum_attempts:
                    error.downloaded_bytes = transferred_bytes
                    raise
                self.sleep(min(2 ** (attempts - 1), 4))
                continue

            transferred_bytes += len(content)

            if (
                response.status_code in {429, 500, 502, 503, 504}
                and attempts < self.maximum_attempts
            ):
                retry_after = response.headers.get("retry-after")
                delay = min(2 ** (attempts - 1), 4)
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            retry_at = parsedate_to_datetime(retry_after)
                            delay = max(
                                0.0, (retry_at - datetime.now(UTC)).total_seconds()
                            )
                        except (TypeError, ValueError, OverflowError):
                            pass
                delay = min(delay, self.maximum_retry_delay)
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
                transferred_bytes=transferred_bytes,
            )

    def head(self, url: str) -> FetchResult:
        requested_url = validate_public_url(url, resolver=self.resolver)
        current_url = requested_url
        redirects: list[str] = []
        attempts = 0
        while True:
            attempts += 1
            response = self._head_request(current_url)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                if len(redirects) >= self.maximum_redirects:
                    raise httpx.TooManyRedirects(
                        "Maximum redirect count exceeded", request=response.request
                    )
                current_url = validate_public_url(
                    urljoin(current_url, location), resolver=self.resolver
                )
                redirects.append(current_url)
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
                content=b"",
                headers=public_headers,
                redirect_chain=redirects,
                fetched_at=datetime.now(UTC),
                attempts=attempts,
                transferred_bytes=0,
            )

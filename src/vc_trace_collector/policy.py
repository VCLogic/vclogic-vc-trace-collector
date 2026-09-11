"""Network safety, URL normalization, exclusions, and corpus gates."""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
import tomllib
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    CanonicalDocument,
    ExclusionDecision,
    InclusionStatus,
    MaterialRole,
    SpeakerStatus,
)


class UnsafeUrl(ValueError):
    pass


_TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not scheme or not hostname:
        raise UnsafeUrl(f"URL must include scheme and host: {url}")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise UnsafeUrl(f"Invalid host: {hostname}") from error
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    path = quote(unquote(parsed.path or "/"), safe="/%:@-._~")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMETERS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


Resolver = Callable[..., Iterable[tuple]]


def validate_public_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeUrl("Only HTTP and HTTPS URLs are permitted")
    if parsed.username or parsed.password:
        raise UnsafeUrl("Credentials in URLs are not permitted")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrl("URL has no hostname")

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        addresses.append(ipaddress.ip_address(hostname))
    except ValueError:
        try:
            answers = resolver(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except OSError as error:
            raise UnsafeUrl(f"Could not resolve host {hostname}") from error
        for answer in answers:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except (ValueError, IndexError, TypeError):
                continue

    if not addresses:
        raise UnsafeUrl(f"Host {hostname} resolved to no addresses")
    if any(not address.is_global for address in addresses):
        raise UnsafeUrl(f"Host {hostname} resolves to a non-public address")
    return canonicalize_url(url)


class ExclusionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    version: str = "1"
    action: str = Field(pattern=r"^(exclude|review)$")
    reason: str
    domains: list[str] = Field(default_factory=list)
    url_patterns: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    programmes: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


def _domain_matches(hostname: str, configured: str) -> bool:
    configured = configured.casefold().strip().lstrip(".")
    hostname = hostname.casefold().rstrip(".")
    return hostname == configured or hostname.endswith(f".{configured}")


class RuleSet:
    def __init__(self, rules: list[ExclusionRule]):
        self.rules = rules

    @classmethod
    def from_toml(cls, path: Path) -> "RuleSet":
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        return cls([ExclusionRule.model_validate(row) for row in data.get("rules", [])])

    @classmethod
    def pitch_default(cls) -> "RuleSet":
        return cls(
            [
                ExclusionRule(
                    rule_id="exclude-the-pitch",
                    version="1",
                    action="exclude",
                    reason="Downstream leakage firewall: The Pitch sources are prohibited",
                    domains=["thepitch.show"],
                    channels=["The Pitch Show"],
                    programmes=["The Pitch"],
                )
            ]
        )

    def evaluate(
        self,
        *,
        url: str | None = None,
        title: str | None = None,
        channel: str | None = None,
        programme: str | None = None,
        company: str | None = None,
        text: str | None = None,
        stage: str,
    ) -> ExclusionDecision:
        parsed = urlsplit(url or "")
        haystack = "\n".join(value for value in (title, text) if value).casefold()
        for rule in self.rules:
            matches: dict[str, str] = {}
            if parsed.hostname and any(_domain_matches(parsed.hostname, item) for item in rule.domains):
                matches["domain"] = parsed.hostname
            if url and any(fnmatch.fnmatch(url.casefold(), pattern.casefold()) for pattern in rule.url_patterns):
                matches["url"] = url
            if channel and any(channel.casefold() == item.casefold() for item in rule.channels):
                matches["channel"] = channel
            if programme and any(programme.casefold() == item.casefold() for item in rule.programmes):
                matches["programme"] = programme
            if company and any(company.casefold() == item.casefold() for item in rule.companies):
                matches["company"] = company
            matched_keyword = next(
                (item for item in rule.keywords if item.casefold() in haystack), None
            )
            if matched_keyword:
                matches["keyword"] = matched_keyword
            if matches:
                status = (
                    InclusionStatus.EXCLUDED
                    if rule.action == "exclude"
                    else InclusionStatus.REVIEW_REQUIRED
                )
                return ExclusionDecision(
                    status=status,
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    stage=stage,
                    reason=rule.reason,
                    matched_fields=matches,
                )
        return ExclusionDecision(status=InclusionStatus.INCLUDED, stage=stage)


def eligible_for_corpus(document: CanonicalDocument) -> bool:
    if document.inclusion_status != InclusionStatus.INCLUDED:
        return False
    if document.material_role not in {
        MaterialRole.AUTHORED_BY_TARGET,
        MaterialRole.SPOKEN_BY_TARGET,
    }:
        return False
    if document.material_role == MaterialRole.SPOKEN_BY_TARGET:
        return document.speaker_attribution.status in {
            SpeakerStatus.VERIFIED_HUMAN,
            SpeakerStatus.ACCEPTED_MODEL,
        }
    return document.speaker_attribution.status == SpeakerStatus.NOT_APPLICABLE

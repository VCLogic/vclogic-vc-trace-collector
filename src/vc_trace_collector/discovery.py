"""Identity-safe source discovery and reference-voice planning."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .extract import ExtractedPage, extract_page
from .fetch import Fetcher
from .models import (
    Affiliation,
    ApprovalStatus,
    Confidence,
    IdentityEvidence,
    MaterialRole,
    ReferenceVoiceCandidate,
    ResolvedIdentity,
    ResolutionStatus,
    SourceCandidate,
    SourcePlan,
    SourceType,
)
from .policy import InclusionStatus, RuleSet, canonicalize_url


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value or "investor"


def stable_id(namespace: str, value: str) -> str:
    return f"{namespace}:{sha256(value.encode('utf-8')).hexdigest()[:20]}"


def generate_queries(name: str, firms: list[str]) -> list[str]:
    anchors = firms or ["venture investor"]
    queries: set[str] = set()
    for anchor in anchors:
        queries.update(
            {
                f'"{name}" {anchor}',
                f'"{name}" {anchor} interview',
                f'"{name}" {anchor} podcast',
                f'"{name}" {anchor} YouTube',
                f'"{name}" {anchor} article OR blog',
            }
        )
    return sorted(queries)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    snippet: str = ""
    rank: int = Field(ge=1)
    query: str
    provider: str


class SearchProvider(Protocol):
    provider_name: str

    def search(self, query: str, limit: int = 10) -> list[SearchResult]: ...


class SearxngSearchProvider:
    """Optional operator-supplied SearXNG JSON endpoint."""

    provider_name = "searxng"

    def __init__(self, endpoint: str, *, timeout: float = 30):
        self.endpoint = endpoint
        self.timeout = timeout

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        response = httpx.get(
            self.endpoint,
            params={"q": query, "format": "json"},
            timeout=self.timeout,
            follow_redirects=False,
        )
        response.raise_for_status()
        rows = response.json().get("results", [])
        return [
            SearchResult(
                url=row["url"],
                title=row.get("title", ""),
                snippet=row.get("content", ""),
                rank=index,
                query=query,
                provider=self.provider_name,
            )
            for index, row in enumerate(rows[:limit], start=1)
            if row.get("url")
        ]


class DiscoveryRefinement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    affiliations: list[Affiliation] = Field(default_factory=list)
    competing_hypotheses: list[str] = Field(default_factory=list)
    candidate_roles: dict[str, MaterialRole] = Field(default_factory=dict)


class DiscoveryProvider(Protocol):
    provider_name: str
    model_name: str

    def refine(
        self,
        *,
        name: str,
        evidence: list[IdentityEvidence],
        candidates: list[SourceCandidate],
    ) -> DiscoveryRefinement: ...


class OpenAICompatibleDiscoveryProvider:
    """Structured-output adapter for an operator-configured chat endpoint."""

    provider_name = "openai-compatible"

    def __init__(self, endpoint: str, api_key: str, model: str, *, timeout: float = 60):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model_name = model
        self.timeout = timeout

    def refine(
        self,
        *,
        name: str,
        evidence: list[IdentityEvidence],
        candidates: list[SourceCandidate],
    ) -> DiscoveryRefinement:
        schema = DiscoveryRefinement.model_json_schema()
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Resolve a venture investor and classify sources using only the supplied "
                        "evidence. Preserve ambiguity. Return JSON only; do not include reasoning."
                    ),
                },
                {
                    "role": "user",
                    "content": {
                        "name": name,
                        "evidence": [item.model_dump(mode="json") for item in evidence],
                        "candidates": [item.model_dump(mode="json") for item in candidates],
                        "response_schema": schema,
                    },
                },
            ],
            "response_format": {"type": "json_object"},
        }
        response = httpx.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return DiscoveryRefinement.model_validate_json(content)


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: ResolvedIdentity
    evidence: list[IdentityEvidence]
    source_plan: SourcePlan
    reference_voice_candidates: list[ReferenceVoiceCandidate] = Field(default_factory=list)
    provider_operations: list[dict[str, object]] = Field(default_factory=list)


_KNOWN_NAMESAKES = {
    "michael hyatt": [
        "Michael S. Hyatt, author and publishing executive (competing identity; not assumed identical)"
    ]
}


def _extract_affiliations(page: ExtractedPage, explicit_firm: str | None) -> list[Affiliation]:
    firms: list[Affiliation] = []
    if explicit_firm:
        firms.append(Affiliation(firm=explicit_firm, current=True))
    patterns = [
        r"(?:co-founder|cofounder)\s+of\s+([A-Z][A-Za-z0-9.&'-]+(?:\s+[A-Z][A-Za-z0-9.&'-]+){0,2})",
        r"co-founded\s+([A-Z][A-Za-z0-9.&'-]+(?:\s+[A-Z][A-Za-z0-9.&'-]+){0,2})",
    ]
    existing = {item.firm.casefold() for item in firms}
    for pattern in patterns:
        for match in re.finditer(pattern, page.text):
            firm = match.group(1).strip(" ,.;()")
            if firm.casefold() not in existing:
                firms.append(Affiliation(firm=firm, role="co-founder", current=None))
                existing.add(firm.casefold())
    return firms


def _source_type(url: str) -> SourceType:
    hostname = (urlsplit(url).hostname or "").casefold()
    path = urlsplit(url).path.casefold()
    if hostname.endswith("youtube.com") or hostname == "youtu.be":
        return SourceType.YOUTUBE
    if hostname.endswith("substack.com"):
        return SourceType.SUBSTACK
    if hostname.endswith("medium.com"):
        return SourceType.MEDIUM
    if path.endswith(("/feed", "/feed/", ".rss", ".xml", ".atom")):
        return SourceType.RSS_FEED
    return SourceType.WEB_ARTICLE


def _search_role(name: str, result: SearchResult, source_type: SourceType) -> MaterialRole:
    combined = f"{result.title}\n{result.snippet}".casefold()
    if name.casefold() not in combined:
        return MaterialRole.UNKNOWN
    if source_type == SourceType.YOUTUBE and any(
        word in combined for word in ("interview", "podcast", "talk", "keynote", "conversation")
    ):
        return MaterialRole.REFERENCE_VOICE
    if source_type in {SourceType.SUBSTACK, SourceType.MEDIUM}:
        return MaterialRole.AUTHORED_BY_TARGET
    return MaterialRole.UNKNOWN


class DiscoveryService:
    def __init__(
        self,
        *,
        fetcher: Fetcher,
        search_provider: SearchProvider | None = None,
        discovery_provider: DiscoveryProvider | None = None,
        rules: RuleSet | None = None,
        search_limit_per_query: int = 10,
    ):
        self.fetcher = fetcher
        self.search_provider = search_provider
        self.discovery_provider = discovery_provider
        self.rules = rules or RuleSet.pitch_default()
        self.search_limit_per_query = search_limit_per_query

    def discover(
        self,
        *,
        name: str,
        firm: str | None = None,
        known_profile_url: str | None = None,
    ) -> DiscoveryResult:
        slug = slugify(f"{name}-{firm}" if firm else name)
        evidence: list[IdentityEvidence] = []
        candidates_by_url: dict[str, SourceCandidate] = {}
        page: ExtractedPage | None = None

        if known_profile_url:
            fetched = self.fetcher.fetch(known_profile_url)
            page = extract_page(fetched.content, fetched.final_url)
            exact_name = name.casefold() in f"{page.title}\n{page.text}".casefold()
            evidence_id = stable_id("evidence", page.canonical_url)
            evidence.append(
                IdentityEvidence(
                    evidence_id=evidence_id,
                    url=known_profile_url,
                    canonical_url=page.canonical_url,
                    publisher=urlsplit(page.canonical_url).hostname,
                    query=f"known profile supplied for {name}",
                    search_provider="user_supplied",
                    excerpt=page.text[:500],
                    claim=f"Profile identifies {name}" if exact_name else "Profile identity is ambiguous",
                    validation_status="supports" if exact_name else "ambiguous",
                )
            )
            exclusion = self.rules.evaluate(
                url=page.canonical_url,
                title=page.title,
                text=page.text,
                stage="discovery",
            )
            candidates_by_url[page.canonical_url] = SourceCandidate(
                candidate_id=stable_id("candidate", page.canonical_url),
                url=known_profile_url,
                canonical_url=page.canonical_url,
                source_type=SourceType.WEB_PROFILE,
                material_role=MaterialRole.IDENTITY_EVIDENCE,
                title=page.title,
                description=page.description,
                discovered_via="user_supplied_profile",
                discovery_queries=[f"known profile supplied for {name}"],
                evidence_ids=[evidence_id],
                identity_confidence=Confidence(
                    score=0.9 if exact_name else 0.4, method="exact_name_profile", version="1"
                ),
                source_confidence=Confidence(score=0.9, method="supplied_profile", version="1"),
                approval_status=(
                    ApprovalStatus.REJECTED
                    if exclusion.status == InclusionStatus.EXCLUDED
                    else ApprovalStatus.PENDING
                ),
                decision_reason=exclusion.reason,
            )

        affiliations = _extract_affiliations(page, firm) if page else (
            [Affiliation(firm=firm, current=True)] if firm else []
        )
        firm_names = [item.firm for item in affiliations]
        queries = generate_queries(name, firm_names)
        provider_operations: list[dict[str, object]] = []

        if self.search_provider:
            for query in queries:
                results = self.search_provider.search(query, limit=self.search_limit_per_query)
                provider_operations.append(
                    {
                        "provider": self.search_provider.provider_name,
                        "query": query,
                        "results": len(results),
                    }
                )
                for result in results:
                    canonical = canonicalize_url(result.url)
                    source_type = _source_type(canonical)
                    role = _search_role(name, result, source_type)
                    exclusion = self.rules.evaluate(
                        url=canonical,
                        title=result.title,
                        text=result.snippet,
                        stage="discovery",
                    )
                    existing = candidates_by_url.get(canonical)
                    if existing:
                        existing.discovery_queries = sorted(
                            set(existing.discovery_queries + [result.query])
                        )
                        continue
                    identity_score = 0.8 if name.casefold() in f"{result.title} {result.snippet}".casefold() else 0.3
                    candidates_by_url[canonical] = SourceCandidate(
                        candidate_id=stable_id("candidate", canonical),
                        url=result.url,
                        canonical_url=canonical,
                        source_type=source_type,
                        material_role=role,
                        title=result.title,
                        description=result.snippet,
                        discovered_via=result.provider,
                        discovery_queries=[result.query],
                        identity_confidence=Confidence(
                            score=identity_score, method="search_name_anchor", version="1"
                        ),
                        source_confidence=Confidence(
                            score=0.7, method="search_result", version="1"
                        ),
                        approval_status=(
                            ApprovalStatus.REJECTED
                            if exclusion.status == InclusionStatus.EXCLUDED
                            else ApprovalStatus.PENDING
                        ),
                        decision_reason=exclusion.reason,
                    )

        exact_evidence = any(item.validation_status == "supports" for item in evidence)
        identity = ResolvedIdentity(
            slug=slug,
            canonical_name=name,
            affiliations=affiliations,
            authoritative_profiles=[page.canonical_url] if page else [],
            resolution_status=ResolutionStatus.PROVISIONAL,
            identity_confidence=Confidence(
                score=0.9 if exact_evidence and affiliations else (0.7 if exact_evidence else 0.4),
                method="identity_evidence_policy",
                version="1",
            ),
            evidence_ids=[item.evidence_id for item in evidence],
            competing_hypotheses=_KNOWN_NAMESAKES.get(name.casefold(), []),
        )

        candidates = sorted(candidates_by_url.values(), key=lambda item: item.canonical_url)
        if self.discovery_provider:
            refinement = self.discovery_provider.refine(
                name=name, evidence=evidence, candidates=candidates
            )
            identity.canonical_name = refinement.canonical_name
            identity.aliases = refinement.aliases
            if refinement.affiliations:
                identity.affiliations = refinement.affiliations
            identity.competing_hypotheses = sorted(
                set(identity.competing_hypotheses + refinement.competing_hypotheses)
            )
            for candidate in candidates:
                role = refinement.candidate_roles.get(candidate.candidate_id)
                if role:
                    candidate.material_role = role
            provider_operations.append(
                {
                    "provider": self.discovery_provider.provider_name,
                    "model": self.discovery_provider.model_name,
                    "action": "identity_and_source_refinement",
                }
            )

        plan = SourcePlan(
            plan_id=stable_id("plan", f"{slug}:{'|'.join(queries)}"),
            investor_slug=slug,
            queries=queries,
            candidates=candidates,
            requires_review=True,
            estimated_cost_usd=sum(
                (candidate.estimated_cost_usd for candidate in candidates), start=0
            ),
            estimated_media_seconds=sum(
                candidate.estimated_media_seconds for candidate in candidates
            ),
        )
        voice_candidates = [
            ReferenceVoiceCandidate(
                candidate_id=stable_id("voice", candidate.canonical_url),
                source_candidate_id=candidate.candidate_id,
                source_url=candidate.canonical_url,
                identity_confidence=candidate.identity_confidence,
            )
            for candidate in candidates
            if candidate.material_role == MaterialRole.REFERENCE_VOICE
        ]
        return DiscoveryResult(
            identity=identity,
            evidence=evidence,
            source_plan=plan,
            reference_voice_candidates=voice_candidates,
            provider_operations=provider_operations,
        )

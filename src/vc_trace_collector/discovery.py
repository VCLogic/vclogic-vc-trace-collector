"""Identity-safe source discovery and reference-voice planning."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .audit import BudgetLedger
from .extract import ExtractedPage, extract_page
from .fetch import Fetcher, FetchTooLarge
from .models import (
    Affiliation,
    ApprovalStatus,
    Confidence,
    IdentityEvidence,
    MaterialRole,
    ReferenceVoiceCandidate,
    ResolutionStatus,
    ResolvedIdentity,
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
    channel: str | None = None


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
        self.last_usage: dict[str, int] = {}

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
                    "content": json.dumps(
                        {
                            "name": name,
                            "evidence": [
                                item.model_dump(mode="json") for item in evidence
                            ],
                            "candidates": [
                                item.model_dump(mode="json") for item in candidates
                            ],
                            "response_schema": schema,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
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
        response_data = response.json()
        usage = response_data.get("usage") or {}
        self.last_usage = {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }
        content = response_data["choices"][0]["message"]["content"]
        return DiscoveryRefinement.model_validate_json(content)


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: ResolvedIdentity
    evidence: list[IdentityEvidence]
    source_plan: SourcePlan
    reference_voice_candidates: list[ReferenceVoiceCandidate] = Field(
        default_factory=list
    )
    provider_operations: list[dict[str, object]] = Field(default_factory=list)


_KNOWN_NAMESAKES = {
    "michael hyatt": [
        "Michael S. Hyatt, author and publishing executive (competing identity; not assumed identical)"
    ]
}


def _extract_affiliations(
    page: ExtractedPage, explicit_firm: str | None
) -> list[Affiliation]:
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


def _source_type(url: str, context: str = "") -> SourceType:
    hostname = (urlsplit(url).hostname or "").casefold()
    path = urlsplit(url).path.casefold()
    if (
        hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
        or hostname == "youtu.be"
    ):
        return SourceType.YOUTUBE
    if (
        hostname
        in {"podcasts.apple.com", "podcasters.spotify.com", "creators.spotify.com"}
        or "/podcast" in path
        or re.search(r"\b(podcast|episode)\b", context, flags=re.IGNORECASE)
    ):
        return SourceType.PODCAST
    if hostname == "substack.com" or hostname.endswith(".substack.com"):
        return SourceType.SUBSTACK
    if hostname == "medium.com" or hostname.endswith(".medium.com"):
        return SourceType.MEDIUM
    if path.endswith(("/feed", "/feed/", ".rss", ".xml", ".atom")):
        return SourceType.RSS_FEED
    return SourceType.WEB_ARTICLE


def _search_role(
    name: str, result: SearchResult, source_type: SourceType
) -> MaterialRole:
    combined = f"{result.title}\n{result.snippet}".casefold()
    if name.casefold() not in combined:
        return MaterialRole.UNKNOWN
    if source_type in {SourceType.YOUTUBE, SourceType.PODCAST} and any(
        word in combined
        for word in ("interview", "podcast", "talk", "keynote", "conversation")
    ):
        return MaterialRole.SPOKEN_BY_TARGET
    return MaterialRole.UNKNOWN


def _page_role(name: str, page: ExtractedPage, source_type: SourceType) -> MaterialRole:
    if page.author and page.author.casefold().strip() == name.casefold().strip():
        return MaterialRole.AUTHORED_BY_TARGET
    combined = f"{page.title}\n{page.description or ''}\n{page.text}".casefold()
    if (
        source_type in {SourceType.YOUTUBE, SourceType.PODCAST}
        and name.casefold() in combined
        and any(
            word in combined
            for word in ("interview", "podcast", "conversation", "guest")
        )
    ):
        return MaterialRole.SPOKEN_BY_TARGET
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
        maximum_search_operations: int = 20,
        maximum_download_bytes: int | None = None,
        budget: BudgetLedger | None = None,
        search_operation_cost_usd: Decimal = Decimal(0),
        discovery_operation_id: str | None = None,
        discovery_operation_cost_usd: Decimal = Decimal(0),
        maximum_provider_operations: int = 100,
    ):
        self.fetcher = fetcher
        self.search_provider = search_provider
        self.discovery_provider = discovery_provider
        self.rules = rules or RuleSet.pitch_default()
        self.search_limit_per_query = search_limit_per_query
        self.maximum_search_operations = max(0, maximum_search_operations)
        self.maximum_download_bytes = maximum_download_bytes
        self.downloaded_bytes = 0
        self.budget = budget
        self.search_operation_cost_usd = search_operation_cost_usd
        self.discovery_operation_id = discovery_operation_id
        self.discovery_operation_cost_usd = discovery_operation_cost_usd
        self.maximum_provider_operations = maximum_provider_operations
        self.retrieved_fetches = []

    def _fetch(self, url: str):
        remaining = (
            self.maximum_download_bytes - self.downloaded_bytes
            if self.maximum_download_bytes is not None
            else None
        )
        if remaining is not None and remaining <= 0:
            raise RuntimeError("Downloaded-byte budget exhausted during discovery")
        try:
            fetched = self.fetcher.fetch(url, maximum_bytes=remaining)
        except (FetchTooLarge, httpx.TransportError, httpx.TimeoutException) as error:
            self.downloaded_bytes += int(getattr(error, "downloaded_bytes", 0))
            raise
        self.downloaded_bytes += fetched.transferred_bytes
        return fetched

    def discover(
        self,
        *,
        name: str,
        firm: str | None = None,
        known_profile_url: str | None = None,
        source_urls: list[str] | None = None,
        supplied_files: list[Path] | None = None,
        supplied_role: MaterialRole = MaterialRole.UNKNOWN,
    ) -> DiscoveryResult:
        slug = slugify(f"{name}-{firm}" if firm else name)
        evidence: list[IdentityEvidence] = []
        candidates_by_url: dict[str, SourceCandidate] = {}
        page: ExtractedPage | None = None

        if known_profile_url:
            fetched = self._fetch(known_profile_url)
            self.retrieved_fetches.append(fetched)
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
                    claim=f"Profile identifies {name}"
                    if exact_name
                    else "Profile identity is ambiguous",
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
                    score=0.9 if exact_name else 0.4,
                    method="exact_name_profile",
                    version="1",
                ),
                source_confidence=Confidence(
                    score=0.9, method="supplied_profile", version="1"
                ),
                approval_status=(
                    ApprovalStatus.REJECTED
                    if exclusion.status == InclusionStatus.EXCLUDED
                    else ApprovalStatus.PENDING
                ),
                decision_reason=exclusion.reason,
            )

        affiliations = (
            _extract_affiliations(page, firm)
            if page
            else ([Affiliation(firm=firm, current=True)] if firm else [])
        )
        for source_url in source_urls or []:
            direct_suffix = Path(urlsplit(source_url).path).suffix.casefold()
            if direct_suffix in {
                ".mp3",
                ".m4a",
                ".opus",
                ".ogg",
                ".wav",
                ".flac",
                ".mp4",
                ".mov",
                ".mkv",
                ".webm",
            }:
                canonical = canonicalize_url(source_url)
                candidates_by_url[canonical] = SourceCandidate(
                    candidate_id=stable_id("candidate", canonical),
                    url=source_url,
                    canonical_url=canonical,
                    source_type=SourceType.PODCAST,
                    material_role=MaterialRole.REFERENCE_VOICE,
                    title=Path(urlsplit(source_url).path).name or None,
                    description=(
                        "Direct public media supplied by operator; identity and duration "
                        "require review before collection"
                    ),
                    discovered_via="user_supplied_direct_media",
                    discovery_queries=[f"operator supplied media for {name}"],
                    identity_confidence=Confidence(
                        score=0.5,
                        method="operator_supplied_media_requires_review",
                        version="1",
                    ),
                    source_confidence=Confidence(
                        score=0.8, method="operator_supplied_url", version="1"
                    ),
                    approval_status=ApprovalStatus.PENDING,
                )
                continue
            fetched = self._fetch(source_url)
            self.retrieved_fetches.append(fetched)
            source_page = extract_page(fetched.content, fetched.final_url)
            combined = f"{source_page.title}\n{source_page.description or ''}\n{source_page.text}"
            exact_name = name.casefold() in combined.casefold()
            affiliation_match = any(
                affiliation.firm.casefold() in combined.casefold()
                for affiliation in affiliations
            )
            evidence_id = stable_id("evidence", source_page.canonical_url)
            evidence.append(
                IdentityEvidence(
                    evidence_id=evidence_id,
                    url=source_url,
                    canonical_url=source_page.canonical_url,
                    publisher=urlsplit(source_page.canonical_url).hostname,
                    query=f"operator supplied source for {name}",
                    search_provider="user_supplied",
                    excerpt=source_page.text[:500],
                    claim=(
                        f"Source names {name} with a known affiliation"
                        if exact_name and affiliation_match
                        else f"Source names {name}"
                        if exact_name
                        else "Source identity is ambiguous"
                    ),
                    validation_status=(
                        "supports" if exact_name and affiliation_match else "ambiguous"
                    ),
                )
            )
            source_type = _source_type(
                source_page.canonical_url,
                (
                    f"{source_page.title} {source_page.description or ''} "
                    f"{source_page.text[:500]} "
                    f"{'podcast' if b'<audio' in fetched.content.lower() else ''}"
                ),
            )
            exclusion = self.rules.evaluate(
                url=source_page.canonical_url,
                title=source_page.title,
                text=source_page.text,
                stage="discovery",
            )
            candidates_by_url[source_page.canonical_url] = SourceCandidate(
                candidate_id=stable_id("candidate", source_page.canonical_url),
                url=source_url,
                canonical_url=source_page.canonical_url,
                source_type=source_type,
                material_role=_page_role(name, source_page, source_type),
                title=source_page.title,
                description=source_page.description or source_page.text[:300],
                discovered_via="user_supplied_source",
                discovery_queries=[f"operator supplied source for {name}"],
                evidence_ids=[evidence_id],
                identity_confidence=Confidence(
                    score=0.9
                    if exact_name and affiliation_match
                    else (0.7 if exact_name else 0.3),
                    method="name_affiliation_source",
                    version="1",
                ),
                source_confidence=Confidence(
                    score=0.9, method="operator_supplied_url", version="1"
                ),
                approval_status=(
                    ApprovalStatus.REJECTED
                    if exclusion.status == InclusionStatus.EXCLUDED
                    else ApprovalStatus.PENDING
                ),
                decision_reason=exclusion.reason,
            )
        for supplied_file in supplied_files or []:
            path = Path(supplied_file).expanduser().resolve(strict=True)
            source_uri = path.as_uri()
            candidates_by_url[source_uri] = SourceCandidate(
                candidate_id=stable_id("candidate", source_uri),
                url=source_uri,
                canonical_url=source_uri,
                source_type=SourceType.SUPPLIED,
                material_role=supplied_role,
                title=path.name,
                description="Operator-supplied local public-trace snapshot",
                discovered_via="user_supplied_file",
                discovery_queries=[f"operator supplied file for {name}"],
                identity_confidence=Confidence(
                    score=0.9,
                    method="operator_attested_file_identity",
                    version="1",
                ),
                source_confidence=Confidence(
                    score=0.9, method="operator_supplied_file", version="1"
                ),
                approval_status=ApprovalStatus.PENDING,
            )
        firm_names = [item.firm for item in affiliations]
        queries = generate_queries(name, firm_names)
        provider_operations: list[dict[str, object]] = []
        reported_diagnostics = 0

        if self.search_provider:
            for query in queries[: self.maximum_search_operations]:
                operation_id = f"search:{stable_id('query', query)}"
                if self.budget:
                    if (
                        self.budget.provider_operations
                        >= self.maximum_provider_operations
                    ):
                        raise RuntimeError(
                            "Provider-operation budget exhausted before search"
                        )
                    self.budget.reserve(
                        operation_id,
                        self.search_operation_cost_usd,
                        provider=self.search_provider.provider_name,
                    )
                try:
                    results = self.search_provider.search(
                        query, limit=self.search_limit_per_query
                    )
                except Exception:
                    if self.budget:
                        # The provider accepted the invocation and may bill failed calls.
                        self.budget.settle(
                            operation_id,
                            self.search_operation_cost_usd,
                            provider=self.search_provider.provider_name,
                        )
                    raise
                if self.budget:
                    self.budget.settle(
                        operation_id,
                        self.search_operation_cost_usd,
                        provider=self.search_provider.provider_name,
                    )
                operation: dict[str, object] = {
                    "provider": self.search_provider.provider_name,
                    "query": query,
                    "results": len(results),
                }
                diagnostics = getattr(self.search_provider, "diagnostics", [])
                if len(diagnostics) > reported_diagnostics:
                    operation["diagnostics"] = diagnostics[reported_diagnostics:]
                    reported_diagnostics = len(diagnostics)
                provider_operations.append(operation)
                for result in results:
                    canonical = canonicalize_url(result.url)
                    source_type = _source_type(
                        canonical, f"{result.title} {result.snippet}"
                    )
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
                    search_context = f"{result.title} {result.snippet}".casefold()
                    name_matches = name.casefold() in search_context
                    affiliation_matches = any(
                        affiliation.firm.casefold() in search_context
                        for affiliation in affiliations
                    )
                    identity_score = (
                        0.9
                        if name_matches and affiliation_matches
                        else (0.6 if name_matches else 0.3)
                    )
                    identity_method = (
                        "search_name_affiliation_anchor"
                        if affiliation_matches
                        else "search_name_only"
                    )
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
                            score=identity_score,
                            method=identity_method,
                            version="1",
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
                score=0.9
                if exact_evidence and affiliations
                else (0.7 if exact_evidence else 0.4),
                method="identity_evidence_policy",
                version="1",
            ),
            evidence_ids=[item.evidence_id for item in evidence],
            competing_hypotheses=_KNOWN_NAMESAKES.get(name.casefold(), []),
        )

        candidates = sorted(
            candidates_by_url.values(), key=lambda item: item.canonical_url
        )
        if self.discovery_provider:
            operation_id = self.discovery_operation_id or (
                f"discover-llm:{stable_id('identity', slug)}"
            )
            if self.budget:
                self.budget.reserve(
                    operation_id,
                    self.discovery_operation_cost_usd,
                    provider=self.discovery_provider.provider_name,
                    model=self.discovery_provider.model_name,
                )
            try:
                refinement = self.discovery_provider.refine(
                    name=name, evidence=evidence, candidates=candidates
                )
            except Exception:
                if self.budget:
                    self.budget.settle(
                        operation_id,
                        self.discovery_operation_cost_usd,
                        provider=self.discovery_provider.provider_name,
                        model=self.discovery_provider.model_name,
                    )
                raise
            if self.budget:
                usage = getattr(self.discovery_provider, "last_usage", {})
                self.budget.settle(
                    operation_id,
                    self.discovery_operation_cost_usd,
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    provider=self.discovery_provider.provider_name,
                    model=self.discovery_provider.model_name,
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
                    **getattr(self.discovery_provider, "last_usage", {}),
                }
            )

        plan = SourcePlan(
            plan_id=stable_id(
                "plan",
                json.dumps(
                    {
                        "investor_slug": slug,
                        "queries": queries,
                        "candidates": [
                            candidate.model_dump(mode="json")
                            for candidate in candidates
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
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
            if candidate.material_role
            in {MaterialRole.REFERENCE_VOICE, MaterialRole.SPOKEN_BY_TARGET}
        ]
        return DiscoveryResult(
            identity=identity,
            evidence=evidence,
            source_plan=plan,
            reference_voice_candidates=voice_candidates,
            provider_operations=provider_operations,
        )

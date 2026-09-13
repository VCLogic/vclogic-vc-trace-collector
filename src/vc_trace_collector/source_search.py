"""Deterministic, source-specific query generation and candidate construction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .discovery import SearchResult, stable_id
from .models import (
    ApprovalStatus,
    Confidence,
    MaterialRole,
    ResolvedIdentity,
    SourceCandidate,
    SourceType,
)
from .policy import InclusionStatus, RuleSet, canonicalize_url


class SourceSearchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    queries: list[str]
    added: int = 0
    updated: int = 0
    result_count: int = 0
    failed: int = 0


class SourceSearchObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str | None = None
    source_type: SourceType
    query: str
    requested_provider: str
    result_provider: str | None = None
    status: str = Field(pattern=r"^(succeeded|failed)$")
    cached: bool = False
    rank: int | None = None
    url: str | None = None
    title: str | None = None
    error: str | None = None


_SUFFIXES: dict[SourceType, tuple[str, ...]] = {
    SourceType.YOUTUBE: ("interview YouTube", "talk YouTube", "podcast YouTube"),
    SourceType.PODCAST: ("podcast interview", "podcast guest", "podcast RSS"),
    SourceType.WEB_ARTICLE: ("personal blog", "article by", "interview transcript"),
    SourceType.WEB_PROFILE: ("official profile", "venture capital profile"),
    SourceType.RSS_FEED: ("blog RSS feed", "podcast RSS feed"),
    SourceType.SUBSTACK: ("Substack", "site:substack.com"),
    SourceType.MEDIUM: ("Medium", "site:medium.com"),
}


def build_source_queries(
    name: str, firms: list[str], source_type: SourceType
) -> list[str]:
    if source_type not in _SUFFIXES:
        raise ValueError(f"Search is not supported for {source_type.value}")
    anchors = firms or ["venture investor"]
    return sorted(
        {
            f'"{name}" "{anchor}" {suffix}'
            for anchor in anchors
            for suffix in _SUFFIXES[source_type]
        }
    )


def candidate_from_search_result(
    *,
    identity: ResolvedIdentity,
    source_type: SourceType,
    result: SearchResult,
    rules: RuleSet,
) -> SourceCandidate:
    canonical = canonicalize_url(result.url)
    context = f"{result.title}\n{result.snippet}".casefold()
    name_matches = identity.canonical_name.casefold() in context
    affiliation_matches = any(
        affiliation.firm.casefold() in context
        for affiliation in identity.affiliations
    )
    identity_score = (
        0.9
        if name_matches and affiliation_matches
        else (0.6 if name_matches else 0.3)
    )
    spoken = source_type in {SourceType.YOUTUBE, SourceType.PODCAST} and any(
        marker in context
        for marker in ("interview", "podcast", "talk", "keynote", "conversation")
    )
    exclusion = rules.evaluate(
        url=canonical,
        title=result.title,
        text=result.snippet,
        stage="discovery",
    )
    return SourceCandidate(
        candidate_id=stable_id("candidate", canonical),
        url=result.url,
        canonical_url=canonical,
        source_type=source_type,
        material_role=(
            MaterialRole.SPOKEN_BY_TARGET if spoken else MaterialRole.UNKNOWN
        ),
        title=result.title,
        description=result.snippet,
        discovered_via=result.provider,
        discovery_queries=[result.query],
        identity_confidence=Confidence(
            score=identity_score,
            method=(
                "search_name_affiliation_anchor"
                if affiliation_matches
                else "search_name_only"
            ),
            version="1",
        ),
        source_confidence=Confidence(
            score=0.7,
            method="source_specific_search_result",
            version="1",
        ),
        approval_status=(
            ApprovalStatus.REJECTED
            if exclusion.status == InclusionStatus.EXCLUDED
            else ApprovalStatus.PENDING
        ),
        decision_reason=exclusion.reason,
    )

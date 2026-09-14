"""Download public portfolio evidence for extraction in a separate repository.

No corpus exclusion policy, investment extraction or LLM provider runs here.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from pydantic import Field, ValidationError

from .audit import BudgetExceeded, BudgetLedger, redact
from .discovery import SearchProvider, SearchResult
from .extract import extract_page
from .fetch import Fetcher
from .models import ResolvedIdentity, StrictModel, utc_now
from .policy import canonicalize_url
from .progress import download_batch, download_item
from .storage import (
    append_jsonl,
    canonical_json,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)


def digest(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


class PortfolioEvidence(StrictModel):
    source_id: str
    source_url: str
    final_url: str
    title: str
    text: str
    published_at: str | None = None
    collected_at: str
    raw_path: str
    content_sha256: str
    metadata_sha256: str = ""


class PortfolioOptions(StrictModel):
    refresh: bool = False
    max_searches: int = Field(default=20, ge=0, le=1000)
    max_pages: int = Field(default=30, ge=1, le=1000)
    limit_per_query: int = Field(default=10, ge=1, le=100)
    max_cost_usd: Decimal = Field(default=Decimal(10), ge=0)
    search_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    max_download_bytes: int = Field(default=100_000_000, gt=0)


class PortfolioService:
    def __init__(
        self,
        workspace: Path,
        identity: ResolvedIdentity,
        search: SearchProvider,
        fetcher: Fetcher,
        options: PortfolioOptions,
    ):
        self.root = Path(workspace) / "portfolio"
        self.identity, self.search, self.fetcher = identity, search, fetcher
        self.options = options
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = BudgetLedger(
            self.root / "audit/costs.jsonl", options.max_cost_usd
        )

    def event(self, action: str, **details):
        append_jsonl(
            self.root / "audit/events.jsonl",
            redact({"at": utc_now().isoformat(), "action": action, **details}),
        )

    def write_cache(self, path: Path, value: object):
        if path.exists():
            original = path.read_bytes()
            archived = (
                self.root
                / "cache_history"
                / path.parent.name
                / f"{path.stem}-{sha256(original).hexdigest()}.json"
            )
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes(original)
            self.event(
                "cache_version_preserved",
                path=str(path.relative_to(self.root)),
                archived=str(archived.relative_to(self.root)),
            )
        write_json(path, value)

    def search_query(self, query: str) -> list[SearchResult]:
        provider_identity = getattr(
            self.search, "cache_identity", self.search.provider_name
        )
        key = digest([provider_identity, query, self.options.limit_per_query])
        allowed_providers = {self.search.provider_name}
        for channel in ("web", "youtube"):
            provider = getattr(self.search, channel, None)
            if provider:
                allowed_providers.add(provider.provider_name)
        path = self.root / "search" / f"{key}.json"
        if path.exists() and not self.options.refresh:
            cached = read_json(path)
            if (
                cached.get("provider_identity") != provider_identity
                or cached.get("query") != query
                or cached.get("limit") != self.options.limit_per_query
                or cached.get("cache_sha256")
                != digest({k: v for k, v in cached.items() if k != "cache_sha256"})
            ):
                raise ValueError("Search cache metadata mismatch")
            rows = [SearchResult.model_validate(row) for row in cached["results"]]
            if any(
                row.query != query or row.provider not in allowed_providers
                for row in rows
            ):
                raise ValueError("Search cache result provenance mismatch")
            self.event(
                "search_cached", query=query, path=str(path.relative_to(self.root))
            )
            return rows
        operation = f"portfolio-search:{key}:{utc_now().isoformat()}"
        self.budget.reserve(
            operation, self.options.search_cost_usd, provider=self.search.provider_name
        )
        try:
            rows = self.search.search(query, limit=self.options.limit_per_query)
            diagnostics = getattr(self.search, "diagnostics", [])
            if diagnostics:
                errors = list(diagnostics)
                diagnostics.clear()
                raise RuntimeError(str(redact(errors)))
            cached = {
                "query": query,
                "provider": self.search.provider_name,
                "provider_identity": provider_identity,
                "limit": self.options.limit_per_query,
                "results": [r.model_dump(mode="json") for r in rows],
            }
            if any(
                row.query != query or row.provider not in allowed_providers
                for row in rows
            ):
                raise ValueError("Search result provenance mismatch")
            cached["cache_sha256"] = digest(cached)
            self.write_cache(path, cached)
            self.event("search_completed", query=query, count=len(rows))
            return rows
        finally:
            # A failed provider request may still be billed; reserve its full bound.
            self.budget.settle(
                operation,
                self.options.search_cost_usd,
                provider=self.search.provider_name,
            )

    def page(self, url: str) -> PortfolioEvidence:
        canonical = canonicalize_url(url)
        path = self.root / "sources" / f"{digest(canonical)}.json"
        if path.exists() and not self.options.refresh:
            return self.cached_page(canonical)
        used = sum(
            int(row.get("bytes", 0))
            for row in read_jsonl(self.root / "audit/transfers.jsonl")
        )
        remaining = self.options.max_download_bytes - used
        if remaining <= 0:
            raise BudgetExceeded("portfolio-download-bytes")
        before = getattr(self.fetcher, "downloaded_bytes_total", None)
        try:
            result = self.fetcher.fetch(
                canonical, maximum_bytes=min(remaining, 5_000_000)
            )
        except Exception as error:
            append_jsonl(
                self.root / "audit/transfers.jsonl",
                {
                    "url": canonical,
                    "bytes": (
                        self.fetcher.downloaded_bytes_total - before
                        if before is not None
                        else int(
                            getattr(
                                error, "downloaded_bytes", min(remaining, 5_000_000)
                            )
                        )
                    ),
                    "at": utc_now().isoformat(),
                    "status": "failed",
                },
            )
            raise
        append_jsonl(
            self.root / "audit/transfers.jsonl",
            {
                "url": canonical,
                "bytes": (
                    self.fetcher.downloaded_bytes_total - before
                    if before is not None
                    else result.transferred_bytes or len(result.content)
                ),
                "at": utc_now().isoformat(),
                "status": "succeeded",
            },
        )
        mime = result.headers.get("content-type", "").casefold()
        if mime and not any(value in mime for value in ("html", "text/plain", "xhtml")):
            raise ValueError("Portfolio extraction requires an HTML or plain-text page")
        page = extract_page(result.content, result.final_url)
        content_hash = sha256(result.content).hexdigest()
        raw_path = Path("raw") / f"{content_hash}.html"
        (self.root / raw_path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / raw_path).write_bytes(result.content)
        evidence = PortfolioEvidence(
            source_id=digest(canonical),
            source_url=canonical,
            final_url=result.final_url,
            title=page.title,
            text=page.text,
            published_at=page.published_at,
            collected_at=result.fetched_at.isoformat(),
            raw_path=str(raw_path),
            content_sha256=content_hash,
        )
        evidence.metadata_sha256 = digest(
            evidence.model_dump(mode="json", exclude={"metadata_sha256"})
        )
        self.write_cache(path, evidence)
        self.event(
            "page_collected",
            source_id=evidence.source_id,
            bytes=len(result.content),
            sha256=content_hash,
        )
        return evidence

    def cached_page(self, url: str) -> PortfolioEvidence:
        """Validate existing evidence without network access, even during refresh."""
        canonical = canonicalize_url(url)
        path = self.root / "sources" / f"{digest(canonical)}.json"
        evidence = PortfolioEvidence.model_validate(read_json(path))
        if (
            evidence.source_id != digest(canonical)
            or evidence.source_url != canonical
            or evidence.raw_path != f"raw/{evidence.content_sha256}.html"
            or evidence.metadata_sha256
            != digest(evidence.model_dump(mode="json", exclude={"metadata_sha256"}))
        ):
            raise ValueError("Source cache metadata mismatch")
        raw = (self.root / evidence.raw_path).resolve()
        if (
            not raw.is_relative_to(self.root.resolve())
            or sha256(raw.read_bytes()).hexdigest() != evidence.content_sha256
        ):
            raise ValueError(
                "Cached portfolio evidence failed content-hash verification"
            )
        self.event("page_cached", source_id=evidence.source_id)
        return evidence

    def run(self, source_urls: list[str] | None = None) -> dict:
        name = self.identity.canonical_name
        anchors = [f'"{name}" "{a.firm}"' for a in self.identity.affiliations[:3]] or [
            f'"{name}" venture investor'
        ]
        queries = deque(
            f"{anchor} {purpose}"
            for anchor in anchors
            for purpose in ("portfolio investments", "invested funding announcement")
        )
        urls = deque([*self.identity.authoritative_profiles, *(source_urls or [])])
        documents: dict[str, PortfolioEvidence] = {}
        seen_urls: set[str] = set()
        failures = 0
        for path in sorted((self.root / "sources").glob("*.json")):
            try:
                cached = PortfolioEvidence.model_validate(read_json(path))
                if (
                    path.stem != digest(cached.source_url)
                    or cached.source_id != path.stem
                ):
                    raise ValueError("Source cache filename binding mismatch")
                urls.append(cached.source_url)
                try:
                    documents[cached.source_id] = self.cached_page(cached.source_url)
                except ValueError:
                    if not self.options.refresh:
                        raise
            except (ValueError, OSError) as error:
                failures += 1
                self.event(
                    "source_cache_invalid",
                    path=str(path.relative_to(self.root)),
                    error_type=type(error).__name__,
                )
        searched = fetched = 0
        status = "complete"
        write_json(
            self.root / "config.json",
            {
                "identity": self.identity.model_dump(mode="json"),
                "options": self.options.model_dump(mode="json"),
                "mode": "download_only",
                "corpus_exclusions_applied": False,
            },
        )
        try:
            while urls or queries:
                if not urls:
                    if searched >= self.options.max_searches:
                        break
                    query = queries.popleft()
                    searched += 1
                    download_item(f"Portfolio search: {query}")
                    try:
                        urls.extend(row.url for row in self.search_query(query))
                    except BudgetExceeded:
                        raise
                    except Exception as error:
                        failures += 1
                        self.event("search_failed", query=query, error=str(error))
                        queries.clear()
                    continue
                url = urls.popleft()
                try:
                    canonical = canonicalize_url(url)
                    if canonical in seen_urls:
                        continue
                    if fetched >= self.options.max_pages:
                        status = "page_limit_reached"
                        break
                    seen_urls.add(canonical)
                    fetched += 1
                    download_item(f"Portfolio page {fetched}: {canonical}")
                    evidence = self.page(canonical)
                    documents[evidence.source_id] = evidence
                except BudgetExceeded:
                    raise
                except Exception as error:
                    failures += 1
                    self.event(
                        "page_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error="Evidence validation failed"
                        if isinstance(error, ValidationError)
                        else str(error),
                    )
                finally:
                    self.write_documents(documents)
                    download_batch(fetched, self.options.max_pages, failed=failures)
        except BudgetExceeded as error:
            status = "budget_stopped"
            self.event("budget_stopped", operation=str(error))
        self.write_documents(documents)
        report = {
            "status": status
            if status != "complete"
            else ("partial" if failures else "complete"),
            "mode": "download_only",
            "documents": len(documents),
            "pages_attempted": fetched,
            "queries_considered": searched,
            "failures": failures,
            "extraction_enabled": False,
            "search_limit_reached": bool(queries)
            and searched >= self.options.max_searches,
            "coverage": "bounded_public_evidence_not_a_complete_portfolio",
            "corpus_exclusions_applied": False,
            "accounted_cost_upper_bound_usd": str(self.budget.spent),
            "finished_at": utc_now().isoformat(),
        }
        write_json(self.root / "run_summary.json", report)
        write_json(
            self.root / "handoff.json",
            {
                "schema_version": "1.0",
                "dataset_type": "public_portfolio_evidence",
                "investor": self.identity.model_dump(mode="json"),
                "documents": "documents.jsonl",
                "sources": "sources/",
                "raw": "raw/",
                "manifest": "manifest.json",
                "status": "run_summary.json",
                "extraction_performed": False,
                "corpus_exclusions_applied": False,
                "note": "Source-page publication dates are not investment dates. Identity validation, "
                "company/date extraction and summaries belong to the downstream tool.",
            },
        )
        own_files = {
            "config.json",
            "documents.jsonl",
            "handoff.json",
            "run_summary.json",
        }
        own_directories = {"raw", "sources", "search", "audit", "cache_history"}
        files = sorted(
            path
            for path in self.root.rglob("*")
            if path.is_file()
            and (
                str(path.relative_to(self.root)) in own_files
                or path.relative_to(self.root).parts[0] in own_directories
            )
            and not path.name.endswith((".sqlite", "-wal", "-shm", "-journal"))
        )
        write_json(
            self.root / "manifest.json",
            {
                "files": [
                    {
                        "path": str(path.relative_to(self.root)),
                        "sha256": sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in files
                ]
            },
        )
        return report

    def write_documents(self, documents: dict[str, PortfolioEvidence]) -> None:
        write_jsonl(
            self.root / "documents.jsonl", [documents[key] for key in sorted(documents)]
        )

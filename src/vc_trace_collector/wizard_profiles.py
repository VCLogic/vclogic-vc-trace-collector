"""Bounded, auditable profile lookup before identity resolution."""

from decimal import Decimal
from uuid import uuid4

from .audit import BudgetLedger, redact
from .config import RunConfig
from .discovery import SearchResult, slugify
from .policy import canonicalize_url
from .public_search import agent_reach_public_search_provider
from .storage import append_jsonl, read_json, write_json
from .wizard_models import Option, backend_options


def money(ui, key, message, default):
    value = Decimal(ui.text(key, message, str(default)))
    if not value.is_finite() or value < 0:
        raise ValueError("Monetary limits must be finite and non-negative")
    return value


def resolve_profile(pipeline, ui, name, *, provider=None):
    firm = ui.text("firm", "Known firm (optional)")
    url = ui.text(
        "profile_url", "Known public profile URL (blank = find possible profiles)"
    )
    maximum = money(ui, "max_cost", "Maximum cumulative provider cost in USD", "10")
    cost = money(
        ui,
        "search_cost",
        "Conservative cost per search in USD (0 for free backend)",
        "0",
    )
    config = RunConfig(
        name=name,
        firm=firm or None,
        known_profile_url=url or None,
        output_dir=str(pipeline.output_dir),
        public_search_enabled=False,
        maximum_cost_usd=maximum,
        search_operation_cost_usd=cost,
    )
    workspace = pipeline.workspace(slugify(name))
    if not url:
        backend = ui.select(
            "profile_backend",
            "Profile search backend",
            backend_options(),
        )
        provider = provider or (
            agent_reach_public_search_provider()
            if backend == "agent-reach"
            else pipeline.search_provider
        )
        if provider is None:
            raise ValueError(
                "No profile search backend configured. Supply a known profile URL."
            )
        query = f'"{name}" {firm or "venture investor"} profile'
        cache = workspace / "discovery/profile_options.json"
        provider_id = str(getattr(provider, "cache_identity", provider.provider_name))
        data = read_json(cache) if cache.exists() else {}
        results = []
        if data.get("query") == query and data.get("backend") == provider_id:
            results = [SearchResult.model_validate(row) for row in data["results"]]
            ui.show("Reusing cached profile search evidence.")
        else:
            ui.show(
                f"One profile search, up to 10 options; reserved cost ${cost}. "
                "This lookup is separate from subsequent platform-search counts."
            )
            if not ui.confirm("confirm_profile_search", "Search possible profiles?"):
                return None
            preflight = getattr(provider, "preflight", None)
            if preflight:
                problem = preflight(query)
                if problem:
                    raise ValueError(f"Profile backend unavailable: {redact(problem)}")
            ledger = BudgetLedger(workspace / "audit/costs.jsonl", maximum)
            operation = f"wizard-profile:{uuid4().hex}"
            ledger.reserve(operation, cost, provider=provider.provider_name)
            error = None
            try:
                results = provider.search(query, limit=10)[:10]
                diagnostics = getattr(provider, "diagnostics", [])
                if diagnostics:
                    ui.show({"search_diagnostics": redact(diagnostics)})
            except Exception as failure:
                error = redact(str(failure))
                ui.show(
                    f"Profile lookup failed: {error}. Supply a known profile URL instead."
                )
            finally:
                # Conservatively account for an attempted billable request even on failure.
                ledger.settle(operation, cost, provider=provider.provider_name)
            data = {
                "query": query,
                "backend": provider_id,
                "results": [row.model_dump(mode="json") for row in results],
                "error": error,
            }
            append_jsonl(workspace / "audit/profile_searches.jsonl", redact(data))
            if error is None:
                write_json(cache, redact(data))
        ui.show(
            {
                "possible_profiles_not_verified_people": [
                    r.model_dump(mode="json") for r in results
                ]
            }
        )
        url = ui.select(
            "profile",
            "Choose a possible profile, enter another URL, or stop",
            [
                *[Option(value=r.url, label=f"{r.title} — {r.url}") for r in results],
                Option(value="manual", label="Supply another profile URL"),
                Option(value="stop", label="Stop; identity still unresolved"),
            ],
        )
        if url == "stop":
            return None
        if url == "manual":
            url = ui.text("manual_url", "Public profile URL")
    canonical = canonicalize_url(url)
    if not canonical.startswith(("https://", "http://")):
        raise ValueError("A public HTTP(S) profile URL is required")
    ui.show(
        f"Selected identity evidence for {name}: {canonical}. "
        "Namesakes have not been merged. The page will now be retrieved for validation."
    )
    if not ui.confirm("confirm_profile", "Is this the intended person's profile?"):
        return None
    config.known_profile_url = canonical
    return canonical, config


def preserve_firm_hint(pipeline, slug, firm, url):
    """Keep a user's affiliation hint with provenance, without changing the name slug."""
    from .discovery import stable_id
    from .models import Affiliation, IdentityEvidence
    from .storage import read_jsonl, write_jsonl

    if not firm:
        return
    workspace = pipeline.workspace(slug)
    identity = pipeline._load_identity(slug)
    evidence_id = stable_id("identity", f"human-firm:{slug}:{firm}:{url}")
    if not any(a.firm.casefold() == firm.casefold() for a in identity.affiliations):
        identity.affiliations.append(Affiliation(firm=firm, evidence_ids=[evidence_id]))
    if evidence_id not in identity.evidence_ids:
        identity.evidence_ids.append(evidence_id)
    path = workspace / "identity/identity_evidence.jsonl"
    evidence = read_jsonl(path)
    if not any(e["evidence_id"] == evidence_id for e in evidence):
        evidence.append(
            IdentityEvidence(
                evidence_id=evidence_id,
                url=url,
                canonical_url=url,
                query="User supplied affiliation hint",
                search_provider="human",
                excerpt=firm,
                claim=f"User reports affiliation with {firm}; dates/current status unknown",
                validation_status="human_supplied_unreviewed",
            ).model_dump(mode="json")
        )
    write_jsonl(path, evidence)
    write_json(workspace / "identity/resolved_identity.json", identity)

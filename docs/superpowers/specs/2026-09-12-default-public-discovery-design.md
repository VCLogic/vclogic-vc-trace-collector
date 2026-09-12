# Default Public Discovery Design

## Goal

Make `vc-trace-collector discover --name ...` execute useful public-source
discovery without requiring SearXNG or API credentials, while preserving the
existing identity review, leakage firewall, budgets, and audit trail.

## Selected approach

Discovery will use two deterministic, credential-free adapters by default:

1. A general-web adapter backed by the `ddgs` Python package. It searches for
   firm profiles, personal sites, blogs, articles, podcasts, and interviews.
2. A YouTube adapter backed by `yt-dlp`'s `ytsearch` facility. It executes the
   generated YouTube-specific queries and returns video metadata without
   downloading media during discovery.

An explicitly configured SearXNG endpoint remains supported and replaces the
default general-web adapter. Direct YouTube discovery remains independent so
that a general search outage does not remove YouTube results.

The no-key general-web adapter is best-effort: public search services can
throttle or change behavior. Such failures are recorded and isolated rather
than converted into a misleading successful empty result.

## Components

- `DdgSearchProvider` implements the existing `SearchProvider` protocol and
  normalizes public web results into `SearchResult` records.
- `YtDlpSearchProvider` implements the same protocol but accepts only generated
  YouTube queries. It invokes `yt-dlp --dump-json --flat-playlist --no-download`
  without using a shell.
- `CompositeSearchProvider` routes each query to the appropriate adapters and
  deterministically deduplicates canonical URLs.
- Pipeline construction selects providers in this order:
  1. explicitly injected provider (tests/operator integration);
  2. configured SearXNG plus direct YouTube search;
  3. default DDG plus direct YouTube search.

The existing discovery engine continues to fetch candidate pages, score source
and identity confidence, classify material roles, apply exclusion rules, and
require human review. Search results never bypass validation.

## CLI and dependency behavior

Basic installation includes the general no-key web-search dependency. The
YouTube adapter is available with the existing optional extra:

```bash
uv sync --extra youtube
```

The normal command requires no provider environment variables:

```bash
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --firm "Hyatt Family Office" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --output-dir outputs-full
```

`VC_TRACE_SEARCH_ENDPOINT` remains an optional SearXNG override. A new
`--disable-public-search` flag permits deterministic supplied-URL-only runs and
offline tests. The existing search-operation and monetary budgets apply to all
adapters.

## Data flow and auditability

Generated queries flow through the composite provider. Every returned result
retains its query, provider name, and rank. Provider invocation, failures,
retries, and costs use the existing audit and budget records. Candidate URLs
then follow the existing identity validation and policy path before appearing
in the review plan.

YouTube discovery retrieves metadata only. Media download remains part of the
later collection stage, so discovery does not incur unreviewed audiovisual
processing cost. The authoritative AV path transcribes normalized audio rather
than substituting platform captions.

## Safety and quality controls

- Subprocess arguments are passed as a list; query strings are never evaluated
  by a shell.
- Search results receive the existing canonicalization and deduplication.
- The known Pitch profile remains identity evidence and is excluded from the
  corpus by the leakage firewall.
- Ambiguous names such as author Michael S. Hyatt remain explicit competing
  hypotheses and require human review.
- A provider failure is visible in the audit and does not discard results from
  another provider.
- Search result counts and subprocess timeouts are bounded.

## Testing

Unit tests will use injected fake DDG responses and an injected subprocess
runner. They will verify normalization, YouTube-only routing, deduplication,
timeouts, malformed output, provider failure isolation, and shell-safe command
construction.

Pipeline tests will prove that default provider construction works without
SearXNG, that an explicit SearXNG endpoint overrides only general web search,
and that disabling public search preserves supplied-URL-only behavior.

One opt-in live test will search for Michael Hyatt and assert that discovery
returns at least one identity-relevant result and at least one YouTube result.
Paid APIs and credentials remain unnecessary for the default test suite.

## Acceptance criteria

- A fresh Michael Hyatt discovery executes web and YouTube searches without
  `VC_TRACE_SEARCH_ENDPOINT` or an API key.
- The audit shows nonzero search operations and provider provenance.
- The resulting review plan contains more than the three manually seeded
  sources when public providers respond successfully.
- Failures are explicit and resumable.
- Existing offline tests remain green.

# Default Public Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Restore credential-free YouTube discovery and add best-effort credential-free general web discovery to the normal CLI.

**Architecture:** New search adapters implement the existing `SearchProvider` protocol and are composed by query type. The normal CLI injects the composite provider; programmatic pipelines and offline tests remain deterministic unless a provider is explicitly injected. Search results continue through the existing confidence, policy, review, budget, and audit path.

**Tech Stack:** Python 3.11+, Pydantic, Typer, `ddgs`, `yt-dlp`, pytest, uv.

---

### Task 1: Add deterministic search adapters

**Files:**
- Create: `src/vc_trace_collector/public_search.py`
- Create: `tests/test_public_search.py`

- [x] **Step 1: Write failing adapter tests**

Add tests that instantiate `DdgSearchProvider` with an injected callable and
`YtDlpSearchProvider` with an injected subprocess runner. Assert normalized
`SearchResult` fields, canonical YouTube watch URLs, bounded result counts,
shell-free argument lists, malformed-line tolerance, and error diagnostics.
Add a composite-provider test proving YouTube queries route to yt-dlp while
ordinary queries route to DDG and a failing adapter returns an audited empty
result without preventing later queries.

- [x] **Step 2: Verify the adapter tests fail for the missing module**

Run:

```bash
uv run pytest tests/test_public_search.py -q
```

Expected: collection fails because `vc_trace_collector.public_search` does not
exist.

- [x] **Step 3: Implement the adapters**

Create these public APIs:

```python
class DdgSearchProvider:
    provider_name = "ddg"

    def __init__(self, search=None): ...
    def search(self, query: str, limit: int = 10) -> list[SearchResult]: ...


class YtDlpSearchProvider:
    provider_name = "yt-dlp"

    def __init__(self, runner=subprocess.run, timeout: float = 120): ...
    def search(self, query: str, limit: int = 10) -> list[SearchResult]: ...


class CompositeSearchProvider:
    provider_name = "public-search"

    def __init__(self, web, youtube): ...
    def search(self, query: str, limit: int = 10) -> list[SearchResult]: ...


def default_public_search_provider(*, searxng_endpoint: str | None = None): ...
```

`YtDlpSearchProvider` must call `yt-dlp` with a list equivalent to:

```python
[
    "yt-dlp",
    "--dump-json",
    "--flat-playlist",
    "--no-download",
    "--playlist-end",
    str(limit),
    f"ytsearch{limit}:{query}",
]
```

The composite catches adapter exceptions, appends concise public diagnostics,
and returns an empty result for that query. It never includes secrets or a
traceback in its diagnostics.

- [x] **Step 4: Verify the adapter tests pass**

Run:

```bash
uv run pytest tests/test_public_search.py -q
```

Expected: all tests pass.

- [x] **Step 5: Commit the adapters**

```bash
git add src/vc_trace_collector/public_search.py tests/test_public_search.py
git commit -m "feat: add credential-free public search adapters"
```

### Task 2: Enable adapters in the CLI with an offline opt-out

**Files:**
- Modify: `src/vc_trace_collector/cli.py`
- Modify: `src/vc_trace_collector/config.py`
- Modify: `src/vc_trace_collector/discovery.py`
- Modify: `src/vc_trace_collector/pipeline.py`
- Modify: `tests/test_pipeline_cli.py`

- [x] **Step 1: Write failing CLI and audit tests**

Add a CLI test whose default factory receives a composite provider without a
SearXNG environment variable. Add a test for `--disable-public-search` that
asserts `RunConfig.public_search_enabled` is false and no provider operations
run. Add a discovery test that asserts composite diagnostics appear in the
structured provider-operation audit data.

- [x] **Step 2: Verify the new tests fail**

Run:

```bash
uv run pytest tests/test_pipeline_cli.py tests/test_discovery.py -q
```

Expected: failures mention the missing CLI option, config field, or provider
diagnostics.

- [x] **Step 3: Wire default discovery into the CLI**

Add this config field:

```python
public_search_enabled: bool = True
```

Add `--disable-public-search` to `discover` and `collect`. When no custom
`pipeline_factory` is supplied to `create_app`, build a `Pipeline` with
`default_public_search_provider()`. If `VC_TRACE_SEARCH_ENDPOINT` is set, pass
it to the builder so SearXNG replaces DDG while yt-dlp remains active.

Before constructing `DiscoveryService`, select:

```python
search_provider = self.search_provider if config.public_search_enabled else None
```

Copy sanitized composite diagnostics into each matching
`provider_operations` entry so failures remain visible in
`audit/events.jsonl`.

- [x] **Step 4: Verify CLI and discovery tests pass**

Run:

```bash
uv run pytest tests/test_pipeline_cli.py tests/test_discovery.py -q
```

Expected: all tests pass.

- [x] **Step 5: Commit CLI integration**

```bash
git add src/vc_trace_collector/cli.py src/vc_trace_collector/config.py \
  src/vc_trace_collector/discovery.py src/vc_trace_collector/pipeline.py \
  tests/test_pipeline_cli.py tests/test_discovery.py
git commit -m "feat: enable public discovery by default in CLI"
```

### Task 3: Dependencies, documentation, and live Michael Hyatt proof

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md`
- Create: `tests/test_live_public_search.py`

- [x] **Step 1: Add a failing live acceptance test**

Create an opt-in `@pytest.mark.live` test that runs the default public provider
for one ordinary Michael Hyatt query and one YouTube query. Assert that the
combined normalized result set is nonempty and includes at least one YouTube
URL when public providers are available; skip with the recorded diagnostic
when a public service blocks the environment.

- [x] **Step 2: Update dependencies and lockfile**

Add `ddgs>=9,<10` to core dependencies and retain `yt-dlp` in the existing
`youtube` extra, then run:

```bash
uv lock
uv sync --extra youtube
```

- [x] **Step 3: Update the README**

Document that normal CLI discovery uses DDG plus direct yt-dlp search without
credentials; SearXNG is an optional override; `--disable-public-search` is for
offline/supplied-source runs; and public no-key search may be throttled.

- [x] **Step 4: Run static and offline verification**

Run:

```bash
uv run ruff check .
uv run pytest -q
```

Expected: lint succeeds and all offline tests pass.

- [x] **Step 5: Run live provider verification**

Run:

```bash
uv run pytest -q -m live tests/test_live_public_search.py
```

Expected: the test passes, or explicitly skips only when the public provider
reports environmental blocking.

- [x] **Step 6: Run fresh Michael Hyatt discovery**

Preserve `outputs/michael-hyatt` and execute:

```bash
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --firm "Hyatt Family Office" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --max-search-operations 30 \
  --output-dir outputs-full
```

Verify that `outputs-full/michael-hyatt-hyatt-family-office/audit/events.jsonl`
contains nonzero provider operations and that the review plan contains public
web and YouTube candidates when providers responded.

- [x] **Step 7: Commit documentation and dependency changes**

```bash
git add pyproject.toml uv.lock README.md tests/test_live_public_search.py
git commit -m "docs: explain default public discovery"
```


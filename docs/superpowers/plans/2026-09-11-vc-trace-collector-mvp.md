# VC Trace Collector MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a standalone, auditable name-to-corpus collector with Michael Hyatt as the live pilot and with reference-voice discovery and audiovisual processing extension points.

**Architecture:** Implement a staged modular monolith under `src/vc_trace_collector`. Immutable filesystem artifacts and canonical JSON/JSONL are the portable record; SQLite stores transactional operation state and cost reservations. Discovery is provider-neutral with a deterministic fallback and an optional OpenAI-compatible structured-output adapter, while collection, policy, normalization, export, and verification remain deterministic.

**Tech Stack:** Python 3.12, uv, Pydantic 2, Typer, HTTPX, Beautiful Soup, feedparser, pytest; optional yt-dlp, youtube-transcript-api, soundfile, scipy, pyannote.audio, and Whisper-compatible providers.

---

## File map

- `pyproject.toml`: package metadata, console script, core and optional dependencies, test configuration.
- `src/vc_trace_collector/models.py`: typed identity, source, artifact, document, attribution, audit, manifest, and report models.
- `src/vc_trace_collector/config.py`: TOML/run configuration loading and frozen snapshots.
- `src/vc_trace_collector/storage.py`: atomic JSON/JSONL, hashing, artifact storage, and SQLite state.
- `src/vc_trace_collector/audit.py`: sanitized event and cost ledger.
- `src/vc_trace_collector/policy.py`: exclusion matching and corpus eligibility.
- `src/vc_trace_collector/fetch.py`: safe, cached, rate-limited HTTP client.
- `src/vc_trace_collector/extract.py`: deterministic HTML/feed extraction and link discovery.
- `src/vc_trace_collector/discovery.py`: identity evidence, query generation, source/reference-voice candidates, and optional LLM refinement.
- `src/vc_trace_collector/collectors.py`: web, feed, supplied-file, and YouTube collector interfaces.
- `src/vc_trace_collector/av.py`: transcript, diarization, speaker matching, reference profiles, and FFmpeg helpers.
- `src/vc_trace_collector/process.py`: normalization, deterministic deduplication, and canonical document creation.
- `src/vc_trace_collector/export.py`: canonical and legacy exports.
- `src/vc_trace_collector/verify.py`: hash, lineage, exclusion, speaker, and manifest verification.
- `src/vc_trace_collector/pipeline.py`: resumable stage orchestration.
- `src/vc_trace_collector/cli.py`: `discover`, `review`, `collect`, `process`, `export`, `status`, and `verify` commands.
- `config/defaults.toml`: safe defaults and model/provider placeholders.
- `config/exclusions.example.toml`: generalized Pitch rules.
- `tests/`: unit, integration, contract, and opt-in live coverage.

### Task 1: Package scaffold and typed models

**Files:**
- Create: `pyproject.toml`
- Create: `src/vc_trace_collector/__init__.py`
- Create: `src/vc_trace_collector/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing model tests**

```python
from vc_trace_collector.models import Confidence, ResolvedIdentity, SourceCandidate


def test_identity_preserves_competing_hypotheses():
    identity = ResolvedIdentity(
        slug="michael-hyatt",
        canonical_name="Michael Hyatt",
        resolution_status="provisional",
        identity_confidence=Confidence(score=0.8, method="evidence_policy", version="1"),
        competing_hypotheses=["Michael S. Hyatt, author"],
    )
    assert identity.competing_hypotheses == ["Michael S. Hyatt, author"]


def test_source_candidate_requires_discovery_provenance():
    candidate = SourceCandidate(
        candidate_id="candidate-1",
        url="https://example.test/michael",
        canonical_url="https://example.test/michael",
        source_type="web_profile",
        material_role="identity_evidence",
        discovery_queries=["Michael Hyatt BlueCat investor"],
        identity_confidence=Confidence(score=0.9, method="name_firm_match", version="1"),
        source_confidence=Confidence(score=0.8, method="source_policy", version="1"),
    )
    assert candidate.discovery_queries
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `uv run pytest tests/test_models.py -q`

Expected: failure because the package and models do not exist.

- [ ] **Step 3: Implement the package and Pydantic models**

Define string enums for resolution, material role, approval, inclusion, and speaker attribution. Define `Confidence`, `Affiliation`, `ResolvedIdentity`, `IdentityEvidence`, `SourceCandidate`, `SourceDecision`, `RawArtifact`, `TranscriptInfo`, `SpeakerAttribution`, `SpeechSegment`, `CanonicalDocument`, `AuditEvent`, `CostEntry`, `CollectionManifest`, `QualityReport`, and `RunSummary`. Require timezone-aware UTC timestamps and validate confidence in `[0, 1]`.

```python
class Confidence(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    method: str
    version: str


class SourceCandidate(BaseModel):
    candidate_id: str
    url: str
    canonical_url: str
    source_type: SourceType
    material_role: MaterialRole = MaterialRole.UNKNOWN
    discovery_queries: list[str] = Field(min_length=1)
    identity_confidence: Confidence
    source_confidence: Confidence
    estimated_cost_usd: Decimal = Decimal("0")
    estimated_media_seconds: float = 0
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
```

- [ ] **Step 4: Run model tests**

Run: `uv run pytest tests/test_models.py -q`

Expected: all model tests pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src tests/test_models.py
git commit -m "feat: scaffold typed collector domain"
```

### Task 2: Deterministic storage, state, audit, and budget

**Files:**
- Create: `src/vc_trace_collector/storage.py`
- Create: `src/vc_trace_collector/audit.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_audit_budget.py`

- [ ] **Step 1: Write failing storage and budget tests**

```python
def test_artifacts_are_addressed_by_sha256(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes(b"public evidence", category="web", suffix=".html")
    assert artifact.sha256 == sha256(b"public evidence").hexdigest()
    assert artifact.path.read_bytes() == b"public evidence"


def test_budget_refuses_operation_before_limit_is_exceeded(tmp_path):
    ledger = BudgetLedger(tmp_path / "costs.jsonl", maximum=Decimal("1.00"))
    ledger.reserve("first", Decimal("0.75"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve("second", Decimal("0.26"))
```

- [ ] **Step 2: Run tests and verify missing implementations**

Run: `uv run pytest tests/test_storage.py tests/test_audit_budget.py -q`

Expected: failures for missing storage and audit modules.

- [ ] **Step 3: Implement atomic storage, SQLite operation state, event redaction, and reservations**

Use SHA-256 paths, canonical JSON with sorted keys, UTF-8 JSONL appends, temporary-file replacement, and SQLite tables for operations and reservations. Redact keys matching token, secret, password, cookie, and authorization.

```python
class BudgetLedger:
    def reserve(self, operation_id: str, amount: Decimal) -> None:
        if self.committed + self.reserved + amount > self.maximum:
            raise BudgetExceeded(operation_id)
        self._append(CostEntry(operation_id=operation_id, kind="reservation", amount_usd=amount))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_storage.py tests/test_audit_budget.py -q`

Expected: all storage and budget tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/storage.py src/vc_trace_collector/audit.py tests
git commit -m "feat: add auditable artifact state and budgets"
```

### Task 3: URL safety, canonicalization, exclusions, and corpus gates

**Files:**
- Create: `src/vc_trace_collector/policy.py`
- Create: `config/defaults.toml`
- Create: `config/exclusions.example.toml`
- Test: `tests/test_policy.py`

- [ ] **Step 1: Write failing policy tests**

```python
def test_pitch_profile_is_identity_evidence_but_excluded_from_corpus():
    rules = RuleSet.pitch_default()
    decision = rules.evaluate(
        url="https://www.thepitch.show/investors/michael-hyatt",
        title="Michael Hyatt | The Pitch",
        channel=None,
        text="investment episodes and outcomes",
    )
    assert decision.status == "excluded"
    assert decision.rule_id == "exclude-the-pitch"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://127.0.0.1/private",
    "http://169.254.169.254/latest/meta-data",
])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(UnsafeUrl):
        validate_public_url(url)
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_policy.py -q`

Expected: failures because policy functions do not exist.

- [ ] **Step 3: Implement canonical URLs, public-network validation, rules, and inclusion gates**

Rules match normalized domains, URL patterns, channels, programmes, companies, and keywords. Evaluate before collection and before export. Unknown and review-required items fail closed.

```python
def eligible_for_corpus(document: CanonicalDocument) -> bool:
    return (
        document.inclusion_status == InclusionStatus.INCLUDED
        and document.material_role in {MaterialRole.AUTHORED_BY_TARGET, MaterialRole.SPOKEN_BY_TARGET}
        and document.speaker_attribution.status not in {SpeakerStatus.UNCERTAIN, SpeakerStatus.UNAVAILABLE}
    )
```

- [ ] **Step 4: Run policy tests**

Run: `uv run pytest tests/test_policy.py -q`

Expected: all policy tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/policy.py config tests/test_policy.py
git commit -m "feat: enforce network and leakage policy"
```

### Task 4: Safe fetching and deterministic extraction

**Files:**
- Create: `src/vc_trace_collector/fetch.py`
- Create: `src/vc_trace_collector/extract.py`
- Create: `tests/fixtures/michael_hyatt_profile.html`
- Create: `tests/fixtures/feed.xml`
- Test: `tests/test_fetch_extract.py`

- [ ] **Step 1: Write failing extraction tests**

```python
def test_extracts_identity_from_michael_hyatt_profile(profile_html):
    page = extract_page(profile_html, "https://www.thepitch.show/investors/michael-hyatt")
    assert page.title == "Michael Hyatt"
    assert "BlueCat" in page.text
    assert page.canonical_url.endswith("/investors/michael-hyatt")


def test_feed_entries_preserve_author_and_source(feed_bytes):
    entries = extract_feed(feed_bytes, "https://example.test/feed")
    assert entries[0].author == "Michael Hyatt"
    assert entries[0].discovered_from == "https://example.test/feed"
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_fetch_extract.py -q`

Expected: failures for missing fetch/extract modules.

- [ ] **Step 3: Implement HTTP fetching and HTML/feed extraction**

Use one HTTPX client with redirect validation, response size limits, safe headers, retry classification, conditional request metadata, and injected transport for tests. Extract JSON-LD, canonical link, title, author, dates, main text, and same-domain links while preserving raw HTML.

```python
class Fetcher:
    def fetch(self, url: str) -> FetchResult:
        validate_public_url(url, resolver=self.resolver)
        response = self.client.get(url, follow_redirects=False)
        return self._follow_validated_redirects(response)
```

- [ ] **Step 4: Run extraction tests**

Run: `uv run pytest tests/test_fetch_extract.py -q`

Expected: all extraction tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/fetch.py src/vc_trace_collector/extract.py tests
git commit -m "feat: add safe web and feed extraction"
```

### Task 5: Identity discovery, source planning, and reference-voice candidates

**Files:**
- Create: `src/vc_trace_collector/discovery.py`
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write failing discovery tests**

```python
def test_known_profile_resolves_bluecat_michael_not_author(profile_fixture):
    result = DiscoveryService(fetcher=profile_fixture).discover(
        name="Michael Hyatt",
        firm=None,
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    assert "BlueCat" in {a.firm for a in result.identity.affiliations}
    assert "Michael S. Hyatt, author" in result.identity.competing_hypotheses
    assert result.source_plan.requires_review is True


def test_discovery_proposes_voice_queries():
    queries = generate_queries("Michael Hyatt", ["BlueCat"])
    assert '"Michael Hyatt" BlueCat interview' in queries
    assert any("podcast" in query for query in queries)
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_discovery.py -q`

Expected: failures because discovery is missing.

- [ ] **Step 3: Implement bounded deterministic discovery plus optional structured LLM refinement**

The deterministic layer parses the known profile, generates identity/material/voice queries, classifies obvious links, and retains namesake hypotheses. The optional LLM adapter receives only structured evidence and must return schema-valid JSON; invalid output is rejected and audited. No chain-of-thought is requested or stored.

```python
class DiscoveryProvider(Protocol):
    def refine(self, request: DiscoveryRequest) -> DiscoveryRefinement: ...


def generate_queries(name: str, firms: list[str]) -> list[str]:
    anchors = firms or ["venture investor"]
    return sorted({
        f'"{name}" {anchor}',
        f'"{name}" {anchor} interview',
        f'"{name}" {anchor} podcast',
        f'"{name}" {anchor} YouTube',
    })
```

- [ ] **Step 4: Run discovery tests**

Run: `uv run pytest tests/test_discovery.py -q`

Expected: all discovery tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/discovery.py tests/test_discovery.py
git commit -m "feat: discover identities sources and voice candidates"
```

### Task 6: Review gate and deterministic collectors

**Files:**
- Create: `src/vc_trace_collector/collectors.py`
- Test: `tests/test_collectors.py`
- Test: `tests/test_review.py`

- [ ] **Step 1: Write failing collector and review tests**

```python
def test_collection_refuses_unreviewed_plan(tmp_path, pending_plan):
    with pytest.raises(ReviewRequired):
        collect_approved_sources(pending_plan, workspace=tmp_path)


def test_one_failed_source_does_not_remove_success(tmp_path, mixed_collectors):
    result = collect_approved_sources(mixed_collectors.plan, workspace=tmp_path)
    assert result.collected == 1
    assert result.failed == 1
    assert next((tmp_path / "raw/web").rglob("*.html")).exists()
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_collectors.py tests/test_review.py -q`

Expected: failures for missing collectors and review gate.

- [ ] **Step 3: Implement approval decisions and web/feed/supplied/YouTube collectors**

Define `Collector` protocol, registry, per-candidate operations, raw sidecars, and structured failures. YouTube support uses lazy optional imports for metadata and captions; media acquisition runs only after exclusions and duration-budget checks. Supplied files are copied into content-addressed storage.

```python
def collect_approved_sources(plan: SourcePlan, workspace: Path) -> CollectionResult:
    if plan.requires_review and any(c.approval_status == ApprovalStatus.PENDING for c in plan.candidates):
        raise ReviewRequired(plan.plan_id)
    for candidate in plan.candidates:
        if candidate.approval_status == ApprovalStatus.APPROVED:
            run_isolated_collection(candidate, workspace)
```

- [ ] **Step 4: Run collector tests**

Run: `uv run pytest tests/test_collectors.py tests/test_review.py -q`

Expected: all collector and review tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/collectors.py tests
git commit -m "feat: collect reviewed sources independently"
```

### Task 7: Audiovisual providers and uncertainty-safe speaker matching

**Files:**
- Create: `src/vc_trace_collector/av.py`
- Test: `tests/test_av.py`

- [ ] **Step 1: Write failing audiovisual policy tests**

```python
def test_weak_best_match_is_uncertain():
    decision = match_target_speaker(
        reference=np.array([1.0, 0.0]),
        speakers={"A": np.array([0.60, 0.80]), "B": np.array([0.55, 0.835])},
        minimum_score=0.75,
        minimum_margin=0.10,
    )
    assert decision.status == "uncertain"


def test_clear_match_records_score_and_margin():
    decision = match_target_speaker(
        reference=np.array([1.0, 0.0]),
        speakers={"A": np.array([1.0, 0.0]), "B": np.array([0.0, 1.0])},
        minimum_score=0.75,
        minimum_margin=0.10,
    )
    assert decision.status == "accepted_model"
    assert decision.score == pytest.approx(1.0)
    assert decision.margin == pytest.approx(1.0)
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_av.py -q`

Expected: failure because audiovisual APIs do not exist.

- [ ] **Step 3: Implement provider protocols, FFmpeg derivatives, transcript alignment, reference profiles, and matching**

Define lazy `TranscriptProvider`, `DiarizationProvider`, and `EmbeddingProvider` protocols. Implement model-independent matching and interval overlap alignment in core. Provide optional adapters for command-line FFmpeg, existing captions, Whisper-compatible STT, and pyannote without importing heavy dependencies at module import time.

```python
def match_target_speaker(reference, speakers, minimum_score, minimum_margin):
    ranked = sorted(
        ((label, cosine_similarity(reference, vector)) for label, vector in speakers.items()),
        key=lambda item: item[1], reverse=True,
    )
    label, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
    margin = score - runner_up
    status = "accepted_model" if score >= minimum_score and margin >= minimum_margin else "uncertain"
    return SpeakerMatch(label=label, score=score, runner_up_score=runner_up, margin=margin, status=status)
```

- [ ] **Step 4: Run audiovisual tests**

Run: `uv run pytest tests/test_av.py -q`

Expected: all audiovisual tests pass without installing audiovisual extras.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/av.py tests/test_av.py
git commit -m "feat: add configurable speaker attribution pipeline"
```

### Task 8: Processing, deduplication, export, and verification

**Files:**
- Create: `src/vc_trace_collector/process.py`
- Create: `src/vc_trace_collector/export.py`
- Create: `src/vc_trace_collector/verify.py`
- Test: `tests/test_process_export.py`
- Test: `tests/test_verify.py`

- [ ] **Step 1: Write failing processing and compatibility tests**

```python
def test_exact_duplicate_retains_relationship():
    docs = deduplicate([document("one", "same text"), document("two", "same text")])
    assert docs[1].duplicate_of == docs[0].document_version_id


def test_legacy_export_omits_excluded_and_uncertain(tmp_path):
    export_persona_sources(tmp_path, [included_blog(), excluded_pitch(), uncertain_talk()])
    blog = read_jsonl(tmp_path / "blog.jsonl")
    talks = read_jsonl(tmp_path / "talks.jsonl")
    assert list(blog[0]) == ["doc_id", "title", "source", "full_text"]
    assert talks == []
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_process_export.py tests/test_verify.py -q`

Expected: failures because processing/export/verification are missing.

- [ ] **Step 3: Implement normalization, fingerprints, canonical and legacy exports, manifests, quality, and verification**

Normalize Unicode and whitespace without rewriting meaning. Use stable source/content/version hashes and deterministic ordering. Verification checks file hashes, source approval, raw lineage, exclusions, speaker policy, counts, and regeneration fingerprint.

```python
def legacy_blog_row(document: CanonicalDocument) -> dict[str, object]:
    return {
        "doc_id": document.source_item_id,
        "title": document.title,
        "source": document.source_type.value,
        "full_text": document.text,
    }
```

- [ ] **Step 4: Run processing and verification tests**

Run: `uv run pytest tests/test_process_export.py tests/test_verify.py -q`

Expected: all processing, export, and verification tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/process.py src/vc_trace_collector/export.py src/vc_trace_collector/verify.py tests
git commit -m "feat: produce verified canonical and legacy corpora"
```

### Task 9: Resumable pipeline and CLI

**Files:**
- Create: `src/vc_trace_collector/config.py`
- Create: `src/vc_trace_collector/pipeline.py`
- Create: `src/vc_trace_collector/cli.py`
- Test: `tests/test_pipeline_cli.py`

- [ ] **Step 1: Write failing end-to-end CLI tests**

```python
def test_discover_writes_reviewable_plan(cli, tmp_path, profile_server):
    result = cli.invoke(app, [
        "discover", "--name", "Michael Hyatt", "--known-profile-url",
        profile_server.url, "--output-dir", str(tmp_path),
    ])
    assert result.exit_code == 0
    assert next(tmp_path.rglob("source_plan.json")).exists()


def test_collect_stops_at_review_checkpoint(cli, tmp_path, profile_server):
    result = cli.invoke(app, [
        "collect", "--name", "Michael Hyatt", "--known-profile-url",
        profile_server.url, "--output-dir", str(tmp_path),
    ])
    assert result.exit_code == 3
    assert "review required" in result.output.lower()
```

- [ ] **Step 2: Run tests and verify failures**

Run: `uv run pytest tests/test_pipeline_cli.py -q`

Expected: failures because CLI orchestration is missing.

- [ ] **Step 3: Implement configuration, commands, stage resume, status, and exit codes**

Expose `discover`, `review`, `collect`, `process`, `export`, `status`, and `verify`. `collect` is the umbrella command and exits cleanly at review unless explicit auto-approval passes. Save frozen run configuration and all decisions.

```python
@app.command()
def collect(
    name: str = typer.Option(...),
    firm: str | None = None,
    known_profile_url: str | None = None,
    output_dir: Path = Path("outputs"),
    auto_approve_discovery: bool = False,
    resume: str | None = None,
) -> None:
    Pipeline(output_dir).collect(
        name=name,
        firm=firm,
        known_profile_url=known_profile_url,
        auto_approve=auto_approve_discovery,
        resume=resume,
    )
```

- [ ] **Step 4: Run the complete offline suite**

Run: `uv run pytest -q`

Expected: all tests pass without network or paid credentials.

- [ ] **Step 5: Commit**

```bash
git add src/vc_trace_collector/config.py src/vc_trace_collector/pipeline.py src/vc_trace_collector/cli.py tests/test_pipeline_cli.py
git commit -m "feat: expose resumable collector CLI"
```

### Task 10: Documentation, security checks, and Michael Hyatt live pilot

**Files:**
- Create: `README.md`
- Create: `SECURITY.md`
- Create: `NOTICE.md`
- Create: `.env.example`
- Create: `.gitignore`
- Test: `tests/live/test_michael_hyatt.py`

- [ ] **Step 1: Add an opt-in live acceptance test**

```python
@pytest.mark.live
def test_michael_hyatt_profile_is_identity_evidence_and_excluded(tmp_path):
    result = run_discovery(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
        output_dir=tmp_path,
    )
    assert result.identity.canonical_name == "Michael Hyatt"
    assert any(a.firm == "BlueCat" for a in result.identity.affiliations)
    profile = next(c for c in result.candidates if "thepitch.show" in c.canonical_url)
    assert profile.material_role == "identity_evidence"
    assert profile.approval_status == "rejected"
```

- [ ] **Step 2: Run the offline suite before live access**

Run: `uv run pytest -m 'not live' -q`

Expected: all offline tests pass.

- [ ] **Step 3: Document installation, commands, provider variables, platform limits, and credential policy**

Document core and optional extras, human review, auto-approval, resume, exclusion configuration, reference voice, model selection, budgets, output contracts, and the separation from Investment Memory. `.env.example` contains names and empty placeholders only. `.gitignore` excludes `.env`, cookies, outputs, caches, media, model artifacts, and virtual environments.

- [ ] **Step 4: Execute the live Michael Hyatt discovery and firewall test**

Run:

```bash
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --output-dir outputs
uv run pytest tests/live/test_michael_hyatt.py -m live -q
```

Expected: identity evidence mentions BlueCat; the profile is preserved as evidence but rejected from corpus collection by the Pitch rule; a reviewable independent-source and reference-voice plan is produced.

- [ ] **Step 5: Run release verification and commit**

```bash
uv run pytest -q
uv run vc-trace-collector status --investor michael-hyatt --output-dir outputs
uv run vc-trace-collector verify --investor michael-hyatt --output-dir outputs
git add README.md SECURITY.md NOTICE.md .env.example .gitignore tests/live
git commit -m "docs: document and verify collector MVP"
```

Expected: tests pass; status reports the live discovery checkpoint; verification reports no excluded Pitch material in any corpus file.


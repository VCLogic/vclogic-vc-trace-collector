# VC Trace Collector Design

## Purpose

`vc-trace-collector` is a standalone Python repository that turns a venture
capitalist's name, optional firm, and optional known profile into a clean,
auditable, source-linked corpus of that investor's public traces.

The repository stops at corpus creation. It does not generate Investment
Memory, apply a rationale taxonomy, assess pitches, predict investment
decisions, or support founder rehearsal.

## First milestone

The first milestone proves the complete workflow for Charles Hudson:

1. Resolve Charles Hudson and Precursor Ventures using retrieved evidence.
2. Discover written and audiovisual source candidates.
3. Discover candidate reference-voice recordings.
4. Produce a costed source plan and pause for human review by default.
5. Collect at least one first-person web or feed source, one supplied local
   artifact, and one non-excluded audiovisual appearance.
6. Reject at least one source through the configurable Pitch leakage rule.
7. Process one audiovisual item through transcript selection or STT,
   diarization, reference-voice matching, and target-speech extraction.
8. Route a deliberately weak speaker match to human review.
9. Demonstrate isolated source failure and successful resume.
10. Produce canonical outputs, a public audit trace, quality reports, and the
    legacy `blog.jsonl`, `talks.jsonl`, and `_manifest.json` export.

Podcast, X, LinkedIn-export, and broader platform coverage use the same
interfaces and schemas, but they do not delay this vertical milestone.

## Existing implementation disposition

The old repositories are read-only references. The new repository has no
runtime imports from them and does not copy their data, credentials, cookies,
caches, virtual environments, or generated corpora.

Concepts to adapt include:

- RSS, sitemap, pagination, and link discovery from the blog collectors.
- Playwright with a lightweight HTTP fallback.
- Medium RSS/profile and generic website extraction strategies.
- YouTube search, channel, playlist, metadata, and caption acquisition.
- Optional X API collection when approved credentials are available.
- `yt-dlp` and FFmpeg media acquisition and audio normalization.
- Diarization, reference embedding, cosine speaker comparison, and ordered
  reconstruction of target segments.
- Per-source failure isolation and incremental operation.
- The intent of the existing Pitch firewall and legacy persona-source export.

The orchestration, schemas, identity controls, URL handling, storage,
confidence policy, audiovisual checkpointing, exclusion engine, audit ledger,
budget enforcement, verification, and package structure are rewritten.

The inspected reference repositories have no explicit license or notice. Code
is rewritten from observed behavior unless reuse rights are confirmed. Any
adapted implementation is recorded in `NOTICE.md` with source repository,
source path, commit where available, author, and adaptation notes. Generated
or collected third-party content is not assumed to be redistributable merely
because it is public.

## Architecture

The system is a modular monolith: one installable `vc_trace_collector` Python
package, one CLI, immutable filesystem artifacts, and one SQLite operational
state database per investor workspace.

The filesystem is the portable source of record. SQLite provides transactional
checkpoints, item state, locks, cost reservations, and fast status queries. It
can be rebuilt from manifests and audit artifacts.

The pipeline is staged:

```text
input and frozen run configuration
  -> identity hypotheses and evidence
  -> source discovery and deterministic validation
  -> costed source plan
  -> human review or explicit thresholded auto-approval
  -> independent deterministic collectors
  -> immutable raw artifacts
  -> written and audiovisual processors
  -> identity, attribution, exclusion, deduplication, and quality gates
  -> canonical corpus
  -> compatibility export and verification
```

Each operation has an idempotency key derived from its input artifact hashes,
relevant configuration hash, processor version, and model identifier. A model
or rule change invalidates only affected downstream operations.

Writes use temporary files followed by atomic replacement. Successful artifacts
are never removed because another source fails. Retrying an operation creates
an audit event and never overwrites immutable raw evidence.

## Package structure

```text
vc-trace-collector/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── NOTICE.md
├── SECURITY.md
├── config/
│   ├── defaults.toml
│   └── exclusions.example.toml
├── src/vc_trace_collector/
│   ├── cli.py
│   ├── config.py
│   ├── models/
│   ├── discovery/
│   ├── collectors/
│   ├── processors/
│   ├── policy/
│   ├── providers/
│   ├── orchestration/
│   ├── storage/
│   ├── export/
│   └── verification.py
└── tests/
    ├── fixtures/
    ├── unit/
    ├── integration/
    ├── contract/
    └── live/
```

Core dependencies are lightweight. Browser automation, YouTube acquisition,
provider SDKs, and audiovisual ML dependencies are optional `uv` extras.
FFmpeg is an explicitly checked external capability. No CUDA-specific package
index, model, device, provider, or credential is hard-coded.

## Identity and discovery

The discovery agent is a bounded typed tool loop, not a general autonomous
agent framework. Its only tools are configured search and evidence retrieval.
It has maximum turns, search-result limits, media-minute limits, and a monetary
budget.

The LLM may:

- Propose distinct identity hypotheses, aliases, and current or prior firms.
- Generate targeted search queries.
- Extract structured identity claims from retrieved evidence.
- Classify likely authored, spoken, third-party, and irrelevant sources.
- Produce concise evidence-linked rationales and a source plan.

The LLM may not finally resolve ambiguous people, approve prohibited sources,
override exclusions, or declare uncertain speech verified. Identity acceptance
uses deterministic evidence policy or human review.

`ResolvedIdentity` stores canonical name, aliases, current and time-bounded
previous affiliations, authoritative profiles, supporting evidence IDs,
identity confidence and method, resolution status, and reviewer information.

`IdentityEvidence` stores the discovery query, search provider and rank,
retrieved URL, publisher, retrieval time, bounded excerpt, extracted claim,
raw-artifact hash, and validation result.

`SourceCandidate` stores source type, canonical URL, material role, discovery
provenance, source confidence, identity confidence, expected collection and
processing cost, expected media duration, evidence IDs, and approval state.

Scores are always accompanied by their calculation method and model or policy
version. Human status is distinct from numeric confidence. Competing people
remain visible until explicitly rejected.

## Reference-voice discovery

Discovery also proposes reference-voice candidates from firm-hosted videos,
personal channels, clearly introduced conference talks, solo podcasts, and
user-supplied recordings.

Candidate selection is grounded independently of voice similarity to avoid
circular attribution. A candidate records its source URL, platform ID,
identity evidence, proposed start and end time, detected speaker count,
overlap estimate, audio-quality metrics, extraction method, and audio hash.

Statuses are:

- `user_supplied_verified`
- `human_verified_public_source`
- `high_confidence_candidate`
- `uncertain`
- `rejected`

Only the first two create a verified reference profile by default. Explicit
automated runs may use a high-confidence candidate, but subsequent attribution
is labelled `accepted_model`, never `verified_human`. Multiple independently
verified samples are combined when available, and disagreement is reported.

## Collection

Collectors implement a shared typed protocol and emit raw artifacts plus
metadata. Initial collectors are HTTP web pages, RSS/Atom feeds, Substack and
Medium through common feed/web strategies, YouTube metadata/captions/media,
and supplied local files. Podcast feeds/media, X API/export, and LinkedIn
export adapters follow the same protocol.

The fetch layer provides:

- Per-host rate limits and concurrency controls.
- Timeouts, exponential backoff, jitter, and `Retry-After` handling.
- ETag and Last-Modified conditional requests.
- Response status, headers safe for publication, redirect chain, MIME type,
  byte length, and retrieval timestamp.
- HTTP(S)-only network policy with DNS and redirect checks against private,
  loopback, link-local, and otherwise unsafe destinations.
- Configurable body and media size limits.
- Raw byte hashing and content-addressed storage.

Local supplied files are snapshotted into raw storage so later processing does
not depend on a mutable original path. The original path is retained as
provenance subject to public-trace redaction policy.

## Audiovisual processing

The audiovisual pipeline is:

```text
media artifact
  -> normalized audio derivative
  -> timestamped existing transcript when usable, otherwise configured STT
  -> configured diarization
  -> per-speaker embedding comparison with approved reference profile
  -> transcript/diarization time alignment
  -> target speech segments and attribution decision
```

An existing transcript is preferred, but an unlabelled transcript without
timestamps cannot establish voice attribution. In that case the processor
creates an aligned STT transcript or marks attribution unavailable.

Speaker acceptance requires both a configured model-specific absolute score
and a configured margin over the runner-up. Stored evidence includes the score,
runner-up score, margin, threshold, reference artifact IDs, embedding model,
diarization model, and review status.

Speaker statuses are `verified_human`, `accepted_model`, `uncertain`,
`rejected`, and `unavailable`. Uncertain or unavailable speech is excluded from
the default corpus and routed to review.

Original media is immutable. Normalized PCM and compressed audio are derived
artifacts. Destructive pruning is outside normal collection and requires a
separate explicit retention action after verification.

## Canonical records and lineage

Identifiers distinguish a logical source from its content versions:

```text
source_item_id = hash(source type + canonical URL or platform identifier)
content_hash = hash(canonical normalized content)
document_version_id = hash(source_item_id + content_hash)
```

`RawArtifact` stores source URL or supplied path, MIME type, original metadata,
collection method/version, retrieval timestamp, byte hash, size, parent
artifact, and rights/terms notes.

`CanonicalDocument` works across modalities and stores investor ID, source and
artifact IDs, canonical URL or local path, source type, modality, material
role, title, authors, speakers, publication date and precision, collection
time, normalized text, content hash, original metadata, extraction method,
transcript method, speaker attribution, identity assessment, inclusion
decision, duplicate relationship, and complete parent lineage.

`SpeechSegment` stores start, end, text, diarized label, attribution status,
match evidence, transcript provenance, and review decision.

Exact deduplication uses canonical URL/platform ID and content hashes. Near
deduplication uses deterministic text fingerprints. Every suppressed duplicate
retains a `duplicate_of` link and the policy that selected the preferred copy.

## Exclusion policy

Versioned rules match domains, URL patterns, channel IDs and names, programme
titles, companies, authors, speakers, titles, descriptions, and content
keywords. Actions are `exclude` or `review`.

Rules run during candidate discovery, before expensive download, after content
extraction, and immediately before export. The decision stores rule ID and
version, stage, matched-field summary, action, decision source, and override
history.

Leakage-sensitive exports fail closed: excluded, unknown, and review-required
material cannot enter the corpus. Only target-authored writing and accepted
target speech are included by default. Firm profiles and third-party material
remain evidence unless separable target-authored or target-spoken content is
verified.

The existing Pitch firewall becomes a normal rule set covering known channel
IDs and names, programme names, domains, and configured keywords.

## Output layout

```text
outputs/<investor-slug>/
├── identity/
│   ├── resolved_identity.json
│   ├── identity_evidence.jsonl
│   ├── reference_voice_candidates.jsonl
│   ├── approved_reference_voices.jsonl
│   └── reference_voice_profile.json
├── discovery/
│   ├── source_candidates.jsonl
│   ├── source_plan.json
│   ├── approved_sources.jsonl
│   └── rejected_sources.jsonl
├── raw/{web,social,video,podcast,supplied}/
├── processed/
│   ├── documents.jsonl
│   ├── target_speech.jsonl
│   └── excluded_documents.jsonl
├── corpus/
│   ├── blog.jsonl
│   ├── talks.jsonl
│   └── all_documents.jsonl
├── audit/
│   ├── events.jsonl
│   ├── operations.jsonl
│   ├── costs.jsonl
│   ├── failures.jsonl
│   └── runs/
├── state/state.sqlite
├── config_snapshot.json
├── collection_manifest.json
├── quality_report.json
└── run_summary.json
```

The public audit trace records concise action summaries, structured model
inputs and outputs, retrieved evidence, tool calls, decisions, failures,
retries, tokens, cost, and media duration. It does not store private model
chain-of-thought. Secrets, cookies, authorization headers, and sensitive local
paths are redacted.

Manifests use canonical JSON and stable ordering. Their fingerprint covers
selected corpus records, artifact hashes, configuration, rules, and model
identifiers. Volatile run timing and retry history remain in audit and run
summary files rather than destabilizing the corpus fingerprint.

## Budget enforcement

Each potentially paid operation provides a conservative estimate. The ledger
reserves that amount before the operation begins and settles it against actual
provider usage afterward. An operation that could exceed the remaining budget
is recorded as `budget_exhausted` and is not started. Retries require their own
reservation.

The run also limits search operations, provider calls, downloaded bytes, and
audiovisual minutes so local or nominally free work cannot expand without
bound.

## CLI

`collect` is the end-to-end command:

```text
uv run vc-trace-collector collect --name "Charles Hudson"
```

It runs discover, the review checkpoint, collection, processing, export, and
verification. It pauses after source-plan creation unless
`--auto-approve-discovery` is explicit and all configured thresholds pass.

Stage commands are `discover`, `review`, `collect`, `process`, `export`,
`status`, and `verify`. Options include firm, known profile URL, output path,
source types, exclusions, discovery/transcription/diarization/embedding models,
reference voice, maximum cost, maximum search/provider usage, maximum media
minutes, resume target, and collection-only, processing-only, or export-only
modes. Processing-only and export-only require an existing workspace.

`review` supports interactive decisions and a structured decision file for
batch automation. Every human or automatic decision produces an audit event.

## Verification and quality

Verification fails when identity has not been accepted, an included document
lacks approved-source and raw-artifact lineage, a hash is invalid, excluded or
pending material appears in the corpus, target speech lacks acceptable
attribution, an automatic match violates configured thresholds, manifest
counts disagree with files, or deterministic export regeneration changes the
fingerprint.

The quality report covers metadata completeness, extraction quality, source
diversity, first-person ratio, duplicate counts, transcript coverage, verified
target-speech duration, uncertain attribution, exclusion decisions, failures,
retries, and budget use. Unknown publication dates are permitted but reported.

The compatibility exporter produces strict downstream records:

- `blog.jsonl`: `doc_id`, `title`, `source`, `full_text`
- `talks.jsonl`: `doc_id`, `video_id`, `source`, `text`
- `_manifest.json`: legacy counts and leakage flags

The compatibility representation is generated from canonical records and is
never used as the internal schema.

## Testing

Unit tests cover schemas, URL canonicalization, network safety, hashing,
atomic writes, confidence policies, exclusions, normalization, deduplication,
transcript alignment, numeric segment ordering, budget reservations, and log
redaction.

Collector tests use local HTML, RSS, Atom, Medium-like, Substack-like, podcast,
and social-export fixtures. Audiovisual tests use a short synthetic or
appropriately licensed multi-speaker fixture plus fake STT, diarization, and
embedding providers.

An offline integration test runs a fictitious investor through fake discovery
and a local HTTP server. One source succeeds, one fails, one is excluded, a
weak voice match is routed to review, and resume completes without repeating
successful operations.

Contract tests generate the legacy persona-source files with synthetic text.
Security tests cover private-network URLs, unsafe redirects, path traversal,
malicious retrieved instructions, oversized downloads, and secret-bearing
provider errors. Live tests are opt-in and never required by default CI.

## Security response before reuse

The credential embedded in the old transcription experiment must be revoked or
rotated outside this repository. No old credential value, cookie file, `.env`,
or authenticated cache enters the new Git history. The new repository provides
only documented environment-variable names and placeholder examples.


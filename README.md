# vc-trace-collector

`vc-trace-collector` builds a clean, auditable, source-linked collection of a
venture investor's public traces. It resolves an identity, proposes a source
plan, pauses for review, collects approved public material, normalizes and
deduplicates it, applies leakage rules, and exports a canonical corpus plus the
legacy `blog.jsonl`, `talks.jsonl`, and `_manifest.json` files.

This repository does **not** generate an Investment Memory, score pitches,
predict investment decisions, or run founder rehearsals.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Status

The MVP is a modular Python package with:

- deterministic known-profile parsing, query generation, identity evidence,
  and explicit namesake hypotheses;
- credential-free DDG web search, direct `yt-dlp` YouTube search, optional
  SearXNG override, and optional structured LLM refinement;
- human review or confidence-gated automatic approval;
- safe, rate-limited web/feed collection, supplied-file ingestion, optional
  YouTube metadata/audio collection, and bounded podcast-enclosure
  downloads;
- configurable exclusion rules with a built-in The Pitch leakage firewall;
- content-addressed raw artifacts, resumable SQLite operation state, sanitized
  audit events, canonical documents, deterministic manifests, and verification;
- resumable provider-neutral transcription, diarization, human-approved voice
  embedding, target-speaker matching, and target-only speech extraction.

The first vertical slice supports web pages, feeds, supplied files, YouTube,
and podcast pages/enclosures.
LinkedIn and X should be provided as user-approved exports unless the operator
configures an authorized collector. Audiovisual processing is opt-in because it
can be costly: a reference must be human-approved, and weak model matches are
routed to review rather than exported as verified speech.

## Install

Python 3.11–3.13 and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
uv sync
uv run vc-trace-collector --help
```

Install only the optional capabilities you need:

```bash
# YouTube discovery/download plus local Whisper and pyannote processing
uv sync --extra youtube --extra av --extra av-local

# Optional browser-backed web collection
uv sync --extra browser
```

The core web workflow does not install GPU frameworks, browser binaries, or
paid-provider SDKs.

Agent Reach is an optional external capability checker and router. Install it
in an isolated tool environment, then inspect (but do not modify) available
backends:

```bash
pipx install "https://github.com/Panniantong/agent-reach/archive/main.zip"
agent-reach install --env=auto
uv run vc-trace-collector doctor --json
```

`doctor` reports executables and the active backend Agent Reach selected for
each channel. It does not search for an investor and it does not install tools.
Use Agent Reach's explicit `--system` installer only if you intend it to modify
your user-level tool configuration.

Set local model configuration in `.env`; the CLI loads it automatically from
the repository's current working directory:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set HF_TOKEN and VC_TRACE_AV_DEVICE as needed.
```

Do not copy `.env` to another filename and commit it. Existing shell variables
take precedence over values in `.env`.

## Michael Hyatt: complete staged workflow

Run these stages from the repository root. Each stage is resumable; rerunning a
completed operation uses its audited cache.

### 1. Check dependencies

```bash
uv run vc-trace-collector doctor --json
```

For the complete audiovisual path, `ffmpeg`, `ffprobe`, and `yt-dlp` should be
reported as ready. Agent Reach web discovery additionally needs `agent-reach`
and `mcporter`; `doctor` only reports their status and never installs them.

### 2. Resolve the identity

```bash
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --disable-public-search
```

`--disable-public-search` affects this `discover` invocation only: it prevents
the built-in DDG/YouTube search while the known profile is used to resolve the
identity. It does not disable any later `search-source` command. Keeping the
steps separate makes platform searches explicit, independently resumable, and
easy to audit.

The supplied profile is retained as identity evidence but excluded from corpus
material by the configurable `thepitch.show` leakage firewall.

### 3. Search each source family

```bash
uv run vc-trace-collector search-source --investor michael-hyatt --source youtube --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source podcast --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source web_article --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source web_profile --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source rss_feed --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source substack --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source medium --backend agent-reach
uv run vc-trace-collector list-sources --investor michael-hyatt
```

Search only discovers URLs and appends candidates. It does not download media,
run Whisper, or run pyannote.

To run a precise search instead of the generated queries, repeat `--query`.
Explicit queries replace the generated set for that invocation:

```bash
uv run vc-trace-collector search-source \
  --investor michael-hyatt \
  --source youtube \
  --backend agent-reach \
  --query '"Michael Hyatt" "The Pitch" YouTube' \
  --query '"Michael Hyatt" BlueCat investor interview YouTube' \
  --limit-per-query 100 \
  --max-search-operations 100

uv run vc-trace-collector search-source \
  --investor michael-hyatt \
  --source web_article \
  --backend agent-reach \
  --query '"Michael Hyatt" BlueCat investor article' \
  --query '"Michael Hyatt" Dyadem founder interview'
```

`--max-search-operations` raises the total ceiling saved during discovery and
records the old and new values in the audit log. It cannot lower the saved
ceiling. The Pitch Show results are retained as discovered evidence with their
channel metadata, but the default leakage firewall marks them rejected rather
than allowing them into the final corpus.

If Agent Reach reports that Exa is offline, verify the backend outside the
collector before retrying. On a machine that reaches the internet through
`HTTP_PROXY`/`HTTPS_PROXY`, `mcporter` needs a Node version with environment
proxy support and an explicit opt-in:

```bash
nvm use 25
export NODE_USE_ENV_PROXY=1
mcporter call exa.web_search_exa \
  query='"Michael Hyatt" BlueCat investor' \
  numResults=1
```

You may put `NODE_USE_ENV_PROXY=1` in the repository-local `.env`; the CLI
loads it automatically. A missing `mcporter` is caught before a search starts.
If Exa cannot be reached, the command stops after the first failed query and
does not consume the saved search-operation allowance for that unreachable
backend attempt.

### 4. Review the proposed sources

Copy the real candidate IDs from `list-sources` into `decisions.json`. Mark a
clean recording intended for voice comparison as `reference_voice`; mark each
appearance whose target speech should enter the corpus as `spoken_by_target`:

```json
[
  {
    "candidate_id": "candidate:<reference-id>",
    "status": "approved",
    "reason": "Human confirmed identity and suitability as a voice reference",
    "decided_by": "human",
    "material_role": "reference_voice"
  },
  {
    "candidate_id": "candidate:<appearance-id>",
    "status": "approved",
    "reason": "Human confirmed the target investor appears in this recording",
    "decided_by": "human",
    "material_role": "spoken_by_target"
  }
]
```

Then record the identity and source decisions:

```bash
uv run vc-trace-collector review \
  --investor michael-hyatt \
  --confirm-identity \
  --decision-file decisions.json
```

Do not approve a namesake or uncertain source merely to continue the run.

### 5. Fetch approved material

Fetch one platform or candidate at a time:

```bash
uv run vc-trace-collector fetch-source --investor michael-hyatt --source youtube
uv run vc-trace-collector fetch-source --investor michael-hyatt --source podcast
uv run vc-trace-collector fetch-source --investor michael-hyatt --candidate-id 'candidate:<id>'
```

YouTube collection downloads metadata and audio. Platform captions are not used
for target-speech extraction.

### 6. Approve a clean reference-voice interval

After fetching the reference recording, take its `voice:` ID from
`outputs/michael-hyatt/identity/reference_voice_candidates.jsonl`. Listen to the
recording and select an interval containing Michael Hyatt alone—no host,
crosstalk, or music:

```bash
uv run vc-trace-collector review-voice \
  --investor michael-hyatt \
  --candidate-id 'voice:<actual-id>' \
  --reviewer human \
  --start-seconds 120 \
  --end-seconds 165 \
  --diarization-model pyannote/speaker-diarization-3.1
```

### 7. Process each target appearance

```bash
uv run vc-trace-collector process-source \
  --investor michael-hyatt \
  --candidate-id 'candidate:<appearance-id>' \
  --transcription-model turbo \
  --diarization-model pyannote/speaker-diarization-3.1
```

This stage extracts audio, diarizes speakers, compares them with the approved
voice reference, transcribes target turns, and flags uncertain attribution for
review.

### 8. Export and verify

```bash
uv run vc-trace-collector export --investor michael-hyatt
uv run vc-trace-collector verify --investor michael-hyatt
uv run vc-trace-collector status --investor michael-hyatt
```

The run is complete only when `verify` passes. Outputs are under
`outputs/michael-hyatt/`, including the compatibility files `blog.jsonl`,
`talks.jsonl`, and `_manifest.json`.

## Alternative all-in-one run

After discovery and review, the higher-level command can resume and execute the
remaining stages:

```bash
uv run vc-trace-collector collect \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --resume latest
```

For a non-interactive run, `--auto-approve-discovery` accepts only candidates
above the configured confidence thresholds. A remaining namesake hypothesis,
including the Michael S. Hyatt namesake, still stops cleanly for human review.

```bash
uv run vc-trace-collector collect \
  --name "Investor Name" \
  --firm "Firm Name" \
  --auto-approve-discovery \
  --max-cost-usd 5.00 \
  --max-search-operations 20 \
  --max-media-minutes 60
```

`--source-url` is repeatable. It seeds an independently discovered page or
recording into the same identity validation and review workflow; it never
bypasses confidence checks or exclusion rules.

When an LLM discovery provider is enabled, also pass a conservative upper-bound
reservation for its single refinement call. The call will not start without it:

```bash
--discovery-call-budget-usd 0.25
```

Stage commands are available independently:

```bash
uv run vc-trace-collector discover --help
uv run vc-trace-collector review --help
uv run vc-trace-collector search-source --help
uv run vc-trace-collector list-sources --help
uv run vc-trace-collector fetch-source --help
uv run vc-trace-collector review-voice --help
uv run vc-trace-collector collect --help
uv run vc-trace-collector process --help
uv run vc-trace-collector process-source --help
uv run vc-trace-collector export --help
uv run vc-trace-collector status --help
uv run vc-trace-collector verify --help
```

`collect` also accepts `--collection-only`, `--processing-only`, and
`--export-only`. Only one may be selected at a time.

## Agent Reach staged workflow

Agent Reach does not replace collectors. It identifies working upstream
backends: the staged CLI uses `yt-dlp` for YouTube and the Agent Reach Exa
configuration through `mcporter` for web, blog, and podcast URL discovery.
Each search appends to the same reviewable plan without removing prior human
decisions:

```bash
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --disable-public-search
uv run vc-trace-collector search-source \
  --investor michael-hyatt --source youtube --backend agent-reach
uv run vc-trace-collector search-source \
  --investor michael-hyatt --source podcast --backend agent-reach
uv run vc-trace-collector search-source \
  --investor michael-hyatt --source web_article --backend agent-reach
uv run vc-trace-collector list-sources --investor michael-hyatt
```

After reviewing the plan, download one platform or one item at a time:

```bash
uv run vc-trace-collector fetch-source \
  --investor michael-hyatt --source youtube
uv run vc-trace-collector fetch-source \
  --investor michael-hyatt --candidate-id 'candidate:<id>'
```

Run pyannote, reference-voice matching, target-turn extraction, and Whisper for
one approved appearance with:

```bash
uv run vc-trace-collector process-source \
  --investor michael-hyatt \
  --candidate-id 'candidate:<id>' \
  --transcription-model turbo \
  --diarization-model pyannote/speaker-diarization-3.1
```

The project-local agent skill at
`.agents/skills/vc-trace-collector/SKILL.md` instructs compatible coding agents
to use these same public, audited commands. It never grants credentials or
bypasses human review.

## Discovery providers

The normal CLI searches the public web through DDG without credentials. When
the `youtube` extra is installed, YouTube-specific queries use `yt-dlp`
directly, matching the discovery behavior of the reference collector:

```bash
uv sync --extra youtube
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt"
```

No-key public search is best-effort: a public service may throttle automated
requests or change its interface. Failures are isolated and recorded in the
discovery audit. Use `--disable-public-search` for offline tests or a run that
must evaluate only supplied profiles, URLs, and files.

An operator-controlled SearXNG JSON endpoint can replace DDG for general web
search. Direct YouTube discovery remains enabled:

```bash
export VC_TRACE_SEARCH_ENDPOINT="https://search.example/search"
```

Structured LLM refinement is enabled only when all three variables are set:

```bash
export VC_TRACE_LLM_ENDPOINT="https://provider.example/v1/chat/completions"
export VC_TRACE_LLM_API_KEY="..."
export VC_TRACE_DISCOVERY_MODEL="provider-model-name"
```

The LLM receives structured public evidence and returns schema-validated JSON.
The audit stores the model/provider operation, token usage when returned, and a
concise action record, never private chain-of-thought. Search, fetching,
approval, hashing, policy evaluation, normalization, deduplication, export, and
verification remain deterministic.

Do not commit these variables. Copy `.env.example` only as a list of supported
names, then place local values in `.env`:

```bash
cp .env.example .env
chmod 600 .env
```

The CLI automatically loads `.env` from the current working directory. Values
already exported in the shell take precedence over values in `.env`. The file
is ignored by Git and must never be force-added or committed.

## Reference voice and speaker attribution

Discovery generates interview, podcast, and YouTube queries and records likely
single-identity voice sources in
`identity/reference_voice_candidates.jsonl`. A usable voice reference should:

- have strong independent identity evidence;
- contain a clean interval dominated by the target investor;
- avoid music, crosstalk, or unidentified panel speech;
- be human-reviewed before it becomes a reference profile.

The authoritative AV path follows the original `transcribe_investors.py`
workflow: downloaded media is normalized by FFmpeg to mono 16 kHz WAV;
pyannote diarizes it and produces speaker embeddings; those embeddings are
compared with the human-approved reference voice; each diarized speaker turn
is extracted; and Whisper transcribes the extracted WAV segments. Platform
captions are not fetched or substituted for this transcription path. Only
segments attributed to the target speaker can enter `target_speech.jsonl`.

Cosine matching uses minimum-score and runner-up-margin gates. A low score or
narrow margin produces `uncertain`, not verified speech. Models, device
selection, and access tokens are supplied by the operator; none are hard-coded.

After collecting an approved reference source, inspect
`identity/reference_voice_candidates.jsonl`, select a clean interval, and approve
it explicitly:

```bash
uv run vc-trace-collector review-voice \
  --investor michael-hyatt \
  --candidate-id 'voice:<id from the JSONL file>' \
  --reviewer 'analyst@example.com' \
  --start-seconds 120 \
  --end-seconds 165 \
  --diarization-model "pyannote/speaker-diarization-3.1"

uv run vc-trace-collector process \
  --investor michael-hyatt \
  --transcription-model turbo \
  --diarization-model "pyannote/speaker-diarization-3.1"

uv run vc-trace-collector export --investor michael-hyatt
```

Stage-specific model and cost selections update `config_snapshot.json` and
append a structured configuration event to the audit trace. Options omitted at
a later invocation retain their previously saved values.

Use a decision-file entry with `"material_role": "spoken_by_target"` for an
appearance that should enter target-speech processing. A separate clean
reference recording is preferable to the recording being evaluated. Local
Whisper and pyannote adapters are available in the `av-local` extra; `HF_TOKEN`
and `VC_TRACE_AV_DEVICE` are optional environment variables consumed only by
the selected models.

Set conservative per-operation price estimates with
`--transcription-cost-usd`, `--diarization-cost-usd`, and
`--embedding-cost-usd`; search calls use `--search-operation-cost-usd`. They
are reserved transactionally before provider or model invocation.
`--max-download-bytes`, `--max-provider-operations`, and
`--max-media-minutes` are aggregate run ceilings. Media with an unknown
duration is rejected before model calls. Provider reservations are immutable
attempt records: retries and dispatched failures consume the operation ceiling,
and potentially billable failures are conservatively settled. The public
JSONL cost trace is reconciled against the transactional SQLite ledger during
verification.
By default, any failed or unresolved approved source prevents a verified
export; `--allow-partial-run` is an explicit, manifest-visible opt-out.

## Exclusions and source review

`config/exclusions.example.toml` demonstrates domain, channel, programme,
company, author, speaker, keyword, and URL-pattern controls. Rules are evaluated
at discovery, post-metadata collection, processing, export, and verification.
Items that are excluded, third-party, unknown, or have uncertain speaker
attribution cannot enter the corpus. A post-metadata `review` rule pauses before
media download; an analyst may record its rule ID in a decision file's
`override_rule_ids` and rerun review/collection. The decision history is part of
the signed manifest provenance.

Direct audio URLs are not downloaded during discovery. Their review decision
must include a positive `estimated_media_seconds` value before collection.
Extensionless podcast URLs are classified with a safe HEAD request before GET;
an audio/video MIME type without an approved duration also fails closed.

Pass the file with `--exclusion-file`. Its validated rule contents are frozen
inside `config_snapshot.json` and fingerprinted in the collection manifest.

Additional run-level exclusions are available from the CLI:

```bash
uv run vc-trace-collector collect \
  --name "Investor Name" \
  --exclude-domain example.com \
  --exclude-channel "Prohibited Show"
```

## Output contract

Each investor has an isolated workspace:

```text
outputs/<investor-slug>/
├── identity/
│   ├── resolved_identity.json
│   ├── identity_evidence.jsonl
│   ├── reference_voice_candidates.jsonl
│   └── reference_voice_profile.json
├── discovery/
│   ├── source_plan.json
│   ├── source_candidates.jsonl
│   ├── search_observations.jsonl
│   ├── approved_sources.jsonl
│   └── rejected_sources.jsonl
├── raw/{web,social,video,podcast,supplied}/
├── processed/
│   ├── documents.jsonl
│   ├── target_speech.jsonl
│   ├── av_attribution_results.jsonl
│   ├── av_candidate_outcomes.jsonl
│   ├── collection_candidate_outcomes.jsonl
│   └── excluded_documents.jsonl
├── corpus/
│   ├── blog.jsonl
│   ├── talks.jsonl
│   └── all_documents.jsonl
├── audit/
├── state/state.sqlite
├── collection_manifest.json
├── exclusion_rules_snapshot.json
├── quality_report.json
└── run_summary.json
```

Raw bytes are immutable and addressed by SHA-256. Canonical JSON serialization,
stable identifiers, file hashes, extraction metadata, source URLs, identity
confidence, inclusion decisions, and model attribution make every exported
record traceable back to source artifacts. The compatibility export is written
both under `corpus/` and at the investor workspace root as required by the
existing downstream consumer.

Direct network connections verify the connected socket against public DNS to
mitigate rebinding. Environment proxy use is disabled unless the operator sets
`VC_TRACE_ALLOW_ENV_PROXY=1`; that explicit opt-in treats the proxy as part of
the network trust boundary while retaining pre/post DNS validation.

## Tests

The default suite is local and requires no paid API or network:

```bash
uv run pytest -q
```

Run the explicit public-network contract separately:

```bash
uv run pytest -m live tests/live/test_michael_hyatt.py -q
```

## Development principles

- Never treat matching names as sufficient identity proof.
- Never silently promote an uncertain speaker match.
- Preserve raw evidence and decisions; derived files are reproducible.
- Fail one source independently and keep successful source results.
- Reserve provider budget before paid work and stop before exceeding it.
- Respect robots, site terms, authentication boundaries, rate limits, and
  applicable law. Prefer official feeds and user-provided exports.

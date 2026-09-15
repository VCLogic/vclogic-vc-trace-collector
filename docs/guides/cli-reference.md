# Manual CLI and configuration

Start with the [guided workflow](../../README.md) unless you need scripts or
individual commands. This page is a reference, not another required setup path.
Run commands from the repository root. Replace example candidate IDs and file
paths with actual values from your workspace.

## Command map

| Command | Purpose |
|---|---|
| `doctor` | Inspect available tools/backends; no collection |
| `wizard` | Interactive discovery, download or processing |
| `discover` | Create an identity and initial source plan |
| `search-source` | Append URLs for one platform to an existing plan |
| `list-sources` | Inspect candidates, roles and decisions |
| `review` | Save human identity/source decisions |
| `fetch-source` | Download approved candidates |
| `stage-reference` | Register a local reference file; do not copy/process it |
| `review-voice` | Human-approve a voice interval and create its embedding |
| `process-source` | Process one approved spoken appearance |
| `process` | Normalize/process downloaded material; optional candidate subset |
| `export` | Write the corpus and compatibility outputs, then verify |
| `verify` | Check corpus provenance and quality constraints |
| `status` | Show saved corpus-run status |
| `portfolio` | Independently search/download portfolio evidence |
| `collect` | Higher-level orchestration for scripted workflows |

Each command has `--help`. Use `--output-dir` consistently if not using `outputs/`.

## Discovery and source review

For a **new** workspace with a known profile:

```bash
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --disable-public-search
```

`--disable-public-search` skips built-in searching in that `discover` invocation.
It still retrieves the supplied profile. It does not disable later
`search-source` commands. Do not rerun `discover` over an existing workspace to
change its identity; use the wizard's existing-workspace path to resume review.

Search one chosen platform at a time; generated queries are the default:

```bash
uv run --extra youtube vc-trace-collector search-source \
  --investor michael-hyatt --source youtube --backend agent-reach
uv run vc-trace-collector search-source \
  --investor michael-hyatt --source web_article --backend agent-reach
uv run vc-trace-collector list-sources --investor michael-hyatt --json
```

Other source values are `web_profile`, `podcast`, `rss_feed`, `substack`, `medium`,
`x`, `linkedin_export` and `supplied`. X/LinkedIn access may require an authorized
backend or user-provided exports; discovery is not a promise of download access.

For targeted searches, repeat `--query`. These replace the generated queries
for that invocation. `--limit-per-query` defaults to 10, accepts up to 100, and
is a request limit—not a guaranteed count of distinct sources:

```bash
uv run vc-trace-collector search-source \
  --investor michael-hyatt --source web_article --backend agent-reach \
  --query '"Michael Hyatt" BlueCat investing philosophy' \
  --limit-per-query 20 --max-search-operations 100
```

`--max-search-operations` can raise the saved cumulative platform-search ceiling;
it cannot lower it. Changes are audited. Reruns can update existing candidates
rather than add new ones.

The wizard avoids manual JSON. For scripts, save a new decision file using real
IDs from `list-sources`:

```json
[
  {
    "candidate_id": "candidate:actual-id",
    "status": "approved",
    "material_role": "spoken_by_target",
    "reason": "Human confirmed this is the intended investor's interview",
    "decided_by": "human:reviewer"
  }
]
```

After the human confirms identity:

```bash
uv run vc-trace-collector review --investor michael-hyatt \
  --decision-file selected-decisions.json --reviewer human:reviewer --confirm-identity
```

Use `authored_by_target` for first-person writing, `spoken_by_target` for target
appearances, or `reference_voice` for a reference-only source. Defer by omitting
an item from the decision file. Do not set speaker verification without evidence.

## Download a platform or individual source

```bash
uv run --extra youtube vc-trace-collector fetch-source \
  --investor michael-hyatt --source youtube
uv run vc-trace-collector fetch-source --investor michael-hyatt --source podcast
uv run vc-trace-collector fetch-source \
  --investor michael-hyatt --candidate-id candidate:actual-id
```

`--source` collects approved items of that type; other pending candidates do not
block them. Valid completed downloads are reused. For partial selection, pass
candidate IDs instead of a whole platform. Failed sources are retained in
`processed/collection_candidate_outcomes.jsonl` for diagnosis and retry.

Interactive downloads show source counts, transferred bytes, speed and ETA when
known. Unknown sizes use an indeterminate bar; redirected logs have no animation.
No processing starts after a download command.

## Voice reference and audiovisual processing

Install FFmpeg externally (`ffmpeg` and `ffprobe` must be on PATH). Optional
Python dependencies are installed by `uv run --extra av --extra av-local`.
The supported local AV dependency versions are recorded in
[pyproject.toml](../../pyproject.toml) and [uv.lock](../../uv.lock).

To register a local sample without copying its bytes or running a model:

```bash
uv run vc-trace-collector stage-reference --investor michael-hyatt \
  --file /path/to/public-reference.wav --reviewer human:reviewer
```

Fetch the returned source candidate ID in the download stage. Then obtain its
actual `voice:` ID from `identity/reference_voice_candidates.jsonl`.

Listen and choose a target-only interval. In a scripted run, inspect the actual
source duration with `ffprobe` before submitting timestamps; the wizard handles
that check interactively. These timestamps are examples only:

```bash
uv run --extra av --extra av-local vc-trace-collector review-voice \
  --investor michael-hyatt --candidate-id voice:actual-id \
  --reviewer human:reviewer --start-seconds 120 --end-seconds 165 \
  --diarization-model pyannote/speaker-diarization-3.1 --embedding-model ""
```

An empty `--embedding-model ""` clears a standalone embedding setting so the
reference uses the diarization pipeline's embedding space. Use the same
diarization model for the reference and interviews; arbitrary standalone voice
embeddings are not interchangeable.

Process an approved downloaded appearance:

```bash
uv run --extra av --extra av-local vc-trace-collector process-source \
  --investor michael-hyatt --candidate-id candidate:actual-id \
  --transcription-model turbo --diarization-model pyannote/speaker-diarization-3.1
```

The local path normalizes media with FFmpeg, diarizes speakers with pyannote,
transcribes turns with Whisper and matches compatible reference embeddings.
Platform captions are not substituted. Cosine similarity must pass both the
minimum score and the margin over the runner-up; otherwise attribution remains
uncertain. The configured defaults are 0.75 and 0.10, respectively.

`review-voice` preserves earlier approved references. Adding a reference can
reuse existing diarization/transcription caches and recompute only attribution.
Saved model/cost settings persist when omitted on later calls.

For selected **written** items:

```bash
uv run vc-trace-collector process --investor michael-hyatt \
  --candidate-id candidate:actual-written-id
```

Repeat `--candidate-id` for a subset. Omitting it processes the workspace.
After processing, use `export`, `verify` and `status` as shown in the README.

## Environment and provider settings

The CLI loads `.env` from the current working directory. Existing exported shell
variables take precedence. Use [.env.example](../../.env.example) as the template;
never overwrite an existing `.env` just to follow setup instructions. Restrict
permissions with `chmod 600 .env`. Do not copy it into a tracked filename.

| Variable | Purpose |
|---|---|
| `HF_TOKEN` | Access to operator-selected gated Hugging Face AV models |
| `VC_TRACE_AV_DEVICE` | Device, such as `cpu`, `cuda` or `mps` |
| `VC_TRACE_SEARCH_ENDPOINT` | Optional SearXNG JSON endpoint instead of DDG |
| `VC_TRACE_LLM_ENDPOINT` | Optional chat-completions endpoint for discovery refinement |
| `VC_TRACE_LLM_API_KEY` | Key for that optional provider—not a Hugging Face token |
| `VC_TRACE_DISCOVERY_MODEL` | Optional discovery refinement model |
| `VC_TRACE_ALLOW_ENV_PROXY` | Set `1` to trust operator-managed HTTP(S) proxies |
| `NODE_USE_ENV_PROXY` | Node proxy opt-in for mcporter on a supporting Node release |

The optional discovery LLM also requires a conservative
`--discovery-call-budget-usd` on `discover`/`collect`. It is not needed for normal
deterministic discovery or portfolio downloads. Audit records contain structured
actions and reported token/cost usage, never private chain-of-thought.

Agent Reach is external to this package. Its Exa route uses `mcporter`; see the
[Agent Reach project](https://github.com/Panniantong/agent-reach) for installation
and configuration. This repository does not silently install or configure it.
To isolate an Exa connectivity problem from collector logic:

```bash
mcporter call exa.web_search_exa query='"Michael Hyatt" BlueCat investor' numResults=1
```

A direct mcporter command does not load this repository's `.env`. Configure its
shell environment separately when testing proxy settings. For collector
commands, `.env` is loaded automatically.

## Budgets, exclusions and completeness

Run limits live in `config_snapshot.json`. `discover`/`collect` expose
`--max-cost-usd`, `--max-search-operations`, `--max-media-minutes`,
`--max-download-bytes` and `--max-provider-operations`. Stage-specific operation
cost estimates can be supplied with `--transcription-cost-usd`,
`--diarization-cost-usd`, `--embedding-cost-usd` and `--search-operation-cost-usd`
on their respective commands. Reservations occur before provider work; retries
can consume budget too. Zero cost estimates are not live provider price quotes.

Corpus rules can exclude domains, channels, programmes, companies, authors,
speakers, keywords and URL patterns. See
[config/exclusions.example.toml](../../config/exclusions.example.toml). Supply
`--exclusion-file` during discovery/collection setup; its rules are frozen into
the run. The default The Pitch firewall applies to the corpus, not to the
independent portfolio dataset. Identity evidence may still be retained.

Unknown/third-party material and uncertain speaker matches do not enter the
first-person corpus. A policy `review` decision can require additional review
before acquisition; ordinary source approval is not a blanket policy override.
Direct audio sources need approved duration estimates before collection.

By default, failed or unresolved approved sources prevent a verified export.
`--allow-partial-run` is an explicit manifest-visible opt-out, not a fix for
missing data. Verification checks the collected corpus; it cannot prove that
discovery found every relevant public trace.

The higher-level `collect` command also supports mutually exclusive
`--collection-only`, `--processing-only` and `--export-only` modes. Use its help
for automation; the wizard is the simpler starting point.

## Portfolio limits and output details

`portfolio` defaults to 20 search queries, 30 attempted pages, 10 results per
query, a $10 cost ceiling and zero estimated search cost. Set
`--search-operation-cost-usd` if the backend bills requests. Its download budget
is 100 MB total with at most 5 MB per page. A limit is not a completeness claim.

Use repeated `--source-url` to seed known portfolio/funding pages. Reruns reuse
validated caches. Explicit `--refresh` refetches within budgets and preserves
overwritten caches under `cache_history/`. Corrupt caches fail validation.
The legacy `--collect-only` flag is accepted but unnecessary; extraction/model
and assessment-import options are no longer available.

Portfolio outputs include `handoff.json`, `documents.jsonl`, `raw/`, `sources/`,
`search/`, `audit/`, `run_summary.json` and `manifest.json`. Old extraction-era
files, if present, are preserved but are not current handoff inputs.

Corpus outputs retain raw artifacts, canonical records, exclusions, hashes and
source links. Compatibility files are written at both the workspace root and
under `corpus/`. AV caches live in `state/av_diarization/`, `state/av_transcripts/`
and `state/av_attributions/`; detailed speaker scores are in
`processed/av_attribution_results.jsonl`.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Zero search results or fewer than requested | Backend errors, rate limits, duplicate URLs and identity/exclusion decisions |
| Agent Reach / Exa offline | Test mcporter directly; check backend configuration, network and proxy settings |
| No source downloaded | Approval status, selected platform, prior successful cache and `collection_candidate_outcomes.jsonl` |
| Podcast page exists but no audio | Collection failure details; the page may not expose a usable episode enclosure |
| Missing AV dependency or model access | AV extras, FFmpeg, `HF_TOKEN`, gated model approval and selected device |
| Reference rejected or no target speech | Clean interval, correct identity, matching embedding space, scores and runner-up margin |
| Processing is slow | Current stage/progress, recording duration, model loading, selected device and cache status |
| No animated progress | Bars appear in an interactive terminal, not redirected logs |
| Verification fails | `quality_report.json`, excluded/review-required records and failed approved sources |
| Wizard says interactive terminal required | Run it in your terminal or use the individual CLI commands |

Never resolve these problems by blindly approving namesakes, removing exclusions
or describing uncertain matches as verified. Respect site terms, authentication
boundaries and rate limits; prefer official feeds and authorized exports.

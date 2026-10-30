# VC Trace Collector

Collect a venture investor's public articles, interviews, videos and podcasts into
a source-linked corpus. A separate repository can use that corpus to build an
Investment Memory.

**Run `vc-trace-collector wizard` for one guided run: discover → triage →
download → voice reference → process.** Agents use the separate CLI commands.

[Get started](#get-started) · [Run the workflow](#run-the-workflow) ·
[Find your data](#where-is-the-data) · [Use an agent](#use-claude-code-or-codex) ·
[Related repositories](#dependencies-and-related-vclogic-repositories) ·
[Manual CLI and configuration](docs/guides/cli-reference.md)

## Guided run

Start here—one linear run from a name to a verified corpus:

```bash
uv run --extra youtube --extra av --extra av-local vc-trace-collector wizard
```

For discovery and written material only, omit the extras. `.env` is loaded
automatically.

The YouTube extra includes yt-dlp's JavaScript challenge-solver scripts. Install
a supported JavaScript runtime (Deno is yt-dlp's default) and make it available
on `PATH`. This supports YouTube extraction but does not guarantee that YouTube
will accept every request; repeated bot-check failures pause bulk downloading.

The wizard walks through: name and affiliation → confirm identity → platforms to
skip → discovery → triage table → download → voice references → processing →
export and verify. Each finished step is saved; run
`wizard --investor <slug>` to continue, and Ctrl-C tells you where you stopped.

## Dependencies and related VCLogic repositories

**This collector runs independently.** No other VCLogic repository is required
to install it, discover sources, download material, or process recordings.
Its Python dependencies and optional extras are declared in [pyproject.toml](pyproject.toml).
The relationships below are primarily downstream data handoffs, not dependencies
that must be installed alongside the collector.

| Repository | Responsibility | Relationship to this collector |
|---|---|---|
| [vclogic-vc-investment-memory](https://github.com/VCLogic/vclogic-vc-investment-memory) | Reviews collected evidence and generates the investor's source-linked Investment Memory wiki, including portfolio/relationship analysis | **Direct downstream consumer:** reads the complete `outputs/<investor-slug>/` folder; does not import the collector as a Python package |
| [vclogic-vc-investor-onboarding](https://github.com/VCLogic/vclogic-vc-investor-onboarding) | Validates an investor wiki and prepares an assessment bundle with identity, retrieval indexes and configuration; optionally collects historical pitches and decisions | **Indirect downstream consumer:** takes the generated wiki, not the collector's raw source plan; its own setup uses the investment-memory and assessment packages |
| [vclogic-vc-agentic-assessment](https://github.com/VCLogic/vclogic-vc-agentic-assessment) | Runs investor-specific pitch assessment and rehearsal | Uses the prepared investor assets installed through onboarding; assessment does not run inside this collector |
| [vclogic-web-application](https://github.com/VCLogic/vclogic-web-application) | Provides the web interface and API for investor profiles, assessment and rehearsal | Uses the assessment engine and prepared investor bundles; it is not required for the collector CLI or skill |

The data flow is:

```text
trace-collector → investment-memory → investor-onboarding → agentic-assessment
                     investor wiki       runtime bundle          ↑
                                                          web-application
                                                          (interface/API)
```

Each stage is run separately; this collector does not automatically invoke the
other repositories. Follow each linked repository's README for its installation,
credentials and runtime requirements. In particular, onboarding's sibling-checkout
requirements do not apply to running this collector by itself.

### Handoff to Investment Memory

Pass the **whole investor output folder**, for example
`outputs/jane-roe/`, to the investment-memory tool. Preserve relative paths
and all available `identity/`, `discovery/`, `raw/`, `processed/`, `corpus/`,
`state/`, `portfolio/`, audit and manifest files. The receiving tool requires a
confirmed `identity/resolved_identity.json` with a slug matching the folder name.
You can pass the folder's path directly or transfer the folder without copying
the collector's `.env`, credentials or virtual environment.

The `blog.jsonl`, `talks.jsonl` and `_manifest.json` export remains available for
legacy consumers, but those files alone are **not** the complete input expected
by the current Investment Memory workflow. Untranscribed recordings remain
untranscribed: the memory builder does not automatically process raw media.
Portfolio evidence stays separate from the public-trace corpus; investment-fact
extraction belongs to investment-memory, not this collector. Historical pitch
collection in onboarding is also separate from this collector's corpus exclusions.

## Get started

Use Python 3.11–3.13 and [uv](https://docs.astral.sh/uv/).
Run all commands from the repository directory.

For a new checkout:

```bash
git clone https://github.com/VCLogic/vclogic-vc-trace-collector.git
cd vclogic-vc-trace-collector
```

Install the basic collector and YouTube support:

```bash
uv sync --extra youtube
```

**You do not need Whisper, pyannote, a GPU or a Hugging Face token to discover sources.**
Those are relevant only when processing audio/video.

### Host tools

Two tools are installed outside Python. `doctor` (below) reports whether they are found.

| Tool | Needed for | Install |
|---|---|---|
| ffmpeg (with ffprobe) | Audio/video download, voice references and transcription | Windows `winget install Gyan.FFmpeg` · macOS `brew install ffmpeg` · Linux `sudo apt install ffmpeg`, then open a new terminal |
| mcporter with Exa | The `agent-reach` search backend (optional; `default` works without it) | `npm install -g mcporter`, then `mcporter config add exa https://mcp.exa.ai/mcp --scope home`. Test with `mcporter call exa.web_search_exa query=test numResults=1` |

### Choose a search backend

The wizard offers two choices:

| Backend | What it uses | What you need |
|---|---|---|
| `default` | DDG web search and yt-dlp YouTube search | No search API key; public search can be rate-limited |
| `agent-reach` | Exa through mcporter for web search; yt-dlp for YouTube | A working Agent Reach/Exa setup |

If Agent Reach is not configured, choose `default`. An unavailable backend is
not evidence that the investor has no sources.

To inspect installed tools and backends:

```bash
uv run --extra youtube vc-trace-collector doctor --json
```

`doctor` checks capabilities. It does **not** discover investors, install tools
or download their material.

## Run the workflow

The examples use Jane Roe. Replace his name and workspace slug for another
investor. Workspaces use the name only, such as `jane-roe`.

```bash
uv run --extra youtube --extra av --extra av-local vc-trace-collector wizard \
  --name "Jane Roe"
```

### 1. Identity and discovery

Give a firm or known profile if you have one; otherwise choose from possible
profiles. For Jane Roe you can supply
`https://www.thepitch.show/investors/jane-roe`; that page can establish
identity even though The Pitch material is excluded from the corpus by the
default rules. Confirm the displayed person before any platform search. Then
tick the platforms to **skip** (e.g. podcast); nothing ticked searches all.

### 2. Triage

A full-screen table lists every candidate, highest identity confidence first.

| Key | Action |
|---|---|
| `↑` `↓` | move |
| `a` / `x` | approve / reject |
| `r` | cycle material role |
| `A` | approve every pending row with confidence ≥ 0.8 |
| `u` | paste URLs discovery missed (added as approved) |
| `p` | skip a platform (its rows are rejected) |
| `/` | search title/URL |
| `enter` | details and identity evidence |
| `s` / `q` | save and continue / save and quit |

| Material | Role |
|---|---|
| An article or post written by the investor | `authored_by_target` |
| A video or podcast containing the investor speaking | `spoken_by_target` |
| A recording used only for voice comparison | `reference_voice` |
| Someone else's discussion of the investor | `third_party` — not first-person corpus material |

Do not approve a namesake or assume a high confidence score proves identity.
Approval does not override corpus exclusions.

### 3. Download

Approved sources are downloaded with live progress. Successful downloads are
reused; failures are listed at the end of the run.

### 4. Voice references

Needed only for YouTube or podcast recordings. First ensure `ffmpeg` and
`ffprobe` are installed. Create `.env` from [.env.example](.env.example) **only
if you do not already have one**, then edit it locally:

```dotenv
HF_TOKEN=your-hugging-face-token
VC_TRACE_AV_DEVICE=cuda
```

Use `cpu` instead of `cuda` if needed and accept the gated model access in
Hugging Face. Never commit credentials.

Pick one of the three suggested recordings (or browse, or a local file). The
wizard runs pyannote diarization and lists each speaker with their speaking
time; choose **▶** to hear an 8-second sample, then pick the investor. If the
split is wrong, type a range such as `03:10-03:40` instead. Add references from
several recordings to cope with different microphones and noise. Without any
reference, audio/video sources are skipped and reported.

### 5. Process and export

Sources are processed one by one with progress (Whisper for transcription,
pyannote for diarization and voice comparison). A failing source is reported and
the run continues. The run ends with export, verification and a summary of
counts, output path and failures. To repeat them later:

```bash
uv run vc-trace-collector export --investor jane-roe
uv run vc-trace-collector verify --investor jane-roe
```

Check failures and review-required items before treating the corpus as complete.
A successful verification does not guarantee discovery found every public source.

## Where is the data?

Everything for this example is under `outputs/jane-roe/`.

| Location | Contents |
|---|---|
| `identity/` | Person, supporting evidence and voice references |
| `discovery/source_plan.json` | Discovered sources and approval decisions |
| `raw/` | Downloaded material and provenance |
| `processed/documents.jsonl` | Normalized records, including inclusion/exclusion status |
| `processed/target_speech.jsonl` | Extracted target speech |
| `corpus/all_documents.jsonl` | Included documents after export |
| `blog.jsonl`, `talks.jsonl`, `_manifest.json` | Compatibility export for the downstream builder |
| `quality_report.json`, `run_summary.json` | Quality checks and run status |
| `audit/` | Actions, decisions, failures and provider accounting |

**For the Investment Memory builder:** supply the whole investor folder as
described in [Handoff to Investment Memory](#handoff-to-investment-memory), not
just the compatibility export. Keep the manifests and source links for provenance.
This collector does not run that builder or copy files into its repository.

To inspect run status:

```bash
uv run vc-trace-collector status --investor jane-roe
```

## Portfolio evidence is separate

Once the investor identity exists, collect portfolio evidence with:

```bash
uv run vc-trace-collector portfolio --investor jane-roe
```

This command uses Agent Reach by default; use `--backend default` if needed.
It searches and downloads portfolio/funding pages with their readable text.

**Portfolio collection is download-only.** It does not identify investments,
extract investment dates, summarize companies or call an extraction LLM.
The Pitch and other corpus exclusion rules do **not** apply to this dataset.

Give the downstream tool these files:

```text
outputs/jane-roe/portfolio/handoff.json
outputs/jane-roe/portfolio/documents.jsonl
```

Check `portfolio/run_summary.json` and `portfolio/manifest.json` for status.
Page publication dates are not investment dates; the other repository must
assess identity and extract the investment facts.

## Use Claude Code or Codex

Open the agent in this repository and invoke:

- **Codex:** `$vc-trace-collector`
- **Claude Code:** `/vc-trace-collector`

For example: “Discover sources for Jane Roe. Help me confirm his profile
and choose the sources; do not download yet.”

Both skills share the same instructions. They guide the three stages and ask
for missing decisions or a voice reference. Ask “Review the sources here” for
small numbered batches with clickable source links, evidence and proposed roles.
Reply “Approve batch 1” or choose individual items to approve, reject or defer.
Decisions apply only to the displayed items; downloads do not start automatically.
If the agent cannot expose terminal controls, it uses conversational choices and
the scripted CLI instead.
Restart the agent session if the new skill is not listed.

The skill files live under [.agents/skills/vc-trace-collector](.agents/skills/vc-trace-collector/)
and [.claude/skills/vc-trace-collector](.claude/skills/vc-trace-collector/).
Keep the full checkout: the Claude entry point refers to the shared skill.

## Further help

- [Manual CLI, configuration, budgets and troubleshooting](docs/guides/cli-reference.md)
- [Environment variable template](.env.example)
- [Exclusion-rule examples](config/exclusions.example.toml)
- [Changelog](CHANGELOG.md)
- [Attribution and licensing notes](NOTICE.md)

Development checks require no paid API:

```bash
uv run pytest -q
uv run ruff check src tests
```

This repository does not generate Investment Memory, evaluate pitches, predict
investment decisions or run founder rehearsals.

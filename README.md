# VC Trace Collector

Collect a venture investor's public articles, interviews, videos and podcasts into
a source-linked corpus. A separate repository can use that corpus to build an
Investment Memory.

**The workflow has three separate stages: discover → download → process.**
Nothing advances to the next stage automatically.

[Get started](#get-started) · [Run the workflow](#run-the-workflow) ·
[Find your data](#where-is-the-data) · [Use an agent](#use-claude-code-or-codex) ·
[Related repositories](#dependencies-and-related-vclogic-repositories) ·
[Manual CLI and configuration](docs/guides/cli-reference.md)

## Interactive investor dashboard

Start here—no stage flags or shell loops:

```bash
uv run --extra youtube --extra av --extra av-local vc-trace-collector wizard
```

For discovery and written material only, omit the extras. FFmpeg, an approved
voice reference, and the configured model access are still needed for audiovisual
processing; `.env` is loaded automatically.

The YouTube extra includes yt-dlp's JavaScript challenge-solver scripts. Install
a supported JavaScript runtime (Deno is yt-dlp's default) and make it available
on `PATH`. This supports YouTube extraction but does not guarantee that YouTube
will accept every request; repeated bot-check failures pause bulk downloading.

- **Investors:** open an existing VC, see saved counts and active jobs, or choose
  **New investor**. New investors start with platform selection and identity review.
- **Source table:** select a VC with the arrows and press **Enter**. All discovered
  sources appear, with separate approval, download, transcription (`Tx`),
  diarization (`Dia`) and voice-attribution columns. Press **Enter** on a source
  for its URL, files, decisions, voice scores and failure reasons. **Esc** returns
  to investors. Browsing does not start work or approve anything.
- **Find the next VC:** on the investor list, choose **Show VCs with downloaded
  recordings ready to process**. Open one and press **p**. Choose all eligible
  recordings or a subset, then confirm transcription and diarization. Missing
  downloads do not block recordings already downloaded. If a voice reference is
  missing, the wizard guides reference selection first; return and press **p**
  after the reference job finishes.
- **Review:** inspect URLs/evidence, filter sources, or **Accept all pending**.
  Bulk approval keeps previous rejections, material roles and exclusions intact;
  it is not verified speaker attribution.
- **Download:** collect approved missing items, with live progress. Successful
  artifacts are reused. Failed items remain visible for retry.
- **Voice signature:** choose from up to three metadata-ranked downloaded clips
  (or browse all), listen using the displayed URL/local path, enter `1:11` or `71`,
  and confirm only the target speaks. The tool creates the reference embedding.
- **Process:** explicitly start transcription, diarization and speaker matching.
  Uncertain results remain review-required. Export/verification is a separate choice.
- **Jobs:** reconnect to persistent background jobs. Closing the dashboard leaves
  workers running. **Pause after item** takes effect between items. Reopen the
  job and choose **Resume / retry** for failed or unfinished items; completed stages use caches.

Use arrow keys, Space to select checkboxes, Tab to move to buttons, Enter to
confirm, Escape to go back, or the mouse. No browser UI is required. Managed jobs
serialize writes to each investor and use one managed GPU worker at a time.
External CLI jobs cannot be universally controlled by the dashboard; the known
bulk-download runner is detected and protected. Workers currently require Linux.

In the source table: **/** search, **f** cycle status filters, **p** process ready
recordings, **d** download/retry, **r** review sources, **v** voice reference,
**j** jobs, **a** other actions, **Ctrl-R** reload saved progress, **?** help.
Enter leaves search and returns focus to the table; keyboard shortcuts apply
when the table is focused. Processing asks for confirmation and does not act on
the highlighted row automatically.

`done` means a saved transcript/diarization result exists; it does not mean the
speaker is verified. `matched` means model-accepted attribution, `uncertain`
needs review, and `operator` means recorded human speaker attribution. Source
approval is separate. `?` means the stage is unknown; inspect the failure details.
Changed model settings may require recomputation despite saved stage results.
Download failures and processing failures are counted separately.

The older `wizard --stage discover|download|process` commands remain available.

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
`outputs/michael-hyatt/`, to the investment-memory tool. Preserve relative paths
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

The examples use Michael Hyatt. Replace his name and workspace slug for another
investor. New wizard workspaces use the name only, such as `michael-hyatt`.

### 1. Discover and review sources

```bash
uv run --extra youtube vc-trace-collector wizard \
  --stage discover --name "Michael Hyatt"
```

The wizard asks you to:

1. Provide a firm or known profile if you have one; otherwise choose from possible profiles.
2. Confirm the intended person using the displayed evidence.
3. Choose platforms and search limits.
4. Select sources to approve, reject or defer, then confirm the saved decisions.

For Michael Hyatt, you can supply
`https://www.thepitch.show/investors/michael-hyatt` when asked for a known profile.
That page can establish identity even though The Pitch material is excluded from
the public-trace corpus by the default rules.

**How to select sources:** choose **select**, use arrow keys to move and Space
to tick items, then press Enter. Choose **done** to continue to the decision
screen. The menu also provides source details, text/platform/status filters and
confidence sorting. Ctrl+C cancels unsaved choices.

Review different roles in separate batches:

| Material | Role to choose |
|---|---|
| An article or post written by the investor | `authored_by_target` |
| A video or podcast containing the investor speaking | `spoken_by_target` |
| A recording used only for voice comparison | `reference_voice` |
| Someone else's discussion of the investor | `third_party` — not first-person corpus material |

Do not approve a namesake or assume a high confidence score proves identity.
Unselected and deferred sources stay unchanged. Approval does not override
corpus exclusions.

**This stage finds URLs and retrieves identity evidence. It does not download
recordings or run audiovisual models.**

To review more batches or resume discovery without replacing the saved identity:

```bash
uv run --extra youtube vc-trace-collector wizard \
  --stage discover --investor michael-hyatt
```

Select no platforms if you only want to review existing candidates.

### 2. Download approved material

```bash
uv run --extra youtube vc-trace-collector wizard \
  --stage download --investor michael-hyatt
```

Select the approved sources you want, then confirm. The terminal shows progress
and reports failures/skips. Rerun this stage to retry selected failures; valid
successful downloads are reused.

**Downloading does not start transcription or diarization.** A podcast page
without acquired audio is not a successfully downloaded podcast episode.

### 3. Process downloaded material

For **written material only**, no AV dependencies are needed:

```bash
uv run vc-trace-collector wizard --stage process --investor michael-hyatt
```

For **YouTube or podcast recordings**, first ensure `ffmpeg` and `ffprobe` are
installed. Create `.env` from [.env.example](.env.example) **only if you do not
already have one**, then edit it locally:

```dotenv
HF_TOKEN=your-hugging-face-token
VC_TRACE_AV_DEVICE=cuda
```

Use an appropriate device for your machine, such as `cpu` instead of `cuda`.
Accept the required gated model access in Hugging Face. The CLI loads `.env`
automatically; no `export` or `source .env` is needed. Never commit credentials.

Run AV processing with its optional dependencies:

```bash
uv run --extra av --extra av-local vc-trace-collector wizard \
  --stage process --investor michael-hyatt
```

Select downloaded recordings and the models to use. The local pipeline uses
**Whisper for transcription** and **pyannote for diarization and voice comparison**,
not platform captions.

If there is no approved voice reference, the wizard asks you to choose a
downloaded recording and provide a clean target-only interval after listening.
It checks timestamps against the recording's duration. You can also stage a
local reference file; acquire it in the download stage, then return here.
Reference embeddings use the selected diarization pipeline's matching embedding
space. Uncertain speaker matches remain flagged for review.

Cached transcription and diarization are reused when valid. Processing does not
silently download additional recordings.

At the end, the wizard offers to **export and verify**. If you skip that prompt,
run these later:

```bash
uv run vc-trace-collector export --investor michael-hyatt
uv run vc-trace-collector verify --investor michael-hyatt
```

Check failures and review-required items before treating the corpus as complete.
A successful verification does not guarantee discovery found every public source.

## Where is the data?

Everything for this example is under `outputs/michael-hyatt/`.

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
uv run vc-trace-collector status --investor michael-hyatt
```

## Portfolio evidence is separate

Once the investor identity exists, collect portfolio evidence with:

```bash
uv run vc-trace-collector portfolio --investor michael-hyatt
```

This command uses Agent Reach by default; use `--backend default` if needed.
It searches and downloads portfolio/funding pages with their readable text.

**Portfolio collection is download-only.** It does not identify investments,
extract investment dates, summarize companies or call an extraction LLM.
The Pitch and other corpus exclusion rules do **not** apply to this dataset.

Give the downstream tool these files:

```text
outputs/michael-hyatt/portfolio/handoff.json
outputs/michael-hyatt/portfolio/documents.jsonl
```

Check `portfolio/run_summary.json` and `portfolio/manifest.json` for status.
Page publication dates are not investment dates; the other repository must
assess identity and extract the investment facts.

## Use Claude Code or Codex

Open the agent in this repository and invoke:

- **Codex:** `$vc-trace-collector`
- **Claude Code:** `/vc-trace-collector`

For example: “Discover sources for Michael Hyatt. Help me confirm his profile
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

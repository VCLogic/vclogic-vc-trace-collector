---
name: vc-trace-collector
description: Use when collecting, reviewing, downloading, diarizing, or exporting public traces for a venture investor with the vc-trace-collector repository.
---

# VC Trace Collector

## Overview

Drive the audited CLI one stage at a time. Agent Reach selects discovery backends; the repository remains authoritative for review, collection, hashing, exclusions, pyannote attribution, and export.

## Required order

Work from the repository root. Read `.env` variable names only; never print their values.

```bash
uv run vc-trace-collector doctor --json
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt"
```

Search each platform independently. Repeat for `web_article`, `web_profile`, `rss_feed`, `substack`, and `medium` as needed:

```bash
uv run vc-trace-collector search-source --investor michael-hyatt --source youtube --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source podcast --backend agent-reach
```

Show `outputs/michael-hyatt/discovery/source_candidates.jsonl` to the human. Do not bypass identity or source review. The human must approve each source and label clean reference material `reference_voice` and target appearances `spoken_by_target`. Record decisions through `review --decision-file`; use `review --help` for its schema and options.

Fetch platforms separately, or fetch one candidate ID:

```bash
uv run vc-trace-collector fetch-source --investor michael-hyatt --source youtube
uv run vc-trace-collector fetch-source --investor michael-hyatt --source podcast
uv run vc-trace-collector fetch-source --investor michael-hyatt --candidate-id 'candidate:<id>'
```

For a human-selected clean interval, create the voice reference:

```bash
uv run vc-trace-collector review-voice \
  --investor michael-hyatt \
  --candidate-id 'voice:<id>' \
  --reviewer human \
  --start-seconds 120 --end-seconds 165 \
  --diarization-model pyannote/speaker-diarization-3.1
```

Process every approved target appearance separately:

```bash
uv run vc-trace-collector process-source \
  --investor michael-hyatt \
  --candidate-id 'candidate:<id>' \
  --transcription-model turbo \
  --diarization-model pyannote/speaker-diarization-3.1
```

Finish with `export`, `verify`, and `status`. Do not use platform captions. Do not claim uncertain speaker attribution is verified. Never place credentials or cookies in commands, logs, decisions, or committed files.

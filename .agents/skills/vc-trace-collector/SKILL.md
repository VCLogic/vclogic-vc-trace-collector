---
name: vc-trace-collector
description: Use when collecting, reviewing, downloading, diarizing, or exporting public traces or portfolio investment evidence for a venture investor with the vc-trace-collector repository.
---

# VC Trace Collector

## Overview

Drive the audited CLI one stage at a time. Agent Reach selects discovery backends; the repository remains authoritative for review, collection, hashing, exclusions, pyannote attribution, and export.

## Required order

For **portfolio investments**, use the independent workflow below instead of
source-plan review, corpus filtering, processing or corpus export.

## Portfolio downloads (no corpus exclusions or LLM extraction)

For an already resolved investor:

```bash
uv run vc-trace-collector portfolio --investor michael-hyatt
```

This automatically searches with Agent Reach and downloads evidence. No LLM key
is required. The legacy `--collect-only` flag is accepted but unnecessary.
Do not perform investment extraction or company summarization with this skill;
that work belongs to another repository. There is no portfolio assessment-import
or model-extraction command here.

No corpus exclusions apply to this dataset, including The Pitch. Do not filter
downloaded portfolio sources by programme, domain, company, keyword or date.
Acquisition safety and configured request/byte/cost limits still apply.
Do not change corpus approvals to collect portfolio evidence.

Hand off `outputs/<investor>/portfolio/handoff.json` and
`portfolio/documents.jsonl` to the downstream tool. The JSONL contains readable
page text, source URLs, page publication dates, collection timestamps and hashes.
Original content is in `portfolio/raw/`; per-page metadata/text is in
`portfolio/sources/`. Source publication dates are not investment dates, and a
download does not verify that the page concerns the intended investor.

Check `portfolio/run_summary.json` and `portfolio/manifest.json`, not the corpus
status or verify commands. Report failures and incomplete coverage. Explicit
`--refresh` refetches within configured budgets while preserving prior cache
versions. Legacy extraction files are preserved but are not current handoff inputs.

## Public-trace corpus workflow

Work from the repository root. Read `.env` variable names only; never print their values.

```bash
uv run vc-trace-collector doctor --json
uv run vc-trace-collector discover \
  --name "Michael Hyatt" \
  --known-profile-url "https://www.thepitch.show/investors/michael-hyatt" \
  --disable-public-search
```

Search each platform independently. Repeat for `web_article`, `web_profile`, `rss_feed`, `substack`, and `medium` as needed:

```bash
uv run vc-trace-collector search-source --investor michael-hyatt --source youtube --backend agent-reach
uv run vc-trace-collector search-source --investor michael-hyatt --source podcast --backend agent-reach
uv run vc-trace-collector list-sources --investor michael-hyatt
```

Use repeatable `--query` options when generated queries miss a known programme,
firm, or site. Explicit queries replace generated queries for that invocation.
For example:

```bash
uv run vc-trace-collector search-source \
  --investor michael-hyatt --source youtube --backend agent-reach \
  --query '"Michael Hyatt" "The Pitch" YouTube' \
  --limit-per-query 100 --max-search-operations 100
```

For the public-trace corpus, the default firewall retains The Pitch results as
rejected discovery evidence, never as corpus inputs. This does not apply to the
independent portfolio workflow above. If Agent Reach is unavailable, report the
single actionable failure; do not repeatedly retry every generated query.

Show the `list-sources` output to the human. Do not bypass identity or source review. Write their decisions to a JSON array such as:

```json
[
  {"candidate_id": "candidate:<actual-id>", "status": "approved", "reason": "Human verified identity and clean voice sample", "decided_by": "human", "material_role": "reference_voice"},
  {"candidate_id": "candidate:<actual-id>", "status": "approved", "reason": "Human verified target interview", "decided_by": "human", "material_role": "spoken_by_target"}
]
```

Pass it to `review --confirm-identity --decision-file decisions.json`. Include a decision for every pending item being fetched.

Fetch platforms separately, or fetch one candidate ID:

```bash
uv run vc-trace-collector fetch-source --investor michael-hyatt --source youtube
uv run vc-trace-collector fetch-source --investor michael-hyatt --source podcast
uv run vc-trace-collector fetch-source --investor michael-hyatt --candidate-id 'candidate:<id>'
```

After fetching, obtain the actual `voice:` ID from `outputs/michael-hyatt/identity/reference_voice_candidates.jsonl`. The human must listen and select an interval containing only the target: no music, crosstalk, host speech, or unidentified panel speech. Then create the reference:

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

Finish with `export`, then `verify` and `status`; stop and report any verification failure. Do not use platform captions. Do not claim uncertain speaker attribution is verified. Never place credentials or cookies in commands, logs, decisions, or committed files.

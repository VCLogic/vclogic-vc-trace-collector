# Portfolio evidence: separate, download-only

For an already resolved investor:

```bash
uv run vc-trace-collector portfolio --investor investor-name
```

This searches and downloads public evidence without corpus exclusions, including
The Pitch. It does not extract investments, infer dates, summarize companies or
invoke an extraction LLM. No portfolio LLM key is required. Do not use corpus
approval or processing to collect portfolio data.

Hand off `outputs/<slug>/portfolio/handoff.json` and `documents.jsonl` to the other
repository. Original pages, source metadata and search evidence stay under
`raw/`, `sources/` and `search/`. Page publication dates are not investment dates.
Downloaded pages may concern namesakes and require downstream assessment.

Check `portfolio/run_summary.json` and `portfolio/manifest.json`; corpus status
does not describe portfolio completion. Respect acquisition safety, byte/search/
cost budgets, and report incomplete coverage. `--refresh` is an explicit refetch;
existing extraction-era files remain untouched but are not current handoff inputs.

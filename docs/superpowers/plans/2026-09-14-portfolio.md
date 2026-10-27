# Portfolio collection implementation plan

## Scope revision

Portfolio collection is now download-only by user instruction. Remove portfolio
LLM providers, assessment imports and investment extraction models. Preserve
existing raw/source caches; expose `documents.jsonl` and `handoff.json` for another
repository's extraction tool. Retain search/fetch budgets, cache validation and
the no-corpus-exclusions boundary. The original implementation plan below is
historical; its extraction steps no longer describe current scope.

Approved scope: named companies, investment/announcement dates, short company
summaries and source links. No Investment Memory generation. No corpus filters,
including The Pitch, apply to portfolio collection. Keep uncertainty and all dates.

1. Add strict portfolio models and evidence validation in `portfolio.py`.
   Tests: unknown dates, real calendar dates, date precision, exact supporting
   excerpts, personal versus firm attribution, and unfiltered The Pitch sources.
2. Add an independently cached service using the existing safe Fetcher and Agent
   Reach search provider. Search identity-anchored portfolio queries, extract
   companies, and follow up with company funding/product queries. Preserve raw
   evidence, query provenance, all model assessments, and partial successes.
3. Add configurable chat-endpoint extraction and local agent-assessment imports.
   Bound pages, searches, response bytes, model output and monetary reservations.
   Record tokens and conservative charges; never store private reasoning.
4. Expose `portfolio` with search-only and supplied-assessment modes. Default to
   Agent Reach and existing LLM environment configuration; require an explicit
   model-call cost bound before model calls. Reuse download progress.
5. Document CLI/env configuration and agent use. Run fixture tests and full suite.

Verification: `uv run pytest tests/test_portfolio.py -q`, full `uv run pytest -q`,
`uv run vc-trace-collector portfolio --help`, and `git diff --check`.
No real provider calls or portfolio downloads during implementation verification.

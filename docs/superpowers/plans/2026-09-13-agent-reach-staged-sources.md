# Agent Reach Staged Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add capability diagnostics and per-source search, fetch, and audiovisual processing commands that can be driven from either the CLI or a project-local agent skill.

**Architecture:** Agent Reach is treated as an optional health checker and capability router, not as a runtime Python import. A focused adapter snapshots sanitized `agent-reach doctor --json` output and selects deterministic upstream search tools. Existing collectors and the FFmpeg/pyannote/voice-match/Whisper pipeline gain source filters rather than being duplicated.

**Tech Stack:** Python 3.11+, Typer, Pydantic, subprocess with argument arrays, existing yt-dlp/HTTP collectors, pytest, Agent Reach executable when installed.

---

### Task 1: Capability diagnostics

**Files:**
- Create: `src/vc_trace_collector/capabilities.py`
- Modify: `src/vc_trace_collector/cli.py`
- Create: `tests/test_capabilities.py`

- [ ] **Step 1: Write failing tests** for safe executable probes, sanitized Agent Reach JSON parsing, missing-tool results, and the `doctor --json` CLI output.
- [ ] **Step 2: Run** `uv run pytest tests/test_capabilities.py -q` and confirm the missing module/command behavior fails.
- [ ] **Step 3: Implement** typed capability records and a runner that invokes only fixed argument arrays (`agent-reach doctor --json`, `ffmpeg -version`, `yt-dlp --version`) with timeouts and no shell.
- [ ] **Step 4: Run** `uv run pytest tests/test_capabilities.py -q` and confirm all capability tests pass.

### Task 2: Source-specific search and append-only planning

**Files:**
- Create: `src/vc_trace_collector/source_search.py`
- Modify: `src/vc_trace_collector/public_search.py`
- Modify: `src/vc_trace_collector/pipeline.py`
- Modify: `src/vc_trace_collector/cli.py`
- Create: `tests/test_source_search.py`

- [ ] **Step 1: Write failing tests** showing `search-source --source youtube` generates only YouTube queries, preserves query/provider provenance, deduplicates existing canonical URLs, applies exclusions, and appends candidates without erasing review decisions.
- [ ] **Step 2: Run** `uv run pytest tests/test_source_search.py -q` and confirm failures are caused by the missing source-search API.
- [ ] **Step 3: Implement** source-specific query templates and `Pipeline.search_source`, using yt-dlp for YouTube and the configured general web provider for blogs, podcasts, articles, and profiles.
- [ ] **Step 4: Run** `uv run pytest tests/test_source_search.py -q` and confirm the new tests pass.

### Task 3: Per-source and per-candidate collection

**Files:**
- Modify: `src/vc_trace_collector/collectors.py`
- Modify: `src/vc_trace_collector/pipeline.py`
- Modify: `src/vc_trace_collector/cli.py`
- Modify: `tests/test_collectors.py`
- Modify: `tests/test_pipeline_cli.py`

- [ ] **Step 1: Write failing tests** for `fetch-source --source youtube` and `fetch-source --candidate-id ...`, including rejection of unapproved candidates and isolation of one candidate's failure.
- [ ] **Step 2: Run** the focused collector and CLI tests and confirm they fail for the absent filters.
- [ ] **Step 3: Add** candidate filters to the existing resumable collection loop and expose them through `Pipeline.fetch_source` and the CLI.
- [ ] **Step 4: Run** focused tests and confirm they pass.

### Task 4: Per-candidate audiovisual processing

**Files:**
- Modify: `src/vc_trace_collector/pipeline.py`
- Modify: `src/vc_trace_collector/cli.py`
- Modify: `tests/test_av.py`
- Modify: `tests/test_pipeline_cli.py`

- [ ] **Step 1: Write failing tests** proving `process-source --candidate-id ...` runs diarization, target-speaker matching, segment extraction, and Whisper only for that candidate while retaining prior processed results.
- [ ] **Step 2: Run** focused AV tests and confirm failure due to the missing candidate filter.
- [ ] **Step 3: Filter** the existing authoritative AV pipeline by candidate ID and merge deterministic processed outputs rather than overwriting other candidates.
- [ ] **Step 4: Run** focused tests and confirm they pass.

### Task 5: Agent skill and operator documentation

**Files:**
- Create: `.agents/skills/vc-trace-collector/SKILL.md`
- Modify: `README.md`
- Modify: `.env.example`
- Create: `tests/test_agent_skill.py`

- [ ] **Step 1: Write a failing contract test** that checks the project skill contains the actual CLI commands, mandatory review boundaries, no-caption AV rule, and no credentials.
- [ ] **Step 2: Run** `uv run pytest tests/test_agent_skill.py -q` and confirm it fails because the skill is absent.
- [ ] **Step 3: Write** the project-local skill and a concise manual command sequence for Michael Hyatt. Document backend-specific optional dependencies and sanitized doctor snapshots.
- [ ] **Step 4: Run** the skill contract test and documentation command smoke tests.

### Task 6: Full verification

**Files:**
- Modify only files required by failures found in this task.

- [ ] **Step 1: Run** `uv run ruff check .`.
- [ ] **Step 2: Run** `uv run pytest -q`.
- [ ] **Step 3: Run** CLI help smoke tests for `doctor`, `search-source`, `fetch-source`, and `process-source`.
- [ ] **Step 4: Inspect** `git diff --check` and `git status --short`, preserving the operator-owned untracked `example.env`.

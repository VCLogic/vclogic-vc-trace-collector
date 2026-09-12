# Stage-Specific Audiovisual Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move AV model configuration from discovery to the CLI stages that use those models while preserving an auditable effective run configuration.

**Architecture:** A focused pipeline helper applies non-null stage overrides to the validated `RunConfig`, persists the snapshot, and records changed fields in the audit log. `review-voice` and `process` pass their own model/cost options to the pipeline; combined `collect` retains full-run configuration for automation and backward compatibility.

**Tech Stack:** Python, Typer, Pydantic, pytest, uv

---

### Task 1: Define the CLI contract

**Files:**
- Modify: `tests/test_pipeline_cli.py`
- Modify: `src/vc_trace_collector/cli.py`

- [x] **Step 1: Write failing CLI tests**

Add tests that assert `discover --help` omits AV-stage model options, `process --help` accepts transcription/diarization models and costs, and `review-voice --help` accepts diarization/embedding models and embedding cost. Use fake pipeline methods to assert parsed values are passed as typed keyword arguments.

- [x] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_pipeline_cli.py -k 'stage_specific or routes_av' -q`

Expected: failures showing the old discovery options and missing stage options.

- [x] **Step 3: Implement the minimal CLI changes**

Remove the six AV model/cost parameters from `discover` and its `RunConfig` construction. Add optional strings to `process` and `review-voice`, convert supplied costs to `Decimal`, and pass values to the matching pipeline method.

- [x] **Step 4: Verify the CLI tests pass**

Run: `uv run pytest tests/test_pipeline_cli.py -k 'stage_specific or routes_av' -q`

Expected: all selected tests pass.

### Task 2: Persist and audit stage configuration

**Files:**
- Modify: `tests/test_pipeline_cli.py`
- Modify: `src/vc_trace_collector/pipeline.py`

- [x] **Step 1: Write failing pipeline tests**

Create a discovered fixture workspace, invoke `process` with model/cost overrides, and assert `config_snapshot.json` contains the effective values plus one `configuration` audit event. Invoke it again with the same values and assert no duplicate configuration event. Extend the reference-voice fixture path to assert its embedding configuration is persisted before embedding.

- [x] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_pipeline_cli.py -k 'persists_av_configuration' -q`

Expected: failures because the pipeline methods reject stage-specific keywords.

- [x] **Step 3: Add the configuration helper and method parameters**

Implement `_configure_stage(workspace, stage, **updates) -> RunConfig`. Load and validate the saved configuration, discard `None` values, compare effective values, write only when changed, and append an audit event with old and new non-secret values. Call it from `approve_reference_voice` and `process` after workspace inputs are validated and before model or cost-provider work begins.

- [x] **Step 4: Verify pipeline tests pass**

Run: `uv run pytest tests/test_pipeline_cli.py -k 'persists_av_configuration' -q`

Expected: all selected tests pass.

### Task 3: Update operator documentation and verify

**Files:**
- Modify: `README.md`

- [x] **Step 1: Revise the staged Michael Hyatt workflow**

Show discovery without AV model options, voice approval with its selected model, and processing with transcription and diarization models. Explain that each stage persists and audits its effective configuration.

- [x] **Step 2: Run release checks**

Run: `uv run pytest -q`

Expected: all offline tests pass.

Run: `uv run ruff check .`

Expected: no lint failures.

Run: `uv build`

Expected: source and wheel builds succeed.

- [x] **Step 3: Commit the implementation**

Commit CLI, pipeline, test, documentation, and plan changes in one atomic feature commit.

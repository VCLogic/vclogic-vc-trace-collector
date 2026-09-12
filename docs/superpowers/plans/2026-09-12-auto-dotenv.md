# Automatic `.env` Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically load `.env` for every CLI invocation without overriding exported shell variables.

**Architecture:** Add `python-dotenv` as a core dependency and invoke `load_dotenv(override=False)` when constructing the Typer application. Verify behavior at the CLI boundary and document the simplified operator workflow.

**Tech Stack:** Python 3.11+, Typer, python-dotenv, pytest, uv

---

### Task 1: Specify CLI environment loading

**Files:**
- Modify: `tests/test_pipeline_cli.py`

- [x] **Step 1: Write the failing test**

Add a test that writes `VC_TRACE_SEARCH_ENDPOINT` to a temporary `.env`, changes into that directory, constructs the CLI application, invokes `status`, and observes the environment value during pipeline construction. Set a different process value and repeat to prove process environment precedence.

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline_cli.py::test_cli_loads_dotenv_without_overriding_process_environment -q`

Expected: FAIL because the CLI does not load `.env`.

### Task 2: Implement automatic loading

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/vc_trace_collector/cli.py`

- [x] **Step 1: Add the dependency**

Add `python-dotenv>=1.1,<2` to core project dependencies and refresh `uv.lock` with `uv lock`.

- [x] **Step 2: Load `.env` during CLI construction**

Import `load_dotenv` and call `load_dotenv(override=False)` at the start of `create_app`, before any environment-backed provider is constructed.

- [x] **Step 3: Run the focused test**

Run: `uv run pytest tests/test_pipeline_cli.py::test_cli_loads_dotenv_without_overriding_process_environment -q`

Expected: `1 passed`.

### Task 3: Document and verify

**Files:**
- Modify: `README.md`

- [x] **Step 1: Document `.env` usage**

State that `.env` loads automatically, exported variables take precedence, and `.env` must never be committed.

- [x] **Step 2: Run quality checks**

Run: `uv run pytest -q`

Expected: all offline tests pass.

Run: `uv run ruff check .`

Expected: no lint errors.

Run: `uv build`

Expected: wheel and source distribution build successfully.

- [x] **Step 3: Commit**

Commit the dependency, implementation, test, documentation, spec, and plan as one atomic feature commit.

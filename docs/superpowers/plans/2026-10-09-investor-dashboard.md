# Investor Dashboard Implementation Plan

**Goal:** Implement the approved investor-first terminal workflow without replacing
the audited collectors or interrupting live work.

**Architecture:** Full-screen prompt_toolkit presentation, read-only workspace
projections, and persistent subprocess jobs invoking Pipeline. Existing explicit
wizard stages remain compatible. Work in the current checkout as requested.

**Tech Stack:** Python, installed prompt_toolkit, Pydantic, POSIX process locks,
pytest; no browser frontend or paid API dependencies.

## Task 1 — Durable jobs

Create `dashboard_jobs.py` and `tests/test_dashboard_jobs.py`. Implement
`launch_job(output_dir, slug, stage, candidate_ids, **options) -> dict`,
`list_jobs(output_dir) -> list[dict]`, `request_pause(output_dir, job_id)`,
and module worker entrypoint. Support download/process/search/reference jobs.
Persist request/status/progress, process identity, and item outcomes. Lock a
workspace and the GPU globally during managed processing. Detect stale workers
and known external runners without taking ownership. Download failures preserve
successful results; repeated errors back off and pause. Preserve budgets and
approvals. Write failing fixture tests for duplicate launches, stale PID identity,
cache reuse, pause, errors and stage dispatch before implementation.

## Task 2 — Dashboard projection and reference helpers

Create `dashboard_state.py` with summaries based on source_items, identity and
job records. Add tests for counts, next actions, incomplete artifacts, corrupt
workspaces and deterministic top-three reference suggestions. Add a shared time
parser accepting seconds, mm:ss and hh:mm:ss with finite/range validation.
Reuse reference selection lineage from wizard_reference without requiring a
pre-existing voice candidate for an acquired approved recording.

## Task 3 — Full-screen presentation and guided controller

Create `dashboard.py` and `dashboard_ui.py`. Build investor dashboard, New investor,
platform selection before discovery, review table/filter/detail actions,
Accept all pending with confirmation, download and process selection, live job
display, and voice reference shortlist. All prompts permit Back/Cancel. Present
URLs/local paths before human voice confirmation. Advanced models/settings are
optional. Workers run independently when UI exits. Keep legacy --stage paths.
Add offline UI/controller tests using fake prompt answers/providers; check
keyboard escape, source selections and no implicit stage execution.

## Task 4 — Integrate, verify, document

Route no-stage wizard to dashboard in cli.py; preserve scripted stage commands.
Declare prompt_toolkit directly (already installed transitively). Document entry,
shortcuts, resume behavior and verification semantics in README/changelog, without
overwriting unrelated edits. Run wizard, CLI, job, reference and dashboard tests,
then full offline suite. Use a pseudo-terminal smoke test without live operations.
Review spec compliance and code quality separately before declaring completion.

## Verification commands

`uv run pytest -q tests/test_dashboard_jobs.py tests/test_dashboard_state.py tests/test_dashboard.py tests/test_wizard.py`

`uv run pytest -q`

Expected: all selected offline tests pass; no network/model/download launches from
tests. Any failures are diagnosed before claiming completion. Review checks locks,
untrusted terminal content, stale processes, file path containment, and approval
boundaries. No automatic git push or unrelated file staging.

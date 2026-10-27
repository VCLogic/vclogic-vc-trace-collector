# Table-first investor browser implementation plan

**Goal:** Opening an investor immediately shows all discovered sources and independent stage statuses. Enter opens details; Escape returns. Processing downloaded recordings is explicit and independent of failed downloads.

**Architecture:** Reuse prompt_toolkit and existing guarded Pipeline/job operations. Add a read-only inventory projection and table presenter; retain old explicit wizard stages. No llmfit dependency or new model runs.

**Execution:** Inline in the current worktree as approved. Preserve existing changes and output data; no blanket staging or push.

## Task 1 — Evidence-backed inventory

- [ ] Add tests in `tests/test_inventory.py` for unknown vs complete stages, voice-match versus operator source approval, failure reasons, and downloaded-media readiness.
- [ ] Run `uv run pytest -q tests/test_inventory.py`; expect missing inventory module failure.
- [ ] Add `src/vc_trace_collector/inventory.py`: `inventory(pipeline, slug)` returns source rows with approval, download, transcription, diarization, attribution, files, decisions and errors. Use AV results and typed stage caches rather than inference from generic success. Written sources show not-applicable.
- [ ] Run inventory tests; expect green.

## Task 2 — Keyboard-first source browser

- [ ] Add tests for Enter detail, Esc back, filter/search, no implicit mutations and processing selection.
- [ ] Add `src/vc_trace_collector/inventory_ui.py`: searchable source table, stage filters, details and visible keyboard shortcuts. Actions return to the controller, never mutate from presentation.
- [ ] Change `Dashboard.investor` to open the table, reuse explicit guarded actions and expose processing/downloads/review/reference/discovery/jobs. Remove automatic next-action entry point and ambiguous aggregated failure labels.
- [ ] Expose home filter for investors with downloaded recordings awaiting processing. Keep source inspection available during active jobs. Add `media_only` processing selection for the direct transcription action.
- [ ] Run focused UI/controller tests; expect green, with legacy stage behavior preserved.

## Task 3 — Verification and documentation

- [ ] Update README and changelog with the actual shortcuts and stage meanings.
- [ ] Run full offline pytest, focused Ruff, diff whitespace check and a read-only terminal smoke test at 80 columns.
- [ ] Fresh-context review of changed files; fix substantive findings with regression tests.

## Ledger

- Approved spec: user's table-first flow and GO in chat; no further design gate.
- Shared interfaces: inventory rows feed presenter and readiness counts; presenter returns action names already handled by controller.
- Status contract: source approval is not speaker verification. A completed cache means saved stage output, not a promise it matches newly changed model settings.
- Task 1 complete: inventory tests passed after missing-module and model-status RED cases; partial upstream caches and failures are separate.
- Task 2 complete: real keyboard Enter/Esc tests and controller tests passed; actual 80-column Beck table/detail/back navigation smoke-tested without writes.
- Task 3 complete: README/changelog updated; final full suite 336 passed, 2 deselected; focused Ruff and git diff --check clean.
- Final review: fresh read-only reviewer identified malformed cache JSON blocking inventory. Reproduced array/null/unhashable-ID cases and fixed with regression tests. No other substantive findings.
- No new GPU work, downloads, approvals, commits or pushes performed for this UI change. Existing user changes preserved.

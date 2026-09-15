# Guided Collector Implementation Plan

> Execute inline with test-driven-development and verification-before-completion.

**Goal:** Ship the approved staged wizard and a shared Claude/Codex skill.

**Architecture:** Terminal prompts call a small workflow controller. The controller
delegates all durable decisions and operations to existing Pipeline services.
Typed choices and filesystem-derived status prevent hidden stage transitions.

**Stack:** Python, Pydantic, Typer, Rich, Questionary, pytest and uv.

## Tasks

- [x] Add `tests/test_wizard.py`: scripted prompts, fixture Pipeline, stage call
  recording; assert cancelled decisions stay pending and downloaded-only
  processing cannot fetch. Run `uv run pytest tests/test_wizard.py -q` red.
- [x] Add `wizard_models.py` for typed menu options and artifact-backed item state;
  `wizard.py` for discovery/review/download/process orchestration; `terminal.py`
  for searchable checkbox menus, details, filtering and safe cancellation.
  Use `Pipeline.review(..., confirm_identity=True)` only after human confirmation,
  `fetch_source(..., candidate_ids=...)` only on approved selection, and
  `process(..., candidate_ids=...)` only on local selected artifacts.
- [x] Add `wizard_profiles.py` for bounded profile search with search provenance,
  budget accounting and manual-URL fallback. Never merge name matches or overwrite
  an existing identity. Run profile tests with fixture providers.
- [x] Register `wizard --stage discover|download|process --name ... --investor ...`
  in `cli.py`; add Questionary dependency and update uv lock. Test redirected
  invocation exits with actionable noninteractive alternatives.
- [x] Add reference prompting and stage-specific dependency checks, local file
  staging for a future download action, media interval validation through the
  existing reference service, export/verify confirmation and partial-result
  reporting. Test missing reference, invalid input and cache-preserving delegation.
- [x] Exercise the existing skill baseline, then update the shared skill and add
  Claude entry point with linked stage references. Validate YAML/links and run
  an independent fixture-based agent scenario without live actions.
- [x] Update README and CHANGELOG with exact entry points, keyboard instructions,
  agent fallback, dependency setup, pipeline boundaries and portfolio scope.
- [x] Run `uv run pytest -q`, `uv run ruff check src tests`, `git diff --check`,
  CLI help and a pseudo-terminal smoke test. Review the diff against the spec.

## Acceptance scenarios

```python
# Scripted prompt adapter exercises real controller, not terminal input.
assert cancelled_review.source_plan == original.source_plan
assert download_calls == [approved_candidate_id]
assert discovery_calls_to_av == []
assert process_calls_to_fetch == []
assert missing_reference_model_calls == []
```

Baseline and final tests are local-only. Existing user outputs, decision files,
environment files and historical untracked plans are excluded from changes.

## Verification results

- Baseline: 232 passed, 2 live tests deselected.
- Added regression tests were observed failing before implementation.
- Final suite: 261 passed, 2 live tests deselected; Ruff and whitespace checks pass.
- Both skill entry points pass quick_validate; independent agent instructions
  test passes after correcting local-reference and written-subset guidance.
- Independent code review rechecked four fixes and found no remaining Important
  blockers. The firm hint retains provenance without changing name-only folders.
- Pseudo-terminal smoke test shows the three-stage menu and exits 130 on Ctrl+C.
- No live investor collection, GPU models, paid API calls or remote push performed.

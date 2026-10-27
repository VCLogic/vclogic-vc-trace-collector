# Deduplication performance repair

User approved the repair on 2026-09-18: preserve completed model work, avoid
repeated comparisons, use fast candidate rejection, and report post-processing.

## Design

Keep character-level SequenceMatcher and the existing threshold. Reject pairs
using its mathematically safe length and character-frequency upper bounds before
calling ratio(). Persist ordered pair decisions keyed by actual text digests,
algorithm version, and threshold. Recompute canonical selection each run so
review-policy changes are respected; cache only content similarity decisions.
Use existing atomic JSON storage for the disposable workspace cache.

Changing to approximate token fingerprints would change duplicate semantics;
deferring all deduplication until export would leave intermediate records stale.
Safe bounds plus memoization are the smallest compatible repair.

## Execution (inline)

- [x] Add regression tests for upper-bound pruning, cached repeat runs, invalidation,
  canonical preference and deduplication progress; observe failures first.
- [x] Implement bounds and optional persistent pair cache in process.py; wire both
  processing deduplication calls and final save progress in pipeline.py.
- [x] Run focused and full local tests, benchmark the saved Conwell corpus on copies,
  and compare against legacy results on small generated examples.
- [x] Resume the interrupted recording from model caches and restart only unfinished
  approved queue items with isolated standard input. Report actual outcomes.

Do not modify discovery decisions, speaker thresholds, model caches, or unrelated
user edits. Do not commit credentials, output datasets, or user decision files.

## Verification

268 tests passed, 2 live tests deselected. Cold deduplication of 87 saved records:
36.954 seconds; warm repeat: 0.019 seconds. Generated-pair tests match the legacy
ratio predicate, including directional pair behavior. Interrupted candidate
8c8e5636a958b1346863 finalized with transcript, diarization, and attribution cache
hits; existing batch resumed after its completion. No model caches were deleted.

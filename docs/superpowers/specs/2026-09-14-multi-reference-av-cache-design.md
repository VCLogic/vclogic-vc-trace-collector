# Multi-Reference AV Cache Design

## Purpose

Make speaker attribution stable across recording conditions and make reference-voice changes cheap. Adding or replacing a human-verified voice sample must not rerun Whisper, and must not rerun pyannote when the required diarization embeddings are already cached.

This change remains within collection and corpus preparation. It does not generate Investment Memory or assess pitches.

## Problem

The current AV cache stores one combined `TargetSpeechResult`. Its cache key includes the active `ReferenceVoiceProfile`, the transcription model, and the diarization model. Therefore, changing the reference voice invalidates diarization, transcription, alignment, and attribution together.

The combined result retains only the winning speaker attribution. It does not retain all per-speaker embeddings, so a new reference cannot be compared with the earlier diarization output. `reference_voice_profile.json` also represents only the most recently approved reference, even though `reference_voice_candidates.jsonl` can contain multiple human-verified candidates.

The Michael Hyatt run demonstrated both problems. A Zoom-style reference accepted one recording that a clean stage reference did not, while the stage reference accepted other recordings that the Zoom reference did not. A single reference is sensitive to channel and recording conditions.

## Approaches Considered

### 1. Lower the global similarity threshold

This is the smallest code change, but it weakens every attribution and does not solve redundant processing. It risks admitting a wrong speaker merely because that speaker was the best weak match. Rejected.

### 2. Replace the single reference and retain the combined cache

This preserves the existing data model, but every reference change reruns expensive model work and results remain sensitive to the selected recording. Rejected.

### 3. Independent caches plus a verified reference ensemble

Cache diarization, transcription/alignment, and attribution separately. Persist every verified reference embedding and score every diarized speaker against every compatible reference. Accept a speaker when at least one trusted reference clears the score threshold and the aggregated winning speaker clears the runner-up margin. Preserve the complete score matrix. Selected.

## Data Model

### Reference voice set

Add `identity/reference_voice_profiles.jsonl`. Each row is an immutable, versioned `ReferenceVoiceProfile` for one human-approved interval. The existing `identity/reference_voice_profile.json` remains a compatibility projection of the latest profile during migration.

Profile identity is deterministic over:

- investor slug;
- reference candidate ID;
- reference artifact hash;
- selected start and end timestamps;
- embedding provider and model version;
- embedding values.

Approving a reference appends or replaces only the deterministic profile with the same profile ID. It never deletes other verified profiles. References with incompatible embedding dimensions or models are excluded from an attribution attempt with an explicit audit reason.

### Diarization cache

Add `state/av_diarization/<hash>.json`, containing:

- input artifact ID and SHA-256;
- diarization provider, model, and version;
- diarized turns;
- every speaker label and its embedding;
- media duration;
- creation timestamp and schema version.

Its key depends only on the audio artifact and diarization implementation. It never includes a reference voice, thresholds, or Whisper settings.

### Transcript cache

Add `state/av_transcripts/<hash>.json`, containing:

- input artifact ID and SHA-256;
- transcription provider, model, and version;
- timed transcript segments without target-speaker identity;
- transcript metadata;
- creation timestamp and schema version.

Its key depends only on the audio artifact and transcription implementation. Transcription may still run over diarized turns for the current local Whisper strategy, in which case the diarization cache key and segmentation strategy version are also inputs. It never includes voice references or speaker thresholds.

### Attribution cache

Add `state/av_attributions/<hash>.json`, containing:

- diarization cache key;
- ordered reference profile IDs;
- per-speaker, per-reference cosine scores;
- each speaker's aggregate score;
- winning and runner-up speakers;
- score and margin thresholds;
- aggregation method and version;
- final attribution status.

Its key includes the diarization cache key, compatible reference profile IDs, thresholds, and aggregation version. It does not include Whisper output.

Extend `SpeakerAttribution` with optional structured score evidence while retaining the current scalar fields for downstream compatibility.

## Reference Aggregation

For each diarized speaker, calculate cosine similarity against every compatible human-verified reference. The speaker's aggregate score is the maximum individual score. This implements the rule “a speaker may match any trusted recording condition” and avoids centroids that can blur channel-specific voice characteristics.

Rank speakers by aggregate score. The winner is `accepted_model` only when:

- its aggregate score is at least `speaker_minimum_score`; and
- its aggregate score exceeds the runner-up aggregate score by at least `speaker_minimum_margin`.

Otherwise the result is `uncertain`. A maximum score is not a probability. The evidence records which reference supplied each aggregate maximum and retains all other scores.

Using a source as its own reference will naturally yield a near-perfect score. This is permitted only because the reference interval was explicitly verified by a human. It does not convert unrelated portions or other sources to `verified_human`; their resulting status remains `accepted_model` unless separately human-reviewed.

## Processing Flow

1. Resolve or extract the canonical audio artifact.
2. Load the diarization cache, or run pyannote once and persist turns plus all embeddings.
3. Load the transcript cache, or run Whisper once and persist target-neutral timed text.
4. Load all compatible human-verified reference profiles.
5. Load the attribution cache, or compute the full score matrix and aggregate decision.
6. Align timed text to diarized turns deterministically.
7. Select text assigned to the winning speaker.
8. Apply the corpus gate: only `accepted_model` or `verified_human` speech may enter the corpus.
9. Record cache hits, model operations, exclusions, and decisions in the audit trace.

Changing references or thresholds repeats steps 4–9 only. Changing Whisper repeats transcript generation and alignment but not diarization. Changing pyannote invalidates diarization, attribution, and alignment.

## Existing Workspace Migration

On first use:

- import the current `reference_voice_profile.json` into `reference_voice_profiles.jsonl` if its deterministic profile ID is absent;
- keep all verified candidates and allow the user to reapprove earlier intervals to reconstruct overwritten embeddings;
- treat legacy `state/av_results` as final-result evidence, not as a diarization cache, because it lacks the complete speaker embedding set;
- reuse legacy timed transcript segments to seed the transcript cache when artifact and transcription model metadata match;
- rerun pyannote once per legacy media artifact to populate the new diarization cache;
- never rerun Whisper solely because references changed.

Migration is resumable and idempotent. Original files remain intact.

## Failure and Audit Behavior

- No compatible references: stop the item as review-required before attribution.
- Empty diarization: record an unavailable attribution without invoking Whisper unnecessarily.
- Incompatible reference model or dimensions: omit that reference and record the reason; fail review-required if none remain.
- Corrupt cache: reject it, record the validation error, and recompute only that stage.
- Model failure: retain successful caches from earlier stages.
- Budget accounting charges only actual provider/model invocations; cache hits cost zero and record zero processed media duration.

No private chain-of-thought is stored. Audit records contain model identifiers, cache keys, score evidence, concise action summaries, failures, and costs.

## CLI Behavior

Keep existing commands compatible:

```bash
uv run vc-trace-collector review-voice ...
uv run vc-trace-collector process-source ...
```

`review-voice` adds the profile to the verified set instead of replacing the only profile. `process-source` reports stage-level cache hits and the number of compatible references used. No new command is required for the cache correction.

A future `review-speaker` command is outside this change; uncertain model results continue to require a separate audited human-review feature rather than manual JSON edits.

## Validation and Tests

Unit tests will prove that:

- approving two references preserves both immutable profiles;
- duplicate approval is idempotent;
- incompatible profiles are excluded with evidence;
- per-reference scores and max aggregation are deterministic;
- score and runner-up margin gates remain enforced;
- a reference-only change hits diarization and transcript caches while recomputing attribution;
- a threshold-only change recomputes only attribution;
- a Whisper-model change does not rerun diarization;
- a pyannote-model change invalidates dependent caches;
- legacy transcript data can seed the new transcript cache without a Whisper call;
- uncertain speech stays out of corpus exports;
- manifests and verification validate cache provenance and reference evidence.

Integration tests use fake deterministic providers and local fixture audio. Paid APIs, Hugging Face downloads, GPUs, and network access are not required by default.

## Acceptance Criteria

- Multiple human-verified voice profiles are retained and used.
- Every model attribution preserves the complete per-reference score evidence.
- Adding a compatible reference does not invoke Whisper.
- Adding a reference does not invoke pyannote after the new diarization cache exists.
- A legacy workspace requires at most one pyannote migration pass per media artifact and no Whisper rerun when reusable timed text exists.
- Corpus safety remains fail-closed for uncertain attribution.
- Export and verification remain compatible with `blog.jsonl`, `talks.jsonl`, and `_manifest.json`.

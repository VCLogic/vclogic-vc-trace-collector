# Multi-Reference AV Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve multiple verified voice references and independently cache diarization, transcription, and speaker attribution so reference changes never rerun Whisper and only require pyannote once for legacy media.

**Architecture:** Add typed, content-addressed AV stage records and pure functions for multi-reference matching. `Pipeline.process` will orchestrate the three caches, seed target-neutral transcripts from legacy AV results, and build the existing `TargetSpeechResult` compatibility projection. Reference approval will upsert immutable profiles into a JSONL set while retaining the current single-profile JSON projection.

**Tech Stack:** Python 3.12, Pydantic, pytest, existing JSON/JSONL storage helpers, pyannote provider abstraction, Whisper provider abstraction, SHA-256 canonical JSON hashing.

---

### Task 1: Add multi-reference score evidence and deterministic matching

**Files:**
- Modify: `src/vc_trace_collector/models.py:210-225`
- Modify: `src/vc_trace_collector/av.py:114-175`
- Test: `tests/test_av.py`

- [ ] **Step 1: Write the failing aggregation tests**

Add tests proving that different speakers may match different references, the maximum trusted-reference score is used per speaker, the runner-up margin is calculated from aggregated speaker scores, and every individual score is retained:

```python
def test_multiple_references_use_best_trusted_recording_condition() -> None:
    decision = match_target_speaker_references(
        references=[
            ReferenceEmbedding(profile_id="studio", embedding=[1.0, 0.0]),
            ReferenceEmbedding(profile_id="zoom", embedding=[0.0, 1.0]),
        ],
        speakers={"VC": [0.0, 1.0], "HOST": [0.7, 0.7]},
        minimum_score=0.75,
        minimum_margin=0.10,
    )
    assert decision.status == SpeakerStatus.ACCEPTED_MODEL
    assert decision.speaker_label == "VC"
    assert decision.score == pytest.approx(1.0)
    assert decision.matched_reference_profile_id == "zoom"
    assert len(decision.score_evidence) == 2


def test_reference_ensemble_applies_margin_after_aggregation() -> None:
    decision = match_target_speaker_references(
        references=[
            ReferenceEmbedding(profile_id="one", embedding=[1.0, 0.0]),
            ReferenceEmbedding(profile_id="two", embedding=[0.0, 1.0]),
        ],
        speakers={"A": [1.0, 0.0], "B": [0.0, 1.0]},
        minimum_score=0.75,
        minimum_margin=0.10,
    )
    assert decision.status == SpeakerStatus.UNCERTAIN
    assert decision.margin == pytest.approx(0.0)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_av.py::test_multiple_references_use_best_trusted_recording_condition tests/test_av.py::test_reference_ensemble_applies_margin_after_aggregation -q
```

Expected: collection/import failure because `ReferenceEmbedding` and `match_target_speaker_references` do not exist.

- [ ] **Step 3: Add typed evidence fields**

In `models.py`, add strict models and backward-compatible optional fields:

```python
class ReferenceSimilarity(StrictModel):
    reference_profile_id: str
    reference_artifact_ids: list[str] = Field(default_factory=list)
    score: float = Field(ge=-1, le=1)


class SpeakerScoreEvidence(StrictModel):
    speaker_label: str
    aggregate_score: float = Field(ge=-1, le=1)
    matched_reference_profile_id: str
    reference_scores: list[ReferenceSimilarity] = Field(min_length=1)
```

Extend `SpeakerAttribution` with:

```python
matched_reference_profile_id: str | None = None
score_evidence: list[SpeakerScoreEvidence] = Field(default_factory=list)
aggregation_method: str | None = None
aggregation_version: str | None = None
```

- [ ] **Step 4: Implement deterministic ensemble matching**

In `av.py`, add:

```python
class ReferenceEmbedding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    embedding: list[float] = Field(min_length=1)


def match_target_speaker_references(
    references: Sequence[ReferenceEmbedding],
    speakers: dict[str, Sequence[float]],
    minimum_score: float,
    minimum_margin: float,
    *,
    diarization_model: str | None = None,
    embedding_model: str | None = None,
) -> SpeakerAttribution:
    if not references:
        raise ValueError("At least one compatible reference embedding is required")
    evidence = []
    for label in sorted(speakers):
        scores = [
            ReferenceSimilarity(
                reference_profile_id=reference.profile_id,
                reference_artifact_ids=reference.artifact_ids,
                score=cosine_similarity(reference.embedding, speakers[label]),
            )
            for reference in sorted(references, key=lambda item: item.profile_id)
        ]
        winner = max(scores, key=lambda item: (item.score, item.reference_profile_id))
        evidence.append(
            SpeakerScoreEvidence(
                speaker_label=label,
                aggregate_score=winner.score,
                matched_reference_profile_id=winner.reference_profile_id,
                reference_scores=scores,
            )
        )
    ranked = sorted(
        evidence,
        key=lambda item: (item.aggregate_score, item.speaker_label),
        reverse=True,
    )
    winner = ranked[0]
    runner_up_score = ranked[1].aggregate_score if len(ranked) > 1 else -1.0
    margin = winner.aggregate_score - runner_up_score
    status = (
        SpeakerStatus.ACCEPTED_MODEL
        if winner.aggregate_score >= minimum_score and margin >= minimum_margin
        else SpeakerStatus.UNCERTAIN
    )
    matched = next(
        item
        for item in winner.reference_scores
        if item.reference_profile_id == winner.matched_reference_profile_id
    )
    return SpeakerAttribution(
        status=status,
        speaker_label=winner.speaker_label,
        score=winner.aggregate_score,
        runner_up_score=runner_up_score,
        margin=margin,
        minimum_score=minimum_score,
        minimum_margin=minimum_margin,
        diarization_model=diarization_model,
        embedding_model=embedding_model,
        reference_artifact_ids=matched.reference_artifact_ids,
        matched_reference_profile_id=winner.matched_reference_profile_id,
        score_evidence=ranked,
        aggregation_method="max_verified_reference",
        aggregation_version="1",
    )
```

Keep `match_target_speaker` as a compatibility wrapper using one `ReferenceEmbedding`.

- [ ] **Step 5: Run focused and existing AV tests**

Run:

```bash
uv run pytest tests/test_av.py -q
```

Expected: all AV tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/vc_trace_collector/models.py src/vc_trace_collector/av.py tests/test_av.py
git commit -m "feat: score speakers against verified reference ensemble"
```

### Task 2: Persist an immutable set of verified reference profiles

**Files:**
- Modify: `src/vc_trace_collector/models.py:298-306`
- Modify: `src/vc_trace_collector/pipeline.py:982-1175`
- Modify: `src/vc_trace_collector/verify.py`
- Test: `tests/test_pipeline_cli.py`

- [ ] **Step 1: Write failing profile-set tests**

Add an integration test that approves two collected reference candidates, reapproves one, and asserts two deterministic profiles remain:

```python
profiles_path = output / "michael-hyatt/identity/reference_voice_profiles.jsonl"
profiles = read_jsonl(profiles_path)
assert len(profiles) == 2
assert len({row["profile_id"] for row in profiles}) == 2
assert read_json(output / "michael-hyatt/identity/reference_voice_profile.json")[
    "profile_id"
] == profiles[-1]["profile_id"]
```

Also add a test that starts with only legacy `reference_voice_profile.json`, calls the profile loader, and gets one migrated profile without modifying or deleting the legacy file.

- [ ] **Step 2: Verify RED**

Run the new tests directly. Expected: missing `reference_voice_profiles.jsonl` and missing `profile_id`.

- [ ] **Step 3: Add deterministic profile identity**

Extend `ReferenceVoiceProfile` with `profile_id: str | None = None` for legacy parsing. Add a helper that returns a copied model with a deterministic ID:

```python
def identify_reference_profile(profile: ReferenceVoiceProfile) -> ReferenceVoiceProfile:
    payload = profile.model_dump(mode="json", exclude={"profile_id", "created_at"})
    profile_id = "voice-profile:" + sha256(canonical_json(payload).encode()).hexdigest()[:20]
    return profile.model_copy(update={"profile_id": profile_id})
```

- [ ] **Step 4: Add idempotent profile-set loading and upsert**

Add private pipeline helpers:

```python
def _load_reference_profiles(
    self, workspace: Path
) -> list[ReferenceVoiceProfile]:
    profiles_path = workspace / "identity/reference_voice_profiles.jsonl"
    profiles = [
        identify_reference_profile(ReferenceVoiceProfile.model_validate(row))
        for row in read_jsonl(profiles_path)
    ]
    legacy_path = workspace / "identity/reference_voice_profile.json"
    if legacy_path.exists():
        profiles.append(
            identify_reference_profile(
                ReferenceVoiceProfile.model_validate(read_json(legacy_path))
            )
        )
    by_id = {profile.profile_id: profile for profile in profiles}
    return sorted(by_id.values(), key=lambda item: (item.created_at, item.profile_id or ""))


def _upsert_reference_profile(
    self, workspace: Path, profile: ReferenceVoiceProfile
) -> list[ReferenceVoiceProfile]:
    identified = identify_reference_profile(profile)
    profiles = {
        item.profile_id: item for item in self._load_reference_profiles(workspace)
    }
    profiles[identified.profile_id] = identified
    ordered = sorted(
        profiles.values(), key=lambda item: (item.created_at, item.profile_id or "")
    )
    write_jsonl(workspace / "identity/reference_voice_profiles.jsonl", ordered)
    write_json(workspace / "identity/reference_voice_profile.json", identified)
    return ordered
```

Load JSONL first, migrate the legacy JSON projection when absent, sort by `(created_at, profile_id)`, and atomically rewrite JSONL on upsert. `approve_reference_voice` must add the new profile and continue writing the latest compatibility JSON.

- [ ] **Step 5: Extend verification**

Validate unique profile IDs, verified-human status, nonempty equal embedding dimensions per compatible model, referenced artifact existence, and agreement between the latest compatibility JSON and an entry in the set.

- [ ] **Step 6: Run focused tests and commit**

```bash
uv run pytest tests/test_pipeline_cli.py -k 'reference_voice' -q
git add src/vc_trace_collector/models.py src/vc_trace_collector/pipeline.py src/vc_trace_collector/verify.py tests/test_pipeline_cli.py
git commit -m "feat: preserve verified reference voice profiles"
```

### Task 3: Introduce typed independent AV stage cache records

**Files:**
- Create: `src/vc_trace_collector/av_cache.py`
- Modify: `src/vc_trace_collector/av.py`
- Test: `tests/test_av_cache.py`

- [ ] **Step 1: Write failing content-addressed cache tests**

Create tests asserting reference changes affect only attribution keys, Whisper changes affect only transcript keys, and pyannote changes affect diarization plus dependent keys:

```python
def test_reference_change_does_not_change_model_stage_keys() -> None:
    first = cache_keys(fixture_input(), reference_ids=["one"])
    second = cache_keys(fixture_input(), reference_ids=["one", "two"])
    assert first.diarization == second.diarization
    assert first.transcript == second.transcript
    assert first.attribution != second.attribution
```

Test strict round-trip validation for corrupt or extra fields.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_av_cache.py -q
```

Expected: import failure because `av_cache` does not exist.

- [ ] **Step 3: Implement cache schemas**

Define strict Pydantic records:

```python
class DiarizationCacheRecord(StrictModel):
    schema_version: str = "1.0"
    cache_key: str
    artifact_id: str
    artifact_sha256: str
    provider: str
    model: str
    model_version: str
    result: DiarizationResult
    created_at: AwareDatetime = Field(default_factory=utc_now)


class TranscriptCacheRecord(StrictModel):
    schema_version: str = "1.0"
    cache_key: str
    artifact_id: str
    artifact_sha256: str
    provider: str
    model: str
    model_version: str
    diarization_cache_key: str
    segmentation_version: str = "diarized-turns-v1"
    transcript: TranscriptInfo
    segments: list[TimedText]
    created_at: AwareDatetime = Field(default_factory=utc_now)


class AttributionCacheRecord(StrictModel):
    schema_version: str = "1.0"
    cache_key: str
    diarization_cache_key: str
    reference_profile_ids: list[str]
    minimum_score: float
    minimum_margin: float
    aggregation_method: str = "max_verified_reference"
    aggregation_version: str = "1"
    attribution: SpeakerAttribution
    created_at: AwareDatetime = Field(default_factory=utc_now)
```

Provide canonical key functions and `load_validated`/atomic `write_json` helpers rooted at `state/av_diarization`, `state/av_transcripts`, and `state/av_attributions`.

- [ ] **Step 4: Extract target-neutral transcription**

Refactor `av.py` so model operations can be called independently:

```python
def transcribe_diarized_turns(
    audio_path: Path,
    diarization: DiarizationResult,
    transcript_provider: TranscriptProvider,
    segment_extractor: Callable[..., Path] | None = None,
) -> TranscriptResult:
    extractor = segment_extractor or extract_audio_segment
    segments: list[TimedText] = []
    info = TranscriptInfo(
        method="speech_to_text",
        provider=transcript_provider.provider_name,
        model=transcript_provider.model_name,
    )
    with tempfile.TemporaryDirectory(prefix="vc-trace-segments-") as directory:
        for index, turn in enumerate(diarization.turns):
            segment_path = Path(directory) / f"segment_{index:05d}.wav"
            extractor(
                audio_path,
                segment_path,
                start_seconds=turn.start_seconds,
                end_seconds=turn.end_seconds,
            )
            result = transcript_provider.transcribe(segment_path)
            info = result.info
            segments.extend(
                TimedText(
                    start_seconds=turn.start_seconds + item.start_seconds,
                    end_seconds=min(
                        turn.end_seconds, turn.start_seconds + item.end_seconds
                    ),
                    text=item.text,
                )
                for item in result.segments
                if min(turn.end_seconds, turn.start_seconds + item.end_seconds)
                > turn.start_seconds + item.start_seconds
            )
    return TranscriptResult(info=info, segments=segments)


def assemble_target_speech(
    *,
    diarization: DiarizationResult,
    transcript: TranscriptResult,
    attribution: SpeakerAttribution,
) -> TargetSpeechResult:
    aligned = align_transcript_to_speakers(transcript.segments, diarization.turns)
    target = [
        item
        for item in aligned
        if attribution.speaker_label is not None
        and item.speaker_label == attribution.speaker_label
    ]
    media_seconds = max(
        [item.end_seconds for item in aligned]
        + [item.end_seconds for item in diarization.turns]
        + [0.0]
    )
    return TargetSpeechResult(
        transcript=transcript.info,
        attribution=attribution,
        aligned_segments=aligned,
        target_segments=target,
        media_seconds=media_seconds,
    )
```

`process_target_speech` remains a compatibility composition of diarize, transcribe, match, and assemble.

- [ ] **Step 5: Run cache and AV tests and commit**

```bash
uv run pytest tests/test_av_cache.py tests/test_av.py -q
git add src/vc_trace_collector/av.py src/vc_trace_collector/av_cache.py tests/test_av.py tests/test_av_cache.py
git commit -m "feat: separate audiovisual stage records"
```

### Task 4: Orchestrate stage caches and seed legacy transcripts

**Files:**
- Modify: `src/vc_trace_collector/pipeline.py:1190-1585`
- Modify: `src/vc_trace_collector/state.py`
- Test: `tests/test_pipeline_cli.py`

- [ ] **Step 1: Write failing provider-call-count tests**

Extend the existing counting-provider integration setup:

```python
collector.process_source("michael-hyatt", candidate_id=interview.candidate_id)
first_diarizations = diarization.calls
first_transcriptions = transcript.calls

collector.approve_reference_voice(
    "michael-hyatt", candidate_id=second_voice_id, reviewer="reviewer"
)
collector.process_source("michael-hyatt", candidate_id=interview.candidate_id)

assert diarization.calls == first_diarizations
assert transcript.calls == first_transcriptions
assert len(latest_document.speaker_attribution.score_evidence) == 2
```

Add separate tests proving a threshold-only change invokes neither provider, a transcript-model change invokes only transcription, and a diarization-model change invokes both dependent stages.

- [ ] **Step 2: Write the failing legacy-seed test**

Create a workspace with a legacy `processed/av_attribution_results.jsonl` row containing timed `aligned_segments`, no new transcript cache, and counting fake providers. Process it with a newly approved reference and assert pyannote is called once while Whisper is never called.

- [ ] **Step 3: Verify RED**

Run each new integration test and confirm provider call counts exceed the expected values under the combined cache.

- [ ] **Step 4: Replace combined-cache orchestration**

Within the AV artifact loop:

```python
diarization_record, diarization_hit = av_cache.get_or_run_diarization(
    artifact=processing_artifact,
    audio_path=path,
    provider=diarization_provider,
)
transcript_record, transcript_hit = av_cache.get_or_run_transcript(
    artifact=processing_artifact,
    audio_path=path,
    diarization=diarization_record,
    provider=transcript_provider,
    legacy_segments=_legacy_timed_segments(workspace, processing_artifact),
)
attribution_record, attribution_hit = av_cache.get_or_run_attribution(
    diarization=diarization_record,
    references=compatible_references,
    minimum_score=config.speaker_minimum_score,
    minimum_margin=config.speaker_minimum_margin,
)
result = assemble_target_speech(
    diarization=diarization_record.result,
    transcript=transcript_record.as_result(),
    attribution=attribution_record.attribution,
)
```

Reserve and settle cost/media operations only around cache misses. Emit one structured audit event listing the three stage keys, hit booleans, compatible profile IDs, and omitted-profile reasons.

- [ ] **Step 5: Implement safe legacy transcript seeding**

Read matching rows from `processed/av_attribution_results.jsonl`. Accept a row only when candidate/artifact provenance and transcript provider/model match. Strip `speaker_label` and `overlap_seconds` from legacy aligned segments to produce target-neutral `TimedText`, then realign those timestamps to the new diarization. Record `migration_source="legacy_av_result"` in transcript-cache metadata. Never infer or migrate missing speaker embeddings.

- [ ] **Step 6: Keep compatibility output deterministic**

Write the assembled `TargetSpeechResult` to the existing `state/av_results` compatibility location and merge one latest canonical document per candidate/version. Do not append an identical document as both included and duplicate on a cache-hit rerun.

- [ ] **Step 7: Run integration tests and commit**

```bash
uv run pytest tests/test_pipeline_cli.py -k 'process_source or reference_change or legacy_transcript' -q
git add src/vc_trace_collector/pipeline.py src/vc_trace_collector/state.py tests/test_pipeline_cli.py
git commit -m "feat: reuse AV stages across reference changes"
```

### Task 5: Verify provenance, update documentation, and exercise Michael Hyatt migration

**Files:**
- Modify: `src/vc_trace_collector/verify.py`
- Modify: `src/vc_trace_collector/export.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_process_export.py`
- Test: `tests/test_verify.py`

- [ ] **Step 1: Write failing verification and export tests**

Add tests that reject missing cache dependencies, mismatched cache keys, model-accepted attribution without score evidence, and profiles referring to missing artifacts. Add an export test showing only the latest included version of a reprocessed candidate enters `talks.jsonl`.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_verify.py tests/test_process_export.py -k 'av_cache or reference_profile or reprocessed' -q
```

Expected: new tests fail because verification does not traverse stage provenance and export retains duplicate versions.

- [ ] **Step 3: Implement provenance verification and deterministic latest-version export**

Verify each attribution record links to an existing diarization record and all referenced profiles; each transcript links to an existing diarization record; each model-accepted canonical document contains the matching evidence. Resolve reprocessed candidates by deterministic document version and inclusion precedence before legacy export.

- [ ] **Step 4: Document behavior**

Add README sections showing that `review-voice` accumulates references and that `process-source` reports cache hits. Add a changelog entry describing the cache migration, legacy compatibility, and the fact that the first legacy rerun requires pyannote once but not Whisper.

- [ ] **Step 5: Run the full automated suite**

```bash
uv run pytest -q
```

Expected: all default tests pass without network, paid APIs, Hugging Face downloads, or a GPU.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/vc_trace_collector/verify.py src/vc_trace_collector/export.py README.md CHANGELOG.md tests/test_process_export.py tests/test_verify.py
git commit -m "docs: document multi-reference AV provenance"
```

- [ ] **Step 7: Reapprove the earlier Michael Hyatt reference**

After automated verification passes, reconstruct the earlier verified Zoom profile from its retained candidate and interval:

```bash
uv run --extra av --extra av-local vc-trace-collector review-voice \
  --investor michael-hyatt \
  --candidate-id 'voice:candidate:220a65f7d78e5c4adc70' \
  --reviewer 'human:dpasch01' \
  --start-seconds 108 --end-seconds 175 \
  --diarization-model pyannote/speaker-diarization-3.1
```

Expected: two verified profiles exist: the full Seismic clip and the retained Tank Talk interval.

- [ ] **Step 8: Run the Michael Hyatt migration and confirm cache behavior**

Process all four approved candidate IDs sequentially. The first post-migration pass may invoke pyannote to populate missing per-speaker embeddings, but audit events must show transcript cache seeding and no Whisper invocation. Repeating the same commands must show all three stages as cache hits.

- [ ] **Step 9: Export and verify**

```bash
uv run vc-trace-collector export --investor michael-hyatt
uv run vc-trace-collector verify --investor michael-hyatt
uv run vc-trace-collector status --investor michael-hyatt
```

Expected: the corpus includes only model-accepted or human-verified target speech; any remaining uncertain item stays in review-required output. Report unrelated pre-existing discovery/state verification errors separately rather than weakening validation.

- [ ] **Step 10: Final repository checks**

```bash
git status --short
git log --oneline -8
```

Expected: only the user's pre-existing untracked files remain; implementation commits are present and no credential files are staged.

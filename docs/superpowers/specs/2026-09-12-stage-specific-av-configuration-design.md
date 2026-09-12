# Stage-Specific Audiovisual Configuration Design

## Problem

The `discover` command currently accepts transcription, diarization, and voice-embedding options even though it performs none of those operations. It stores the values early in `config_snapshot.json` for later stages, which preserves reproducibility but makes the CLI misleading.

## Considered Approaches

1. **Stage-specific options:** put voice-model options on `review-voice` and transcription/diarization options on `process`. Persist each selection before that stage runs and audit the change. This is the selected approach because commands describe the work they perform.
2. **Separate `configure-av` command:** configure every AV model between collection and processing. This avoids repeated model arguments but introduces another required command and a new configuration surface.
3. **Environment-only model selection:** read all model names from `.env`. This is concise but hides important reproducibility inputs and makes per-investor settings awkward.

## CLI Contract

`discover` retains identity, search, source, exclusion, and run-wide budget controls. It no longer exposes transcription, diarization, embedding, or their per-operation cost options.

`review-voice` gains optional `--diarization-model`, `--embedding-model`, and `--embedding-cost-usd` options. At least one usable voice model must exist either in the saved run configuration or in this invocation.

`process` gains optional `--transcription-model`, `--diarization-model`, `--transcription-cost-usd`, and `--diarization-cost-usd` options. Existing saved values remain valid for backward compatibility. Missing required models continue to produce `ReviewRequired` before a provider is invoked.

The combined `collect` command keeps its current full-workflow options because it can perform discovery, collection, processing, and export in one invocation.

## Persistence and Audit

Before `review-voice` or `process` performs model work, the pipeline validates the supplied values through `RunConfig`, writes the effective configuration to `config_snapshot.json`, and appends a structured `configuration` audit event containing only the changed public model/cost fields. No token or credential is recorded.

Omitted options do not erase saved values. Supplying the same value is a no-op and does not create a redundant event. The final manifest fingerprint uses the effective configuration.

## Tests

CLI tests verify that discovery help excludes AV-stage flags, the two consuming commands expose the appropriate flags, and values reach pipeline methods. Pipeline tests verify persistence, audit events, no-op behavior, and existing saved-configuration compatibility. The full offline suite, lint, and package build remain release gates.

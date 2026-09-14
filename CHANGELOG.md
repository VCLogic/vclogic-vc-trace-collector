# Changelog

All notable changes to `vc-trace-collector` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Additive, human-verified reference voice profiles with deterministic profile
  IDs and complete per-speaker/per-reference similarity evidence.
- Independent content-addressed caches for diarization, timed transcription,
  and speaker attribution, including legacy transcript-cache migration.
- Repeatable `search-source --query` overrides for targeted source discovery.
- Audited `--max-search-operations` increases for existing resumable runs.
- Structured YouTube channel provenance in search observations and candidates.

### Fixed

- Platform-specific downloads now collect approved candidates even when other
  candidates on the same platform remain pending review.
- Adding or changing a reference voice now recomputes only attribution when
  reusable diarization and transcription results exist; reprocessing replaces
  the candidate result instead of duplicating corpus documents.
- Reapproving a bounded voice interval now resolves back to the original media
  instead of attempting to cut the same timestamps from an earlier excerpt.
- Human-selected reference excerpts are no longer processed as independent
  target talks, and cosine scores are clamped against floating-point roundoff.
- Verification now checks reference-profile lineage and dependencies between
  audiovisual stage-cache records.
- Artifact loading now distinguishes collector provenance sidecars from raw
  yt-dlp `.metadata.json` payloads during processing, export, verification,
  and legacy resume.
- The `av-local` extra now pins the compatible PyTorch and TorchAudio 2.8.0
  pair and Hugging Face Hub 0.x API required by pyannote 3.x.
- Legacy pyannote checkpoints now load under PyTorch's secure weights-only
  default using a scoped allowlist of four installed pyannote/PyTorch types.
- Agent Reach preflight now catches a missing `mcporter` before reserving
  budget, and unreachable Exa failures stop after one query without consuming
  the search-operation allowance.
- Agent Reach Exa searches now request JSON output and parse Exa records
  carried in MCP text content instead of incorrectly reporting invalid JSON.
- Existing pending candidates are enriched with newly discovered channel
  metadata so channel-based leakage rules apply on incremental reruns.

## [0.1.0] - 2026-09-13

### Added

- Complete name-to-corpus workflow covering identity resolution, source
  discovery and review, deterministic collection, normalization, exclusions,
  export, and independent verification.
- Source-specific staged CLI commands: `doctor`, `search-source`,
  `list-sources`, `fetch-source`, and `process-source`.
- Optional Agent Reach integration using its configured Exa backend through
  `mcporter`, alongside direct `yt-dlp` YouTube discovery.
- Web, RSS/Atom, Substack, Medium, podcast, YouTube, and supplied-file
  collection paths with isolated failures and resumable operation state.
- FFmpeg audio normalization, configurable Whisper transcription, pyannote
  diarization and embeddings, human-approved reference voices, target-speaker
  matching, and target-only speech extraction.
- Content-addressed raw artifacts, canonical document records, deterministic
  manifests, public audit events, transactional budget accounting, and legacy
  `blog.jsonl`, `talks.jsonl`, and `_manifest.json` exports.
- Configurable domain, channel, programme, company, author, speaker, keyword,
  and URL-pattern exclusions, including the default `thepitch.show` leakage
  firewall.
- Automatic loading of local `.env` values without overriding variables that
  are already present in the process environment.
- Project-local `vc-trace-collector` agent skill describing the same staged,
  human-reviewed workflow exposed by the CLI.

### Security

- Added DNS and connected-peer validation for public fetches, bounded downloads,
  rate limiting, sanitized audit records, strict credential handling, and
  fail-closed media-duration and provider-budget gates.
- Prevented uncertain identity and speaker matches from being represented as
  verified corpus material.

### Changed

- Audiovisual processing uses downloaded audio and a diarize-first pipeline;
  platform captions are not used as target-speaker transcripts.
- Source search, collection, and processing can run independently per platform
  or candidate while preserving earlier decisions and successful outputs.
- Verification reconciles search observations and collection outcomes against
  cached provider results, operation state, raw metadata, source candidates,
  run summaries, and manifest hashes.

# Full-audio transcription experiment

User-approved plan: safely pause the active Mac batch, preserve its completed
results, implement on an isolated branch, benchmark an already completed video
using the same audio/Whisper model/diarization, and resume only after a successful
comparison. No recordings or existing caches are deleted.

Implementation: opt-in `VC_TRACE_TRANSCRIPTION_STRATEGY=full-audio-words-v1`.
Whisper transcribes the recording once with word timestamps. Diarization assigns
each word to its unique overlapping speaker; words overlapping multiple speakers
or no speaker remain unattributed. Adjacent words with the same attribution form
readable segments. Word spans may include leading silence.
Keep existing output models, record strategy in transcript method/cache identity,
and retain legacy per-turn behavior by default. Unsupported providers fail closed.

Tests: one full-model call, no per-turn extraction, boundary and overlap handling,
existing schema validation, strategy-separated cache keys, and existing suite.
Benchmark: save old/new results outside production, measure each strategy's wall
time, compare text coverage, attribution consistency, timestamps and schema.
No ground-truth transcription is available: text agreement is diagnostic, not WER.
Merge only if faster with no clear material quality regression. Otherwise retain
the branch and restart the old strategy. Resume only unfinished successful-eligible
IDs, preserving earlier completed and failed outcomes for audit.

## Measured comparison (2026-10-13)

Completed Mac Conwell interview: https://www.youtube.com/watch?v=uh5ZHxCL0io
(421.361 seconds, 77 diarized turns). Tesla T4, Whisper `turbo`; both paths used
the same loaded model, audio, cached diarization, and reference attribution.
Model loading and diarization are excluded from these transcription timings.

| Check | Per-turn | Full-audio words |
|---|---:|---:|
| Wall time, final comparison | 53.69 s | 26.51 s |
| Transcript words | 1,620 | 1,603 |
| Target-speaker words | 478 | 464 |

The new path was 2.03x faster in this comparison. Normalized token sequence
agreement was 97.30% for the complete transcript and 96.18% for target speech.
These are agreement scores, **not measured transcription accuracy**. The first
run incurred word-timestamp kernel setup and was 1.45x faster (55.57 vs 38.33 s).
Expect different speedups for other recordings; pyannote time is unchanged.

Schema and speaker-match score/status were unchanged. Text, segmentation and
timestamps are not byte-identical. Inspection found no clear material regression
in the sampled target passages; 15 words remained unattributed around competing
speakers. They remain in the full aligned transcript rather than being silently
assigned to the investor. This is not an exhaustive manual audio accuracy audit.

Benchmark files and the pre-change backup are under
`outputs/benchmarks/full-audio-20261013/`; production recordings were not replaced.
The strategy remains opt-in. To use it, set
`VC_TRACE_TRANSCRIPTION_STRATEGY=full-audio-words-v1` in the process environment
or `.env` before starting the CLI. Unset it (or set `diarized-turns-v1`) to revert.
Strategies use separate transcript caches; diarization caches remain reusable.
Legacy phrase results are not migrated into word-timestamp caches.

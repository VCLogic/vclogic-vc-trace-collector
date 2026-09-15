# Processing and voice references

Offer `wizard --stage process --investor investor-name`. It lists local downloaded
items and does not fetch remote media. Written normalization needs no reference.
For AV, check installed dependencies and configured models only now. The local
pipeline uses Whisper for transcription and pyannote for diarization/embeddings;
do not substitute platform captions.

If an approved reference exists, reuse it. Otherwise stop before model work and
ask the human to choose a downloaded recording plus target-only start/end times,
or supply a local public sample. Provide the actual local playback path. The human
must listen and confirm the target, with no music, crosstalk or host speech.
An agent must not claim it listened merely from a title or transcript.

If the sample is not acquired, return to the download stage. The wizard can stage
a local file without downloading it. Never silently redownload a recording in a
processing-only request. Read actual voice IDs from
`identity/reference_voice_candidates.jsonl`:

```bash
uv run --extra av --extra av-local vc-trace-collector review-voice \
  --investor investor-name --candidate-id voice:actual-id \
  --reviewer human:reviewer --start-seconds 108 --end-seconds 175 \
  --diarization-model pyannote/speaker-diarization-3.1 --embedding-model ""
```

This command can create a model embedding, so obtain approval first. Model names
are configurable; the numbers above are syntax examples, not usable timestamps.
Use the same diarization model for the reference and interviews. An empty
`--embedding-model ""` clears a saved standalone setting and uses that pipeline's
embedding space; do not substitute an incompatible standalone embedding model.
Probe the recording duration with `ffprobe` before accepting an interval in a
scripted run. The wizard performs this interval check itself.
The CLI loads `.env`; do not ask the user to paste HF_TOKEN into chat.

If no voice candidate exists, a conversational run can stage a local public
sample (including a downloaded recording selected by the human):

```bash
uv run vc-trace-collector stage-reference --investor investor-name \
  --file /actual/path/reference.wav --reviewer human:reviewer
```

This records a source, not a verified voice, and does not copy or process bytes.
Ask permission to return to downloading; fetch the returned candidate ID, then
read the actual voice ID and request listening/timestamps. Do not make this
transition during a processing-only request without the user's agreement.

After reference approval and processing consent:

```bash
uv run --extra av --extra av-local vc-trace-collector process-source \
  --investor investor-name --candidate-id candidate:actual-id \
  --transcription-model turbo --diarization-model pyannote/speaker-diarization-3.1
```

`process-source` is for spoken material. For selected downloaded written items:

```bash
uv run vc-trace-collector process --investor investor-name \
  --candidate-id candidate:actual-written-id
```

Repeat `--candidate-id` for multiple selected items; omitting it processes the
workspace, so do not omit it when the user chose a subset. Reuse independent
transcription/diarization/attribution caches. A reference change should only
recompute attribution when upstream caches are valid. Model matches can remain
uncertain; report review-required and failed outcomes, not verified speech.

Read `processed/documents.jsonl`, `processed/target_speech.jsonl`,
`processed/av_candidate_outcomes.jsonl` and `quality_report.json`. Offer `export`,
then `verify` and `status`, as an explicit final action. Report verification
failures. Export does not create an Investment Memory or write another repo.

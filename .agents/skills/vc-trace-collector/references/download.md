# Download only

Read the existing plan and download outcomes. Confirm the selected approved items
and limits. Offer `wizard --stage download --investor investor-name` in the human's
terminal, or use explicit commands after conversational selection:

```bash
uv run --extra youtube vc-trace-collector fetch-source \
  --investor investor-name --source youtube
uv run vc-trace-collector fetch-source --investor investor-name --source podcast
uv run vc-trace-collector fetch-source --investor investor-name \
  --candidate-id candidate:actual-id
```

Use `--source` only when the user chose all approved items on that platform. For
individual selection, use candidate IDs. Do not lower identity thresholds or
approve pending items merely to download more material. Successful downloads are
resumable; report failed sources and explicitly selected retries.

The wizard can stage a local reference file in the source plan. Download that
`supplied` candidate in this stage before returning to processing. Staging alone
does not copy its bytes or verify the voice.

Inspect `outputs/<slug>/processed/collection_candidate_outcomes.jsonl` and
`raw/` provenance. A podcast page without acquired audio is not a downloaded
podcast episode. Do not infer completion from a warning-free command alone.
Do not launch transcription, diarization or export at the end of download.

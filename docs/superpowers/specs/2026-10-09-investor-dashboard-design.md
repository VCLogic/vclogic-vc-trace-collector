# Investor-first terminal dashboard

## Approved direction

Replace the stage-first prompt maze with a full-screen, keyboard- and mouse-
operated terminal dashboard. No browser mockup or web application. Keep the
existing Python pipeline, audit records, corpus formats, and scriptable commands.
This is an interface and job-orchestration change, not a new collection engine.

## Entry and navigation

`uv run vc-trace-collector wizard` opens the investor dashboard. Explicit legacy
`--stage` invocations continue to work. The dashboard lists existing workspaces
and a prominent New investor action. It shows the investor's name and firm,
discovered/approved/downloaded/processed counts, review-required and failure counts,
active stage, and next recommended action. Sort active and attention-needed
workspaces first. Missing/corrupt workspace records are visible errors, not silently
empty or completed collections. Reload reconciles records without starting jobs.

Use a restrained dark terminal palette, clear focus outlines, readable tables,
status words as well as color, a persistent breadcrumb, and a footer showing
keyboard shortcuts. Support narrow terminals with stacked panels and scrollable
details. Escape goes back; quitting does not silently cancel background work.

## New investor and discovery

1. Enter name and optional firm/profile URL.
2. Choose platforms using labeled checkboxes, with Select all and Clear actions.
   Explain which backends are available and which need setup or supplied exports.
3. Resolve identity and show profile evidence for human confirmation before search.
4. Show the selected platforms and configured limits before starting discovery.
   Advanced provider/model controls are collapsed rather than mandatory prompts.
5. Show progress by platform/query, candidate counts, and understandable failures.
   Never use a fabricated percentage when the final query count is unknown.
6. Save candidates and lead directly to source review, with Back and Save & exit.

## Review

Display a searchable table with title, URL, platform, identity confidence,
material role, decision, and download/processing status. A detail pane contains
identity evidence and discovery provenance. Support approve, reject, defer,
select visible, and Accept all pending. Bulk approval requires one explicit
confirmation showing count and low-confidence warnings. It preserves previously
rejected sources, exclusion rules, confidence values, and material roles.

Source approval authorizes collection, not factual verification of all content.
Wrong-person matches must remain easy to reject. No automatic promotion of
uncertain identity or voice attribution to verified status.

## Download and resume

After review, offer Download approved missing sources, with platform selection
and a clear count. Map discovery candidates to acquired artifacts through IDs,
not filenames or titles. Podcast pages alone are not downloaded audio. Show
per-source and overall progress, throughput when measured, failed-source details,
backoff state, disk headroom, and Retry failed. Unknown totals are indeterminate.

Reuse the existing cache, budgets, exclusions, source isolation, and sanitized
failure reporting. Never redownload successful intact artifacts merely because
the interface was closed. Do not change limits or approve sources implicitly.

## Voice signature

For audiovisual processing, reuse compatible human-approved references. Otherwise
suggest up to three already-downloaded, approved appearances, with title,
clickable/copyable source URL, local audio path, duration, identity confidence,
and a short explanation of the ranking. Ranking uses identity evidence and
recording metadata; it must not claim to have listened or verified clean speech.
Provide Browse all and an explicit local-file workflow if no suitable clip exists.

The user selects a recording and enters start/end as mm:ss, hh:mm:ss, or seconds.
Validate finite values, ordering, and actual media duration using ffprobe before
any model call. Require confirmation that only the target speaks in that interval.
Then extract the interval and create the embedding using the existing audited
reference operation. Preserve source lineage and support additional references.
On SSH, show a copyable public URL and local path; do not assume the remote host
can open a browser or play sound on the user's computer.

## Processing

Show downloaded eligible items, configured models/device, reference, and limits.
Start only after confirmation. Show extraction, diarization, transcription,
speaker matching, and saving as distinct stages. Use real progress events and
cache hits. Preserve uncertain attribution as review-required. Process one GPU
job at a time by default; downloads may proceed alongside it. Do not auto-export
or start another investor's processing without selection.

## Persistent jobs and existing data

Use detached workers with persisted job ID, investor, stage, selected candidate
IDs, timestamps, process identity, progress events, and outcome. Reopening the
dashboard reconnects to persisted jobs. A heartbeat/process-identity check
distinguishes active from interrupted jobs; a PID alone is not sufficient.
Use process-held locks to prevent duplicate writers for the same workspace and
concurrent managed GPU workers. Worker completion and failure release locks.
Pause requests take effect between items; explain this before confirmation.

Reconcile existing source plans, collection outcomes, artifact paths, processing
outcomes, and voice profiles without migrating or rewriting them just to display
the dashboard. Existing external CLI/batch jobs are shown as externally managed
when detectable; the UI must not silently take ownership or start overlapping work.
The currently running download batch must not be stopped by implementation/tests.

## Architecture alternatives

- Extend questionary prompts: smallest dependency change, but does not provide a
  persistent full-screen dashboard or live multi-investor navigation.
- Full-screen terminal frontend over the current pipeline and persisted workers:
  recommended; satisfies the requested interaction while retaining CLI/agent APIs.
- Browser UI: richer media playback, but requires another deployment surface and
  SSH/network setup; outside this request.

Separate presentation, read-only workspace projections, persisted job lifecycle,
and pipeline adapters. Do not place model or network logic inside UI callbacks.
Terminal widgets receive sanitized untrusted titles and errors; never render raw
escape sequences or expose tokens, cookies, or embedding vectors.

## Validation and delivery

1. Fixture tests for workspace counts, incomplete artifacts, stale jobs, and
   proposed next actions; discovery is never equated with collection.
2. UI tests for dashboard navigation, new investor/platform choices, filtered
   selection, bulk-review confirmation, cancellation, and narrow-screen layout.
3. Worker tests for duplicate launch prevention, resume/cache reuse, failures,
   bounded retry/backoff, disk/budget stops, and reconnect after UI exit.
4. Reference tests for ranking, 0–3 suggestions, missing media, timestamp formats,
   invalid/out-of-range intervals, explicit voice confirmation, and lineage.
5. Offline end-to-end flow with fake providers: new investor through discovery,
   review, download, reference, processing, and reopened progress display.
6. Existing CLI/wizard tests remain passing. No paid APIs, GPU jobs, or bulk
   downloads are launched by tests. Run a real interactive terminal smoke test.
7. Document the single entry command, shortcuts, stage boundaries, job behavior,
   review semantics, and dependency installation; update the changelog.

Delivery is complete only when the entire fixture workflow and reconnection pass,
not when the dashboard alone renders. Investment Memory generation stays out of
scope. Existing user edits and old repositories remain untouched.

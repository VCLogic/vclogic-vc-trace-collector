# Guided collector skill and terminal wizard

## Agreed scope

Add a lightweight terminal wizard and a repository-local skill usable by Claude
and Codex. Both drive the same existing collection backend. A user can start
with an investor name, confirm a supported identity, select sources, download
material, and process downloaded material without composing decision JSON.
Do not generate Investment Memory or extract portfolio investment facts.

## Interfaces and ownership

Introduce `vc-trace-collector wizard` with explicit `--stage discover`,
`--stage download`, and `--stage process` entry points. With no stage, show a
stage menu and existing workspaces. Accept optional `--name`, `--investor`, and
`--output-dir`. These are proposed new interfaces, not existing commands.

Keep current scripted CLI commands backward compatible. A small workflow
controller delegates to Pipeline services, with separate terminal presentation
and typed selection/state models. Do not place workflow logic into the already
large CLI module. Use a lightweight terminal-prompt library for keyboard-driven
menus and checkbox selection, retaining Rich for evidence and progress display.

Maintain one canonical skill under `.agents/skills/vc-trace-collector/` and a
Claude-discoverable entry under `.claude/skills/vc-trace-collector/`. Share detailed
stage references rather than maintaining divergent workflow instructions.
Document invocation and installation in the repository README. The skill checks
actual CLI capabilities and never invents flags.

## Discovery stage

1. Ask for a name and optional firm or known profile; inspect existing workspace
   identity before starting another run.
2. Search possible profiles using available configured backends. Present URLs,
   titles, affiliations and retrieved identity evidence. Search results are
   candidates, not verified people. Do not combine namesakes automatically.
3. Ask the user to confirm the intended profile/person or supply another URL.
   Empty or ambiguous results remain unresolved; show an actionable next step.
4. Present platform checkboxes, backend availability and search limits. Search
   only selected platforms and retain query provenance.
5. Review candidates in a searchable checkbox list with platform/status filters,
   confidence sorting in either direction, and a detail view containing evidence,
   URLs, material role and exclusion reasons. Support approve, reject and defer.
   Unselected items stay unchanged. Confirm a summary before saving decisions.

Discovery may retrieve profile/evidence pages needed for validation. It must not
download audiovisual media or invoke transcription, diarization or embeddings.
Do not ask for GPU/model settings during discovery. Corpus exclusions remain
visible and enforced; manual selection is not a leakage-rule bypass.

## Download stage

Load existing decisions and show approved items by platform, including prior
download status. The user selects platforms or individual items and confirms the
planned downloads and configured limits. Reuse existing resumable collection,
progress reporting and isolated error handling. Offer explicit retry of failed
items without silently refreshing successful downloads. Pending and rejected
items are not downloaded. No processing starts when downloads finish.

Portfolio remains a separate download-only dataset, outside corpus filters and
speaker processing. Its existing command and handoff files stay compatible;
this wizard does not introduce portfolio fact extraction.

## Process stage

Show downloaded items, processing status, dependencies and relevant configuration.
Written normalization does not require audiovisual dependencies or a voice sample.
For selected audiovisual items, check for an existing human-approved reference.
If missing, ask for a local sample or a downloaded recording plus start/end
timestamps, and explicit confirmation that only the target speaks in the sample.
Provide a local playback path; do not claim the agent listened or verified a voice.
If a reference still needs downloading, return a download-stage action rather than
quietly mixing the stages. Validate intervals against the actual media duration.

Run configured Whisper transcription, pyannote diarization and speaker matching
on downloaded media, not platform captions. Preserve independent caches and
display progress. Changing the voice reference must not rerun reusable
transcription/diarization. Uncertain attribution remains review-required.
Present export/verification as an explicit final action, not automatic publication.

## Agent interaction and persistence

The skill follows the same stages and asks only for missing decisions. In an
interactive user terminal it offers the wizard. When an agent tool cannot expose
interactive controls to the user, use conversational choices and existing
noninteractive commands; do not launch an inaccessible TUI or answer human
verification prompts on the user's behalf.

Persist confirmed choices through existing identity, source-plan and audit
services. Save minimal wizard preferences separately; backend records remain
authoritative. Cancellation before confirmation does not apply draft decisions.
Interrupted operations retain completed work. On resume reconcile state from
actual artifacts, not a wizard completion flag. Do not overwrite existing
workspace identity silently or print/store credentials. Network failures and
missing backends yield concise actionable errors with failure counts.

## Validation

- Unit-test workflow decisions with an injected prompt adapter and local fixtures.
- Verify discovery never invokes media download or AV providers; download never
  invokes processing; processing never silently downloads remote sources.
- Test namesakes, no results, deferred choices, exclusions, cancellation, existing
  workspaces, partial failures and resumable selections.
- Test missing/approved references, invalid intervals, attribution uncertainty,
  written-only processing and cache reuse without real GPUs or paid APIs.
- Test CLI compatibility, non-TTY behavior and keyboard-selection integration.
- Validate both skill entry points and shared references; exercise realistic
  name-only, ambiguous-profile and missing-reference scenarios with an independent
  agent using fixtures and no live network or provider charges.
- Run the existing test suite, lint and whitespace checks before reporting done.

## Delivery boundary

Ship the skill, terminal wizard, stage-controller code, tests, README and changelog
updates together. Preserve user data and local environment files. No live VC
collection, model run, or remote push is implied by implementation approval.

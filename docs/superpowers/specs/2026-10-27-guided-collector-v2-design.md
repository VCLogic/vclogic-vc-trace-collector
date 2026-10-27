# Guided collector v2

## Goal

Agents run most collection through the CLI. The wizard exists for the parts that
need a human: confirming identity, triaging sources and approving voice references.
Replace the stage-first wizard and the uncommitted dashboard with one linear flow
from name to verified export.

## Approach

A plain sequential script (`guided.py`) using questionary prompts and rich progress
bars, plus one full-screen prompt_toolkit triage table. No new dependencies, no job
runner, no stage menu. All durable work goes through existing `Pipeline` methods.

## Flow

`vc-trace-collector wizard` runs:

1. **Who.** Investor name; optional firm or known profile URL.
2. **Is this them?** Show resolved identity and evidence; Yes / No. No: offer to
   enter a profile URL and re-resolve, or quit. Yes: `pipeline.review(...,
   confirm_identity=True)`.
3. **Platforms.** Checkbox list of all `SourceType`s except `supplied`, all ticked
   by default. Limits (results per query, search-operation ceiling, cost ceiling)
   shown on one line; "Edit limits?" defaults to No.
4. **Discovery.** `pipeline.search_source` per platform. A bar per platform; when
   the total is unknown, a spinner with a running candidate count, never a
   fabricated percentage. A stop caused by backend or budget ends the step with the
   reported reason.
5. **Triage.** Full-screen table (below).
6. **Fetch.** `pipeline.fetch_source` on all approved candidates with
   `with_download_progress`. Failures listed with reasons at the end of the step.
7. **Voice reference.** Loop (below). Skipped when no approved, downloaded media.
8. **Process.** `pipeline.process` per source with `with_processing_progress`. A
   failing source is recorded and skipped; the run continues.
9. **Finish.** `pipeline.export` then `pipeline.verify`. Print counts, output paths
   and every failure from steps 6–8. Verification failure is reported as not
   complete.

### Resume

When a workspace for the name (or `--investor` slug) exists, the next step is
determined from saved state:

| Saved state | Resume at |
|---|---|
| identity not confirmed | 2 |
| confirmed, no candidates | 3 |
| candidates, none decided | 5 |
| approved but not downloaded | 6 |
| downloaded media, no verified reference profile | 7 |
| downloaded, not processed | 8 |
| processed | 9 |

The wizard asks "Continue from <step>?"; No offers a list of steps to jump to.
Each step persists on completion. Ctrl-C prints
`Saved. Run wizard again to continue from <step>.` and exits 130.

## Triage table (step 5)

Adapted from `inventory_ui`. Header: name, firm, counts (approved/rejected/pending),
platform toggles, search box. Rows sorted by identity confidence, highest first:
decision mark, confidence, platform, material role, title.

| Key | Action |
|---|---|
| `a` / `x` | approve / reject row |
| `r` | cycle material role |
| `A` | approve all pending rows with confidence ≥ 0.8 |
| `p` | toggle a platform; disabled platforms are hidden and saved as rejected |
| `u` | paste one or more URLs; added as approved candidates |
| `/` | search title/URL |
| `enter` | details: URL, snippet, identity evidence |
| `s` | save and continue |
| `q` | save and quit |

Decisions are held in memory and written in one `pipeline.review(...)` call on `s`
or `q`; reviewer defaults to `$USER`. Keystroke logic lives in a plain class
separate from the prompt_toolkit layout so it can be unit tested.

New pipeline method `add_sources(slug, urls) -> list[SourceCandidate]` reuses the
`source_urls` handling from `discover` to append candidates to an existing plan.
Duplicate URLs (after canonicalization) are ignored.

## Voice reference loop (step 7)

Check `missing_av_dependencies()` first. If anything is missing, print the install
command and offer "Process written sources only" or quit.

Menu: top 3 from `reference_suggestions`, "Browse all downloaded recordings",
"Local file / URL" (local files go through `stage_local_reference` and are fetched
before continuing). After a recording is chosen:

- **Path A (default).** Run `PyannoteDiarizationProvider` on its audio. Group turns
  by speaker and list each speaker's total speaking time. Choosing a number plays an
  8-second sample from that speaker's longest turn with
  `ffplay -nodisp -autoexit` (or prints the extracted clip path when `ffplay` is
  absent). The user picks the investor; their longest turn, capped at 30 s, is
  passed to `pipeline.approve_reference_voice(candidate_id, start, end)`.
- **Path B (fallback).** "None of these / wrong split": type a range like
  `03:10-03:40`, listen to it, confirm, then the same `approve_reference_voice` call.

Then "Add another reference? (N approved)". Each approval is a separate verified
profile; processing matches against all compatible profiles. The loop exits when
the user declines; it requires at least one approval to process media, otherwise
media sources are skipped in step 8 and reported.

## Removed

- `wizard.py` stage flow and the `--stage` option.
- Uncommitted `dashboard.py`, `dashboard_jobs.py`, `dashboard_state.py`,
  `dashboard_ui.py`, `inventory.py`, `inventory_ui.py` and their tests.
  `reference_suggestions` moves into the new module.
- `wizard_models`, `wizard_profiles`, `wizard_reference`: keep only what
  `guided.py` uses.

Granular CLI commands (`discover`, `review`, `search-source`, `collect`, etc.) are
unchanged.

## Errors

- One source failing: record and continue; listed in the final summary.
- Step blocked (identity unconfirmed, budget exhausted, missing dependencies): stop
  with one sentence naming the fix.
- Errors are passed through `redact` before display.

## Testing

- Full run, resume at each step and a failing source, using the fake `Prompts` and
  a stub pipeline.
- Triage state class: approve/reject, bulk approve threshold, platform toggle,
  added URLs.
- Speaker grouping and longest-turn selection from fake `DiarizedTurn`s; time-range
  parsing.
- `add_sources`: appends, canonicalizes and deduplicates.

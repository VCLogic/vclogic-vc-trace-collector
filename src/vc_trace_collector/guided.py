"""Linear guided collection: one run from name to verified export."""

import math
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from .audit import redact
from .config import RunConfig
from .discovery import slugify
from .models import ApprovalStatus, MaterialRole, SourceDecision, SourceType
from .progress import (
    quiet_torchaudio_notices,
    with_download_progress,
    with_processing_progress,
)
from .public_search import agent_reach_public_search_provider
from .source_search import _SUFFIXES
from .storage import read_json, read_jsonl, write_json
from .triage import APPROVED, TriageState, run_triage
from .voice import (
    can_play,
    clock,
    missing_av_dependencies,
    parse_range,
    play,
    reference_suggestions,
    stage_local_reference,
    timestamp_url,
    voice_candidate,
)
from .wizard_models import Option, backend_options, options, source_items

STEPS = ("identity", "platforms", "triage", "fetch", "voice", "process", "finish")
# Only types with search queries; X and LinkedIn exports are added by URL or file.
PLATFORMS = [t for t in SourceType if t in _SUFFIXES]


class Guided:
    def __init__(self, pipeline, ui, *, triage=run_triage, dependency_check=None):
        self.pipeline = pipeline
        self.ui = ui
        self.triage_view = triage
        self.dependency_check = dependency_check or missing_av_dependencies
        self.reviewer = os.environ.get("USER") or "human"
        self.failures: list[str] = []
        self.step = STEPS[0]

    # --- run / resume -------------------------------------------------

    def run(self, *, name=None, investor=None):
        if investor and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", investor):
            raise ValueError("Invalid investor slug")
        slug = investor or self.start(name)
        if slug is None:
            return
        done = self.completed(slug)
        remaining = [s for s in STEPS if s not in done]
        if remaining:
            jump = done and not self.ui.confirm(
                "resume", f"Continue from {remaining[0]}?"
            )
        else:
            # A finished collection can redo a step, e.g. retry failed downloads.
            jump = self.ui.confirm("redo", "Collection is finished. Redo from a step?")
        if jump:
            first = self.ui.select("jump", "Start from", options(STEPS))
            remaining = list(STEPS[STEPS.index(first) :])
        for step in remaining:
            self.step = step
            if getattr(self, step)(slug) is False:
                return
            self.mark(slug, step)

    def start(self, name):
        name = (name or self.ui.text("name", "Investor name")).strip()
        if not name:
            raise ValueError("Investor name is required")
        slug = slugify(name)
        if (self.pipeline.workspace(slug) / "identity/resolved_identity.json").exists():
            return slug
        from .wizard_profiles import preserve_firm_hint, resolve_profile

        def discover(url, config):
            return self.pipeline.discover(
                name=name, known_profile_url=url, config=config
            ).identity.slug

        profile = resolve_profile(self.pipeline, self.ui, name, validate=discover)
        if profile is None:
            return None
        url, config, slug = profile
        preserve_firm_hint(self.pipeline, slug, config.firm, url)
        return slug

    def state_path(self, slug):
        return self.pipeline.workspace(slug) / "guided_state.json"

    def completed(self, slug):
        path = self.state_path(slug)
        if path.exists():
            return read_json(path)["completed"]
        identity = self.pipeline._load_identity(slug)
        return ["identity"] if identity.resolution_status == "confirmed" else []

    def mark(self, slug, step):
        done = self.completed(slug)
        if step not in done:
            write_json(self.state_path(slug), {"completed": [*done, step]})

    def config(self, slug):
        return RunConfig.model_validate(
            read_json(self.pipeline.workspace(slug) / "config_snapshot.json")
        )

    def models(self, slug):
        config = self.config(slug)
        return {
            "transcription_model": config.transcription_model or "turbo",
            "diarization_model": config.diarization_model
            or "pyannote/speaker-diarization-3.1",
        }

    def items(self, slug, *, downloaded=None, media=None):
        return [
            i
            for i in source_items(self.pipeline, slug)
            if i.candidate.approval_status in APPROVED
            and (downloaded is None or i.downloaded == downloaded)
            and (media is None or i.media == media)
        ]

    def references(self, slug):
        workspace = self.pipeline.workspace(slug)
        return sum(
            p.status == "verified_human"
            for p in self.pipeline._load_reference_profiles(workspace)
        )

    # --- steps --------------------------------------------------------

    def identity(self, slug):
        identity = self.pipeline._load_identity(slug)
        self.ui.show(identity.model_dump(mode="json"))
        if identity.resolution_status == "confirmed":
            return True
        if not self.ui.confirm(
            "confirm_identity", f"Is this the right {identity.canonical_name}?"
        ):
            self.ui.show("Stopped. Run wizard again with the correct profile URL.")
            return False
        self.pipeline.review(
            slug, decisions=[], reviewer=self.reviewer, confirm_identity=True
        )
        return True

    def platforms(self, slug):
        skip = set(
            self.ui.check(
                "skip",
                "Platforms to skip (nothing ticked = search all)",
                options(p.value for p in PLATFORMS),
            )
        )
        chosen = [p for p in PLATFORMS if p.value not in skip]
        if not chosen:
            return True
        config = self.config(slug)
        backend = "agent-reach" if shutil.which("mcporter") else "default"
        limit, ceiling = 10, max(config.maximum_search_operations, 20)
        self.ui.show(
            f"{len(chosen)} platforms · {limit} results/query · at most {ceiling} "
            f"searches · cost ceiling ${config.maximum_cost_usd} · backend {backend}"
        )
        if self.ui.confirm("edit_limits", "Edit limits?"):
            backend = self.ui.select("backend", "Search backend", backend_options())
            limit = self.integer("results", "Results per query (1–100)", limit, 1, 100)
            ceiling = self.integer(
                "searches", "Search-operation ceiling", ceiling, ceiling, 10000
            )
        provider = (
            agent_reach_public_search_provider() if backend == "agent-reach" else None
        )
        found = 0
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn(
                "{task.completed}/{task.total} platforms · {task.fields[found]} found"
            ),
            console=Console(stderr=True),
        ) as bar:
            task = bar.add_task("Searching", total=len(chosen), found=0)
            for platform in chosen:
                bar.update(task, description=f"Searching {platform.value}")
                result = self.pipeline.search_source(
                    slug,
                    platform,
                    search_provider=provider,
                    limit_per_query=limit,
                    maximum_search_operations=ceiling,
                )
                found += result.added
                bar.update(task, advance=1, found=found)
                self.failures += [
                    f"search {platform.value}: {e}" for e in result.errors
                ]
                if result.stopped_early:
                    reason = "; ".join(result.errors) or "budget reached"
                    self.ui.show(f"Search stopped at {platform.value}: {reason}")
                    break
        return True

    def integer(self, key, message, default, minimum, maximum):
        while True:
            try:
                value = int(self.ui.text(key, message, str(default)))
                if minimum <= value <= maximum:
                    return value
            except ValueError:
                pass
            self.ui.show(f"Enter a whole number between {minimum} and {maximum}.")

    def triage(self, slug):
        name = self.pipeline._load_identity(slug).canonical_name
        state = TriageState(self.pipeline._load_plan(slug).candidates)
        while True:
            action = self.triage_view(state, name)
            if action == "search":
                state.query = self.ui.text(
                    "search", "Search title/URL (blank = all)", state.query
                )
            elif action == "add":
                urls = self.ui.text("urls", "URLs to add (space-separated)").split()
                try:
                    added = self.pipeline.add_sources(
                        slug, urls, reviewer=self.reviewer
                    )
                    state.add(added)
                except ValueError as error:
                    self.ui.show(str(error))
            elif action == "platform":
                present = sorted({c.source_type.value for c in state.rows})
                state.toggle_platform(
                    self.ui.select("platform", "Skip/unskip platform", options(present))
                )
            elif action == "details" and (c := state.current()):
                self.ui.show(
                    next(
                        i.model_dump(mode="json")
                        for i in source_items(self.pipeline, slug)
                        if i.candidate.candidate_id == c.candidate_id
                    )
                )
            elif action in {"save", "quit"}:
                decisions = state.decisions(self.reviewer)
                if decisions:
                    self.pipeline.review(
                        slug,
                        decisions=decisions,
                        reviewer=self.reviewer,
                        confirm_identity=True,
                    )
                self.ui.show(f"Saved {len(decisions)} decisions.")
                return action == "save"

    @with_download_progress
    def _fetch(self, slug, ids, maximum_media_minutes=None):
        return self.pipeline.fetch_source(
            slug, candidate_ids=ids, maximum_media_minutes=maximum_media_minutes
        )

    def fetch(self, slug):
        pending = self.items(slug, downloaded=False)
        if not pending:
            self.ui.show("Nothing new to download.")
            return True
        maximum = None
        recordings = [
            i for i in pending if i.candidate.source_type in {"youtube", "podcast"}
        ]
        if recordings:
            limit = self.config(slug).maximum_media_minutes
            estimate = sum(i.candidate.estimated_media_seconds for i in recordings)
            self.ui.show(
                f"Audio/video limit: {self.pipeline.media_minutes_collected(slug):.0f}"
                f" of {limit:.0f} minutes used; {len(recordings)} recordings to "
                "download"
                + (f" (about {estimate / 60:.0f} min)" if estimate else "")
                + ". Recordings past the limit are not downloaded."
            )
            if self.ui.confirm("edit_media_limit", "Raise the audio/video limit?"):
                maximum = self.integer(
                    "media_minutes",
                    "Audio/video limit in minutes",
                    math.ceil(limit),
                    math.ceil(limit),
                    100_000,
                )
        result = self._fetch(slug, {i.candidate.candidate_id for i in pending}, maximum)
        self.failures += [f"fetch {f}" for f in result.failures]
        self.ui.show(
            f"Downloaded {result.collected} · failed {result.failed} · "
            f"skipped {result.skipped}"
        )
        audit = self.pipeline.workspace(slug).resolve() / "audit"
        self.ui.show(
            f"Logs: {audit / 'failures.jsonl'} (why each download failed)\n"
            f"      {audit / 'events.jsonl'} (every collection event)"
        )
        return True

    @quiet_torchaudio_notices()
    def voice(self, slug):
        if not self.items(slug, downloaded=True, media=True):
            return True
        missing = self.dependency_check()
        if missing:
            self.ui.show(
                f"Missing audio/video dependencies: {', '.join(missing)}. Install with "
                "uv sync --extra youtube --extra av --extra av-local, plus FFmpeg; set "
                "HF_TOKEN in .env. Audio/video sources will be skipped."
            )
            return True
        name = self.pipeline._load_identity(slug).canonical_name
        while True:
            count = self.references(slug)
            question = (
                f"Add another reference? ({count} approved)"
                if count
                else "Add a voice reference now? (needed to process audio/video)"
            )
            if not self.ui.confirm("add_reference", question):
                return True
            item = self.pick_recording(slug, name)
            if item is not None:
                self.reference_from(slug, item, name)

    def pick_recording(self, slug, name):
        media = self.items(slug, downloaded=True, media=True)
        choice = self.ui.select(
            "reference",
            "Recording with the investor's voice",
            [
                *(
                    Option(
                        value=i.candidate.candidate_id,
                        label=f"{i.candidate.title or i.candidate.canonical_url} · "
                        f"identity {i.candidate.identity_confidence.score:.2f}",
                    )
                    for i in reference_suggestions(media, name)
                ),
                Option(value="browse", label="Browse all downloaded recordings"),
                Option(value="local", label="Local file"),
            ],
        )
        if choice == "browse":
            choice = self.ui.select(
                "reference_all",
                "All downloaded recordings",
                [Option(value=i.candidate.candidate_id, label=i.label) for i in media],
            )
        if choice == "local":
            path = Path(self.ui.text("reference_path", "Path to local audio/video"))
            choice = stage_local_reference(self.pipeline, slug, path, self.reviewer)
            self._fetch(slug, {choice})
        return next(
            (
                i
                for i in source_items(self.pipeline, slug)
                if i.candidate.candidate_id == choice and i.downloaded
            ),
            None,
        )

    def reference_from(self, slug, item, name):
        voice_id, media = voice_candidate(
            self.pipeline, slug, item, reviewer=self.reviewer
        )
        url = item.candidate.url
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            interval = self.typed_range(media, Path(tmp), name, url)
        if interval is None:
            return
        start, end = interval
        duration = self.pipeline.media_probe(media)
        if not duration or end > duration:
            self.ui.show("That range is past the end of the recording.")
            return
        try:
            # Cuts the clip from the downloaded media and stores its voice embedding.
            self.pipeline.approve_reference_voice(
                slug,
                candidate_id=voice_id,
                reviewer=self.reviewer,
                start_seconds=start,
                end_seconds=end,
                diarization_model=self.models(slug)["diarization_model"],
                embedding_model="",
            )
        except Exception as error:
            self.ui.show(f"Reference not saved: {redact(str(error))}")
            return
        self.ui.show(f"Reference saved: {clock(start)}–{clock(end)} of {url}")

    def typed_range(self, media, tmp, name, url):
        """Show the recording's link; the user types where only the investor speaks."""
        self.ui.show(f"Open {url} and find a stretch where only {name} speaks.")
        while True:
            text = self.ui.text(
                "range",
                f"Time range where only {name} speaks "
                "(e.g. 0:00-0:50, blank = skip this recording)",
            )
            if not text:
                return None
            try:
                start, end = parse_range(text)
            except ValueError as error:
                self.ui.show(str(error))
                continue
            # A YouTube source is checked in the browser, not replayed here.
            if can_play() and timestamp_url(url, 0) == url:
                play(media, start, end, tmp)
            return start, end

    @with_processing_progress
    def _process(self, slug, cid, models):
        return self.pipeline.process(slug, candidate_ids={cid}, **models)

    def process(self, slug):
        has_reference = self.references(slug) > 0
        models = self.models(slug)
        for item in self.items(slug, downloaded=True):
            url = item.candidate.canonical_url
            if item.candidate.material_role == MaterialRole.REFERENCE_VOICE:
                continue
            if item.media and not has_reference:
                self.failures.append(f"skipped {url}: no voice reference")
                continue
            try:
                docs = self._process(
                    slug, item.candidate.candidate_id, models if item.media else {}
                )
                counts = Counter(
                    str(d.inclusion_status)
                    for d in docs
                    if d.source_candidate_id == item.candidate.candidate_id
                )
                self.ui.show(f"{item.candidate.title or url}: {dict(counts)}")
            except Exception as error:
                self.failures.append(f"process {url}: {redact(str(error))}")
                self.ui.show(f"Failed {url}: {redact(str(error))}")
        return True

    def talks_without_result(self, slug):
        """Approved spoken sources with no audio/video outcome, which block verify."""
        workspace = self.pipeline.workspace(slug)
        done = {
            row["candidate_id"]
            for row in read_jsonl(workspace / "processed/av_candidate_outcomes.jsonl")
        }
        return [
            c
            for c in self.pipeline._load_plan(slug).candidates
            if c.approval_status in APPROVED
            and c.material_role == MaterialRole.SPOKEN_BY_TARGET
            and c.candidate_id not in done
        ]

    def finish(self, slug):
        self.pipeline.export(slug)
        result = self.pipeline.verify(slug)
        workspace = self.pipeline.workspace(slug)
        failed = read_json(workspace / "run_summary.json")["failures"]
        if not result.passed and failed and not self.config(slug).allow_partial_run:
            self.ui.show(
                f"{failed} sources failed "
                f"(see {workspace.resolve() / 'audit' / 'failures.jsonl'}). "
                "A partial collection leaves them out; the decision is recorded "
                "in the audit log."
            )
            if self.ui.confirm("partial_run", "Accept a partial collection?"):
                self.pipeline.accept_partial_run(slug)
                self.pipeline.export(slug)
                result = self.pipeline.verify(slug)
        missing = [] if result.passed else self.talks_without_result(slug)
        if missing:
            self.ui.show(
                f"⚠ Warning: {len(missing)} approved talks have no audio/video result "
                "(media limit reached or download failed):"
            )
            for candidate in missing:
                self.ui.show(f"  - {candidate.title or candidate.url}  {candidate.url}")
            self.ui.show(
                "Leaving them out rejects them in the source plan and records the "
                "decision in the audit log. They will not be in the corpus or the "
                "investment memory."
            )
            if self.ui.confirm(
                "leave_out_talks", f"Leave these {len(missing)} talks out?"
            ):
                self.pipeline.review(
                    slug,
                    decisions=[
                        SourceDecision(
                            candidate_id=candidate.candidate_id,
                            status=ApprovalStatus.REJECTED,
                            reason="No audio/video result; left out at finish",
                            decided_by=self.reviewer,
                        )
                        for candidate in missing
                    ],
                    reviewer=self.reviewer,
                    confirm_identity=True,
                )
                self.pipeline.export(slug)
                result = self.pipeline.verify(slug)
        items = source_items(self.pipeline, slug)
        self.ui.show(
            f"{'Verified' if result.passed else 'NOT verified'} · "
            f"approved {sum(i.candidate.approval_status in APPROVED for i in items)} · "
            f"downloaded {sum(i.downloaded for i in items)} · "
            f"processed {sum(i.processing_status != 'not processed' for i in items)}\n"
            f"Output: {self.pipeline.workspace(slug)}"
        )
        if not result.passed:
            self.ui.show(result.model_dump(mode="json"))
        if any("Audiovisual outcomes" in error for error in result.errors):
            self.ui.show(
                "Some approved talks have no audio/video result. Leave them out at "
                "finish, reject them in triage, or raise the audio/video limit in fetch."
            )
        for failure in self.failures:
            self.ui.show(f"  ✗ {failure}")
        return True

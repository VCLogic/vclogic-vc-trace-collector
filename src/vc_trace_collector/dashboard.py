"""Investor-first controller; all collection/model work uses existing pipeline APIs."""

import math
import os
import re
from pathlib import Path

from .audit import redact
from .collectors import is_acquired_media
from .config import RunConfig
from .dashboard_jobs import ACTIVE, launch_job, list_jobs, workspace_guard
from .dashboard_state import (
    APPROVED,
    UNFINISHED,
    parse_timestamp,
    reference_suggestions,
    summarize,
    workspaces,
)
from .discovery import slugify
from .models import ApprovalStatus, ReferenceVoiceCandidate, SourceDecision
from .process import load_artifact_records
from .storage import read_json, read_jsonl, write_jsonl
from .terminal import Cancelled
from .wizard import Wizard
from .wizard_models import Option, backend_options, source_items
from .wizard_profiles import preserve_firm_hint, resolve_profile
from .wizard_reference import missing_av_dependencies, stage_local_reference

PLATFORMS = {
    "youtube": "YouTube · video/audio appearances",
    "podcast": "Podcasts · episodes and public audio",
    "web_article": "Articles and personal blogs",
    "web_profile": "Profiles and firm websites",
    "rss_feed": "RSS / Atom feeds",
    "substack": "Substack",
    "medium": "Medium",
    "x": "X · authorized backend or export required",
    "linkedin_export": "LinkedIn · public discovery / supplied exports",
}


def choices(pairs):
    return [Option(value=value, label=label) for value, label in pairs]


class Dashboard:
    def __init__(self, pipeline, ui):
        self.pipeline, self.ui = pipeline, ui

    def run(self, *, investor=None, name=None):
        ready_only = False
        if investor and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", investor):
            raise ValueError("Invalid investor slug")
        if name:
            self.discover(name=name)
        if investor:
            self.investor(investor)
        while True:
            try:
                self.ui.breadcrumb = "Investors"
                rows = self.ui.busy(
                    "Loading saved investor progress", lambda: workspaces(self.pipeline)
                )
                jobs = list_jobs(self.pipeline.output_dir)
                active = {j["slug"]: j for j in jobs if j["status"] in ACTIVE}
                rows.sort(
                    key=lambda r: (
                        r["slug"] not in active,
                        r.get("next") == "results",
                        r["name"],
                    )
                )
                options = choices(
                    [
                        ("new", "+ New investor"),
                        ("jobs", "Jobs · live progress / reconnect"),
                        ("refresh", "Refresh saved progress"),
                        (
                            "ready_filter",
                            "Show all investors"
                            if ready_only
                            else "Show VCs with downloaded recordings ready to process",
                        ),
                    ]
                )
                for row in rows:
                    if ready_only and not row.get("ready_to_process", 0):
                        continue
                    slug = row["slug"]
                    if "error" in row:
                        label = f"! {slug} · unreadable workspace ({row['error']})"
                    else:
                        status = (
                            f"RUNNING {active[slug]['stage']}"
                            if slug in active
                            else f"{row['ready_to_process']} recordings ready to process"
                        )
                        label = (
                            f"{row['name']} · {status}\n"
                            f"    {row['firm'] or 'Firm not resolved'}\n"
                            f"    Found {row['discovered']} | Approved {row['approved']} | Downloaded {row['downloaded']}\n"
                            f"    Awaiting processing review {row['review_required']} | Download failures {row['download_failed']} | Processing failures {row['processing_failed']}"
                        )
                    options.append(Option(value=slug, label=label))
                options.append(
                    Option(value="quit", label="Quit · background jobs keep running")
                )
                action = self.ui.select("dashboard", "Your investors", options)
                if action == "quit":
                    return
                if action == "new":
                    self.discover()
                elif action == "ready_filter":
                    ready_only = not ready_only
                elif action == "jobs":
                    self.jobs()
                elif action != "refresh":
                    self.investor(action)
            except Cancelled:
                return
            except Exception as error:
                self.ui.show(f"Could not complete action: {redact(str(error))}")

    def investor(self, slug):
        from .inventory import inventory
        from .inventory_ui import InventoryBrowser

        while True:
            try:
                self.ui.breadcrumb = f"Investors / {slug}"
                row = summarize(self.pipeline, slug)
                items = self.ui.busy(
                    "Loading source inventory", lambda: inventory(self.pipeline, slug)
                )
                active_jobs = [
                    j
                    for j in list_jobs(self.pipeline.output_dir)
                    if j["slug"] == slug and j["status"] in ACTIVE
                ]
                job_status = " · ".join(
                    f"RUNNING: {j['stage']} — press j for progress" for j in active_jobs
                )
                browser = InventoryBrowser(
                    self.ui, items, row["name"], job_status=job_status
                )
                while True:
                    selection = browser.run()
                    if selection is None:
                        return
                    action, cid = selection
                    if action == "help":
                        self.ui.show(
                            "Arrow keys select a source; Enter opens its URL, files, decisions and errors.\n/ searches; Enter returns from search to the table. f cycles status filters. Ctrl-R reloads saved data.\n\np selects downloaded, approved recordings needing transcription/diarization or retry. It does not redownload or restart completed recordings. A confirmation is required.\nd downloads; r reviews sources; v sets a voice reference; j opens progress; a shows other actions.\n\nTable: Tx = transcription, Dia = diarization. done = saved output, wait = pending, ? = unknown stage.\nVoice matched = model accepted; operator = human attribution decision; uncertain = unresolved. Source approval is separate from voice verification.\n\nEscape returns to investors. Failed downloads do not prevent processing recordings already downloaded."
                        )
                        continue
                    if action != "details":
                        break
                    self.ui.show(
                        next(i for i in items if i.candidate_id == cid).details()
                    )
                if action == "refresh":
                    continue
                if action == "actions":
                    action = self.ui.select(
                        "investor_action",
                        f"{row['name']} · Actions",
                        choices(
                            [
                                (
                                    "discover",
                                    f"Discover more · {row['discovered']} sources found",
                                ),
                                (
                                    "review",
                                    f"Review sources · {row['pending']} pending",
                                ),
                                (
                                    "download",
                                    f"Download / retry · {row['missing']} approved missing",
                                ),
                                (
                                    "reference",
                                    "Voice signature · "
                                    + (
                                        "reference saved"
                                        if row["reference"]
                                        else "choose a recording"
                                    ),
                                ),
                                (
                                    "process",
                                    f"Process downloaded material · {row['processed']} already processed",
                                ),
                                ("jobs", "Live jobs and saved progress"),
                                (
                                    "results",
                                    f"Results · {row['review_required']} need review",
                                ),
                                ("back", "Back to source table"),
                            ]
                        ),
                    )
                if action == "back":
                    continue
                if action == "jobs":
                    self.jobs(slug)
                    continue
                if self.is_busy(slug):
                    self.ui.show(
                        "This investor has an active job. Open Live jobs, or wait before making changes."
                    )
                    continue
                if action == "discover":
                    self.discover(slug=slug)
                elif action == "process_media":
                    self.process(slug, media_only=True)
                elif action == "identity":
                    self.identity(slug)
                elif action in {
                    "review",
                    "download",
                    "reference",
                    "process",
                    "results",
                }:
                    getattr(self, action)(slug)
            except Cancelled:
                return
            except Exception as error:
                self.ui.show(f"Action stopped safely: {redact(str(error))}")

    def is_busy(self, slug):
        return any(
            j["slug"] == slug and j["status"] in ACTIVE
            for j in list_jobs(self.pipeline.output_dir)
        )

    def config(self, slug):
        return RunConfig.model_validate(
            read_json(self.pipeline.workspace(slug) / "config_snapshot.json")
        )

    def identity(self, slug):
        identity = self.pipeline._load_identity(slug)
        if identity.resolution_status == "confirmed":
            return True
        Wizard(self.pipeline, self.ui).show_identity(slug)
        if not self.ui.confirm(
            "confirm_identity",
            "Confirm that these profiles and affiliations describe the intended investor?",
        ):
            return False
        reviewer = self.ui.text("reviewer", "Reviewer name", "human")
        with workspace_guard(self.pipeline.output_dir, slug):
            self.pipeline.review(
                slug, decisions=[], reviewer=reviewer, confirm_identity=True
            )
        return True

    def discover(self, *, slug=None, name=None):
        platforms = self.ui.check(
            "platforms", "Where should we search?", choices(PLATFORMS.items())
        )
        if not platforms:
            return
        if slug is None:
            name = name or self.ui.text("name", "Investor name")
            if not name.strip():
                raise ValueError("An investor name is required")
            slug = slugify(name)
            if not (
                self.pipeline.workspace(slug) / "identity/resolved_identity.json"
            ).exists():
                profile = resolve_profile(self.pipeline, self.ui, name)
                if profile is None:
                    return
                url, config = profile
                with workspace_guard(self.pipeline.output_dir, slug):
                    result = self.ui.busy(
                        "Resolving identity",
                        lambda: self.pipeline.discover(
                            name=name, known_profile_url=url, config=config
                        ),
                    )
                    slug = result.identity.slug
                    preserve_firm_hint(self.pipeline, slug, config.firm, url)
        if not self.identity(slug):
            return
        config = self.config(slug)
        backend = self.ui.select("backend", "Search provider", backend_options())
        limit = 10
        if self.ui.confirm(
            "advanced_search",
            "Change the default 10 results per query? Saved search and money limits remain enforced.",
        ):
            limit = Wizard(self.pipeline, self.ui).integer(
                "results", "Results per query", 10, 1, 100
            )
        if self.ui.confirm(
            "confirm_search",
            f"Search {', '.join(platforms)} for {slug}?\nSearch ceiling: {config.maximum_search_operations}; cumulative budget: ${config.maximum_cost_usd}.\nNext: review the discovered sources. No media downloads yet.",
        ):
            self.launch(
                slug,
                "search",
                [],
                platforms=platforms,
                backend=backend,
                limit_per_query=limit,
                maximum_search_operations=config.maximum_search_operations,
            )

    def review(self, slug):
        mode = self.ui.select(
            "review_mode",
            "Review source identity and relevance",
            choices(
                [
                    ("inspect", "Inspect / filter / select sources"),
                    ("all", "Accept all pending · keep previous rejections"),
                    ("back", "Back"),
                ]
            ),
        )
        if mode == "inspect":
            with workspace_guard(self.pipeline.output_dir, slug):
                Wizard(self.pipeline, self.ui).review(slug)
        elif mode == "all":
            items = [
                i
                for i in source_items(self.pipeline, slug)
                if i.candidate.approval_status == "pending"
            ]
            if not items:
                self.ui.show("No pending sources.")
                return
            low = sum(i.candidate.identity_confidence.score < 0.8 for i in items)
            Wizard(self.pipeline, self.ui).show_identity(slug)
            if not self.ui.confirm(
                "confirm_bulk",
                f"Confirm the displayed investor identity and approve collection of all {len(items)} pending sources for {slug}?\n{low} have identity confidence below 0.8. Namesakes may be present.\nApproval is NOT verified content/speech. Roles, prior rejections and exclusions stay unchanged.",
            ):
                return
            reviewer = self.ui.text("reviewer", "Reviewer name", "human")
            with workspace_guard(self.pipeline.output_dir, slug):
                self.pipeline.review(
                    slug,
                    decisions=[
                        SourceDecision(
                            candidate_id=i.candidate.candidate_id,
                            status=ApprovalStatus.APPROVED,
                            reason="Human approved all pending sources for later content review",
                            decided_by=reviewer,
                        )
                        for i in items
                    ],
                    reviewer=reviewer,
                    confirm_identity=True,
                )
            self.ui.show(
                f"Saved {len(items)} approvals. Next: Download approved missing sources."
            )

    def selection(self, items, stage):
        if not items:
            self.ui.show(
                f"No eligible items for {stage}. Check review and download status."
            )
            return []
        mode = self.ui.select(
            "selection",
            f"{stage.title()} · {len(items)} eligible sources",
            choices(
                [
                    ("all", "All eligible sources"),
                    ("choose", "Choose platforms or individual sources"),
                    ("back", "Back"),
                ]
            ),
        )
        if mode == "back":
            return []
        if mode == "all":
            return items
        types = sorted({i.candidate.source_type.value for i in items})
        platforms = self.ui.check(
            "platforms",
            "Platforms to include",
            choices((t, PLATFORMS.get(t, t)) for t in types),
        )
        filtered = [i for i in items if i.candidate.source_type in platforms]
        ids = set(self.ui.pick_sources(filtered, stage))
        return [i for i in filtered if i.candidate.candidate_id in ids]

    def download(self, slug):
        items = self.selection(
            [
                i
                for i in source_items(self.pipeline, slug)
                if i.candidate.approval_status in APPROVED
                and not i.downloaded
                and i.collection_status != "excluded"
            ],
            "download",
        )
        if not items or not self.identity(slug):
            return
        config = self.config(slug)
        if self.ui.confirm(
            "confirm_job",
            f"Download {len(items)} approved missing sources?\nSaved byte limit: {config.maximum_download_bytes}; media minutes: {config.maximum_media_minutes}; budget: ${config.maximum_cost_usd}.\nSuccessful files are reused. Processing will not start automatically.",
        ):
            self.launch(slug, "download", [i.candidate.candidate_id for i in items])

    def models(self, slug):
        config = self.config(slug)
        models = {
            "transcription_model": config.transcription_model or "turbo",
            "diarization_model": config.diarization_model
            or "pyannote/speaker-diarization-3.1",
        }
        if self.ui.confirm(
            "advanced_models",
            f"Models: Whisper {models['transcription_model']} / {models['diarization_model']}\nChange models? (Not now keeps these defaults.)",
        ):
            models = {
                k: self.ui.text(k, k.replace("_", " ").title(), v)
                for k, v in models.items()
            }
        return models

    def reference(self, slug):
        missing = missing_av_dependencies()
        if missing:
            self.ui.show(
                "Missing audiovisual dependencies: "
                + ", ".join(missing)
                + "\nInstall with uv sync --extra youtube --extra av --extra av-local, plus FFmpeg. Configure HF_TOKEN in .env."
            )
            return
        items = source_items(self.pipeline, slug)
        identity = self.pipeline._load_identity(slug)
        name = summarize(self.pipeline, slug)["name"]
        suggested = reference_suggestions(items, name)
        records = load_artifact_records(self.pipeline.workspace(slug))
        previews = []
        for item in suggested:
            media = next(
                (
                    a
                    for a in records
                    if a.original_metadata.get("candidate_id")
                    == item.candidate.candidate_id
                    and is_acquired_media(a)
                    and str((self.pipeline.workspace(slug) / a.relative_path).resolve())
                    in item.paths
                ),
                None,
            )
            if media:
                duration = media.original_metadata.get("duration_seconds")
                previews.append(
                    f"{item.candidate.title}\n{item.candidate.canonical_url}\nLocal: {(self.pipeline.workspace(slug) / media.relative_path).resolve()}\nDuration: {duration if duration else 'shown after selection'} seconds\nSuggested from approved acquired media, title/name match and identity confidence {item.candidate.identity_confidence.score:.2f}; voice not verified."
                )
        if previews:
            self.ui.show("\n\n".join(previews))
        options = [
            Option(
                value=i.candidate.candidate_id,
                label=f"{i.candidate.title or i.candidate.canonical_url}\n    {i.candidate.canonical_url}\n    identity {i.candidate.identity_confidence.score:.2f} · metadata-ranked, voice not verified",
            )
            for i in suggested
        ]
        options.append(
            Option(value="browse", label="Browse all downloaded approved recordings")
        )
        options.append(
            Option(value="local", label="Supply a local public voice recording")
        )
        cid = self.ui.select(
            "reference", "Choose a voice reference · top suggestions", options
        )
        if cid == "local":
            path = Path(
                self.ui.text("reference_path", "Path to local public audio/video")
            )
            if not self.identity(slug) or not self.ui.confirm(
                "confirm_local",
                "Stage this local file as a reference source? This does not verify the voice or run models.",
            ):
                return
            reviewer = self.ui.text("reviewer", "Reviewer name", "human")
            with workspace_guard(self.pipeline.output_dir, slug):
                candidate_id = stage_local_reference(
                    self.pipeline, slug, path, reviewer
                )
            if self.ui.confirm(
                "confirm_local_download",
                "Copy this approved local source into the collection now? Then return to Voice signature to listen and choose timestamps.",
            ):
                self.launch(slug, "download", [candidate_id])
            return
        if cid == "browse":
            available = [
                i
                for i in items
                if i.downloaded and i.media and i.candidate.approval_status in APPROVED
            ]
            if not available:
                self.ui.show(
                    "No downloaded recordings. Download an appearance, or choose Supply a local public voice recording."
                )
                return
            cid = self.ui.select(
                "reference_all",
                "All downloaded recordings",
                [
                    Option(value=i.candidate.candidate_id, label=i.label)
                    for i in available
                ],
            )
        item = next(i for i in items if i.candidate.candidate_id == cid)
        workspace = self.pipeline.workspace(slug)
        artifact = next(
            a
            for a in load_artifact_records(workspace)
            if a.original_metadata.get("candidate_id") == cid
            and is_acquired_media(a)
            and str((workspace / a.relative_path).resolve()) in item.paths
        )
        path = (workspace / artifact.relative_path).resolve()
        duration = self.pipeline.media_probe(path)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise ValueError("Cannot determine recording duration safely")
        self.ui.show(
            f"Listen and confirm the target voice:\n\n{item.candidate.canonical_url}\n\nLocal playback: {path}\nDuration: {duration / 60:.1f} minutes\n\nChoose only the target speaking, with no host, music or crosstalk."
        )
        start = parse_timestamp(self.ui.text("start", "Start · mm:ss or seconds"))
        end = parse_timestamp(self.ui.text("end", "End · mm:ss or seconds"))
        if not 0 <= start < end <= duration:
            raise ValueError(
                f"Interval must satisfy 0 ≤ start < end ≤ {duration:.1f} seconds"
            )
        models = self.models(slug)
        if not self.ui.confirm(
            "confirm_voice",
            f"I listened: only the intended investor speaks from {start:g}s to {end:g}s.\nCreate the reference embedding using {models['diarization_model']}?",
        ):
            return
        if identity.resolution_status != "confirmed" and not self.identity(slug):
            return
        reviewer = self.ui.text("reviewer", "Reviewer name", "human")
        voice_id = "voice:" + cid
        voice_path = workspace / "identity/reference_voice_candidates.jsonl"
        with workspace_guard(self.pipeline.output_dir, slug):
            rows = read_jsonl(voice_path)
            existing = next((r for r in rows if r["candidate_id"] == voice_id), None)
            if existing is not None:
                existing["artifact_id"] = artifact.artifact_id
            else:
                rows.append(
                    ReferenceVoiceCandidate(
                        candidate_id=voice_id,
                        source_candidate_id=cid,
                        source_url=item.candidate.canonical_url,
                        artifact_id=artifact.artifact_id,
                        identity_confidence=item.candidate.identity_confidence,
                    ).model_dump(mode="json")
                )
            write_jsonl(voice_path, rows)
        self.launch(
            slug,
            "reference",
            [voice_id],
            confirmed=True,
            reviewer=reviewer,
            start_seconds=start,
            end_seconds=end,
            diarization_model=models["diarization_model"],
            embedding_model="",
        )

    def process(self, slug, *, media_only=False):
        from .inventory import ready_recording

        items = self.selection(
            [
                i
                for i in source_items(self.pipeline, slug)
                if i.downloaded
                and (not media_only or ready_recording(i))
                and i.candidate.approval_status in APPROVED
                and i.candidate.material_role != "reference_voice"
                and i.processing_status in UNFINISHED
            ],
            "process",
        )
        if not items or not self.identity(slug):
            return
        models = {}
        compatible = []
        if any(i.media for i in items):
            if not summarize(self.pipeline, slug)["reference"]:
                self.ui.show(
                    "A human-approved voice reference is required first. Choose Voice signature; no models have started."
                )
                return self.reference(slug)
            missing = missing_av_dependencies()
            if missing:
                raise ValueError("Missing AV dependencies: " + ", ".join(missing))
            models = self.models(slug)
            workspace = self.pipeline.workspace(slug)
            profiles = read_jsonl(workspace / "identity/reference_voice_profiles.jsonl")
            legacy = workspace / "identity/reference_voice_profile.json"
            if legacy.exists():
                profiles.append(read_json(legacy))
            compatible = [
                p
                for p in profiles
                if p.get("status") == "verified_human"
                and p.get("embedding_model") == models["diarization_model"]
            ]
            if not compatible:
                self.ui.show(
                    "The saved voice reference uses a different embedding model. Choose Voice signature and create a compatible reference before processing."
                )
                return
        config = self.config(slug)
        reference_ids = sorted(
            {
                str(p.get("reference_candidate_id") or p.get("profile_id"))
                for p in compatible
            }
        )
        if self.ui.confirm(
            "confirm_job",
            f"Process {len(items)} downloaded items?\nModels: {models or 'written normalization'}\nDevice: {os.environ.get('VC_TRACE_AV_DEVICE', 'provider default')}\nReference(s): {', '.join(reference_ids) or 'not needed for written content'}\nMedia limit: {config.maximum_media_minutes} minutes; provider-operation limit: {config.maximum_provider_operations}; budget: ${config.maximum_cost_usd}.\nUses cached stages when valid. Uncertain identities/speakers remain review-required.\nOne managed GPU job at a time. No new downloads.",
        ):
            self.launch(
                slug, "process", [i.candidate.candidate_id for i in items], **models
            )

    def launch(self, slug, stage, ids, **options):
        job = launch_job(self.pipeline.output_dir, slug, stage, ids, **options)
        self.ui.watch(self.pipeline.output_dir, job["id"])

    def jobs(self, slug=None):
        jobs = [
            j
            for j in list_jobs(self.pipeline.output_dir)
            if slug is None or j["slug"] == slug
        ]
        if not jobs:
            self.ui.show(
                "No managed or detectable external jobs. Saved source progress remains on the investor page."
            )
            return
        job_id = self.ui.select(
            "job",
            "Jobs · select to reconnect",
            [
                Option(
                    value=j["id"],
                    label=f"{j['slug']} · {j['stage']} · {j['status']} · {j.get('completed', 0)}/{j.get('total', '?')}",
                )
                for j in jobs
            ],
        )
        job = next(j for j in jobs if j["id"] == job_id)
        if job.get("external"):
            self.ui.show(job)
        else:
            self.ui.watch(self.pipeline.output_dir, job_id)

    def results(self, slug):
        self.ui.show(summarize(self.pipeline, slug))
        self.ui.show(
            f"Review-required results are not automatically verified.\nOutputs: {self.pipeline.workspace(slug).resolve()}\nSee processed/av_candidate_outcomes.jsonl and quality_report.json."
        )
        if self.ui.confirm("export", "Export the current corpus and run verification?"):
            with workspace_guard(self.pipeline.output_dir, slug):
                self.pipeline.export(slug)
                self.ui.show(self.pipeline.verify(slug).model_dump(mode="json"))

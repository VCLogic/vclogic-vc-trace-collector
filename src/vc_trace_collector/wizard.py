"""Guided stage controller. All durable operations use the audited pipeline."""

import re
from collections import Counter

from .audit import redact
from .config import RunConfig
from .discovery import slugify
from .models import ApprovalStatus, MaterialRole, SourceDecision, SourceType
from .progress import with_download_progress, with_processing_progress
from .public_search import agent_reach_public_search_provider
from .storage import read_json, read_jsonl
from .wizard_models import Prompts, backend_options, options, source_items

APPROVED = {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}


class Wizard:
    def __init__(self, pipeline, ui: Prompts, *, dependency_check=None):
        self.pipeline = pipeline
        self.ui = ui
        self.dependency_check = dependency_check

    def run(self, *, stage=None, investor=None, name=None):
        if investor and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", investor):
            raise ValueError("Invalid investor slug")
        stage = stage or self.ui.select(
            "stage", "Choose a stage", options(["discover", "download", "process"])
        )
        if stage not in {"discover", "download", "process"}:
            raise ValueError("Stage must be discover, download or process")
        if stage == "discover":
            return self.discover(name=name, investor=investor)
        if not investor:
            workspaces = sorted(
                path.parent.parent.name
                for path in self.pipeline.output_dir.glob(
                    "*/identity/resolved_identity.json"
                )
            )
            if not workspaces:
                self.ui.show("No workspaces. Run wizard --stage discover first.")
                return
            investor = self.ui.select(
                "workspace", "Investor workspace", options(workspaces)
            )
        return getattr(self, stage)(investor)

    def config(self, slug):
        return RunConfig.model_validate(
            read_json(self.pipeline.workspace(slug) / "config_snapshot.json")
        )

    def identity_ready(self, slug):
        identity = self.pipeline._load_identity(slug)
        self.ui.show(identity.model_dump(mode="json"))
        if identity.resolution_status == "confirmed":
            return True
        self.ui.show("Identity is not confirmed. Run discovery/review first.")
        return False

    def show_identity(self, slug):
        self.ui.show(
            {
                "identity": self.pipeline._load_identity(slug).model_dump(mode="json"),
                "retrieved_evidence": read_jsonl(
                    self.pipeline.workspace(slug) / "identity/identity_evidence.jsonl"
                ),
            }
        )

    def selected(self, slug, purpose):
        items = source_items(self.pipeline, slug)
        if purpose != "review":
            items = [
                item for item in items if item.candidate.approval_status in APPROVED
            ]
        if purpose == "process":
            items = [
                item
                for item in items
                if item.downloaded
                and item.candidate.material_role != MaterialRole.REFERENCE_VOICE
            ]
        ids = set(self.ui.pick_sources(items, purpose))
        by_id = {item.candidate.candidate_id: item for item in items}
        if ids - by_id.keys():
            raise ValueError("Invalid selection: source is not eligible for this stage")
        return [by_id[cid] for cid in sorted(ids)]

    def discover(self, *, name=None, investor=None):
        name = name or (None if investor else self.ui.text("name", "Investor name"))
        if not investor and not (name or "").strip():
            raise ValueError("Investor name is required")
        slug = investor or slugify(name)
        workspace = self.pipeline.workspace(slug)
        if (workspace / "identity/resolved_identity.json").exists():
            self.ui.show("Resuming saved identity; it will not be overwritten.")
            self.show_identity(slug)
        elif investor:
            raise ValueError("Workspace not found. Start discovery with --name.")
        else:
            from .wizard_profiles import preserve_firm_hint, resolve_profile

            profile = resolve_profile(self.pipeline, self.ui, name)
            if profile is None:
                return
            url, config = profile
            result = self.pipeline.discover(
                name=name, known_profile_url=url, config=config
            )
            slug = result.identity.slug
            preserve_firm_hint(self.pipeline, slug, config.firm, url)
            self.show_identity(slug)
        if self.pipeline._load_identity(slug).resolution_status != "confirmed":
            if not self.ui.confirm(
                "confirm_identity",
                "Confirm the displayed person and affiliations before searching sources?",
            ):
                self.ui.show(
                    "Identity remains unresolved. No platform search or collection was started."
                )
                return
            self.pipeline.review(
                slug,
                decisions=[],
                reviewer=self.ui.text("reviewer", "Reviewer name", "human"),
                confirm_identity=True,
            )
        config = self.config(slug)
        platforms = self.ui.check(
            "platforms",
            "Which platforms should we search? (none = review only)",
            options(t.value for t in SourceType if t != SourceType.SUPPLIED),
        )
        if platforms:
            backend = self.ui.select("backend", "Search backend", backend_options())
            limit = self.integer("results", "Results per query (1–100)", 10, 1, 100)
            ceiling = self.integer(
                "searches",
                "Total search-operation ceiling",
                config.maximum_search_operations,
                max(1, config.maximum_search_operations),
                10000,
            )
            self.ui.show(
                f"Search {', '.join(platforms)}; at most {ceiling} total searches; "
                f"saved monetary ceiling ${config.maximum_cost_usd}. No media downloads."
            )
            if self.ui.confirm("confirm_search", "Start these searches?"):
                for platform in platforms:
                    result = self.pipeline.search_source(
                        slug,
                        SourceType(platform),
                        search_provider=agent_reach_public_search_provider()
                        if backend == "agent-reach"
                        else None,
                        limit_per_query=limit,
                        maximum_search_operations=ceiling,
                    )
                    self.ui.show(result.model_dump(mode="json"))
                    if result.stopped_early:
                        self.ui.show(
                            "Search stopped. Resolve the reported backend/budget issue before retrying."
                        )
                        break
        self.review(slug)
        self.ui.show(
            f"Discovery saved in {self.pipeline.workspace(slug)}. "
            f"Next: wizard --stage download --investor {slug}"
        )

    def integer(self, key, message, default, minimum, maximum):
        while True:
            try:
                value = int(self.ui.text(key, message, str(default)))
                if minimum <= value <= maximum:
                    return value
            except ValueError:
                pass
            self.ui.show(f"Enter a whole number between {minimum} and {maximum}.")

    def review(self, slug):
        selected = self.selected(slug, "review")
        if not selected:
            return
        action = self.ui.select(
            "decision",
            "Decision for selected sources",
            options(["approved", "rejected", "defer"]),
        )
        if action == "defer":
            return
        role = None
        if action == "approved":
            role_value = self.ui.select(
                "role",
                "Material role for this batch (review differing roles separately)",
                options(["keep_existing", *(r.value for r in MaterialRole)]),
            )
            if role_value != "keep_existing":
                role = MaterialRole(role_value)
        reason = self.ui.text(
            "reason", "Reason for this decision", "Human reviewed source evidence"
        )
        reviewer = self.ui.text("reviewer", "Reviewer name", "human")
        self.show_identity(slug)
        self.ui.show(
            {
                "decision": action,
                "role": str(role) if role else "unchanged",
                "sources": [item.candidate.canonical_url for item in selected],
                "reason": reason,
            }
        )
        if not self.ui.confirm(
            "confirm_review", "Confirm this person and save these source decisions?"
        ):
            return
        self.pipeline.review(
            slug,
            decisions=[
                SourceDecision(
                    candidate_id=item.candidate.candidate_id,
                    status=ApprovalStatus(action),
                    reason=reason,
                    decided_by=reviewer,
                    material_role=role,
                )
                for item in selected
            ],
            reviewer=reviewer,
            confirm_identity=True,
        )
        self.ui.show(
            f"Saved {len(selected)} decisions. Corpus exclusions remain enforced."
        )

    @with_download_progress
    def _fetch(self, slug, ids):
        return self.pipeline.fetch_source(slug, candidate_ids=ids)

    def download(self, slug):
        selected = self.selected(slug, "download")
        if not selected or not self.identity_ready(slug):
            return
        config = self.config(slug)
        self.ui.show(
            {
                "sources": [item.label for item in selected],
                "max_bytes": config.maximum_download_bytes,
                "max_media_minutes": config.maximum_media_minutes,
                "max_cost_usd": str(config.maximum_cost_usd),
            }
        )
        if not self.ui.confirm(
            "confirm_download",
            "Download selected sources? Successful cached downloads are reused.",
        ):
            return
        result = self._fetch(slug, {item.candidate.candidate_id for item in selected})
        self.ui.show(
            {
                "collected": result.collected,
                "failed": result.failed,
                "skipped": result.skipped,
                "failures": result.failures,
            }
        )
        self.ui.show(
            f"No processing was started. Next: wizard --stage process --investor {slug}"
        )

    @with_processing_progress
    def _process(self, slug, cid, models):
        return self.pipeline.process(slug, candidate_ids={cid}, **models)

    def process(self, slug):
        selected = self.selected(slug, "process")
        if not selected or not self.identity_ready(slug):
            return
        models = {}
        if any(item.media for item in selected):
            from .wizard_reference import prepare_reference

            models = prepare_reference(
                self.pipeline, self.ui, slug, self.dependency_check
            )
            if models is None:
                return
        self.ui.show(
            {
                "sources": [item.label for item in selected],
                "models": models,
                "maximum_media_minutes": self.config(slug).maximum_media_minutes,
                "maximum_cost_usd": str(self.config(slug).maximum_cost_usd),
            }
        )
        if not self.ui.confirm(
            "confirm_process",
            "Process these downloaded sources? No downloads will run.",
        ):
            return
        for item in selected:
            try:
                documents = self._process(
                    slug, item.candidate.candidate_id, models if item.media else {}
                )
                counts = Counter(
                    str(d.inclusion_status)
                    for d in documents
                    if d.source_candidate_id == item.candidate.candidate_id
                )
                self.ui.show({"source": item.label, "document_statuses": dict(counts)})
            except Exception as error:
                self.ui.show(
                    f"Failed {item.candidate.candidate_id}: {redact(str(error))}"
                )
        self.ui.show(
            "Review processed/av_candidate_outcomes.jsonl and quality_report.json for failures/uncertain matches."
        )
        if self.ui.confirm("export", "Export current corpus and verify it now?"):
            self.pipeline.export(slug)
            result = self.pipeline.verify(slug)
            self.ui.show(result.model_dump(mode="json"))
            if not result.passed:
                raise ValueError(
                    "Export verification failed; corpus is not verified complete"
                )

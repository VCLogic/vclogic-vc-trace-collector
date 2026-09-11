"""Command-line interface for the staged collector."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import typer

from .audit import BudgetExceeded
from .collectors import ReviewRequired
from .config import RunConfig
from .models import ApprovalStatus, MaterialRole, SourceDecision
from .pipeline import Pipeline
from .policy import RuleSet
from .storage import read_json

PipelineFactory = Callable[[Path], Pipeline]


def _rules_from_file(path: Path | None):
    return RuleSet.from_toml(path).rules if path else []


def create_app(pipeline_factory: PipelineFactory = Pipeline) -> typer.Typer:
    app = typer.Typer(
        name="vc-trace-collector",
        help="Build auditable, source-linked public-trace corpora for investors.",
        no_args_is_help=True,
    )

    @app.command()
    def discover(
        name: str = typer.Option(..., help="Investor name"),
        firm: str | None = typer.Option(None, help="Known firm"),
        known_profile_url: str | None = typer.Option(None, help="Known public profile"),
        source_url: list[str] | None = typer.Option(
            None, help="Additional public source URL; repeat for multiple sources"
        ),
        supplied_file: list[Path] | None = typer.Option(
            None, help="Local public-trace file to snapshot; repeat for multiple files"
        ),
        supplied_role: MaterialRole = typer.Option(MaterialRole.UNKNOWN),
        exclusion_file: Path | None = typer.Option(
            None, help="TOML file containing additional frozen exclusion rules"
        ),
        exclude_domain: list[str] | None = typer.Option(None),
        exclude_channel: list[str] | None = typer.Option(None),
        discovery_model: str | None = typer.Option(None),
        transcription_model: str | None = typer.Option(None),
        diarization_model: str | None = typer.Option(None),
        embedding_model: str | None = typer.Option(None),
        max_cost_usd: str = typer.Option("10.00"),
        discovery_call_budget_usd: str | None = typer.Option(
            None, help="Conservative reserved cost for the discovery LLM call"
        ),
        max_search_operations: int = typer.Option(20),
        max_media_minutes: float = typer.Option(120),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        config = RunConfig(
            name=name,
            firm=firm,
            known_profile_url=known_profile_url,
            source_urls=source_url or [],
            supplied_files=[str(path) for path in supplied_file or []],
            supplied_role=supplied_role,
            output_dir=str(output_dir),
            excluded_domains=exclude_domain or [],
            excluded_channels=exclude_channel or [],
            exclusion_rules=_rules_from_file(exclusion_file),
            discovery_model=discovery_model,
            transcription_model=transcription_model,
            diarization_model=diarization_model,
            embedding_model=embedding_model,
            maximum_cost_usd=Decimal(max_cost_usd),
            discovery_call_budget_usd=(
                Decimal(discovery_call_budget_usd)
                if discovery_call_budget_usd is not None
                else None
            ),
            maximum_search_operations=max_search_operations,
            maximum_media_minutes=max_media_minutes,
        )
        result = pipeline_factory(output_dir).discover(
            name=name,
            firm=firm,
            known_profile_url=known_profile_url,
            source_urls=source_url or [],
            supplied_files=supplied_file or [],
            supplied_role=supplied_role,
            config=config,
        )
        typer.echo(f"Investor: {result.identity.canonical_name}")
        typer.echo(f"Workspace: {output_dir / result.identity.slug}")
        typer.echo(f"Candidates: {len(result.source_plan.candidates)}")
        typer.echo("Review required before collection.")

    @app.command()
    def review(
        investor: str = typer.Option(..., help="Investor workspace slug"),
        output_dir: Path = typer.Option(Path("outputs")),
        reviewer: str = typer.Option("human", help="Reviewer identifier"),
        decision_file: Path | None = typer.Option(
            None, help="JSON list of source decisions"
        ),
        approve_all_eligible: bool = typer.Option(
            False, help="Approve every pending candidate"
        ),
        confirm_identity: bool = typer.Option(
            False, help="Explicitly confirm the resolved person and affiliations"
        ),
    ) -> None:
        pipeline = pipeline_factory(output_dir)
        plan = pipeline._load_plan(investor)
        decisions: list[SourceDecision] = []
        if decision_file:
            decisions = [
                SourceDecision.model_validate(item) for item in read_json(decision_file)
            ]
        else:
            for candidate in plan.candidates:
                if candidate.approval_status != ApprovalStatus.PENDING:
                    continue
                if approve_all_eligible:
                    approved = (
                        candidate.identity_confidence.score >= 0.8
                        and candidate.material_role
                        not in {MaterialRole.UNKNOWN, MaterialRole.THIRD_PARTY}
                    )
                else:
                    approved = typer.confirm(
                        f"Approve {candidate.source_type.value}: "
                        f"{candidate.canonical_url}?"
                    )
                decisions.append(
                    SourceDecision(
                        candidate_id=candidate.candidate_id,
                        status=ApprovalStatus.APPROVED
                        if approved
                        else ApprovalStatus.REJECTED,
                        reason=(
                            "Approved during source-plan review"
                            if approved
                            else (
                                "Failed bulk-review identity or material-role threshold"
                                if approve_all_eligible
                                else "Rejected during source-plan review"
                            )
                        ),
                        decided_by=reviewer,
                    )
                )
        if not confirm_identity:
            confirm_identity = typer.confirm(
                f"Confirm resolved identity: {plan.investor_slug}?"
            )
        result = pipeline.review(
            investor,
            decisions=decisions,
            reviewer=reviewer,
            confirm_identity=confirm_identity,
        )
        typer.echo(
            f"Identity confirmed; {sum(c.approval_status == ApprovalStatus.APPROVED for c in result.source_plan.candidates)} sources approved."
        )

    @app.command()
    def collect(
        name: str = typer.Option(..., help="Investor name"),
        firm: str | None = typer.Option(None),
        known_profile_url: str | None = typer.Option(None),
        source_url: list[str] | None = typer.Option(None),
        supplied_file: list[Path] | None = typer.Option(None),
        supplied_role: MaterialRole = typer.Option(MaterialRole.UNKNOWN),
        output_dir: Path = typer.Option(Path("outputs")),
        approved_source_type: list[str] | None = typer.Option(None),
        exclude_domain: list[str] | None = typer.Option(None),
        exclude_channel: list[str] | None = typer.Option(None),
        exclusion_file: Path | None = typer.Option(
            None, help="TOML file containing additional frozen exclusion rules"
        ),
        discovery_model: str | None = typer.Option(None),
        transcription_model: str | None = typer.Option(None),
        diarization_model: str | None = typer.Option(None),
        embedding_model: str | None = typer.Option(None),
        max_cost_usd: str = typer.Option("10.00"),
        discovery_call_budget_usd: str | None = typer.Option(
            None, help="Conservative reserved cost for the discovery LLM call"
        ),
        max_search_operations: int = typer.Option(20),
        max_media_minutes: float = typer.Option(120),
        auto_approve_discovery: bool = typer.Option(False),
        resume: str | None = typer.Option(None),
        collection_only: bool = typer.Option(False),
        processing_only: bool = typer.Option(False),
        export_only: bool = typer.Option(False),
    ) -> None:
        if sum((collection_only, processing_only, export_only)) > 1:
            raise typer.BadParameter("Only one execution-only mode may be selected")
        try:
            result = pipeline_factory(output_dir).collect(
                name=name,
                firm=firm,
                known_profile_url=known_profile_url,
                source_urls=source_url or [],
                supplied_files=supplied_file or [],
                supplied_role=supplied_role,
                auto_approve_discovery=auto_approve_discovery,
                resume=resume,
                collection_only=collection_only,
                processing_only=processing_only,
                export_only=export_only,
                approved_source_types=approved_source_type or [],
                excluded_domains=exclude_domain or [],
                excluded_channels=exclude_channel or [],
                exclusion_rules=_rules_from_file(exclusion_file),
                discovery_model=discovery_model,
                transcription_model=transcription_model,
                diarization_model=diarization_model,
                embedding_model=embedding_model,
                maximum_cost_usd=Decimal(max_cost_usd),
                discovery_call_budget_usd=(
                    Decimal(discovery_call_budget_usd)
                    if discovery_call_budget_usd is not None
                    else None
                ),
                maximum_search_operations=max_search_operations,
                maximum_media_minutes=max_media_minutes,
            )
        except ReviewRequired as error:
            typer.echo(f"Review required: {error}")
            raise typer.Exit(3) from error
        except BudgetExceeded as error:
            typer.echo(f"Budget limit reached: {error}")
            raise typer.Exit(4) from error
        if result.verification is not None and not result.verification.passed:
            typer.echo("Verification failed:")
            for error in result.verification.errors:
                typer.echo(f"- {error}")
            raise typer.Exit(1)
        typer.echo(f"Completed workspace: {output_dir / result.investor_slug}")

    @app.command("process")
    def process_command(
        investor: str = typer.Option(...),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        documents = pipeline_factory(output_dir).process(investor)
        typer.echo(f"Processed {len(documents)} documents.")

    @app.command()
    def export(
        investor: str = typer.Option(...),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        pipeline = pipeline_factory(output_dir)
        manifest = pipeline.export(investor)
        verification = pipeline.verify(investor)
        typer.echo(f"Exported {manifest.corpus_documents} corpus documents.")
        if not verification.passed:
            typer.echo("Verification failed.")
            raise typer.Exit(1)

    @app.command("review-voice")
    def review_voice(
        investor: str = typer.Option(..., help="Investor workspace slug"),
        candidate_id: str = typer.Option(..., help="Reference candidate identifier"),
        reviewer: str = typer.Option("human", help="Reviewer identifier"),
        start_seconds: float | None = typer.Option(None),
        end_seconds: float | None = typer.Option(None),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        profile = pipeline_factory(output_dir).approve_reference_voice(
            investor,
            candidate_id=candidate_id,
            reviewer=reviewer,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        typer.echo(
            f"Approved reference voice with {len(profile.embedding)} embedding dimensions."
        )

    @app.command()
    def status(
        investor: str = typer.Option(...),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        typer.echo(
            json.dumps(
                pipeline_factory(output_dir).status(investor), indent=2, default=str
            )
        )

    @app.command()
    def verify(
        investor: str = typer.Option(...),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        result = pipeline_factory(output_dir).verify(investor)
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        if not result.passed:
            raise typer.Exit(1)

    return app


app = create_app()

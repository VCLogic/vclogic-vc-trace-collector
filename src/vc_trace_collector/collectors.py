"""Reviewed, isolated, resumable deterministic source collectors."""

from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditLog, BudgetLedger, redact
from .extract import extract_feed
from .fetch import Fetcher
from .models import (
    ApprovalStatus,
    AuditEvent,
    EventStatus,
    RawArtifact,
    SourceCandidate,
    SourceDecision,
    SourcePlan,
    SourceType,
)
from .policy import InclusionStatus, RuleSet
from .storage import ArtifactStore, StateStore, append_jsonl, canonical_json


class ReviewRequired(RuntimeError):
    pass


class CollectorUnavailable(RuntimeError):
    pass


@dataclass
class CollectionContext:
    workspace: Path
    artifacts: ArtifactStore
    state: StateStore
    rules: RuleSet
    run_id: str
    fetcher: Fetcher | None = None
    audit: AuditLog | None = None
    budget: BudgetLedger | None = None
    approved_source_types: set[SourceType] | None = None
    maximum_media_seconds: float | None = None


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collected: int = 0
    failed: int = 0
    skipped: int = 0
    excluded: int = 0
    artifacts: list[RawArtifact] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)


class Collector(Protocol):
    source_types: set[SourceType]

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]: ...


class CollectorRegistry:
    def __init__(self, collectors: list[Collector]):
        self._collectors: dict[SourceType, Collector] = {}
        for collector in collectors:
            for source_type in collector.source_types:
                self._collectors[source_type] = collector

    def get(self, source_type: SourceType) -> Collector:
        try:
            return self._collectors[source_type]
        except KeyError as error:
            raise CollectorUnavailable(
                f"No collector for {source_type.value}"
            ) from error


def apply_decisions(plan: SourcePlan, decisions: list[SourceDecision]) -> SourcePlan:
    updated = plan.model_copy(deep=True)
    by_id = {candidate.candidate_id: candidate for candidate in updated.candidates}
    for decision in decisions:
        if decision.candidate_id not in by_id:
            raise KeyError(f"Unknown candidate: {decision.candidate_id}")
        candidate = by_id[decision.candidate_id]
        candidate.approval_status = decision.status
        candidate.decision_reason = decision.reason
        if decision.material_role is not None:
            candidate.material_role = decision.material_role
        if decision.speaker_verified:
            if candidate.material_role != "spoken_by_target":
                raise ValueError(
                    "Speaker verification requires spoken_by_target material"
                )
            candidate.speaker_verified_by = decision.decided_by
        for field in ("channel", "programme", "company"):
            value = getattr(decision, field)
            if value is not None:
                setattr(candidate, field, value)
    return updated


def auto_approve(
    plan: SourcePlan,
    *,
    minimum_identity: float,
    minimum_source: float,
) -> SourcePlan:
    updated = plan.model_copy(deep=True)
    for candidate in updated.candidates:
        if candidate.approval_status == ApprovalStatus.REJECTED:
            continue
        if (
            candidate.identity_confidence.score >= minimum_identity
            and candidate.source_confidence.score >= minimum_source
        ):
            candidate.approval_status = ApprovalStatus.AUTO_APPROVED
            candidate.decision_reason = "Passed explicit automatic approval thresholds"
        else:
            candidate.approval_status = ApprovalStatus.PENDING
    return updated


def _suffix_for_mime(mime_type: str | None, fallback: str = ".bin") -> str:
    if not mime_type:
        return fallback
    mime = mime_type.split(";", 1)[0].strip()
    if mime == "application/octet-stream":
        return fallback
    return mimetypes.guess_extension(mime) or fallback


class WebCollector:
    source_types: ClassVar[set[SourceType]] = {
        SourceType.WEB_PROFILE,
        SourceType.WEB_ARTICLE,
        SourceType.SUBSTACK,
        SourceType.MEDIUM,
    }

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        if context.fetcher is None:
            raise CollectorUnavailable("Web collector requires an HTTP fetcher")
        fetched = context.fetcher.fetch(source.url)
        mime = fetched.headers.get("content-type", "text/html")
        stored = context.artifacts.put_bytes(
            fetched.content,
            category="web",
            suffix=_suffix_for_mime(mime, ".html"),
            source_url=fetched.final_url,
            mime_type=mime,
            collection_method="http",
            original_metadata={
                "requested_url": fetched.requested_url,
                "canonical_url": fetched.canonical_url,
                "status_code": fetched.status_code,
                "headers": fetched.headers,
                "redirect_chain": fetched.redirect_chain,
                "attempts": fetched.attempts,
                "candidate_id": source.candidate_id,
            },
        )
        return [stored.record]


class FeedCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.RSS_FEED}

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        if context.fetcher is None:
            raise CollectorUnavailable("Feed collector requires an HTTP fetcher")
        fetched = context.fetcher.fetch(source.url)
        feed_artifact = context.artifacts.put_bytes(
            fetched.content,
            category="web",
            suffix=".xml",
            source_url=fetched.final_url,
            mime_type=fetched.headers.get("content-type", "application/xml"),
            collection_method="rss_atom",
            original_metadata={
                "candidate_id": source.candidate_id,
                "headers": fetched.headers,
                "entries": [
                    entry.model_dump(mode="json")
                    for entry in extract_feed(fetched.content, fetched.final_url)
                ],
            },
        )
        return [feed_artifact.record]


def _podcast_audio_urls(content: bytes, source_url: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    urls: set[str] = set()
    for element in soup.find_all(["audio", "source"]):
        value = element.get("src")
        if value:
            urls.add(urljoin(source_url, str(value)))

    # Podcast hosts frequently serialize enclosure URLs into JSON state rather
    # than audio elements. Decode JSON's escaped slash form before scanning.
    decoded = content.decode("utf-8", errors="replace").replace("\\u002F", "/")
    for match in re.findall(
        r"https?://[^\"'<>\s]+?\.(?:mp3|m4a|opus)(?:\?[^\"'<>\s]*)?",
        decoded,
        re.IGNORECASE,
    ):
        urls.add(match.replace("&amp;", "&"))
    return sorted(urls)[:4]


class PodcastCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.PODCAST}

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        if context.fetcher is None:
            raise CollectorUnavailable("Podcast collector requires an HTTP fetcher")
        fetched = context.fetcher.fetch(source.url)
        mime = fetched.headers.get("content-type", "text/html")
        soup = BeautifulSoup(fetched.content, "html.parser")
        site_name = soup.find("meta", attrs={"property": "og:site_name"})
        author = soup.find("meta", attrs={"name": "author"})
        if site_name and site_name.get("content"):
            source.programme = str(site_name["content"]).strip()
        if author and author.get("content"):
            source.channel = str(author["content"]).strip()
        page = context.artifacts.put_bytes(
            fetched.content,
            category="podcast",
            suffix=_suffix_for_mime(mime, ".html"),
            source_url=fetched.final_url,
            mime_type=mime,
            collection_method="podcast_page_http",
            original_metadata={
                "candidate_id": source.candidate_id,
                "canonical_url": fetched.canonical_url,
                "headers": fetched.headers,
                "status_code": fetched.status_code,
            },
        ).record
        records = [page]
        if mime.split(";", 1)[0].strip().startswith("audio/"):
            return records

        for audio_url in _podcast_audio_urls(fetched.content, fetched.final_url):
            try:
                audio = context.fetcher.fetch(audio_url, maximum_bytes=512_000_000)
            except Exception as error:
                _audit(
                    context,
                    candidate=source,
                    status=EventStatus.FAILED,
                    summary="Podcast enclosure collection failed; page was retained",
                    details={"audio_url": audio_url, "error": str(error)},
                )
                continue
            audio_mime = audio.headers.get("content-type", "application/octet-stream")
            records.append(
                context.artifacts.put_bytes(
                    audio.content,
                    category="podcast",
                    suffix=_suffix_for_mime(
                        audio_mime,
                        Path(urlsplit(audio.final_url).path).suffix or ".bin",
                    ),
                    source_url=audio.final_url,
                    mime_type=audio_mime,
                    collection_method="podcast_enclosure_http",
                    original_metadata={
                        "candidate_id": source.candidate_id,
                        "episode_url": fetched.final_url,
                        "headers": audio.headers,
                        "status_code": audio.status_code,
                    },
                    parent_artifact_ids=[page.artifact_id],
                ).record
            )
        return records


class SuppliedFileCollector:
    source_types: ClassVar[set[SourceType]] = {
        SourceType.SUPPLIED,
        SourceType.LINKEDIN_EXPORT,
    }

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        parsed = urlsplit(source.url)
        path = (
            Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(source.url)
        )
        path = path.expanduser().resolve(strict=True)
        mime, _encoding = mimetypes.guess_type(path.name)
        stored = context.artifacts.put_bytes(
            path.read_bytes(),
            category="supplied"
            if source.source_type == SourceType.SUPPLIED
            else "social",
            suffix=path.suffix or ".bin",
            source_path=str(path),
            mime_type=mime,
            collection_method="supplied_snapshot",
            original_metadata={
                "candidate_id": source.candidate_id,
                "original_name": path.name,
            },
        )
        return [stored.record]


class YouTubeCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.YOUTUBE}

    def __init__(self, *, runner=subprocess.run):
        self.runner = runner

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        hostname = (urlsplit(source.url).hostname or "").casefold()
        if not (
            hostname == "youtube.com"
            or hostname.endswith(".youtube.com")
            or hostname == "youtu.be"
        ):
            raise CollectorUnavailable("YouTube collector requires a YouTube URL")
        command = ["yt-dlp", "--dump-single-json", "--skip-download", source.url]
        try:
            completed = self.runner(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError as error:
            raise CollectorUnavailable(
                "Install the youtube extra to use yt-dlp"
            ) from error
        metadata = json.loads(completed.stdout)
        source.channel = (
            str(metadata.get("channel") or metadata.get("uploader") or "").strip()
            or None
        )
        source.programme = str(metadata.get("series") or "").strip() or None
        video_id = str(metadata.get("id", ""))
        records = [
            context.artifacts.put_bytes(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                ),
                category="video",
                suffix=".metadata.json",
                source_url=source.canonical_url,
                mime_type="application/json",
                collection_method="yt_dlp_metadata",
                original_metadata={
                    "candidate_id": source.candidate_id,
                    "video_id": video_id,
                },
            ).record
        ]
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            transcript = YouTubeTranscriptApi().fetch(video_id)
            segments = [
                {"text": item.text, "start": item.start, "duration": item.duration}
                for item in transcript.snippets
            ]
        except (ImportError, Exception):
            segments = []
        if segments:
            records.append(
                context.artifacts.put_bytes(
                    json.dumps(segments, ensure_ascii=False, sort_keys=True).encode(
                        "utf-8"
                    ),
                    category="video",
                    suffix=".captions.json",
                    source_url=source.canonical_url,
                    mime_type="application/json",
                    collection_method="youtube_captions",
                    original_metadata={
                        "candidate_id": source.candidate_id,
                        "video_id": video_id,
                    },
                    parent_artifact_ids=[records[0].artifact_id],
                ).record
            )
        duration = float(metadata.get("duration") or 0)
        if (
            context.maximum_media_seconds is not None
            and duration > context.maximum_media_seconds
        ):
            raise RuntimeError(
                f"Media duration {duration:.1f}s exceeds remaining limit "
                f"{context.maximum_media_seconds:.1f}s"
            )
        with tempfile.TemporaryDirectory(prefix="vc-trace-youtube-") as directory:
            output_template = str(Path(directory) / "audio.%(ext)s")
            download = [
                "yt-dlp",
                "--no-playlist",
                "--max-filesize",
                "512M",
                "-f",
                "bestaudio/best",
                "-o",
                output_template,
                "--print",
                "after_move:filepath",
                source.url,
            ]
            try:
                downloaded = self.runner(
                    download,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
            except FileNotFoundError as error:
                raise CollectorUnavailable(
                    "Install the youtube extra to download YouTube audio"
                ) from error
            output_lines = [
                line.strip() for line in downloaded.stdout.splitlines() if line.strip()
            ]
            if not output_lines:
                raise RuntimeError("yt-dlp did not report a downloaded audio path")
            media_path = Path(output_lines[-1]).resolve()
            directory_path = Path(directory).resolve()
            if directory_path not in media_path.parents or not media_path.is_file():
                raise RuntimeError("yt-dlp reported an invalid audio path")
            mime, _encoding = mimetypes.guess_type(media_path.name)
            records.append(
                context.artifacts.put_bytes(
                    media_path.read_bytes(),
                    category="video",
                    suffix=media_path.suffix or ".bin",
                    source_url=source.canonical_url,
                    mime_type=mime or "application/octet-stream",
                    collection_method="yt_dlp_audio",
                    original_metadata={
                        "candidate_id": source.candidate_id,
                        "video_id": video_id,
                        "duration_seconds": duration,
                    },
                    parent_artifact_ids=[records[0].artifact_id],
                ).record
            )
        return records


def default_registry() -> CollectorRegistry:
    return CollectorRegistry(
        [
            WebCollector(),
            FeedCollector(),
            PodcastCollector(),
            SuppliedFileCollector(),
            YouTubeCollector(),
        ]
    )


def _audit(
    context: CollectionContext,
    *,
    candidate: SourceCandidate,
    status: EventStatus,
    summary: str,
    outputs: list[str] | None = None,
    details: dict[str, object] | None = None,
) -> None:
    if context.audit:
        context.audit.append(
            AuditEvent(
                event_id=str(uuid4()),
                run_id=context.run_id,
                stage="collect",
                action="collect_source",
                status=status,
                summary=summary,
                input_ids=[candidate.candidate_id],
                output_ids=outputs or [],
                details=details or {},
            )
        )


def collect_approved_sources(
    plan: SourcePlan,
    *,
    context: CollectionContext,
    registry: CollectorRegistry | None = None,
) -> CollectionResult:
    pending = [
        candidate
        for candidate in plan.candidates
        if candidate.approval_status == ApprovalStatus.PENDING
    ]
    if plan.requires_review and pending:
        raise ReviewRequired(f"Source-plan review required: {plan.plan_id}")

    registry = registry or default_registry()
    result = CollectionResult()
    for candidate in plan.candidates:
        if candidate.approval_status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.AUTO_APPROVED,
        }:
            continue
        if (
            context.approved_source_types is not None
            and candidate.source_type not in context.approved_source_types
        ):
            result.skipped += 1
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.SKIPPED,
                summary="Source type is outside the run allowlist",
                details={"source_type": candidate.source_type.value},
            )
            continue
        exclusion = context.rules.evaluate(
            url=candidate.canonical_url,
            title=candidate.title,
            text=candidate.description,
            channel=candidate.channel,
            programme=candidate.programme,
            company=candidate.company,
            stage="pre_collection",
        )
        if exclusion.status == InclusionStatus.EXCLUDED:
            result.excluded += 1
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.SKIPPED,
                summary="Source excluded before collection",
                details=exclusion.model_dump(mode="json"),
            )
            continue

        input_hash = sha256(
            canonical_json(candidate.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        operation_id = f"collect:{candidate.candidate_id}"
        if context.state.is_complete(operation_id, input_hash):
            result.skipped += 1
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.SKIPPED,
                summary="Previously completed collection reused",
            )
            continue

        if context.budget:
            context.budget.reserve(operation_id, candidate.estimated_cost_usd)
        context.state.start_operation(operation_id, input_hash)
        try:
            collector = registry.get(candidate.source_type)
            records = collector.collect(candidate, context)
        except Exception as error:
            if context.budget:
                context.budget.release(operation_id)
            failure = redact(
                {
                    "candidate_id": candidate.candidate_id,
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            )
            context.state.fail_operation(
                operation_id, input_hash, str(failure["message"])
            )
            result.failed += 1
            result.failures.append(failure)
            append_jsonl(context.workspace / "audit/failures.jsonl", failure)
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.FAILED,
                summary="Source collection failed without affecting other sources",
                details=failure,
            )
            continue

        output_ids = [record.artifact_id for record in records]
        if context.budget:
            context.budget.settle(operation_id, candidate.estimated_cost_usd)
        context.state.finish_operation(operation_id, input_hash, ",".join(output_ids))
        result.collected += 1
        result.artifacts.extend(records)
        _audit(
            context,
            candidate=candidate,
            status=EventStatus.SUCCEEDED,
            summary="Source collected",
            outputs=output_ids,
        )
    return result
